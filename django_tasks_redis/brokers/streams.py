"""
The Redis Streams broker.

Every queue is three streams, one per priority level, read through one
consumer group. A worker is a consumer of that group, named by its worker id.
This class owns everything that touches the streams on the consuming side:
reading, acknowledging, promoting delayed tasks, and taking over what a dead
consumer left pending.
"""

import logging

import redis
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from ..utils import (
    deserialize_datetime,
    deserialize_json,
    generate_worker_id,
    get_delayed_key,
    get_priority_stream_key,
    get_result_key,
    priority_to_level,
)
from .base import BrokerMessage, PullBroker

logger = logging.getLogger("django_tasks_redis")

#: Streams are read in this order, so a high priority task is served before a
#: normal one, and a normal one before a low one.
PRIORITY_LEVELS = ["high", "normal", "low"]

#: Pending entries read per XPENDING page while sweeping for stale messages.
PENDING_PAGE_SIZE = 100

#: A worker runs one task at a time, so claiming a whole backlog would park it
#: behind one consumer instead of spreading recovery over the live workers.
MAX_CLAIMS_PER_SWEEP = 100

# Every worker scans the delayed set on every fetch, and a task promoted twice
# runs twice - one that re-enqueues itself then fans out exponentially. ZREM is
# the claim, and the XADD shares its atomic unit, so promotion is exactly once.
_PROMOTE_DELAYED_TASK = """
if redis.call('ZREM', KEYS[1], ARGV[1]) == 0 then
    return 0
end
if redis.call('HGET', KEYS[2], 'status') ~= ARGV[2] then
    return 0
end
redis.call('XADD', KEYS[3], '*', unpack(ARGV, 3))
return 1
"""


def is_missing_group(error):
    """Redis reports an absent stream or consumer group as NOGROUP.

    Anything else is a real error - a connection that dropped, a server that
    stopped answering - and must not be mistaken for an empty queue.
    """
    return "NOGROUP" in str(error)


def next_message_id(message_id):
    """The smallest stream id strictly greater than `message_id`."""
    timestamp, _, sequence = message_id.partition("-")
    return f"{timestamp}-{int(sequence) + 1}"


class RedisStreamsBroker(PullBroker):
    """
    Broker backed by the Redis streams the backend enqueues to.

    The consumer group, the claim timeout and the delivery cap are the
    backend's ``REDIS_CONSUMER_GROUP``, ``REDIS_CLAIM_TIMEOUT`` and
    ``REDIS_MAX_DELIVERIES`` options; the broker reads them from the backend
    rather than parsing them again.

    Consumers:
        A message is delivered to one consumer of the group and stays pending
        for it until acknowledged. receive() therefore takes the worker id, and
        serves the messages that consumer already holds before asking for new
        ones, because that is how a message reclaimed from a dead consumer
        gets delivered again.

    Delivery:
        A message whose task is no longer READY, or whose task hash is gone, is
        acknowledged inside receive() and not returned: running it would not
        help. One whose task is not due yet goes back to the delayed set. A
        message that is returned stays pending until ack() is called, so a
        worker that dies before then leaves it for claim_stale_messages().

    Consumer cleanup:
        Every worker start adds a consumer to the group, and Redis never
        removes one by itself. A worker that exits cleanly removes its own
        with remove_consumer(); one that died is removed by the sweep once its
        messages have been reclaimed and it has been idle for the claim
        timeout, so the group does not grow by one entry per worker start.
    """

    def __init__(self, backend, options=None):
        super().__init__(backend, options)
        self._promote = None

    @property
    def client(self):
        return self.backend.get_client()

    @property
    def consumer_group(self):
        return self.backend.consumer_group

    # -- keys --------------------------------------------------------------

    def queue_names(self, queue_name=None):
        """The queues to read, or the one the caller asked for."""
        if queue_name:
            return [queue_name]
        return list(self.backend.queues) if self.backend.queues else ["default"]

    def stream_key(self, queue_name, priority_level):
        return get_priority_stream_key(
            self.backend.key_prefix, self.backend.alias, queue_name, priority_level
        )

    def stream_keys(self, queue_name=None):
        """Every stream to read, in the order they are served."""
        return [
            self.stream_key(qname, priority_level)
            for priority_level in PRIORITY_LEVELS
            for qname in self.queue_names(queue_name)
        ]

    def delayed_key(self, queue_name):
        return get_delayed_key(self.backend.key_prefix, self.backend.alias, queue_name)

    def result_key(self, task_id):
        return get_result_key(self.backend.key_prefix, self.backend.alias, task_id)

    # -- producing side, shared with the backend ---------------------------

    def ensure_consumer_group(self, stream_key):
        """Create the consumer group on a stream, and the stream with it."""
        try:
            self.client.xgroup_create(
                stream_key, self.consumer_group, id="0", mkstream=True
            )
        except redis.ResponseError as error:
            # Group already exists - this is fine
            if "BUSYGROUP" not in str(error):
                raise

    def stream_entry(self, task_data):
        """
        The fields a stream entry carries for a task.

        A subset of the task hash: enough to name the task and to read the
        queue from a pending entry, no more. The hash stays the source of
        truth for everything else.
        """
        return {
            "task_id": task_data["task_id"],
            "task_path": task_data["task_path"],
            "priority": task_data["priority"],
            "queue_name": task_data["queue_name"],
            "enqueued_at": task_data.get("enqueued_at", ""),
        }

    def requeue(self, task_data):
        """Put a task back on its stream, from its hash."""
        stream_key = self.stream_key(
            task_data.get("queue_name", "default"),
            priority_to_level(int(task_data.get("priority", "0"))),
        )
        self.ensure_consumer_group(stream_key)
        self.client.xadd(stream_key, self.stream_entry(task_data))

    # -- delayed tasks -----------------------------------------------------

    def promote_delayed_tasks(self, queue_name=None):
        """Move delayed tasks to their streams if their time has come."""
        client = self.client
        now_timestamp = timezone.now().timestamp()
        if self._promote is None:
            self._promote = client.register_script(_PROMOTE_DELAYED_TASK)

        for qname in self.queue_names(queue_name):
            delayed_key = self.delayed_key(qname)

            for task_id in client.zrangebyscore(delayed_key, 0, now_timestamp):
                result_key = self.result_key(task_id)
                task_data = client.hgetall(result_key)

                if not task_data:
                    # The result hash expired or was deleted: nothing left to run.
                    client.zrem(delayed_key, task_id)
                    continue

                stream_key = self.stream_key(
                    qname, priority_to_level(int(task_data.get("priority", "0")))
                )
                self.ensure_consumer_group(stream_key)

                entry = self.stream_entry({**task_data, "queue_name": qname})
                fields = [item for pair in entry.items() for item in pair]
                # The script re-checks the status, so a task that is no longer
                # READY leaves the delayed set without being promoted.
                self._promote(
                    keys=[delayed_key, result_key, stream_key],
                    args=[task_id, TaskResultStatus.READY, *fields],
                )

    # -- receive -----------------------------------------------------------

    def receive(self, queue_name=None, max_messages=1, wait_seconds=0, worker_id=None):
        """
        Read messages for a consumer and return the ones that have to run.

        Messages this consumer already owns are served first: after
        claim_stale_messages() has reassigned a dead worker's messages to a
        live consumer, this is where they get delivered again. New messages
        are only read after that, one stream at a time in priority order, so
        that a lower priority message is not held by a worker that is not
        going to run it yet.

        Args:
            queue_name: Queue to read, or None for every queue the backend
                serves.
            max_messages: How many messages to return at most.
            wait_seconds: How long to wait for a new message when every
                stream is empty. Zero polls without waiting. A blocking read
                waits on all streams at once, so a message that arrives on a
                lower priority stream at the same moment as one on a higher
                priority stream may be served first; strict priority still
                holds for messages already queued.
            worker_id: The consumer to read as. It has to be the id the
                worker keeps using, or what it holds is never served again.

        Returns:
            list of BrokerMessage, in priority order.
        """
        if worker_id is None:
            worker_id = generate_worker_id()
        if max_messages < 1:
            return []

        # First, move delayed tasks to streams if their time has come
        self.promote_delayed_tasks(queue_name)

        stream_keys = self.stream_keys(queue_name)
        messages = []

        # Messages this consumer already owns, every stream in one round trip:
        # reading history delivers nothing new, so asking them all at once is
        # free.
        delivered = self._read(stream_keys, worker_id, "0", max_messages)
        self._take(delivered, stream_keys, messages, max_messages)
        if len(messages) >= max_messages:
            return messages

        # New messages, one stream at a time: reading them all at once would
        # hold lower priority messages this worker is not going to run yet.
        for stream_key in stream_keys:
            delivered = self._read(
                [stream_key], worker_id, ">", max_messages - len(messages)
            )
            self._take(delivered, stream_keys, messages, max_messages)
            if len(messages) >= max_messages:
                return messages

        if messages or not wait_seconds or wait_seconds <= 0:
            return messages

        block = max(1, int(wait_seconds * 1000))
        delivered = self._read(stream_keys, worker_id, ">", max_messages, block=block)
        self._take(delivered, stream_keys, messages, max_messages)
        return messages

    def _read(self, stream_keys, worker_id, read_id, count, block=None):
        """
        XREADGROUP on the given streams, as a dict of stream key to entries.

        A stream nothing has ever been written to has no consumer group, and
        that fails the whole read. Create the missing ones and read again.
        """
        streams = dict.fromkeys(stream_keys, read_id)
        try:
            result = self.client.xreadgroup(
                self.consumer_group, worker_id, streams, count=count, block=block
            )
        except redis.ResponseError as error:
            if not is_missing_group(error):
                raise
            for stream_key in stream_keys:
                self.ensure_consumer_group(stream_key)
            result = self.client.xreadgroup(
                self.consumer_group, worker_id, streams, count=count, block=block
            )

        return {name: entries for name, entries in result or [] if entries}

    def _take(self, delivered, stream_keys, messages, max_messages):
        """
        Resolve delivered entries into `messages`, streams in priority order.

        Entries that do not need to run are acknowledged on the spot. Entries
        beyond `max_messages` are left pending for this consumer and come
        back through the history read of the next receive().
        """
        for stream_key in stream_keys:
            for message_id, data in delivered.get(stream_key, []):
                if len(messages) >= max_messages:
                    return
                message = self._resolve(stream_key, message_id, data)
                if message is not None:
                    messages.append(message)

    def _resolve(self, stream_key, message_id, data):
        """
        Turn a delivered stream entry into a message, or None.

        None means the entry does not need to run; it is acknowledged first so
        it is neither delivered nor kept around again.
        """
        client = self.client

        if not data:
            # The entry is gone from the stream and only the pending record is
            # left.
            client.xack(stream_key, self.consumer_group, message_id)
            return None

        task_id = data.get("task_id")
        task_data = client.hgetall(self.result_key(task_id))
        message = BrokerMessage(task_id, handle=(stream_key, message_id), raw=data)

        if not task_data:
            # Task data not found, acknowledge message
            self.ack(message)
            return None

        # Only READY runs. RUNNING means another worker owns the task; if that
        # worker is in fact dead, claim_stale_messages is what notices and
        # hands the task back as READY, because staleness is the only way to
        # tell them apart.
        if task_data.get("status") != TaskResultStatus.READY:
            self.ack(message)
            return None

        run_after = deserialize_datetime(task_data.get("run_after", ""))
        if run_after and run_after > timezone.now():
            # Back to the delayed set, not acknowledged away: an acknowledged
            # message is never delivered again, so dropping it loses the task.
            delayed_key = self.delayed_key(task_data.get("queue_name", "default"))
            client.zadd(delayed_key, {task_id: run_after.timestamp()})
            self.ack(message)
            return None

        return message

    # -- ack / nack --------------------------------------------------------

    def ack(self, message):
        """
        Acknowledge a message and delete the entry behind it.

        XACK only clears the pending entry; the entry itself stays in the
        stream. Without the XDEL every priority stream grows by one entry per
        task, forever.
        """
        stream_key, message_id = message.handle
        self.client.xack(stream_key, self.consumer_group, message_id)
        self.client.xdel(stream_key, message_id)

    def nack(self, message, delay=None):
        """
        Do nothing: the message is already pending for this consumer.

        A pending entry is what a stream has instead of redelivery. It is
        served again by the next receive() of the same consumer, or handed
        to another one by claim_stale_messages() once it has been idle for
        REDIS_CLAIM_TIMEOUT.
        """

    def close(self):
        """Nothing to release: the connection belongs to the backend."""

    # -- recovery ----------------------------------------------------------

    def claim_stale_messages(self, worker_id, claim_timeout=None, max_deliveries=None):
        """
        Take over messages other consumers have held for too long.

        Uses XPENDING and XCLAIM to reassign messages that have been pending
        for longer than the claim timeout to `worker_id`, which must be the
        consumer id a worker actually receives with: receive() serves a
        consumer's own pending messages first, and that is what makes a
        reclaimed task run again. Claiming for a consumer nobody reads leaves
        the task stranded.

        A task left RUNNING by the consumer that died is handed back as READY.
        Staleness is the only thing that tells a dead worker apart from a slow
        one, so this is the only place that decision can be made.

        Consumers that have been idle for the claim timeout and hold no
        pending message are removed from the group at the same time, other
        than `worker_id` itself: a consumer blocked in XREADGROUP looks just
        as idle, and the sweeping worker is the one consumer known to be
        alive.

        Args:
            worker_id: Consumer id to claim the messages for.
            claim_timeout: Seconds a message has to have been pending. If None,
                uses the backend setting. It must be longer than the longest
                task the workers run, otherwise a task that is still running
                is reclaimed and executed twice.
            max_deliveries: Give up on a task that was started this many times
                without finishing, and mark it FAILED. If None, uses the
                backend setting; 0 disables the cap.

        Returns:
            Number of messages claimed.
        """
        if claim_timeout is None:
            claim_timeout = self.backend.claim_timeout
        if max_deliveries is None:
            max_deliveries = self.backend.max_deliveries

        claim_timeout_ms = int(claim_timeout * 1000)
        claimed_count = 0

        for stream_key in self.stream_keys():
            claimed_count += self._claim_stale_in_stream(
                stream_key,
                worker_id,
                claim_timeout_ms,
                max_deliveries,
                limit=MAX_CLAIMS_PER_SWEEP - claimed_count,
            )
            if claimed_count >= MAX_CLAIMS_PER_SWEEP:
                logger.info(
                    "Consumer %s claimed %s stale tasks, stopping this sweep",
                    worker_id,
                    claimed_count,
                )
                # There is more to reclaim; the dead consumers still hold it,
                # so they are left for the sweep that finishes the job.
                return claimed_count

        for stream_key in self.stream_keys():
            self._remove_idle_consumers(stream_key, worker_id, claim_timeout_ms)

        return claimed_count

    def _remove_idle_consumers(self, stream_key, worker_id, idle_timeout_ms):
        """
        Remove the consumers of one stream that are idle and hold nothing.

        Deleting a consumer deletes its pending entries with it, which would
        lose the tasks they name, so only a consumer with none is removed.
        Idle time is measured by Redis, so no clock of ours is involved.
        """
        removed = 0
        for consumer in self._consumers(stream_key):
            if consumer["name"] == worker_id or consumer["pending"]:
                continue
            if consumer["idle"] < idle_timeout_ms:
                continue
            self.client.xgroup_delconsumer(
                stream_key, self.consumer_group, consumer["name"]
            )
            removed += 1

        if removed:
            logger.info("Removed %s idle consumer(s) from %s", removed, stream_key)
        return removed

    def remove_consumer(self, worker_id):
        """
        Remove `worker_id` from the consumer group of every stream it read.

        For a worker that is exiting: its consumer would otherwise stay in the
        group until a sweep found it idle. A consumer that still holds pending
        messages is left alone, since deleting it would lose them; the sweep
        reclaims them first and removes the consumer then.

        Returns:
            Number of streams the consumer was removed from.
        """
        removed = 0
        for stream_key in self.stream_keys():
            for consumer in self._consumers(stream_key):
                if consumer["name"] != worker_id:
                    continue
                if consumer["pending"]:
                    logger.info(
                        "Consumer %s keeps %s pending message(s) on %s; left in "
                        "the group for the stale-message sweep",
                        worker_id,
                        consumer["pending"],
                        stream_key,
                    )
                    break
                self.client.xgroup_delconsumer(
                    stream_key, self.consumer_group, worker_id
                )
                removed += 1
                break
        return removed

    def _consumers(self, stream_key):
        """XINFO CONSUMERS for one stream, or nothing if it has no group yet."""
        try:
            return self.client.xinfo_consumers(stream_key, self.consumer_group)
        except redis.ResponseError as error:
            # A stream nothing was ever written to has no group. Redis reports
            # a missing stream as "no such key" rather than NOGROUP.
            if not is_missing_group(error) and "no such key" not in str(error):
                raise
            return []

    def _claim_stale_in_stream(
        self, stream_key, worker_id, claim_timeout_ms, max_deliveries, limit
    ):
        client = self.client

        try:
            pending = client.xpending(stream_key, self.consumer_group)
        except redis.ResponseError as error:
            # Stream or group doesn't exist
            if not is_missing_group(error):
                raise
            return 0

        if not pending or not pending["pending"]:
            return 0

        claimed_count = 0
        # Reading only the first page would cap recovery at that many messages.
        start = "-"
        while claimed_count < limit:
            pending_range = client.xpending_range(
                stream_key, self.consumer_group, start, "+", count=PENDING_PAGE_SIZE
            )
            if not pending_range:
                break

            for entry in pending_range:
                # entry: {'message_id': ..., 'consumer': ...,
                #         'time_since_delivered': ..., 'times_delivered': ...}
                if entry["time_since_delivered"] < claim_timeout_ms:
                    continue

                # min-idle-time is what makes concurrent sweeps safe: the first
                # XCLAIM resets the idle clock, so the others no longer match.
                claimed = client.xclaim(
                    stream_key,
                    self.consumer_group,
                    worker_id,
                    claim_timeout_ms,
                    [entry["message_id"]],
                )
                if not claimed:
                    continue

                message_id, data = claimed[0]
                message = BrokerMessage(
                    (data or {}).get("task_id"),
                    handle=(stream_key, message_id),
                    raw=data,
                )
                # Redis counts every delivery, including the history reads
                # receive() does for a consumer's other streams, so the counter
                # alone would abandon a task that never ran. It only decides
                # when to look at the hash, where the real number of starts is
                # kept.
                if (
                    max_deliveries
                    and entry["times_delivered"] >= max_deliveries
                    and self._abandon(message, max_deliveries)
                ):
                    continue

                claimed_count += 1
                self._release_interrupted_task(message)
                if claimed_count >= limit:
                    break

            if len(pending_range) < PENDING_PAGE_SIZE:
                break
            start = next_message_id(pending_range[-1]["message_id"])

        return claimed_count

    def _release_interrupted_task(self, message):
        """Hand a task its dead consumer left RUNNING back to the queue."""
        if not message.task_id:
            return

        if self.backend.transition_task_status(
            message.task_id, TaskResultStatus.READY, [TaskResultStatus.RUNNING]
        ):
            logger.warning(
                "Task %s was interrupted mid-run and is queued again", message.task_id
            )

    def _abandon(self, message, max_deliveries):
        """
        Give up on a message whose task was started `max_deliveries` times.

        The task is recorded as failed and the message is acknowledged, so it
        is never handed out again. Returns False, leaving the message to run,
        when the task has been started fewer times than that: a delivery that
        did not lead to a start is not an attempt.
        """
        if not message.task_id:
            # The entry is gone from the stream and only the pending record is
            # left.
            stream_key, message_id = message.handle
            self.client.xack(stream_key, self.consumer_group, message_id)
            return True

        attempts = len(
            deserialize_json(
                self.client.hget(self.result_key(message.task_id), "worker_ids_json")
                or "[]"
            )
            or []
        )
        if attempts < max_deliveries:
            return False

        # False means the task is finished or gone; either way the message has
        # nothing left to run and must not stay pending.
        self.backend.mark_task_failed(
            message.task_id,
            f"Abandoned after {attempts} attempts without a successful run.",
        )
        self.ack(message)
        return True

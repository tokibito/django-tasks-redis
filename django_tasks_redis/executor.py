"""
Public API for executing Redis tasks.

This module provides functions to process tasks stored in Redis
without using management commands.

Example usage:
    from django_tasks_redis import executor

    # Process a single task
    result = executor.process_one_task()

    # Process multiple tasks
    results = executor.process_tasks(max_tasks=10)

    # Process tasks from a specific queue
    results = executor.process_tasks(queue_name="emails", max_tasks=5)
"""

import logging
import socket
import uuid

import redis
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from .utils import (
    deserialize_datetime,
    get_delayed_key,
    get_priority_stream_key,
    get_result_key,
    priority_to_level,
)

logger = logging.getLogger("django_tasks_redis")

PRIORITY_LEVELS = ["high", "normal", "low"]

PENDING_PAGE_SIZE = 100

# A worker runs one task at a time, so claiming a whole backlog would park it
# behind one consumer instead of spreading recovery over the live workers.
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


def _generate_worker_id():
    """Generate a unique worker ID."""
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def _is_missing_group(error):
    """Redis reports an absent stream or consumer group as NOGROUP.

    Anything else is a real error - a connection that dropped, a server that
    stopped answering - and must not be mistaken for an empty queue.
    """
    return "NOGROUP" in str(error)


def _next_message_id(message_id):
    """The smallest stream id strictly greater than `message_id`."""
    timestamp, _, sequence = message_id.partition("-")
    return f"{timestamp}-{int(sequence) + 1}"


def _get_queue_names(backend, queue_name=None):
    if queue_name:
        return [queue_name]
    return list(backend.queues) if backend.queues else ["default"]


def _ack_and_delete(client, backend, stream_key, message_id):
    """
    Acknowledge a message and reclaim the stream entry behind it.

    XACK only clears the pending entry; the message itself stays in the stream.
    Without the XDEL every priority stream grows by one entry per task, forever.
    """
    client.xack(stream_key, backend.consumer_group, message_id)
    client.xdel(stream_key, message_id)


def fetch_task(queue_name=None, backend_name="default", worker_id=None, block=None):
    """
    Fetch and lock a single pending task from Redis Stream.

    This function uses XREADGROUP to safely fetch a task
    without conflicts in multi-worker environments.

    Messages this consumer already owns are served first: claim_stale_tasks
    reassigns the pending messages of a dead worker to a live consumer, and this
    is where they get delivered again. New messages are only read after that.

    Args:
        queue_name: Optional queue name to filter tasks.
        backend_name: Backend name (default: "default").
        worker_id: Optional worker ID. If not provided, one will be generated.
        block: Milliseconds to wait for a new message when every stream is
            empty, instead of returning None immediately. A blocking read waits
            on all streams at once, so a message that arrives on a lower
            priority stream at the same moment as one on a higher priority
            stream may be served first; strict priority still holds for
            messages already queued.

    Returns:
        Task data dict if a task is available, None otherwise.
    """
    if worker_id is None:
        worker_id = _generate_worker_id()

    backend = task_backends[backend_name]
    client = backend.get_client()

    # First, move delayed tasks to streams if their time has come
    _process_delayed_tasks(backend, queue_name)

    queue_names = _get_queue_names(backend, queue_name)
    stream_keys = [
        get_priority_stream_key(backend.key_prefix, backend_name, qname, priority_level)
        # Try each priority level: high, normal, low
        for priority_level in PRIORITY_LEVELS
        for qname in queue_names
    ]

    # Messages this consumer already owns, every stream in one round trip:
    # reading history delivers nothing new, so asking them all at once is free.
    task_data = _read_streams(client, backend, stream_keys, worker_id, "0")
    if task_data is not None:
        return task_data

    # New messages, one stream at a time: reading them all at once would hold
    # lower priority messages this worker is not going to run yet.
    for stream_key in stream_keys:
        task_data = _read_one_task(client, backend, stream_key, worker_id, ">")
        if task_data is not None:
            return task_data

    if block:
        return _read_streams(client, backend, stream_keys, worker_id, ">", block=block)

    return None


def _read_one_task(client, backend, stream_key, worker_id, read_id):
    """Read a single message from one stream and resolve the task behind it."""
    try:
        result = client.xreadgroup(
            backend.consumer_group,
            worker_id,
            {stream_key: read_id},
            count=1,
            block=None,  # Non-blocking (block=0 means block indefinitely)
        )
    except redis.ResponseError as error:
        # Stream or group doesn't exist yet
        if not _is_missing_group(error):
            raise
        return None

    if not result:
        return None

    # result is [(stream_key, [(message_id, data)])]
    _stream_name, messages = result[0]
    if not messages:
        return None

    message_id, data = messages[0]
    return _resolve_message(client, backend, stream_key, message_id, data)


def _read_streams(client, backend, stream_keys, worker_id, read_id, block=None):
    """
    Read one message from every stream at once, optionally waiting for one.

    Returns the first task that has to run, taking the streams in the priority
    order they were given rather than the order Redis replied in. Anything
    delivered for the other streams stays pending for this consumer and is
    served by the next fetch.
    """
    streams = dict.fromkeys(stream_keys, read_id)

    try:
        result = client.xreadgroup(
            backend.consumer_group, worker_id, streams, count=1, block=block
        )
    except redis.ResponseError as error:
        # A stream nothing has ever been written to has no consumer group, and
        # that fails the whole read. Create the missing ones and read again.
        if not _is_missing_group(error):
            raise
        for stream_key in stream_keys:
            backend._ensure_consumer_group(client, stream_key)
        result = client.xreadgroup(
            backend.consumer_group, worker_id, streams, count=1, block=block
        )

    delivered = {name: messages for name, messages in result or [] if messages}
    for stream_key in stream_keys:
        messages = delivered.get(stream_key)
        if not messages:
            continue
        message_id, data = messages[0]
        task_data = _resolve_message(client, backend, stream_key, message_id, data)
        if task_data is not None:
            return task_data

    return None


def _resolve_message(client, backend, stream_key, message_id, data):
    """
    Turn a delivered stream message into runnable task data.

    Returns None when the message does not need to run, acknowledging it first
    so it is neither delivered nor kept around again.
    """
    if not data:
        # The entry is gone from the stream and only the pending record is left.
        client.xack(stream_key, backend.consumer_group, message_id)
        return None

    task_id = data.get("task_id")

    # Get full task data from hash
    result_key = get_result_key(backend.key_prefix, backend.alias, task_id)
    task_data = client.hgetall(result_key)

    if not task_data:
        # Task data not found, acknowledge message
        _ack_and_delete(client, backend, stream_key, message_id)
        return None

    # Only READY runs. RUNNING means another worker owns the task; if that
    # worker is in fact dead, claim_stale_tasks is what notices and hands the
    # task back as READY, because staleness is the only way to tell them apart.
    if task_data.get("status") != TaskResultStatus.READY:
        # Task already processed, acknowledge and skip
        _ack_and_delete(client, backend, stream_key, message_id)
        return None

    # Check run_after constraint
    run_after = deserialize_datetime(task_data.get("run_after", ""))
    if run_after and run_after > timezone.now():
        # Back to the delayed set, not acknowledged away: an acknowledged
        # message is never delivered again, so dropping it loses the task.
        delayed_key = get_delayed_key(
            backend.key_prefix, backend.alias, task_data.get("queue_name", "default")
        )
        client.zadd(delayed_key, {task_id: run_after.timestamp()})
        _ack_and_delete(client, backend, stream_key, message_id)
        return None

    # Store message_id for acknowledgment
    task_data["_stream_key"] = stream_key
    task_data["_message_id"] = message_id
    return task_data


def _process_delayed_tasks(backend, queue_name=None):
    """Move delayed tasks to streams if their time has come."""
    client = backend.get_client()
    now_timestamp = timezone.now().timestamp()
    promote = client.register_script(_PROMOTE_DELAYED_TASK)

    for qname in _get_queue_names(backend, queue_name):
        delayed_key = get_delayed_key(backend.key_prefix, backend.alias, qname)

        # Get tasks ready to be executed
        ready_tasks = client.zrangebyscore(delayed_key, 0, now_timestamp)

        for task_id in ready_tasks:
            # Get task data
            result_key = get_result_key(backend.key_prefix, backend.alias, task_id)
            task_data = client.hgetall(result_key)

            if not task_data:
                # The result hash expired or was deleted: nothing left to run.
                client.zrem(delayed_key, task_id)
                continue

            # Add to stream
            priority = int(task_data.get("priority", "0"))
            priority_level = priority_to_level(priority)
            stream_key = get_priority_stream_key(
                backend.key_prefix, backend.alias, qname, priority_level
            )

            backend._ensure_consumer_group(client, stream_key)
            # The script re-checks the status, so a task that is no longer READY
            # leaves the delayed set without being promoted.
            promote(
                keys=[delayed_key, result_key, stream_key],
                args=[
                    task_id,
                    TaskResultStatus.READY,
                    "task_id",
                    task_id,
                    "task_path",
                    task_data["task_path"],
                    "priority",
                    task_data["priority"],
                    "queue_name",
                    qname,
                    "enqueued_at",
                    task_data.get("enqueued_at", ""),
                ],
            )


def process_one_task(
    queue_name=None, backend_name="default", worker_id=None, block=None
):
    """
    Fetch and execute a single pending task.

    Args:
        queue_name: Optional queue name to filter tasks.
        backend_name: Backend name (default: "default").
        worker_id: Optional worker ID. If not provided, one will be generated.
        block: Milliseconds to wait for a task when every stream is empty.

    Returns:
        TaskResult if a task was processed, None if no task was available.

    Example:
        >>> from django_tasks_redis import executor
        >>> result = executor.process_one_task()
        >>> if result:
        ...     print(f"Processed: {result.id}, status: {result.status}")
        ... else:
        ...     print("No tasks available")
    """
    if worker_id is None:
        worker_id = _generate_worker_id()

    task_data = fetch_task(
        queue_name=queue_name,
        backend_name=backend_name,
        worker_id=worker_id,
        block=block,
    )

    if task_data is None:
        return None

    backend = task_backends[backend_name]
    client = backend.get_client()

    # Extract stream info for acknowledgment
    stream_key = task_data.pop("_stream_key", None)
    message_id = task_data.pop("_message_id", None)

    # An exception leaves the message pending on purpose: claim_stale_tasks hands
    # it out again, bounded by REDIS_MAX_DELIVERIES.
    result = backend.run_task(task_data["task_id"], worker_id=worker_id)

    # Acknowledge message after successful processing
    if stream_key and message_id:
        _ack_and_delete(client, backend, stream_key, message_id)

    return result


def process_tasks(
    queue_name=None,
    backend_name="default",
    max_tasks=0,
    worker_id=None,
):
    """
    Process multiple pending tasks.

    Args:
        queue_name: Optional queue name to filter tasks.
        backend_name: Backend name (default: "default").
        max_tasks: Maximum number of tasks to process (0 = unlimited).
        worker_id: Optional worker ID. If not provided, one will be generated.

    Returns:
        List of TaskResult objects for all processed tasks.

    Example:
        >>> from django_tasks_redis import executor
        >>> results = executor.process_tasks(max_tasks=10)
        >>> print(f"Processed {len(results)} tasks")
        >>> for result in results:
        ...     print(f"  {result.id}: {result.status}")
    """
    if worker_id is None:
        worker_id = _generate_worker_id()

    results = []
    tasks_processed = 0

    while True:
        result = process_one_task(
            queue_name=queue_name,
            backend_name=backend_name,
            worker_id=worker_id,
        )

        if result is None:
            break

        results.append(result)
        tasks_processed += 1

        if max_tasks and tasks_processed >= max_tasks:
            break

    return results


def get_pending_task_count(queue_name=None, backend_name="default"):
    """
    Get the count of pending tasks.

    Args:
        queue_name: Optional queue name to filter tasks.
        backend_name: Backend name (default: "default").

    Returns:
        Number of pending tasks.

    Example:
        >>> from django_tasks_redis import executor
        >>> count = executor.get_pending_task_count()
        >>> print(f"Pending tasks: {count}")
    """
    backend = task_backends[backend_name]
    counts = backend.get_status_counts(queue_name=queue_name)
    return counts.get(TaskResultStatus.READY, 0)


def run_task_by_id(task_id, backend_name="default", worker_id=None, allow_retry=False):
    """
    Execute a specific task by its ID.

    This function is designed for external trigger systems (e.g., Cloud Tasks,
    webhooks) that need to execute a specific task by ID rather than fetching
    the next available task.

    By default, only tasks in READY status can be executed. Use allow_retry=True
    to also execute FAILED tasks (useful for retry mechanisms).

    The task is claimed atomically, so a trigger delivered more than once - the
    normal guarantee of the systems this is meant for - only runs the task once.

    Args:
        task_id: UUID or string ID of the task to execute.
        backend_name: Backend name (default: "default").
        worker_id: Optional worker ID. If not provided, one will be generated.
        allow_retry: If True, also allow execution of FAILED tasks. Their error
                     history is kept, so retries stay auditable.

    Returns:
        TaskResult if the task was executed, None if the task was not found
        or not in an executable status.

    Raises:
        TaskResultDoesNotExist: If no task with the given ID exists.

    Example:
        >>> from django_tasks_redis import executor
        >>> result = executor.run_task_by_id("550e8400-e29b-41d4-a716-446655440000")
        >>> if result:
        ...     print(f"Executed: {result.id}, status: {result.status}")
        ... else:
        ...     print("Task not in executable status")

        # Retry a failed task
        >>> result = executor.run_task_by_id("...", allow_retry=True)
    """
    if worker_id is None:
        worker_id = _generate_worker_id()

    backend = task_backends[backend_name]
    task_data = backend.get_task_data(str(task_id))

    if task_data is None:
        from django.tasks.exceptions import TaskResultDoesNotExist

        raise TaskResultDoesNotExist(task_id)

    allowed_statuses = [TaskResultStatus.READY]
    if allow_retry:
        allowed_statuses.append(TaskResultStatus.FAILED)

    if not backend.transition_task_status(
        str(task_id), TaskResultStatus.RUNNING, allowed_statuses
    ):
        return None

    return backend.run_task(str(task_id), worker_id=worker_id)


def claim_stale_tasks(
    backend_name="default", claim_timeout=None, worker_id=None, max_deliveries=None
):
    """
    Claim stale tasks from pending entries.

    Uses XPENDING and XCLAIM to reclaim tasks that have been
    pending for longer than the claim timeout.

    Messages are claimed for `worker_id`, which must be the consumer id a worker
    actually fetches with: fetch_task serves a consumer's own pending messages
    first, and that is what makes a reclaimed task run again. Claiming for a
    consumer nobody reads leaves the task stranded.

    A task left RUNNING by the consumer that died is handed back as READY.
    Staleness is the only thing that tells a dead worker apart from a slow one,
    so this is the only place that decision can be made.

    Args:
        backend_name: Backend name (default: "default").
        claim_timeout: Timeout in seconds. If None, uses backend setting. It
            must be longer than the longest task the workers run, otherwise a
            task that is still running is reclaimed and executed twice.
        worker_id: Consumer id to claim the messages for. If None, one is
            generated, which only makes sense when nothing will consume them.
        max_deliveries: Give up on a message after this many delivery attempts
            and mark its task FAILED. If None, uses the backend setting; 0
            disables the cap.

    Returns:
        Number of tasks claimed.
    """
    backend = task_backends[backend_name]

    if claim_timeout is None:
        claim_timeout = backend.claim_timeout
    if max_deliveries is None:
        max_deliveries = backend.max_deliveries
    if worker_id is None:
        worker_id = _generate_worker_id()

    claim_timeout_ms = int(claim_timeout * 1000)
    claimed_count = 0

    # Get queue names
    queue_names = list(backend.queues) if backend.queues else ["default"]

    for queue_name in queue_names:
        for priority_level in PRIORITY_LEVELS:
            stream_key = get_priority_stream_key(
                backend.key_prefix, backend_name, queue_name, priority_level
            )
            claimed_count += _claim_stale_in_stream(
                backend,
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
                return claimed_count

    return claimed_count


def _claim_stale_in_stream(
    backend, stream_key, worker_id, claim_timeout_ms, max_deliveries, limit
):
    client = backend.get_client()

    try:
        # Get pending entries
        pending = client.xpending(stream_key, backend.consumer_group)
    except redis.ResponseError as error:
        # Stream or group doesn't exist
        if not _is_missing_group(error):
            raise
        return 0

    if not pending or not pending["pending"]:
        return 0

    claimed_count = 0
    # Reading only the first page would cap recovery at that many messages.
    start = "-"
    while claimed_count < limit:
        # Get detailed pending info
        pending_range = client.xpending_range(
            stream_key,
            backend.consumer_group,
            start,
            "+",
            count=PENDING_PAGE_SIZE,
        )
        if not pending_range:
            break

        for entry in pending_range:
            # entry: {'message_id': ..., 'consumer': ..., 'time_since_delivered': ..., 'times_delivered': ...}
            if entry["time_since_delivered"] < claim_timeout_ms:
                continue

            if max_deliveries and entry["times_delivered"] >= max_deliveries:
                _abandon_message(client, backend, stream_key, entry)
                continue

            # min-idle-time is what makes concurrent sweeps safe: the first
            # XCLAIM resets the idle clock, so the others no longer match.
            claimed = client.xclaim(
                stream_key,
                backend.consumer_group,
                worker_id,
                claim_timeout_ms,
                [entry["message_id"]],
            )

            if claimed:
                claimed_count += 1
                _release_interrupted_task(backend, claimed[0][1])
                if claimed_count >= limit:
                    break

        if len(pending_range) < PENDING_PAGE_SIZE:
            break
        start = _next_message_id(pending_range[-1]["message_id"])

    return claimed_count


def _release_interrupted_task(backend, data):
    """Hand a task its dead consumer left RUNNING back to the queue."""
    task_id = (data or {}).get("task_id")
    if not task_id:
        return

    if backend.transition_task_status(
        task_id, TaskResultStatus.READY, [TaskResultStatus.RUNNING]
    ):
        logger.warning("Task %s was interrupted mid-run and is queued again", task_id)


def _abandon_message(client, backend, stream_key, entry):
    """Stop redelivering a message and record its task as failed."""
    message_id = entry["message_id"]

    messages = client.xrange(stream_key, message_id, message_id)
    task_id = messages[0][1].get("task_id") if messages else None
    if task_id:
        backend.mark_task_failed(
            task_id,
            f"Abandoned after {entry['times_delivered']} delivery attempts "
            f"without a successful run.",
        )

    _ack_and_delete(client, backend, stream_key, message_id)


def purge_completed_tasks(
    backend_name="default", days=7, statuses=None, batch_size=None, dry_run=False
):
    """
    Delete completed tasks older than specified days.

    Args:
        backend_name: Backend name (default: "default").
        days: Delete tasks finished more than this many days ago.
        statuses: List of statuses to delete. Default: [SUCCESSFUL, FAILED].
        batch_size: Tasks read per round trip. If None, uses backend setting.
        dry_run: Count the matching tasks without deleting anything.

    Returns:
        Number of tasks deleted, or that would be deleted for a dry run.
    """
    if statuses is None:
        statuses = [TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED]

    backend = task_backends[backend_name]

    cutoff = timezone.now() - timezone.timedelta(days=days)
    deleted_count = 0

    # A dry run must not write anything, not even index housekeeping.
    for task_id, task_data in backend.iter_task_data(
        batch_size=batch_size, cleanup=not dry_run
    ):
        if task_data.get("status") not in statuses:
            continue

        finished_at = deserialize_datetime(task_data.get("finished_at", ""))
        if finished_at and finished_at < cutoff:
            if not dry_run:
                backend.delete_task_data(task_id)
            deleted_count += 1

    return deleted_count


# Admin API functions


def get_tasks(
    backend_name="default",
    queue_name=None,
    status=None,
    offset=0,
    limit=100,
    order_by="-enqueued_at",
):
    """
    Get a list of tasks.

    Args:
        backend_name: Backend name.
        queue_name: Optional queue name filter.
        status: Optional status filter.
        offset: Starting offset.
        limit: Maximum number of results.
        order_by: Sort order (ignored, always -enqueued_at).

    Returns:
        Tuple of (list of task dicts, total count).
    """
    backend = task_backends[backend_name]
    return backend.get_all_tasks(
        queue_name=queue_name,
        status=status,
        offset=offset,
        limit=limit,
    )


def get_task_by_id(task_id, backend_name="default"):
    """
    Get a task by ID.

    Args:
        task_id: Task ID string.
        backend_name: Backend name.

    Returns:
        Task data dict or None.
    """
    backend = task_backends[backend_name]
    return backend.get_task_data(str(task_id))


def delete_task(task_id, backend_name="default"):
    """
    Delete a task.

    Args:
        task_id: Task ID string.
        backend_name: Backend name.

    Returns:
        True if deleted.
    """
    backend = task_backends[backend_name]
    return backend.delete_task_data(str(task_id))


def delete_tasks(task_ids, backend_name="default"):
    """
    Delete multiple tasks.

    Args:
        task_ids: List of task ID strings.
        backend_name: Backend name.

    Returns:
        Number of tasks deleted.
    """
    backend = task_backends[backend_name]
    deleted = 0
    for task_id in task_ids:
        if backend.delete_task_data(str(task_id)):
            deleted += 1
    return deleted


def reset_task_for_retry(task_id, backend_name="default"):
    """
    Reset a failed task for retry.

    Args:
        task_id: Task ID string.
        backend_name: Backend name.

    Returns:
        True if reset.
    """
    backend = task_backends[backend_name]
    return backend.reset_task_status(str(task_id))


def get_task_counts(backend_name="default", queue_name=None):
    """
    Get task counts by status.

    Args:
        backend_name: Backend name.
        queue_name: Optional queue name filter.

    Returns:
        Dict mapping status to count.
    """
    backend = task_backends[backend_name]
    return backend.get_status_counts(queue_name=queue_name)


def get_queue_stats(backend_name="default", queue_name=None):
    """
    Get queue statistics.

    Args:
        backend_name: Backend name.
        queue_name: Optional queue name filter.

    Returns:
        Dict with queue statistics.
    """
    backend = task_backends[backend_name]
    client = backend.get_client()

    counts = backend.get_status_counts(queue_name=queue_name)

    # Get delayed count
    queue_names = (
        [queue_name]
        if queue_name
        else (list(backend.queues) if backend.queues else ["default"])
    )

    delayed_count = 0
    for qname in queue_names:
        delayed_key = get_delayed_key(backend.key_prefix, backend_name, qname)
        delayed_count += client.zcard(delayed_key)

    return {
        "pending_count": counts.get(TaskResultStatus.READY, 0),
        "running_count": counts.get(TaskResultStatus.RUNNING, 0),
        "successful_count": counts.get(TaskResultStatus.SUCCESSFUL, 0),
        "failed_count": counts.get(TaskResultStatus.FAILED, 0),
        "delayed_count": delayed_count,
    }

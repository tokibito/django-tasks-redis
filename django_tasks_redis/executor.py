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
import os
import socket
import uuid

from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from .utils import (
    deserialize_datetime,
    get_delayed_key,
    get_priority_stream_key,
    get_result_key,
    get_results_index_key,
    priority_to_level,
)

logger = logging.getLogger("django_tasks_redis")

# Priority levels checked for every queue, highest first.
PRIORITY_LEVELS = ("high", "normal", "low")

_process_worker_id = None


def _generate_worker_id():
    """Generate a unique worker ID."""
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def _get_process_worker_id():
    """
    Get a worker ID that is stable for the lifetime of this process.

    Ad-hoc executor calls (views, webhooks, scripts) used to generate a
    fresh worker ID — and therefore a brand-new stream consumer — on every
    call, growing the consumer group forever. A per-process ID keeps the
    consumer count bounded by the number of processes.
    """
    global _process_worker_id
    if _process_worker_id is None:
        _process_worker_id = f"{socket.gethostname()}-{os.getpid()}"
    return _process_worker_id


def _queue_names_for(backend, queue_name=None):
    """Queue names to operate on: the given one, or all configured."""
    if queue_name:
        return [queue_name]
    return list(backend.queues) if backend.queues else ["default"]


# Atomically acknowledge a stuck delivery, flip a RUNNING task back to
# READY, and re-add the entry to the stream. The XACK acts as a mutual
# exclusion: when two workers race to requeue the same message (e.g. a
# shutdown cleanup racing a stale-claim cycle), only the one whose XACK
# returns 1 requeues — the loser does nothing, so the task can neither be
# duplicated nor lost, and a task that finished in the meantime is only
# acknowledged, never clobbered back to READY.
#
# KEYS[1]=stream, KEYS[2]=result hash
# ARGV[1]=group, ARGV[2]=message id, ARGV[3]=RUNNING, ARGV[4]=READY,
# ARGV[5:]=flattened field/value pairs for the re-added entry
_REQUEUE_SCRIPT = """
local acked = redis.call('XACK', KEYS[1], ARGV[1], ARGV[2])
if acked == 0 then
  return 0
end
local status = redis.call('HGET', KEYS[2], 'status')
if status == false then
  return -1
end
if status ~= ARGV[3] and status ~= ARGV[4] then
  return -2
end
if status == ARGV[3] then
  redis.call('HSET', KEYS[2], 'status', ARGV[4])
end
local xadd_args = {'XADD', KEYS[1], '*'}
for i = 5, #ARGV do
  xadd_args[#xadd_args + 1] = ARGV[i]
end
redis.call(unpack(xadd_args))
return 1
"""

# Delete a consumer only if it holds no pending entries — atomically, so a
# delivery landing between the check and the delete cannot be dropped from
# the PEL (XGROUP DELCONSUMER discards the consumer's pending entries).
# KEYS[1]=stream, ARGV[1]=group, ARGV[2]=consumer
_DELCONSUMER_SCRIPT = """
local pending = redis.call('XPENDING', KEYS[1], ARGV[1], '-', '+', 1, ARGV[2])
if #pending > 0 then
  return -1
end
redis.call('XGROUP', 'DELCONSUMER', KEYS[1], ARGV[1], ARGV[2])
return 1
"""


def _get_script(backend, attr, source):
    """Get a cached registered Lua script for the backend's client."""
    script = getattr(backend, attr, None)
    if script is None:
        script = backend.get_client().register_script(source)
        setattr(backend, attr, script)
    return script


def fetch_task(queue_name=None, backend_name="default", worker_id=None):
    """
    Fetch and lock a single pending task from Redis Stream.

    This function uses XREADGROUP to safely fetch a task
    without conflicts in multi-worker environments.

    Args:
        queue_name: Optional queue name to filter tasks.
        backend_name: Backend name (default: "default").
        worker_id: Optional worker ID. If not provided, one will be generated.

    Returns:
        Task data dict if a task is available, None otherwise.
    """
    if worker_id is None:
        worker_id = _get_process_worker_id()

    backend = task_backends[backend_name]
    client = backend.get_client()
    now = timezone.now()

    # First, move delayed tasks to streams if their time has come
    _process_delayed_tasks(backend, queue_name)

    queue_names = _queue_names_for(backend, queue_name)

    # Try each priority level: high, normal, low
    for priority_level in PRIORITY_LEVELS:
        for qname in queue_names:
            stream_key = get_priority_stream_key(
                backend.key_prefix, backend_name, qname, priority_level
            )

            try:
                # Read one message from the stream (non-blocking)
                result = client.xreadgroup(
                    backend.consumer_group,
                    worker_id,
                    {stream_key: ">"},
                    count=1,
                    block=None,  # Non-blocking (block=0 means block indefinitely)
                )

                if result:
                    # result is [(stream_key, [(message_id, data)])]
                    stream_name, messages = result[0]
                    if messages:
                        message_id, data = messages[0]
                        task_id = data.get("task_id")

                        # Get full task data from hash
                        result_key = get_result_key(
                            backend.key_prefix, backend_name, task_id
                        )
                        task_data = client.hgetall(result_key)

                        if task_data:
                            # Check if task is still in READY status
                            status = task_data.get("status")
                            if status != TaskResultStatus.READY:
                                # Task already processed, acknowledge and skip
                                client.xack(
                                    stream_key, backend.consumer_group, message_id
                                )
                                continue

                            # Check run_after constraint
                            run_after = deserialize_datetime(
                                task_data.get("run_after", "")
                            )
                            if run_after and run_after > now:
                                # Not ready yet, acknowledge and skip
                                client.xack(
                                    stream_key, backend.consumer_group, message_id
                                )
                                continue

                            # Store message_id for acknowledgment
                            task_data["_stream_key"] = stream_key
                            task_data["_message_id"] = message_id
                            return task_data

                        # Task data not found, acknowledge message
                        client.xack(stream_key, backend.consumer_group, message_id)

            except Exception:
                # Stream or group doesn't exist yet
                pass

    return None


def _process_delayed_tasks(backend, queue_name=None):
    """Move delayed tasks to streams if their time has come."""
    client = backend.get_client()
    now = timezone.now()
    now_timestamp = now.timestamp()

    queue_names = _queue_names_for(backend, queue_name)

    for qname in queue_names:
        delayed_key = get_delayed_key(backend.key_prefix, backend.alias, qname)

        # Get tasks ready to be executed
        ready_tasks = client.zrangebyscore(delayed_key, 0, now_timestamp)

        for task_id in ready_tasks:
            # Get task data
            result_key = get_result_key(backend.key_prefix, backend.alias, task_id)
            task_data = client.hgetall(result_key)

            if task_data and task_data.get("status") == TaskResultStatus.READY:
                # Add to stream
                priority = int(task_data.get("priority", "0"))
                priority_level = priority_to_level(priority)
                stream_key = get_priority_stream_key(
                    backend.key_prefix, backend.alias, qname, priority_level
                )

                stream_data = {
                    "task_id": task_id,
                    "task_path": task_data["task_path"],
                    "priority": task_data["priority"],
                    "queue_name": qname,
                    "enqueued_at": task_data.get("enqueued_at", ""),
                }

                backend._ensure_consumer_group(client, stream_key)
                backend.xadd_task(client, stream_key, stream_data)

            # Remove from delayed set
            client.zrem(delayed_key, task_id)


def process_one_task(queue_name=None, backend_name="default", worker_id=None):
    """
    Fetch and execute a single pending task.

    Args:
        queue_name: Optional queue name to filter tasks.
        backend_name: Backend name (default: "default").
        worker_id: Optional worker ID. If not provided, one will be generated.

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
        worker_id = _get_process_worker_id()

    task_data = fetch_task(
        queue_name=queue_name, backend_name=backend_name, worker_id=worker_id
    )

    if task_data is None:
        return None

    backend = task_backends[backend_name]
    client = backend.get_client()

    # Extract stream info for acknowledgment
    stream_key = task_data.pop("_stream_key", None)
    message_id = task_data.pop("_message_id", None)

    try:
        result = backend.run_task(task_data["task_id"], worker_id=worker_id)

        # Acknowledge message after successful processing
        if stream_key and message_id:
            client.xack(stream_key, backend.consumer_group, message_id)

        return result
    except Exception:
        # Re-raise exception, message remains in pending for retry
        raise


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
        worker_id = _get_process_worker_id()

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

    Args:
        task_id: UUID or string ID of the task to execute.
        backend_name: Backend name (default: "default").
        worker_id: Optional worker ID. If not provided, one will be generated.
        allow_retry: If True, also allow execution of FAILED tasks.
                     The task will be reset to READY before execution.

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

    current_status = task_data.get("status")
    if current_status not in allowed_statuses:
        return None

    # Reset FAILED task to READY for retry
    if current_status == TaskResultStatus.FAILED:
        backend.reset_task_status(str(task_id))

    return backend.run_task(str(task_id), worker_id=worker_id)


def _requeue_claimed_message(backend, client, stream_key, message_id, msg_data):
    """
    Requeue a claimed pending message so any worker can pick it up again.

    XREADGROUP with ">" only ever delivers new entries, so a message left
    in a consumer's PEL is invisible to every worker. To make the task
    runnable again, the message is acknowledged, a RUNNING task is flipped
    back to READY, and a fresh copy is added to the stream — all in one
    atomic Lua script, so racing requeuers (a shutdown cleanup vs a
    stale-claim cycle) can neither duplicate nor lose the task, and a task
    that finished concurrently is only acknowledged, never re-run.

    Returns True if the task was requeued, False if it only needed an ack
    (already finished, hash gone, or another requeuer won the race).
    """
    task_id = msg_data.get("task_id") if msg_data else None
    if not task_id:
        client.xack(stream_key, backend.consumer_group, message_id)
        return False

    result_key = get_result_key(backend.key_prefix, backend.alias, task_id)

    fields = []
    for field, value in msg_data.items():
        fields.extend((field, value))

    script = _get_script(backend, "_requeue_script", _REQUEUE_SCRIPT)
    outcome = script(
        keys=[stream_key, result_key],
        args=[
            backend.consumer_group,
            message_id,
            str(TaskResultStatus.RUNNING),
            str(TaskResultStatus.READY),
            *fields,
        ],
        client=client,
    )

    if outcome != 1:
        return False

    # The task is READY again; reflect that in the status index (scored by
    # the original enqueue time, matching the hash's own expiry).
    enqueued_at, queue_name = client.hmget(result_key, "enqueued_at", "queue_name")
    enqueued_dt = deserialize_datetime(enqueued_at or "")
    backend.update_status_index(
        client,
        task_id,
        queue_name or None,
        TaskResultStatus.RUNNING,
        TaskResultStatus.READY,
        score=enqueued_dt.timestamp() if enqueued_dt else None,
    )
    return True


def claim_stale_tasks(backend_name="default", claim_timeout=None, worker_id=None):
    """
    Reclaim and requeue tasks whose delivery went stale.

    Uses XPENDING and XCLAIM to find messages that have been pending
    longer than the claim timeout (their worker died or hung), resets
    their task status from RUNNING back to READY, and requeues them so
    any worker can pick them up.

    Args:
        backend_name: Backend name (default: "default").
        claim_timeout: Timeout in seconds. If None, uses backend setting.
        worker_id: Consumer name to claim under. If None, a per-process
            ID is used. Never pass a fresh random ID per call — that
            grows the consumer group forever.

    Returns:
        Number of tasks claimed.
    """
    backend = task_backends[backend_name]
    client = backend.get_client()

    if claim_timeout is None:
        claim_timeout = backend.claim_timeout
    if worker_id is None:
        worker_id = _get_process_worker_id()

    claim_timeout_ms = claim_timeout * 1000
    claimed_count = 0

    queue_names = _queue_names_for(backend)

    for queue_name in queue_names:
        for priority_level in PRIORITY_LEVELS:
            stream_key = get_priority_stream_key(
                backend.key_prefix, backend_name, queue_name, priority_level
            )

            try:
                # Get pending entries
                pending = client.xpending(stream_key, backend.consumer_group)

                if pending and pending["pending"] > 0:
                    # Get detailed pending info
                    pending_range = client.xpending_range(
                        stream_key,
                        backend.consumer_group,
                        "-",
                        "+",
                        count=100,
                    )

                    for entry in pending_range:
                        # entry: {'message_id': ..., 'consumer': ..., 'time_since_delivered': ..., 'times_delivered': ...}
                        if entry["time_since_delivered"] < claim_timeout_ms:
                            continue

                        # Claim the message. If another worker claims it
                        # concurrently, its idle time resets and this
                        # XCLAIM returns nothing.
                        claimed = client.xclaim(
                            stream_key,
                            backend.consumer_group,
                            worker_id,
                            claim_timeout_ms,
                            [entry["message_id"]],
                        )

                        for msg_id, msg_data in claimed or []:
                            _requeue_claimed_message(
                                backend, client, stream_key, msg_id, msg_data
                            )
                            claimed_count += 1

            except Exception:
                # Stream or group doesn't exist
                pass

    return claimed_count


def cleanup_worker(worker_id=None, backend_name="default", queue_name=None):
    """
    Remove a worker's consumer from every consumer group it may be in.

    Call this on graceful worker shutdown. Any entries still pending for
    the consumer are requeued first (so XGROUP DELCONSUMER cannot drop
    in-flight tasks), then the consumer itself is deleted. Without this,
    every worker start leaves a consumer behind forever, and XPENDING /
    XINFO CONSUMERS degrade as the group grows.

    Args:
        worker_id: Consumer name to remove. If None, the per-process ID.
        backend_name: Backend name (default: "default").
        queue_name: Optional queue name; defaults to all configured queues.

    Returns:
        Number of pending entries that were requeued.
    """
    backend = task_backends[backend_name]
    client = backend.get_client()

    if worker_id is None:
        worker_id = _get_process_worker_id()

    requeued = 0

    for qname in _queue_names_for(backend, queue_name):
        for priority_level in PRIORITY_LEVELS:
            stream_key = get_priority_stream_key(
                backend.key_prefix, backend_name, qname, priority_level
            )

            try:
                while True:
                    pending_range = client.xpending_range(
                        stream_key,
                        backend.consumer_group,
                        "-",
                        "+",
                        count=100,
                        consumername=worker_id,
                    )
                    if not pending_range:
                        break

                    for entry in pending_range:
                        # Claim to ourselves (min idle 0) to fetch the
                        # entry data; Redis drops PEL entries whose data
                        # was trimmed from the stream.
                        claimed = client.xclaim(
                            stream_key,
                            backend.consumer_group,
                            worker_id,
                            0,
                            [entry["message_id"]],
                        )
                        for msg_id, msg_data in claimed or []:
                            if _requeue_claimed_message(
                                backend, client, stream_key, msg_id, msg_data
                            ):
                                requeued += 1

                delete = _get_script(
                    backend, "_delconsumer_script", _DELCONSUMER_SCRIPT
                )
                outcome = delete(
                    keys=[stream_key],
                    args=[backend.consumer_group, worker_id],
                    client=client,
                )
                if outcome == -1:
                    # An entry appeared after the drain; leave the consumer
                    # for the stale-claim / reap cycle rather than dropping
                    # its pending entry.
                    logger.warning(
                        "Consumer %s still has pending entries on %s; not deleted",
                        worker_id,
                        stream_key,
                    )
            except Exception:
                # Stream or group doesn't exist
                pass

    return requeued


def reap_idle_consumers(
    backend_name="default", queue_name=None, min_idle_seconds=None, exclude=None
):
    """
    Delete consumers that are idle and hold no pending entries.

    Workers that die without a graceful shutdown (OOM kill, node loss)
    leave their consumer behind; over months these accumulate and make
    XPENDING / XINFO CONSUMERS O(consumers). This reaps any consumer with
    zero pending entries that has been idle longer than the threshold.
    Consumers holding pending entries are never touched — the stale-claim
    path requeues their entries first, after which they become reapable.

    Args:
        backend_name: Backend name (default: "default").
        queue_name: Optional queue name; defaults to all configured queues.
        min_idle_seconds: Idle threshold. If None, uses the backend's
            REDIS_CONSUMER_IDLE_TIMEOUT option (default 24h). A value of
            0/None there disables reaping.
        exclude: Consumer name to never reap (the caller's own).

    Returns:
        Number of consumers deleted.
    """
    backend = task_backends[backend_name]
    client = backend.get_client()

    if min_idle_seconds is None:
        min_idle_seconds = backend.consumer_idle_timeout
        if not min_idle_seconds:
            # Reaping disabled via REDIS_CONSUMER_IDLE_TIMEOUT.
            return 0
    min_idle_ms = min_idle_seconds * 1000

    reaped = 0

    for qname in _queue_names_for(backend, queue_name):
        for priority_level in PRIORITY_LEVELS:
            stream_key = get_priority_stream_key(
                backend.key_prefix, backend_name, qname, priority_level
            )

            try:
                consumers = client.xinfo_consumers(stream_key, backend.consumer_group)
            except Exception:
                # Stream or group doesn't exist
                continue

            for consumer in consumers:
                name = consumer.get("name")
                if not name or name == exclude:
                    continue
                if consumer.get("pending", 0) != 0:
                    # Never reap a consumer that still owns entries.
                    continue
                if consumer.get("idle", 0) < min_idle_ms:
                    continue
                try:
                    # The script re-checks pending atomically with the
                    # delete: a delivery landing after the XINFO snapshot
                    # must not be dropped from the PEL.
                    delete = _get_script(
                        backend, "_delconsumer_script", _DELCONSUMER_SCRIPT
                    )
                    outcome = delete(
                        keys=[stream_key],
                        args=[backend.consumer_group, name],
                        client=client,
                    )
                    if outcome == 1:
                        reaped += 1
                except Exception as e:
                    logger.warning(
                        "Failed to delete idle consumer %s from %s: %s",
                        name,
                        stream_key,
                        e,
                    )

    if reaped:
        logger.info("Reaped %d idle consumer(s)", reaped)
    return reaped


def purge_completed_tasks(
    backend_name="default", days=7, statuses=None, task_path=None
):
    """
    Delete completed tasks older than specified days.

    Args:
        backend_name: Backend name (default: "default").
        days: Delete tasks finished more than this many days ago.
        statuses: List of statuses to delete. Default: [SUCCESSFUL, FAILED].
        task_path: Optional task path filter. Only purge tasks with this task_path.

    Returns:
        Number of tasks deleted.
    """
    if statuses is None:
        statuses = [TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED]

    backend = task_backends[backend_name]
    client = backend.get_client()
    results_index_key = get_results_index_key(backend.key_prefix, backend_name)

    cutoff = timezone.now() - timezone.timedelta(days=days)
    deleted_count = 0

    task_ids = client.smembers(results_index_key)

    for task_id in task_ids:
        result_key = get_result_key(backend.key_prefix, backend_name, task_id)
        task_data = client.hgetall(result_key)

        if not task_data:
            client.srem(results_index_key, task_id)
            continue

        status = task_data.get("status")
        if status not in statuses:
            continue

        # Filter by task_path if specified
        if task_path and task_data.get("task_path") != task_path:
            continue

        finished_at = deserialize_datetime(task_data.get("finished_at", ""))
        if finished_at and finished_at < cutoff:
            client.delete(result_key)
            client.srem(results_index_key, task_id)
            backend.remove_from_status_index(
                client, task_id, task_data.get("queue_name") or None
            )
            deleted_count += 1

    return deleted_count


# Admin API functions


def get_tasks(
    backend_name="default",
    queue_name=None,
    status=None,
    task_path=None,
    priority=None,
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
        task_path: Optional task path filter.
        priority: Optional priority filter.
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
        task_path=task_path,
        priority=priority,
        offset=offset,
        limit=limit,
    )


def get_distinct_field_values(field_name, backend_name="default"):
    """
    Get distinct values for a given task field.

    Args:
        field_name: The task field name.
        backend_name: Backend name.

    Returns:
        Sorted list of distinct values.
    """
    backend = task_backends[backend_name]
    return backend.get_distinct_field_values(field_name)


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

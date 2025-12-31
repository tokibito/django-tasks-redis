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


def _generate_worker_id():
    """Generate a unique worker ID."""
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


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
        worker_id = _generate_worker_id()

    backend = task_backends[backend_name]
    client = backend.get_client()
    now = timezone.now()

    # First, move delayed tasks to streams if their time has come
    _process_delayed_tasks(backend, queue_name)

    # Get queue names to check
    if queue_name:
        queue_names = [queue_name]
    else:
        # Check all queues - get from backend config or use default
        queue_names = list(backend.queues) if backend.queues else ["default"]

    # Try each priority level: high, normal, low
    for priority_level in ["high", "normal", "low"]:
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

    # Get queue names to check
    if queue_name:
        queue_names = [queue_name]
    else:
        queue_names = list(backend.queues) if backend.queues else ["default"]

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
                client.xadd(stream_key, stream_data)

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
        worker_id = _generate_worker_id()

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


def claim_stale_tasks(backend_name="default", claim_timeout=None):
    """
    Claim stale tasks from pending entries.

    Uses XPENDING and XCLAIM to reclaim tasks that have been
    pending for longer than the claim timeout.

    Args:
        backend_name: Backend name (default: "default").
        claim_timeout: Timeout in seconds. If None, uses backend setting.

    Returns:
        Number of tasks claimed.
    """
    backend = task_backends[backend_name]
    client = backend.get_client()

    if claim_timeout is None:
        claim_timeout = backend.claim_timeout

    claim_timeout_ms = claim_timeout * 1000
    claimed_count = 0

    # Get queue names
    queue_names = list(backend.queues) if backend.queues else ["default"]

    for queue_name in queue_names:
        for priority_level in ["high", "normal", "low"]:
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
                        if entry["time_since_delivered"] >= claim_timeout_ms:
                            # Claim the message
                            claimed = client.xclaim(
                                stream_key,
                                backend.consumer_group,
                                _generate_worker_id(),
                                claim_timeout_ms,
                                [entry["message_id"]],
                            )

                            if claimed:
                                claimed_count += 1

            except Exception:
                # Stream or group doesn't exist
                pass

    return claimed_count


def purge_completed_tasks(backend_name="default", days=7, statuses=None):
    """
    Delete completed tasks older than specified days.

    Args:
        backend_name: Backend name (default: "default").
        days: Delete tasks finished more than this many days ago.
        statuses: List of statuses to delete. Default: [SUCCESSFUL, FAILED].

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

        finished_at = deserialize_datetime(task_data.get("finished_at", ""))
        if finished_at and finished_at < cutoff:
            client.delete(result_key)
            client.srem(results_index_key, task_id)
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

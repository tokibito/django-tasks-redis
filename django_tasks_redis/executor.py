"""
Public API for executing Redis tasks.

This module provides functions to process tasks stored in Redis
without using management commands.

The functions are thin wrappers over the backend's broker
(``backend.broker``, a :class:`~django_tasks_redis.brokers.RedisStreamsBroker`),
which is where reading, acknowledging and reclaiming messages live. A worker
loop of its own can use the broker directly.

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

from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from .utils import deserialize_datetime, generate_worker_id

logger = logging.getLogger("django_tasks_redis")

# Kept under its old name for the callers that had it.
_generate_worker_id = generate_worker_id


def _wait_seconds(block):
    """Translate the millisecond `block` argument into the broker's seconds."""
    if not block or block <= 0:
        return 0
    return block / 1000


def fetch_task(queue_name=None, backend_name="default", worker_id=None, block=None):
    """
    Fetch and lock a single pending task from Redis Stream.

    Receives one message through the backend's broker and returns the task
    hash behind it. The message stays pending for `worker_id` until it is
    acknowledged, which this function does not do: prefer
    :func:`process_one_task`, or ``backend.broker.receive()`` for a loop that
    acknowledges messages itself.

    Args:
        queue_name: Optional queue name to filter tasks.
        backend_name: Backend name (default: "default").
        worker_id: Optional worker ID. If not provided, one will be generated.
        block: Milliseconds to wait for a new message when every stream is
            empty, instead of returning None immediately.

    Returns:
        Task data dict if a task is available, None otherwise. The dict also
        carries the message handle as ``_stream_key`` and ``_message_id``,
        as it did before the broker existed.
    """
    if worker_id is None:
        worker_id = generate_worker_id()

    backend = task_backends[backend_name]
    broker = backend.broker

    messages = broker.receive(
        queue_name=queue_name,
        max_messages=1,
        wait_seconds=_wait_seconds(block),
        worker_id=worker_id,
    )
    if not messages:
        return None

    message = messages[0]
    task_data = backend.get_task_data(message.task_id)
    if task_data is None:
        # Gone between the read and here: nothing left to run.
        broker.ack(message)
        return None

    task_data["_stream_key"], task_data["_message_id"] = message.handle
    return task_data


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
        worker_id = generate_worker_id()

    backend = task_backends[backend_name]
    broker = backend.broker

    messages = broker.receive(
        queue_name=queue_name,
        max_messages=1,
        wait_seconds=_wait_seconds(block),
        worker_id=worker_id,
    )
    if not messages:
        return None

    message = messages[0]

    # An exception leaves the message pending on purpose: claim_stale_tasks
    # hands it out again, bounded by REDIS_MAX_DELIVERIES.
    result = backend.run_task(message.task_id, worker_id=worker_id)

    broker.ack(message)
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
        worker_id = generate_worker_id()

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
        worker_id = generate_worker_id()

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

    Wraps ``backend.broker.claim_stale_messages()``: messages other consumers
    have held for longer than the claim timeout are reassigned to `worker_id`,
    and a task their dead consumer left RUNNING is handed back as READY.

    Args:
        backend_name: Backend name (default: "default").
        claim_timeout: Timeout in seconds. If None, uses backend setting. It
            must be longer than the longest task the workers run, otherwise a
            task that is still running is reclaimed and executed twice.
        worker_id: Consumer id to claim the messages for. It must be the id a
            worker actually receives with, or the task is stranded. If None,
            one is generated, which only makes sense when nothing will consume
            them.
        max_deliveries: Give up on a task that was started this many times
            without finishing, and mark it FAILED. If None, uses the backend
            setting; 0 disables the cap.

    Returns:
        Number of tasks claimed.
    """
    if worker_id is None:
        worker_id = generate_worker_id()

    backend = task_backends[backend_name]
    return backend.broker.claim_stale_messages(
        worker_id, claim_timeout=claim_timeout, max_deliveries=max_deliveries
    )


def _process_delayed_tasks(backend, queue_name=None):
    """Move delayed tasks to streams if their time has come."""
    backend.broker.promote_delayed_tasks(queue_name)


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

    Raises:
        ValueError: If days is negative.
    """
    if days < 0:
        # A negative age puts the cutoff in the future, which matches every
        # completed task: a typo would wipe the whole history.
        raise ValueError(f"days must not be negative, got {days}")

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

    delayed_count = 0
    for qname in backend.broker.queue_names(queue_name):
        delayed_count += client.zcard(backend.broker.delayed_key(qname))

    return {
        "pending_count": counts.get(TaskResultStatus.READY, 0),
        "running_count": counts.get(TaskResultStatus.RUNNING, 0),
        "successful_count": counts.get(TaskResultStatus.SUCCESSFUL, 0),
        "failed_count": counts.get(TaskResultStatus.FAILED, 0),
        "delayed_count": delayed_count,
    }

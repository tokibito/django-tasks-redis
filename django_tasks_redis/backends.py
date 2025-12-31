"""
Redis/Valkey task backend implementation.
"""

import asyncio
import logging
import traceback
import uuid
from importlib import import_module
from inspect import iscoroutinefunction

from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import Task, TaskContext, TaskError, TaskResult, TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist
from django.tasks.signals import task_enqueued, task_finished, task_started
from django.utils import timezone
from django.utils.json import normalize_json

from .utils import (
    deserialize_datetime,
    deserialize_json,
    get_delayed_key,
    get_priority_stream_key,
    get_redis_client,
    get_result_key,
    get_results_index_key,
    priority_to_level,
    serialize_datetime,
    serialize_json,
)

logger = logging.getLogger("django_tasks_redis")


class RedisTaskBackend(BaseTaskBackend):
    """A task backend that uses Redis/Valkey for task queuing and storage."""

    supports_defer = True
    supports_async_task = True
    supports_get_result = True
    supports_priority = True

    def __init__(self, alias, params):
        super().__init__(alias, params)
        self._client = None

        # Settings with REDIS_ prefix
        self.result_ttl = self.options.get("REDIS_RESULT_TTL", 2592000)  # 30 days
        # TTL for completed tasks (SUCCESSFUL/FAILED), defaults to result_ttl
        self.completed_task_ttl = self.options.get(
            "REDIS_COMPLETED_TASK_TTL", self.result_ttl
        )
        self.key_prefix = self.options.get("REDIS_KEY_PREFIX", "django_tasks")
        self.consumer_group = self.options.get(
            "REDIS_CONSUMER_GROUP", "django_tasks_workers"
        )
        self.claim_timeout = self.options.get("REDIS_CLAIM_TIMEOUT", 300)
        self.block_timeout = self.options.get("REDIS_BLOCK_TIMEOUT", 5000)

    def get_client(self):
        """Get or create Redis client."""
        if self._client is None:
            self._client = get_redis_client(self.options)
        return self._client

    def get_auth_handler(self):
        """
        Get the authentication handler for task execution endpoints.

        Subclasses can override this to provide custom authentication.
        The handler should be a callable that takes a request and returns:
        - None if authentication succeeds
        - A JsonResponse with error details if authentication fails

        Returns:
            Callable or None
        """
        return None

    def enqueue(self, task, args, kwargs):
        """
        Enqueue a task to Redis.

        Args and kwargs must be JSON-serializable.
        """
        self.validate_task(task)

        # Normalize args and kwargs to ensure JSON serialization
        normalized_args = normalize_json(list(args))
        normalized_kwargs = normalize_json(dict(kwargs))

        task_id = str(uuid.uuid4())
        now = timezone.now()

        # Prepare task data for Redis Hash
        task_data = {
            "task_id": task_id,
            "task_path": self._get_task_path(task),
            "args_json": serialize_json(normalized_args),
            "kwargs_json": serialize_json(normalized_kwargs),
            "status": TaskResultStatus.READY,
            "priority": str(task.priority),
            "queue_name": task.queue_name,
            "backend_name": self.alias,
            "run_after": serialize_datetime(task.run_after),
            "takes_context": "true" if task.takes_context else "false",
            "enqueued_at": serialize_datetime(now),
            "started_at": "",
            "finished_at": "",
            "last_attempted_at": "",
            "return_value_json": "",
            "errors_json": serialize_json([]),
            "worker_ids_json": serialize_json([]),
        }

        client = self.get_client()
        result_key = get_result_key(self.key_prefix, self.alias, task_id)
        results_index_key = get_results_index_key(self.key_prefix, self.alias)

        # Store task result in Hash
        client.hset(result_key, mapping=task_data)
        if self.result_ttl > 0:
            client.expire(result_key, self.result_ttl)

        # Add to results index for iteration
        client.sadd(results_index_key, task_id)

        # Prepare stream entry data (subset for queue)
        stream_data = {
            "task_id": task_id,
            "task_path": task_data["task_path"],
            "priority": task_data["priority"],
            "queue_name": task.queue_name,
            "enqueued_at": task_data["enqueued_at"],
        }

        # Add to stream or delayed set based on run_after
        if task.run_after is not None and task.run_after > now:
            # Add to delayed sorted set
            delayed_key = get_delayed_key(self.key_prefix, self.alias, task.queue_name)
            client.zadd(delayed_key, {task_id: task.run_after.timestamp()})
        else:
            # Add to priority-based stream
            priority_level = priority_to_level(task.priority)
            stream_key = get_priority_stream_key(
                self.key_prefix, self.alias, task.queue_name, priority_level
            )
            self._ensure_consumer_group(client, stream_key)
            client.xadd(stream_key, stream_data)

        task_result = self._data_to_result(task_data, task)
        task_enqueued.send(sender=self.__class__, task_result=task_result)

        return task_result

    def get_result(self, result_id):
        """Retrieve a task result from Redis."""
        client = self.get_client()
        result_key = get_result_key(self.key_prefix, self.alias, result_id)

        task_data = client.hgetall(result_key)
        if not task_data:
            raise TaskResultDoesNotExist(result_id)

        task = self._resolve_task(task_data["task_path"])
        return self._data_to_result(task_data, task)

    def _get_task_path(self, task):
        """Get the module path of the task function."""
        func = task.func
        return f"{func.__module__}.{func.__qualname__}"

    def _resolve_task(self, task_path):
        """Resolve a Task object from its module path."""
        module_path, func_name = task_path.rsplit(".", 1)
        module = import_module(module_path)
        func = getattr(module, func_name)
        if isinstance(func, Task):
            return func
        return func

    def _data_to_result(self, task_data, task):
        """Convert Redis Hash data to a TaskResult."""
        errors_data = deserialize_json(task_data.get("errors_json", "[]")) or []
        errors = [
            TaskError(
                exception_class_path=e.get("exception_class_path", ""),
                traceback=e.get("traceback", ""),
            )
            for e in errors_data
        ]

        worker_ids = deserialize_json(task_data.get("worker_ids_json", "[]")) or []

        result = TaskResult(
            task=task if isinstance(task, Task) else task,
            id=task_data["task_id"],
            status=TaskResultStatus(task_data["status"]),
            enqueued_at=deserialize_datetime(task_data.get("enqueued_at", "")),
            started_at=deserialize_datetime(task_data.get("started_at", "")),
            finished_at=deserialize_datetime(task_data.get("finished_at", "")),
            last_attempted_at=deserialize_datetime(
                task_data.get("last_attempted_at", "")
            ),
            args=deserialize_json(task_data.get("args_json", "[]")) or [],
            kwargs=deserialize_json(task_data.get("kwargs_json", "{}")) or {},
            backend=task_data.get("backend_name", self.alias),
            errors=errors,
            worker_ids=worker_ids,
        )

        return_value_json = task_data.get("return_value_json", "")
        if return_value_json:
            return_value = deserialize_json(return_value_json)
            object.__setattr__(result, "_return_value", return_value)

        return result

    def _ensure_consumer_group(self, client, stream_key):
        """Ensure consumer group exists for the stream."""
        try:
            client.xgroup_create(stream_key, self.consumer_group, id="0", mkstream=True)
        except Exception as e:
            # Group already exists - this is fine
            if "BUSYGROUP" not in str(e):
                raise

    def run_task(self, task_id, worker_id=None):
        """
        Execute a task by ID (called from executor/management command).

        Args:
            task_id: Task ID string.
            worker_id: Optional worker identifier.

        Returns:
            TaskResult after execution.
        """
        client = self.get_client()
        result_key = get_result_key(self.key_prefix, self.alias, task_id)

        task_data = client.hgetall(result_key)
        if not task_data:
            raise TaskResultDoesNotExist(task_id)

        now = timezone.now()

        # Update status to RUNNING
        worker_ids = deserialize_json(task_data.get("worker_ids_json", "[]")) or []
        if worker_id:
            worker_ids.append(worker_id)

        started_at = task_data.get("started_at", "")
        if not started_at:
            started_at = serialize_datetime(now)

        client.hset(
            result_key,
            mapping={
                "status": TaskResultStatus.RUNNING,
                "started_at": started_at,
                "last_attempted_at": serialize_datetime(now),
                "worker_ids_json": serialize_json(worker_ids),
            },
        )

        task = self._resolve_task(task_data["task_path"])
        task_data["status"] = TaskResultStatus.RUNNING
        task_data["started_at"] = started_at
        task_data["last_attempted_at"] = serialize_datetime(now)
        task_data["worker_ids_json"] = serialize_json(worker_ids)

        task_result = self._data_to_result(task_data, task)
        task_started.send(sender=self.__class__, task_result=task_result)

        try:
            # Get task function
            if isinstance(task, Task):
                func = task.func
                takes_context = task.takes_context
            else:
                func = task
                takes_context = task_data.get("takes_context", "false") == "true"

            # Prepare arguments
            args = deserialize_json(task_data.get("args_json", "[]")) or []
            kwargs = deserialize_json(task_data.get("kwargs_json", "{}")) or {}

            # Execute task
            if takes_context:
                context = TaskContext(task_result=task_result)
                if iscoroutinefunction(func):
                    return_value = asyncio.run(func(context, *args, **kwargs))
                else:
                    return_value = func(context, *args, **kwargs)
            else:
                if iscoroutinefunction(func):
                    return_value = asyncio.run(func(*args, **kwargs))
                else:
                    return_value = func(*args, **kwargs)

            # Normalize return value for JSON serialization
            normalized_return_value = normalize_json(return_value)

            # Success
            finished_at = serialize_datetime(timezone.now())
            client.hset(
                result_key,
                mapping={
                    "status": TaskResultStatus.SUCCESSFUL,
                    "return_value_json": serialize_json(normalized_return_value),
                    "finished_at": finished_at,
                },
            )

            # Set TTL for completed task
            if self.completed_task_ttl > 0:
                client.expire(result_key, self.completed_task_ttl)

            # Refresh and return result
            task_data = client.hgetall(result_key)
            final_result = self._data_to_result(task_data, task)
            logger.info(
                "Task completed successfully: id=%s path=%s",
                final_result.id,
                task_data["task_path"],
            )
            task_finished.send(sender=self.__class__, task_result=final_result)
            return final_result

        except Exception as e:
            # Failure
            error = TaskError(
                exception_class_path=f"{type(e).__module__}.{type(e).__qualname__}",
                traceback=traceback.format_exc(),
            )
            errors = deserialize_json(task_data.get("errors_json", "[]")) or []
            errors.append(
                {
                    "exception_class_path": error.exception_class_path,
                    "traceback": error.traceback,
                }
            )

            finished_at = serialize_datetime(timezone.now())
            client.hset(
                result_key,
                mapping={
                    "status": TaskResultStatus.FAILED,
                    "errors_json": serialize_json(errors),
                    "finished_at": finished_at,
                },
            )

            # Set TTL for completed task
            if self.completed_task_ttl > 0:
                client.expire(result_key, self.completed_task_ttl)

            # Refresh and return result
            task_data = client.hgetall(result_key)
            final_result = self._data_to_result(task_data, task)
            logger.error(
                "Task failed: id=%s path=%s error=%s",
                final_result.id,
                task_data["task_path"],
                error.exception_class_path,
            )
            task_finished.send(sender=self.__class__, task_result=final_result)
            return final_result

    def get_all_tasks(
        self,
        queue_name=None,
        status=None,
        offset=0,
        limit=100,
    ):
        """
        Get all tasks from Redis.

        Args:
            queue_name: Optional queue name filter.
            status: Optional status filter.
            offset: Starting offset.
            limit: Maximum number of results.

        Returns:
            Tuple of (list of task dicts, total count).
        """
        client = self.get_client()
        results_index_key = get_results_index_key(self.key_prefix, self.alias)

        # Get all task IDs from index
        task_ids = client.smembers(results_index_key)

        # Fetch all task data and filter
        tasks = []
        for task_id in task_ids:
            result_key = get_result_key(self.key_prefix, self.alias, task_id)
            task_data = client.hgetall(result_key)

            if not task_data:
                # Task expired, remove from index
                client.srem(results_index_key, task_id)
                continue

            # Apply filters
            if queue_name and task_data.get("queue_name") != queue_name:
                continue
            if status and task_data.get("status") != status:
                continue

            tasks.append(task_data)

        # Sort by enqueued_at descending
        tasks.sort(key=lambda x: x.get("enqueued_at", ""), reverse=True)

        total = len(tasks)

        # Apply pagination
        tasks = tasks[offset : offset + limit]

        return tasks, total

    def get_task_data(self, task_id):
        """
        Get raw task data from Redis.

        Args:
            task_id: Task ID string.

        Returns:
            Task data dict or None.
        """
        client = self.get_client()
        result_key = get_result_key(self.key_prefix, self.alias, task_id)
        return client.hgetall(result_key) or None

    def delete_task_data(self, task_id):
        """
        Delete a task from Redis.

        Args:
            task_id: Task ID string.

        Returns:
            True if deleted, False if not found.
        """
        client = self.get_client()
        result_key = get_result_key(self.key_prefix, self.alias, task_id)
        results_index_key = get_results_index_key(self.key_prefix, self.alias)

        deleted = client.delete(result_key)
        client.srem(results_index_key, task_id)

        return deleted > 0

    def reset_task_status(self, task_id):
        """
        Reset a task's status to READY.

        Args:
            task_id: Task ID string.

        Returns:
            True if reset, False if not found.
        """
        client = self.get_client()
        result_key = get_result_key(self.key_prefix, self.alias, task_id)

        task_data = client.hgetall(result_key)
        if not task_data:
            return False

        client.hset(
            result_key,
            mapping={
                "status": TaskResultStatus.READY,
                "finished_at": "",
                "errors_json": serialize_json([]),
            },
        )

        # Re-add to stream for processing
        queue_name = task_data.get("queue_name", "default")
        priority = int(task_data.get("priority", "0"))
        priority_level = priority_to_level(priority)
        stream_key = get_priority_stream_key(
            self.key_prefix, self.alias, queue_name, priority_level
        )

        stream_data = {
            "task_id": task_id,
            "task_path": task_data["task_path"],
            "priority": task_data["priority"],
            "queue_name": queue_name,
            "enqueued_at": task_data.get("enqueued_at", ""),
        }

        self._ensure_consumer_group(client, stream_key)
        client.xadd(stream_key, stream_data)

        return True

    def get_status_counts(self, queue_name=None):
        """
        Get task counts by status.

        Args:
            queue_name: Optional queue name filter.

        Returns:
            Dict mapping status to count.
        """
        client = self.get_client()
        results_index_key = get_results_index_key(self.key_prefix, self.alias)

        task_ids = client.smembers(results_index_key)

        counts = {
            TaskResultStatus.READY: 0,
            TaskResultStatus.RUNNING: 0,
            TaskResultStatus.SUCCESSFUL: 0,
            TaskResultStatus.FAILED: 0,
        }

        for task_id in task_ids:
            result_key = get_result_key(self.key_prefix, self.alias, task_id)
            task_data = client.hgetall(result_key)

            if not task_data:
                client.srem(results_index_key, task_id)
                continue

            if queue_name and task_data.get("queue_name") != queue_name:
                continue

            status = task_data.get("status")
            if status in counts:
                counts[status] += 1

        return counts

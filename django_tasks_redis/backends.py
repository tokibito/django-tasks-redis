"""
Redis/Valkey task backend implementation.
"""

import asyncio
import logging
import time
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
    get_queue_status_index_key,
    get_redis_client,
    get_result_key,
    get_results_index_key,
    get_status_index_key,
    get_status_index_synced_key,
    priority_to_level,
    serialize_datetime,
    serialize_json,
)

logger = logging.getLogger("django_tasks_redis")

# All statuses tracked in the per-status indexes.
STATUS_INDEX_STATUSES = (
    TaskResultStatus.READY,
    TaskResultStatus.RUNNING,
    TaskResultStatus.SUCCESSFUL,
    TaskResultStatus.FAILED,
)


class RedisTaskBackend(BaseTaskBackend):
    """A task backend that uses Redis/Valkey for task queuing and storage."""

    supports_defer = True
    supports_async_task = True
    supports_get_result = True
    supports_priority = True

    def __init__(self, alias, params):
        super().__init__(alias, params)
        self._client = None
        self._metrics_collector = None
        self._metrics_handler = None
        self._status_index_synced = False
        self._stream_trim_at = {}

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
        # Approximate stream length target. When a stream exceeds this,
        # delivered-and-acknowledged history is trimmed (XTRIM MINID at the
        # consumer group's safe position); undelivered and pending entries
        # are never trimmed, so the stream can exceed the target while a
        # genuine backlog exists. None disables trimming.
        self.stream_maxlen = self.options.get("REDIS_STREAM_MAXLEN", 10000)
        # How often (seconds, per process and stream) to check whether a
        # stream needs trimming. 0 checks on every XADD.
        self.stream_trim_interval = self.options.get("REDIS_STREAM_TRIM_INTERVAL", 60)
        # Consumers idle longer than this (seconds) with no pending entries
        # are removed from the consumer group. None or 0 disables reaping.
        self.consumer_idle_timeout = self.options.get(
            "REDIS_CONSUMER_IDLE_TIMEOUT", 86400
        )

        # Initialize Prometheus metrics if enabled
        self._init_metrics()

    def get_client(self):
        """Get or create Redis client."""
        if self._client is None:
            self._client = get_redis_client(self.options)
        return self._client

    def _init_metrics(self):
        """Initialize Prometheus metrics if enabled and available."""
        enable_metrics = self.options.get("ENABLE_METRICS", False)

        if not enable_metrics:
            return

        try:
            from .metrics import (
                PROMETHEUS_AVAILABLE,
                REGISTRY,
                RedisTaskMetricsCollector,
            )

            if not PROMETHEUS_AVAILABLE:
                logger.warning(
                    "ENABLE_METRICS is True but prometheus-client not installed. "
                    "Install with: pip install django-tasks-redis[prometheus]"
                )
                return

            # Register the custom collector with Prometheus
            # It will query Redis directly when scraped
            collector = RedisTaskMetricsCollector(self)

            # Try to register, but catch duplicate registration errors
            # This can happen in tests or when multiple backends are initialized
            try:
                REGISTRY.register(collector)
                logger.info(
                    "Prometheus metrics collector registered for backend: %s",
                    self.alias,
                )
            except ValueError as e:
                if "Duplicated timeseries" in str(e):
                    logger.debug(
                        "Prometheus metrics collector already registered (duplicate ignored): %s",
                        self.alias,
                    )
                else:
                    raise

        except ImportError as e:
            logger.warning(
                "Failed to initialize metrics (prometheus-client not installed): %s", e
            )

    def _record_task_duration(self, started_at_str, finished_at_dt, status, task_data):
        """
        Record task duration to Prometheus histogram.

        Args:
            started_at_str: Serialized datetime string when task started
            finished_at_dt: datetime object when task finished
            status: Final task status (SUCCESSFUL or FAILED)
            task_data: Dict containing task metadata including queue_name
        """
        enable_metrics = self.options.get("ENABLE_METRICS", False)
        if not enable_metrics:
            return

        try:
            from .metrics import get_task_duration_histogram

            histogram = get_task_duration_histogram()
            if histogram is None:
                return

            # Parse started_at to calculate duration
            started_at_dt = deserialize_datetime(started_at_str)
            if not started_at_dt:
                logger.warning("Cannot record task duration: started_at not set")
                return

            # Ensure both datetimes are timezone-aware
            if started_at_dt.tzinfo is None:
                started_at_dt = started_at_dt.replace(tzinfo=timezone.utc)
            if finished_at_dt.tzinfo is None:
                finished_at_dt = finished_at_dt.replace(tzinfo=timezone.utc)

            # Calculate duration in seconds
            duration = (finished_at_dt - started_at_dt).total_seconds()

            # Get queue name from task data
            queue_name = task_data.get("queue_name", "default")

            # Record the duration with labels
            histogram.labels(
                backend=self.alias,
                queue=queue_name,
                status=status,
            ).observe(duration)

            logger.debug(
                "Recorded task duration: backend=%s queue=%s status=%s duration=%.3fs",
                self.alias,
                queue_name,
                status,
                duration,
            )

        except Exception as e:
            # Don't let metrics recording errors break task execution
            logger.warning("Failed to record task duration metric: %s", e)

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

        # Store the hash, indexes, and per-status entry in one round trip.
        pipe = client.pipeline(transaction=False)
        pipe.hset(result_key, mapping=task_data)
        if self.result_ttl > 0:
            pipe.expire(result_key, self.result_ttl)
        pipe.sadd(results_index_key, task_id)
        # Per-status index entry, scored by enqueue time
        self._queue_status_index_update(
            pipe,
            task_id,
            task.queue_name,
            TaskResultStatus.READY,
            score=now.timestamp(),
        )
        pipe.execute()

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
            self.xadd_task(client, stream_key, stream_data)

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

    def xadd_task(self, client, stream_key, stream_data):
        """
        Add a task entry to a stream and opportunistically trim it.

        A plain XADD MAXLEN would trim the oldest *entries*, which under a
        backlog larger than the target are undelivered tasks — silently
        losing them. Instead, when the stream exceeds REDIS_STREAM_MAXLEN,
        it is trimmed with XTRIM MINID at the consumer group's safe
        position (min pending entry, or last-delivered when nothing is
        pending), so only delivered-and-acknowledged history is removed.
        """
        client.xadd(stream_key, stream_data)
        self._maybe_trim_stream(client, stream_key)

    def _maybe_trim_stream(self, client, stream_key):
        """Trim acked stream history, rate-limited per process and stream."""
        if not self.stream_maxlen:
            return

        now = time.monotonic()
        last = self._stream_trim_at.get(stream_key)
        if (
            last is not None
            and self.stream_trim_interval
            and now - last < self.stream_trim_interval
        ):
            return
        self._stream_trim_at[stream_key] = now

        try:
            if client.xlen(stream_key) <= self.stream_maxlen:
                return
            minid = self._stream_safe_trim_id(client, stream_key)
            if minid and minid != "0-0":
                client.xtrim(stream_key, minid=minid, approximate=True)
        except Exception as e:
            # Never let housekeeping break enqueueing.
            logger.warning("Failed to trim stream %s: %s", stream_key, e)

    def _stream_safe_trim_id(self, client, stream_key):
        """
        The oldest stream ID that must be kept for correctness.

        Everything strictly below the returned ID has been delivered to and
        acknowledged by every consumer group, so trimming it cannot lose a
        task. Returns None when there is no group (nothing has consumed the
        stream yet, so nothing is provably safe to drop).
        """

        def _id_tuple(stream_id):
            ms, _, seq = str(stream_id).partition("-")
            return (int(ms), int(seq or 0))

        groups = client.xinfo_groups(stream_key)
        if not groups:
            return None

        safe = None
        for group in groups:
            if group.get("pending", 0) > 0:
                summary = client.xpending(stream_key, group["name"])
                group_floor = summary.get("min") or group.get("last-delivered-id")
            else:
                group_floor = group.get("last-delivered-id")
            if group_floor is None:
                return None
            if safe is None or _id_tuple(group_floor) < _id_tuple(safe):
                safe = group_floor
        return safe

    # --- Per-status task index -------------------------------------------
    #
    # Sorted sets (one global and one per queue, per status) hold task ids
    # scored by the time the task entered the status. They make status
    # counts and READY-age metrics O(1) instead of one HGETALL per stored
    # result. Entries whose backing hash has expired are pruned by score.

    def _status_index_keys(self, status, queue_name=None):
        """Return the global and (optionally) queue-scoped index keys."""
        keys = [get_status_index_key(self.key_prefix, self.alias, status)]
        if queue_name:
            keys.append(
                get_queue_status_index_key(
                    self.key_prefix, self.alias, queue_name, status
                )
            )
        return keys

    def _status_index_ttl(self, status):
        """TTL that applies to result keys currently in the given status."""
        if status in (TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED):
            return self.completed_task_ttl
        return self.result_ttl

    def _queue_status_index_update(
        self, pipe, task_id, queue_name, new_status, score=None
    ):
        """
        Queue index commands for a status transition on a pipeline.

        The task is removed from EVERY status set except the new one (not
        just the expected previous status): concurrent writers — e.g. a
        stale-claim requeue racing the worker that is finishing the task —
        can otherwise strand the task in a set nobody clears until TTL
        pruning. With a full sweep, the last writer always leaves the
        indexes consistent with its own hash write.
        """
        for status in STATUS_INDEX_STATUSES:
            if status == new_status:
                continue
            for key in self._status_index_keys(status, queue_name):
                pipe.zrem(key, task_id)
        if new_status:
            if score is None:
                score = timezone.now().timestamp()
            for key in self._status_index_keys(new_status, queue_name):
                pipe.zadd(key, {task_id: score})

    def update_status_index(
        self, client, task_id, queue_name, old_status, new_status, score=None
    ):
        """
        Record a task status transition in the status indexes.

        Args:
            client: Redis client.
            task_id: Task ID string.
            queue_name: Queue name of the task.
            old_status: Previous status (kept for API compatibility; the
                task is swept from all non-new status sets regardless).
            new_status: New status, or None on deletion.
            score: Timestamp for the new entry (defaults to now).
        """
        pipe = client.pipeline(transaction=False)
        self._queue_status_index_update(pipe, task_id, queue_name, new_status, score)
        pipe.execute()

    def remove_from_status_index(self, client, task_id, queue_name=None):
        """Remove a task from every status index (used on deletion)."""
        pipe = client.pipeline(transaction=False)
        self._queue_status_index_update(pipe, task_id, queue_name, None)
        pipe.execute()

    def _prune_status_index(self, pipe, status, queue_name=None):
        """Queue pruning of entries whose backing hash must have expired."""
        ttl = self._status_index_ttl(status)
        if ttl and ttl > 0:
            cutoff = timezone.now().timestamp() - ttl
            for key in self._status_index_keys(status, queue_name):
                pipe.zremrangebyscore(key, "-inf", cutoff)

    def has_status_index(self):
        """
        Whether the fast status index has been built for this backend.

        A positive answer is cached in memory: the marker is only ever set,
        never removed (deleting it by hand requires a process restart to be
        picked up).
        """
        if self._status_index_synced:
            return True
        client = self.get_client()
        synced_key = get_status_index_synced_key(self.key_prefix, self.alias)
        if client.exists(synced_key):
            self._status_index_synced = True
            return True
        # A deployment with no stored results has nothing to migrate; mark
        # the index as synced so it is fast from the very first task.
        results_index_key = get_results_index_key(self.key_prefix, self.alias)
        if not client.exists(results_index_key):
            client.set(synced_key, "1")
            self._status_index_synced = True
            return True
        return False

    def ensure_status_index(self):
        """Build the status index if it has not been built yet."""
        if not self.has_status_index():
            self.rebuild_status_index()

    def rebuild_status_index(self, batch_size=500):
        """
        Rebuild the per-status indexes from the stored result hashes.

        Scans the results index once (batched HMGETs, not one round trip
        per key) and repopulates the sorted sets. Transitions maintain the
        indexes unconditionally, so running this alongside active workers
        is supported; a task transitioning mid-scan can transiently be
        re-added under its old status, which its next transition sweeps
        away (terminal-state stragglers age out via TTL pruning). Rebuild
        during a quiet period for an exact result.
        """
        client = self.get_client()
        results_index_key = get_results_index_key(self.key_prefix, self.alias)
        synced_key = get_status_index_synced_key(self.key_prefix, self.alias)

        # Drop existing index keys (global and queue-scoped).
        stale_keys = list(
            client.scan_iter(
                match=f"{self.key_prefix}:{self.alias}:status_index:*", count=1000
            )
        )
        stale_keys = [k for k in stale_keys if k != synced_key]
        if stale_keys:
            client.delete(*stale_keys)

        now_ts = timezone.now().timestamp()
        indexed = 0

        batch = []
        for task_id in client.sscan_iter(results_index_key, count=batch_size):
            batch.append(task_id)
            if len(batch) >= batch_size:
                indexed += self._rebuild_status_index_batch(client, batch, now_ts)
                batch = []
        if batch:
            indexed += self._rebuild_status_index_batch(client, batch, now_ts)

        client.set(synced_key, "1")
        self._status_index_synced = True
        logger.info(
            "Rebuilt status index for backend %s: %d task(s)", self.alias, indexed
        )
        return indexed

    def _rebuild_status_index_batch(self, client, task_ids, now_ts):
        """Index one batch of task ids; returns how many were indexed."""
        pipe = client.pipeline(transaction=False)
        for task_id in task_ids:
            result_key = get_result_key(self.key_prefix, self.alias, task_id)
            pipe.hmget(result_key, "status", "queue_name", "enqueued_at", "finished_at")
        rows = pipe.execute()

        zadds = {}  # key -> {task_id: score}
        stale_ids = []
        indexed = 0
        for task_id, (status, queue_name, enqueued_at, finished_at) in zip(
            task_ids, rows, strict=True
        ):
            if not status:
                # Hash expired; drop the dangling results_index entry too.
                stale_ids.append(task_id)
                continue
            if status not in STATUS_INDEX_STATUSES:
                continue
            if status == TaskResultStatus.READY:
                entered = deserialize_datetime(enqueued_at or "")
            elif status in (TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED):
                entered = deserialize_datetime(finished_at or "")
            else:
                entered = None
            score = entered.timestamp() if entered else now_ts
            for key in self._status_index_keys(status, queue_name or None):
                zadds.setdefault(key, {})[task_id] = score
            indexed += 1

        pipe = client.pipeline(transaction=False)
        for key, mapping in zadds.items():
            pipe.zadd(key, mapping)
        if stale_ids:
            results_index_key = get_results_index_key(self.key_prefix, self.alias)
            pipe.srem(results_index_key, *stale_ids)
        pipe.execute()
        return indexed

    def get_ready_age_bounds(self):
        """
        Get (oldest_age_seconds, newest_age_seconds) for READY tasks.

        Uses the READY status index (scored by enqueued_at), so the cost is
        independent of how many results are stored. Returns None when there
        are no READY tasks or the index has not been built yet.
        """
        if not self.has_status_index():
            return None

        client = self.get_client()
        ready_key = get_status_index_key(
            self.key_prefix, self.alias, TaskResultStatus.READY
        )
        pipe = client.pipeline(transaction=False)
        self._prune_status_index(pipe, TaskResultStatus.READY)
        pipe.zrange(ready_key, 0, 0, withscores=True)
        pipe.zrange(ready_key, -1, -1, withscores=True)
        results = pipe.execute()
        oldest, newest = results[-2], results[-1]
        if not oldest or not newest:
            return None

        now_ts = timezone.now().timestamp()
        oldest_age = max(now_ts - oldest[0][1], 0.0)
        newest_age = max(now_ts - newest[0][1], 0.0)
        return oldest_age, newest_age

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

        queue_name = task_data.get("queue_name") or None
        pipe = client.pipeline(transaction=False)
        pipe.hset(
            result_key,
            mapping={
                "status": TaskResultStatus.RUNNING,
                "started_at": started_at,
                "last_attempted_at": serialize_datetime(now),
                "worker_ids_json": serialize_json(worker_ids),
            },
        )
        self._queue_status_index_update(
            pipe,
            task_id,
            queue_name,
            TaskResultStatus.RUNNING,
            score=now.timestamp(),
        )
        pipe.execute()

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
            finished_at_dt = timezone.now()
            finished_at = serialize_datetime(finished_at_dt)
            pipe = client.pipeline(transaction=False)
            pipe.hset(
                result_key,
                mapping={
                    "status": TaskResultStatus.SUCCESSFUL,
                    "return_value_json": serialize_json(normalized_return_value),
                    "finished_at": finished_at,
                },
            )
            self._queue_status_index_update(
                pipe,
                task_id,
                queue_name,
                TaskResultStatus.SUCCESSFUL,
                score=finished_at_dt.timestamp(),
            )
            # Set TTL for completed task
            if self.completed_task_ttl > 0:
                pipe.expire(result_key, self.completed_task_ttl)
            pipe.execute()

            # Record task duration metric
            self._record_task_duration(
                started_at, finished_at_dt, TaskResultStatus.SUCCESSFUL, task_data
            )

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

            finished_at_dt = timezone.now()
            finished_at = serialize_datetime(finished_at_dt)
            pipe = client.pipeline(transaction=False)
            pipe.hset(
                result_key,
                mapping={
                    "status": TaskResultStatus.FAILED,
                    "errors_json": serialize_json(errors),
                    "finished_at": finished_at,
                },
            )
            self._queue_status_index_update(
                pipe,
                task_id,
                queue_name,
                TaskResultStatus.FAILED,
                score=finished_at_dt.timestamp(),
            )
            # Set TTL for completed task
            if self.completed_task_ttl > 0:
                pipe.expire(result_key, self.completed_task_ttl)
            pipe.execute()

            # Record task duration metric
            self._record_task_duration(
                started_at, finished_at_dt, TaskResultStatus.FAILED, task_data
            )

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
        task_path=None,
        priority=None,
        offset=0,
        limit=100,
    ):
        """
        Get all tasks from Redis.

        Args:
            queue_name: Optional queue name filter.
            status: Optional status filter.
            task_path: Optional task path filter.
            priority: Optional priority filter (as string).
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
            if task_path and task_data.get("task_path") != task_path:
                continue
            if priority and task_data.get("priority") != str(priority):
                continue

            tasks.append(task_data)

        # Sort by enqueued_at descending
        tasks.sort(key=lambda x: x.get("enqueued_at", ""), reverse=True)

        total = len(tasks)

        # Apply pagination
        tasks = tasks[offset : offset + limit]

        return tasks, total

    def get_distinct_field_values(self, field_name):
        """
        Get distinct values for a given field across all tasks.

        Args:
            field_name: The task field to collect distinct values for.

        Returns:
            Sorted list of distinct values.
        """
        client = self.get_client()
        results_index_key = get_results_index_key(self.key_prefix, self.alias)
        task_ids = client.smembers(results_index_key)

        values = set()
        for task_id in task_ids:
            result_key = get_result_key(self.key_prefix, self.alias, task_id)
            value = client.hget(result_key, field_name)
            if value:
                values.add(value)

        return sorted(values)

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

        queue_name = client.hget(result_key, "queue_name")
        deleted = client.delete(result_key)
        client.srem(results_index_key, task_id)
        self.remove_from_status_index(client, task_id, queue_name or None)

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

        queue_name = task_data.get("queue_name", "default")
        enqueued_at = deserialize_datetime(task_data.get("enqueued_at", ""))

        pipe = client.pipeline(transaction=False)
        pipe.hset(
            result_key,
            mapping={
                "status": TaskResultStatus.READY,
                "finished_at": "",
                "errors_json": serialize_json([]),
            },
        )
        self._queue_status_index_update(
            pipe,
            task_id,
            queue_name,
            TaskResultStatus.READY,
            score=enqueued_at.timestamp() if enqueued_at else None,
        )
        pipe.execute()

        # Re-add to stream for processing
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
        self.xadd_task(client, stream_key, stream_data)

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

        if self.has_status_index():
            # Fast path: prune expired entries by score, then ZCARD each
            # status set. Bounded number of commands regardless of how many
            # results are stored.
            pipe = client.pipeline(transaction=False)
            zcard_positions = []
            queued = 0
            for status in STATUS_INDEX_STATUSES:
                ttl = self._status_index_ttl(status)
                if ttl and ttl > 0:
                    self._prune_status_index(pipe, status, queue_name)
                    queued += 2 if queue_name else 1
                # ZCARD the queue-scoped key when filtering, global otherwise.
                pipe.zcard(self._status_index_keys(status, queue_name)[-1])
                zcard_positions.append(queued)
                queued += 1
            results = pipe.execute()
            return {
                status: results[pos]
                for status, pos in zip(
                    STATUS_INDEX_STATUSES, zcard_positions, strict=True
                )
            }

        # Legacy path (index not built yet): scan every stored result.
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

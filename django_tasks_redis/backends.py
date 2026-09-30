"""
Redis/Valkey task backend implementation.
"""

import asyncio
import logging
import time
import traceback
import uuid
from functools import cached_property
from importlib import import_module
from inspect import iscoroutinefunction

from django.tasks.backends.base import BaseTaskBackend
from django.tasks.base import Task, TaskContext, TaskError, TaskResult, TaskResultStatus
from django.tasks.exceptions import TaskResultDoesNotExist
from django.tasks.signals import task_enqueued, task_finished, task_started
from django.utils import timezone
from django.utils.json import normalize_json

from .brokers import RedisStreamsBroker
from .exceptions import TaskAbandoned
from .utils import (
    deserialize_datetime,
    deserialize_json,
    deserialize_timestamp,
    get_delayed_key,
    get_redis_client,
    get_result_key,
    get_results_index_key,
    get_status_index_built_key,
    get_status_index_key,
    priority_to_level,
    serialize_datetime,
    serialize_json,
)

logger = logging.getLogger("django_tasks_redis")

#: The statuses the status index tracks, in the order the scripts below take
#: their sets: KEYS[2..5] are the backend-wide sets, KEYS[6..9] the sets of the
#: task's queue.
STATUS_INDEX_ORDER = (
    TaskResultStatus.READY,
    TaskResultStatus.RUNNING,
    TaskResultStatus.SUCCESSFUL,
    TaskResultStatus.FAILED,
)


def task_log_fields(task_data, worker_id=None, **extra):
    """
    Build the ``extra`` mapping attached to a task's log records.

    These are the fields an operator filters on once the records go through
    a structured (JSON) formatter, so they are kept flat and named apart
    from LogRecord's own attributes.

    Args:
        task_data: The Redis hash dict for the task, or anything that maps
            ``task_id`` / ``task_path`` / ``queue_name`` / ``priority`` /
            ``backend_name`` to its values.
        worker_id: Worker that ran (or is running) the task, or None when
            the record is emitted before a worker is known.
        **extra: Extra fields to merge in last, so a caller can add
            ``status``, ``duration_ms`` or ``error_class`` without
            rebuilding the mapping.
    """
    fields = {
        "task_id": task_data.get("task_id"),
        "task_path": task_data.get("task_path"),
        "queue_name": task_data.get("queue_name"),
        "priority": _priority_as_int(task_data.get("priority")),
        "backend_alias": task_data.get("backend_name"),
        "worker_id": worker_id,
    }
    fields.update(extra)
    return fields


def _priority_as_int(value):
    """
    Coerce a priority value read from the Redis hash to an int.

    The hash stores it as a string (the task was written through
    ``serialize_json``); a None or empty value stays None.
    """
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _elapsed_ms(started_monotonic):
    """
    Milliseconds since a ``time.monotonic()`` reading, rounded to the ms.

    The wall time of the run is kept apart from the stored ``started_at`` /
    ``finished_at`` because those are database-style timestamps that can be
    rewritten by a recovery sweep, and the operator wants the time the task
    actually spent in the function.
    """
    return round((time.monotonic() - started_monotonic) * 1000)


# Move a task to `new_status` in the status index: out of the sets of every
# other status and into the two sets of the new one. Every other set is swept,
# not just the set of the status the task was read in, so whichever of two
# racing writers lands last leaves the index matching the hash it wrote.
# Entries older than `cutoff` have expired with their hash, and nothing else
# takes them out, so the sets being added to are pruned on the way; '' is a
# result that never expires.
#
# KEYS[2..5] the backend-wide sets, KEYS[6..9] the sets of the task's queue,
# both in STATUS_INDEX_ORDER.
_INDEX_STATUS = (
    "local STATUSES = {"
    + ", ".join(f"'{status}'" for status in STATUS_INDEX_ORDER)
    + "}"
    + """
local function index_status(task_id, new_status, score, cutoff)
    for i, status in ipairs(STATUSES) do
        for _, key in ipairs({KEYS[1 + i], KEYS[5 + i]}) do
            if status == new_status then
                if cutoff ~= '' then
                    redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
                end
                redis.call('ZADD', key, score, task_id)
            else
                redis.call('ZREM', key, task_id)
            end
        end
    end
end
"""
)

# Claim a task for a run: move it to RUNNING and record the attempt, but only
# from one of the statuses the caller expects. Check and write in one step, or a
# worker that has read the message and an external trigger for the same task
# both see READY and both run it.
#
# KEYS[1] the task hash, KEYS[2..9] the status index sets
# ARGV[1] the time of the attempt (ISO 8601)
# ARGV[2] the worker id, or "" to record none
# ARGV[3] the status to move to (RUNNING)
# ARGV[4] the task id
# ARGV[5] the time of the attempt as the index score (Unix timestamp)
# ARGV[6] the index cutoff, or ""
# ARGV[7..] the statuses the task may be claimed from
#
# Returns 1 when claimed, 0 when the task is in another status, -1 when there is
# no such task.
_CLAIM_TASK = (
    _INDEX_STATUS
    + """
local status = redis.call('HGET', KEYS[1], 'status')
if status == false then
    return -1
end
local allowed = false
for i = 7, #ARGV do
    if status == ARGV[i] then
        allowed = true
    end
end
if not allowed then
    return 0
end
local fields = {'status', ARGV[3], 'last_attempted_at', ARGV[1]}
local started_at = redis.call('HGET', KEYS[1], 'started_at')
if started_at == false or started_at == '' then
    fields[#fields + 1] = 'started_at'
    fields[#fields + 1] = ARGV[1]
end
if ARGV[2] ~= '' then
    local raw = redis.call('HGET', KEYS[1], 'worker_ids_json')
    local worker_ids = {}
    if raw and raw ~= '' then
        worker_ids = cjson.decode(raw)
    end
    worker_ids[#worker_ids + 1] = ARGV[2]
    fields[#fields + 1] = 'worker_ids_json'
    fields[#fields + 1] = cjson.encode(worker_ids)
end
redis.call('HSET', KEYS[1], unpack(fields))
index_status(ARGV[4], ARGV[3], ARGV[5], ARGV[6])
return 1
"""
)

# Read and write in one step, or two callers both see an executable task and
# both run it.
#
# KEYS[1] the task hash, KEYS[2..9] the status index sets
# ARGV[1] the status to move to
# ARGV[2] the task id
# ARGV[3] the time of the move as the index score (Unix timestamp)
# ARGV[4] the index cutoff, or ""
# ARGV[5..] the statuses the task may be moved from
_TRANSITION_TASK_STATUS = (
    _INDEX_STATUS
    + """
local status = redis.call('HGET', KEYS[1], 'status')
if status == false then
    return 0
end
for i = 5, #ARGV do
    if status == ARGV[i] then
        redis.call('HSET', KEYS[1], 'status', ARGV[1])
        index_status(ARGV[2], ARGV[1], ARGV[3], ARGV[4])
        return 1
    end
end
return 0
"""
)

# Index a task under the status it was read in, if that is still its status:
# a task that moved on in between was indexed by the transition that moved
# it, and re-adding it under the old status would count it twice. A task
# whose hash is gone is taken out of every set.
#
# KEYS[1] the task hash, KEYS[2..9] the status index sets
# ARGV[1] the task id
# ARGV[2] the status the task was read in
# ARGV[3] the index score (Unix timestamp)
# ARGV[4] the index cutoff, or ""
#
# Returns 1 when indexed, 0 when the status has changed, -1 when the hash is
# gone.
_INDEX_TASK = (
    _INDEX_STATUS
    + """
local status = redis.call('HGET', KEYS[1], 'status')
if status == false then
    for i = 2, 9 do
        redis.call('ZREM', KEYS[i], ARGV[1])
    end
    return -1
end
if status ~= ARGV[2] then
    return 0
end
index_status(ARGV[1], ARGV[2], ARGV[3], ARGV[4])
return 1
"""
)


class RedisTaskBackend(BaseTaskBackend):
    """A task backend that uses Redis/Valkey for task queuing and storage."""

    supports_defer = True
    supports_async_task = True
    supports_get_result = True
    supports_priority = True

    # The broker a worker consumes tasks through. A subclass can name another
    # class here; it is built once per backend and reads its settings from it.
    broker_class = RedisStreamsBroker

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
        self._check_socket_timeout()
        # Without a cap, a task that can never finish is started again
        # forever. Counts starts, not deliveries. 0 disables it.
        self.max_deliveries = self.options.get("REDIS_MAX_DELIVERIES", 5)
        self.scan_batch_size = self.options.get("REDIS_SCAN_BATCH_SIZE", 500)

        # Whether the status index is known to cover every stored result; see
        # has_status_index(). Only a positive answer is remembered.
        self._status_index_built = False
        self._status_index_warned = False

        self.broker = self.create_broker()

    def _check_socket_timeout(self):
        """
        Warn about a socket timeout the worker's blocking read cannot fit in.

        The read timeout covers XREADGROUP ... BLOCK too, so a socket timeout
        at or below the block makes every idle wait of a worker raise
        TimeoutError. A warning rather than an error: a process that only
        enqueues is unaffected, and must keep starting.
        """
        socket_timeout = self.options.get("REDIS_SOCKET_TIMEOUT")
        if socket_timeout is None or not self.block_timeout:
            return
        if socket_timeout * 1000 <= self.block_timeout:
            logger.warning(
                "REDIS_SOCKET_TIMEOUT (%ss) does not exceed REDIS_BLOCK_TIMEOUT "
                "(%sms) on the %r task backend: every blocking read of a worker "
                "will raise TimeoutError. Raise the socket timeout above the "
                "block, or leave it unset.",
                socket_timeout,
                self.block_timeout,
                self.alias,
            )

    def get_client(self):
        """Get or create Redis client."""
        if self._client is None:
            self._client = get_redis_client(self.options)
        return self._client

    def create_broker(self):
        """Build the broker workers consume this backend's tasks through."""
        return self.broker_class(self, self.options)

    def get_auth_handlers(self, endpoint=None):
        """
        Get the authentication handlers for the task HTTP endpoints.

        A request is accepted as soon as one handler accepts it, so a backend
        can let both the service that calls the endpoints (Cloud Tasks, a
        webhook) and an external cron job in, each with its own credentials.

        Each handler is a callable that takes a request and returns:
        - None if authentication succeeds
        - A response with error details if authentication fails

        An empty list keeps the endpoints closed: they run and delete tasks,
        so they cannot be reachable without the backend having said how to
        authenticate them.

        Args:
            endpoint: Name of the endpoint being called (``"run"``,
                ``"run_one"``, ``"status"``, ``"execute"`` or ``"purge"``),
                or ``None`` to get every handler regardless of the endpoint
                it applies to.

        Returns:
            list of callables
        """
        handlers = list(self.get_broker_auth_handlers(endpoint))
        handlers.extend(self.get_configured_auth_handlers(endpoint))
        return handlers

    def get_broker_auth_handlers(self, endpoint=None):
        """
        Get the handlers that authenticate the service calling the endpoints.

        These come from the broker, which knows how the service it talks to
        signs its requests. The Redis brokers are pull-only: they have nothing
        to authenticate.

        Returns:
            list of callables
        """
        handlers = []
        if self.broker is not None:
            # A broker that is not a TaskBroker has no handler.
            get_broker_handlers = getattr(self.broker, "get_auth_handlers", None)
            if get_broker_handlers is not None:
                handlers.extend(get_broker_handlers(endpoint) or [])

        return handlers

    def get_configured_auth_handlers(self, endpoint=None):
        """
        Get the handlers built from the ``AUTH_HANDLERS`` backend option.

        Returns:
            list of callables
        """
        return [
            handler
            for handler, endpoints in self._auth_handler_specs
            if endpoint is None or endpoints is None or endpoint in endpoints
        ]

    @cached_property
    def _auth_handler_specs(self):
        """Load ``AUTH_HANDLERS`` once per backend instance."""
        from .auth import load_auth_handlers

        return load_auth_handlers(
            self.options.get("AUTH_HANDLERS"),
            self.options.get("AUTH_HANDLER_OPTIONS"),
        )

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

        is_delayed = task.run_after is not None and task.run_after > now
        if not is_delayed:
            stream_key = self.broker.stream_key(
                task.queue_name, priority_to_level(task.priority)
            )
            # Outside the transaction below: idempotent, and XADD needs it first.
            self.broker.ensure_consumer_group(stream_key)

        # A backend that has never stored a result has nothing to rebuild the
        # status index from. Settle that before the first result exists, so
        # the index is trusted from the first task on and no rebuild is asked
        # for. One round trip per process.
        if not self._status_index_built:
            self.has_status_index()

        # One transaction: a task stored and indexed but never queued would
        # never run, and nothing would report it.
        pipeline = client.pipeline()

        # Store task result in Hash
        pipeline.hset(result_key, mapping=task_data)
        if self.result_ttl > 0:
            result_ttl = self.result_ttl
            if is_delayed:
                # A result that expires before its task is due loses the task.
                result_ttl += int((task.run_after - now).total_seconds())
            pipeline.expire(result_key, result_ttl)

        # Add to results index for iteration
        pipeline.sadd(results_index_key, task_id)

        # Index the task as READY from the time it starts waiting: now, or
        # run_after for a delayed task. That is also when the hash's TTL
        # starts counting for a delayed task, so the entry expires with it.
        self._index_status(
            pipeline,
            task_id,
            task.queue_name,
            TaskResultStatus.READY,
            task.run_after if is_delayed else now,
            now,
        )

        if is_delayed:
            # Add to delayed sorted set
            delayed_key = get_delayed_key(self.key_prefix, self.alias, task.queue_name)
            pipeline.zadd(delayed_key, {task_id: task.run_after.timestamp()})
        else:
            # Add to priority-based stream
            pipeline.xadd(stream_key, self.broker.stream_entry(task_data))

        pipeline.execute()

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

    def claim_task(self, task_id, worker_id=None, from_statuses=None):
        """
        Claim a task for a run: move it to RUNNING and record the attempt.

        The status check and the write are one step, so of two callers racing
        for the same task exactly one wins. `started_at` is set on the first
        attempt only, `last_attempted_at` on every one, and `worker_id` is
        appended to the task's worker ids.

        Args:
            task_id: Task ID string.
            worker_id: Worker identifier to record, or None.
            from_statuses: Statuses the task may be claimed from. Defaults to
                READY alone.

        Returns:
            True if this caller claimed the task, False if it is in another
            status.

        Raises:
            TaskResultDoesNotExist: If no task with the given ID exists.
        """
        if from_statuses is None:
            from_statuses = [TaskResultStatus.READY]

        client = self.get_client()
        result_key = get_result_key(self.key_prefix, self.alias, task_id)

        # The queue names the index sets the script writes, and a script is
        # given every key it touches. A task never changes queue, so reading
        # it ahead of the script is safe.
        queue_name = client.hget(result_key, "queue_name")
        if queue_name is None:
            raise TaskResultDoesNotExist(task_id)

        now = timezone.now()
        claim = client.register_script(_CLAIM_TASK)
        outcome = claim(
            keys=[result_key, *self._status_index_keys(queue_name)],
            args=[
                serialize_datetime(now),
                worker_id or "",
                TaskResultStatus.RUNNING,
                *self._script_index_args(task_id, TaskResultStatus.RUNNING, now),
                *from_statuses,
            ],
        )
        if outcome == -1:
            raise TaskResultDoesNotExist(task_id)
        return outcome == 1

    def run_task(self, task_id, worker_id=None, from_statuses=None):
        """
        Claim a task and execute it (called from executor/management command).

        The task is claimed with :meth:`claim_task` first, so a worker that read
        the task's message and an external trigger for the same task cannot
        both run it: whichever claims second gets None and runs nothing.

        Args:
            task_id: Task ID string.
            worker_id: Optional worker identifier.
            from_statuses: Statuses the task may be run from. Defaults to READY
                alone; ``run_task_by_id(allow_retry=True)`` adds FAILED.

        Returns:
            TaskResult after execution, or None if the task was not in one of
            `from_statuses` and so was not run.

        Raises:
            TaskResultDoesNotExist: If no task with the given ID exists.
        """
        client = self.get_client()
        result_key = get_result_key(self.key_prefix, self.alias, task_id)

        if not self.claim_task(task_id, worker_id, from_statuses):
            return None

        # Read back what the claim wrote, so the result handed to task_started
        # carries the attempt exactly as stored.
        task_data = client.hgetall(result_key)
        if not task_data:
            raise TaskResultDoesNotExist(task_id)

        # Past the RUNNING write but before the block that records failures, so
        # an error here would leave the task RUNNING with nothing to explain it.
        try:
            task = self._resolve_task(task_data["task_path"])
            task_result = self._data_to_result(task_data, task)
            logger.info(
                "Task started: id=%s path=%s",
                task_result.id,
                task_data["task_path"],
                extra=task_log_fields(task_data, worker_id),
            )
            task_started.send(sender=self.__class__, task_result=task_result)
        except Exception as e:
            error = TaskError(
                exception_class_path=f"{type(e).__module__}.{type(e).__qualname__}",
                traceback=traceback.format_exc(),
            )
            self._record_error(task_id, task_data, error)
            logger.exception(
                "Task could not be started: id=%s error=%s",
                task_id,
                error.exception_class_path,
                extra=task_log_fields(
                    task_data,
                    worker_id,
                    status=str(TaskResultStatus.FAILED),
                    error_class=error.exception_class_path,
                ),
            )
            raise

        # Wall time of the run itself, kept apart from started_at / finished_at
        # because those are stored timestamps and can be rewritten by a recovery
        # sweep. The operator wants the time the task actually spent in the
        # function, which is what duration_ms reports.
        started_monotonic = time.monotonic()

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

            # Success. One transaction with the index move, so the index
            # never shows a status the hash does not.
            finished_at = timezone.now()
            pipeline = client.pipeline()
            pipeline.hset(
                result_key,
                mapping={
                    "status": TaskResultStatus.SUCCESSFUL,
                    "return_value_json": serialize_json(normalized_return_value),
                    "finished_at": serialize_datetime(finished_at),
                },
            )

            # Set TTL for completed task
            if self.completed_task_ttl > 0:
                pipeline.expire(result_key, self.completed_task_ttl)

            self._index_status(
                pipeline,
                task_id,
                task_data.get("queue_name", "default"),
                TaskResultStatus.SUCCESSFUL,
                finished_at,
                finished_at,
            )
            pipeline.execute()

            # Refresh and return result
            task_data = client.hgetall(result_key)
            final_result = self._data_to_result(task_data, task)
            logger.info(
                "Task completed successfully: id=%s path=%s",
                final_result.id,
                task_data["task_path"],
                extra=task_log_fields(
                    task_data,
                    worker_id,
                    status=str(TaskResultStatus.SUCCESSFUL),
                    duration_ms=_elapsed_ms(started_monotonic),
                ),
            )
            task_finished.send(sender=self.__class__, task_result=final_result)
            return final_result

        except Exception as e:
            # Failure
            error = TaskError(
                exception_class_path=f"{type(e).__module__}.{type(e).__qualname__}",
                traceback=traceback.format_exc(),
            )
            self._record_error(task_id, task_data, error)

            # Refresh and return result
            task_data = client.hgetall(result_key)
            final_result = self._data_to_result(task_data, task)
            logger.error(
                "Task failed: id=%s path=%s error=%s",
                final_result.id,
                task_data["task_path"],
                error.exception_class_path,
                extra=task_log_fields(
                    task_data,
                    worker_id,
                    status=str(TaskResultStatus.FAILED),
                    duration_ms=_elapsed_ms(started_monotonic),
                    error_class=error.exception_class_path,
                ),
            )
            task_finished.send(sender=self.__class__, task_result=final_result)
            return final_result

    def _record_error(self, task_id, task_data, error):
        """
        Persist a terminal FAILED state with `error` appended to the task.

        Args:
            task_id: Task ID string.
            task_data: Task data read before the failure.
            error: TaskError to append.
        """
        client = self.get_client()
        result_key = get_result_key(self.key_prefix, self.alias, task_id)

        errors = deserialize_json(task_data.get("errors_json", "[]")) or []
        errors.append(
            {
                "exception_class_path": error.exception_class_path,
                "traceback": error.traceback,
            }
        )

        # One transaction with the index move, so the index never shows a
        # status the hash does not.
        finished_at = timezone.now()
        pipeline = client.pipeline()
        pipeline.hset(
            result_key,
            mapping={
                "status": TaskResultStatus.FAILED,
                "errors_json": serialize_json(errors),
                "finished_at": serialize_datetime(finished_at),
            },
        )

        # Set TTL for completed task
        if self.completed_task_ttl > 0:
            pipeline.expire(result_key, self.completed_task_ttl)

        self._index_status(
            pipeline,
            task_id,
            task_data.get("queue_name", "default"),
            TaskResultStatus.FAILED,
            finished_at,
            finished_at,
        )
        pipeline.execute()

    def transition_task_status(self, task_id, to_status, from_statuses):
        """
        Move a task to `to_status`, but only from one of `from_statuses`.

        The check and the write happen in one step, so two callers racing to
        start the same task cannot both win.

        Args:
            task_id: Task ID string.
            to_status: Status to move the task to.
            from_statuses: Statuses the task may be moved from.

        Returns:
            True if this caller made the transition.
        """
        client = self.get_client()
        result_key = get_result_key(self.key_prefix, self.alias, task_id)

        # The queue names the index sets the script writes; see claim_task().
        queue_name = client.hget(result_key, "queue_name")
        if queue_name is None:
            return False

        now = timezone.now()
        transition = client.register_script(_TRANSITION_TASK_STATUS)

        return bool(
            transition(
                keys=[result_key, *self._status_index_keys(queue_name)],
                args=[
                    to_status,
                    *self._script_index_args(task_id, to_status, now),
                    *from_statuses,
                ],
            )
        )

    def mark_task_failed(self, task_id, reason):
        """
        Record a task as FAILED without running it.

        Used when the queue gives up on a task, so an operator sees a failed
        task with a reason instead of one stuck in READY or RUNNING forever.

        No task_finished signal is sent: resolving the task object is itself a
        reason a task gets abandoned, and TaskResult cannot be built without it.

        Args:
            task_id: Task ID string.
            reason: Human-readable explanation, stored as the error traceback.

        Only a task that is READY or RUNNING can be given up on. One that
        already finished keeps its result: a worker that wrote SUCCESSFUL and
        died before acknowledging its message must not be turned into a
        failure by the sweep that finds the message.

        Returns:
            True if recorded, False if the task no longer exists or already
            finished.
        """
        client = self.get_client()
        result_key = get_result_key(self.key_prefix, self.alias, task_id)

        if not self.transition_task_status(
            task_id,
            TaskResultStatus.FAILED,
            [TaskResultStatus.READY, TaskResultStatus.RUNNING],
        ):
            return False

        task_data = client.hgetall(result_key)
        abandoned_path = f"{TaskAbandoned.__module__}.{TaskAbandoned.__qualname__}"
        self._record_error(
            task_id,
            task_data,
            TaskError(
                exception_class_path=abandoned_path,
                traceback=reason,
            ),
        )
        logger.error(
            "Task abandoned: id=%s path=%s reason=%s",
            task_id,
            task_data.get("task_path", ""),
            reason,
            extra=task_log_fields(
                task_data,
                worker_id=None,
                status=str(TaskResultStatus.FAILED),
                error_class=abandoned_path,
            ),
        )
        return True

    def iter_task_data(self, batch_size=None, cleanup=True):
        """
        Yield (task_id, task_data) for every task in the results index.

        The index holds one member per task, so reading it one HGETALL at a time
        costs a round trip per task and materialises the whole index in memory.
        This walks it with SSCAN and fetches each batch in a single pipeline.

        Args:
            batch_size: Tasks per pipelined round trip. If None, uses the
                backend setting.
            cleanup: Drop index members whose task hash has expired.

        Yields:
            Tuples of (task_id, task data dict).
        """
        client = self.get_client()
        results_index_key = get_results_index_key(self.key_prefix, self.alias)
        batch_size = batch_size or self.scan_batch_size

        batch = []
        for task_id in client.sscan_iter(results_index_key, count=batch_size):
            batch.append(task_id)
            if len(batch) >= batch_size:
                yield from self._fetch_task_batch(results_index_key, batch, cleanup)
                batch = []

        if batch:
            yield from self._fetch_task_batch(results_index_key, batch, cleanup)

    def _fetch_task_batch(self, results_index_key, task_ids, cleanup):
        """Fetch one batch of task hashes and drop index members that expired."""
        client = self.get_client()

        pipeline = client.pipeline(transaction=False)
        for task_id in task_ids:
            pipeline.hgetall(get_result_key(self.key_prefix, self.alias, task_id))

        expired = []
        for task_id, task_data in zip(task_ids, pipeline.execute(), strict=True):
            if task_data:
                yield task_id, task_data
            else:
                expired.append(task_id)

        if cleanup and expired:
            client.srem(results_index_key, *expired)

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
            priority: Optional priority filter, matched against the stored
                string form (e.g. "10").
            offset: Starting offset.
            limit: Maximum number of results.

        Returns:
            Tuple of (list of task dicts, total count).
        """
        # Fetch all task data and filter
        tasks = []
        for _task_id, task_data in self.iter_task_data():
            # Apply filters
            if queue_name and task_data.get("queue_name") != queue_name:
                continue
            if status and task_data.get("status") != status:
                continue
            if task_path and task_data.get("task_path") != task_path:
                continue
            if priority is not None and task_data.get("priority") != str(priority):
                continue

            tasks.append(task_data)

        # Sort by enqueued_at descending
        tasks.sort(key=lambda x: x.get("enqueued_at", ""), reverse=True)

        total = len(tasks)

        # Apply pagination
        tasks = tasks[offset : offset + limit]

        return tasks, total

    def get_distinct_task_values(self, fields):
        """
        Collect the distinct values of several task fields in a single pass.

        Reading one field per call walks every stored task once per field, so
        callers that need several fields, like the admin's list filters, ask
        for all of them at once.

        Args:
            fields: Iterable of task data field names.

        Returns:
            Dict mapping each field to the set of its distinct values.
        """
        values = {field: set() for field in fields}
        for _task_id, task_data in self.iter_task_data():
            for field in values:
                value = task_data.get(field)
                if value is not None:
                    values[field].add(value)
        return values

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

        # None when the hash is already gone: the backend-wide index sets are
        # still swept, the queue's are left to expire by score.
        queue_name = client.hget(result_key, "queue_name")

        pipeline = client.pipeline()
        pipeline.delete(result_key)
        pipeline.srem(results_index_key, task_id)
        for key in self._status_index_keys(queue_name):
            pipeline.zrem(key, task_id)
        deleted, *_replies = pipeline.execute()

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

        # The task starts waiting again now: one transaction with the index
        # move, so the index never shows a status the hash does not.
        now = timezone.now()
        pipeline = client.pipeline()
        pipeline.hset(
            result_key,
            mapping={
                "status": TaskResultStatus.READY,
                "finished_at": "",
                "errors_json": serialize_json([]),
            },
        )
        self._index_status(
            pipeline,
            task_id,
            task_data.get("queue_name", "default"),
            TaskResultStatus.READY,
            now,
            now,
        )
        pipeline.execute()

        # Re-add to stream for processing
        self.broker.requeue(task_data)

        return True

    # -- status index ------------------------------------------------------
    #
    # One sorted set per status, backend-wide and per queue, holding the ids
    # of the tasks in that status scored by the time they entered it. A count
    # is a ZCARD instead of a pass over every stored result, and the READY
    # set's ends are the oldest and newest waiting task. Every status write
    # moves the task in the same script or transaction, so the index follows
    # the hash under concurrent writers; an entry whose hash has expired is
    # pruned by score, since nothing else takes it out.

    def _status_index_keys(self, queue_name=None):
        """
        The index sets, backend-wide then queue-scoped, in STATUS_INDEX_ORDER.

        Without a queue name only the backend-wide sets are returned.
        """
        keys = [
            get_status_index_key(self.key_prefix, self.alias, status)
            for status in STATUS_INDEX_ORDER
        ]
        if queue_name is not None:
            keys.extend(
                get_status_index_key(self.key_prefix, self.alias, status, queue_name)
                for status in STATUS_INDEX_ORDER
            )
        return keys

    def _status_index_ttl(self, status):
        """The TTL a result hash carries while it is in `status`."""
        if status in (TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED):
            return self.completed_task_ttl
        return self.result_ttl

    def _status_index_cutoff(self, status, now):
        """
        The score below which an entry of `status` has expired with its hash.

        A hash gets its TTL when it enters READY (counted from the time it
        starts waiting) and again when it finishes, so an entry older than
        the TTL of its status has no hash behind it. RUNNING keeps the READY
        TTL, so a task that expired while running lingers for as long as it
        had waited; a task interrupted and queued again lingers likewise.
        None when results of `status` never expire.
        """
        ttl = self._status_index_ttl(status)
        if not ttl or ttl <= 0:
            return None
        return now.timestamp() - ttl

    def _script_index_args(self, task_id, status, now):
        """The index arguments the transition scripts take after their own."""
        cutoff = self._status_index_cutoff(status, now)
        return [task_id, repr(now.timestamp()), "" if cutoff is None else repr(cutoff)]

    def _index_status(self, pipeline, task_id, queue_name, status, since, now):
        """
        Queue the writes that move `task_id` to `status` in the index.

        The Python side of the ``index_status`` script function, for the
        writers that are a transaction rather than a script: the task leaves
        the sets of every other status and joins the two of `status`, scored
        by `since`, and those two are pruned of expired entries on the way.

        Args:
            pipeline: Pipeline the commands are queued on.
            task_id: Task ID string.
            queue_name: The task's queue.
            status: Status the task is moving to.
            since: Datetime the task entered the status, the entry's score.
            now: Current datetime, for the expiry cutoff.
        """
        keys = self._status_index_keys(queue_name)
        cutoff = self._status_index_cutoff(status, now)
        for position, other in enumerate(STATUS_INDEX_ORDER):
            for key in (keys[position], keys[4 + position]):
                if other == status:
                    if cutoff is not None:
                        pipeline.zremrangebyscore(key, "-inf", cutoff)
                    pipeline.zadd(key, {task_id: since.timestamp()})
                else:
                    pipeline.zrem(key, task_id)

    def _waiting_since(self, task_data):
        """
        When a READY task started waiting: ``max(enqueued_at, run_after)``.

        A delayed task starts waiting when it comes due, so run_after, when
        set, is the origin instead of enqueued_at. The max compares
        datetimes, not the ISO strings: enqueued_at is written from
        timezone.now(), while run_after keeps the caller's offset, and
        strings with different offsets do not compare as times.
        """
        enqueued_at = deserialize_datetime(task_data.get("enqueued_at", ""))
        run_after = deserialize_datetime(task_data.get("run_after", ""))
        if run_after is not None and (enqueued_at is None or run_after > enqueued_at):
            return run_after
        return enqueued_at

    def _status_since(self, task_data, status, now):
        """
        When a stored task entered its status, from the hash, for a rebuild.

        The same clock the transitions write: the time a READY task started
        waiting, the last attempt for RUNNING, finished_at for the rest.
        `now` stands in for a field the hash does not carry.
        """
        if status == TaskResultStatus.READY:
            since = self._waiting_since(task_data)
        elif status == TaskResultStatus.RUNNING:
            since = deserialize_datetime(
                task_data.get("last_attempted_at", "")
            ) or deserialize_datetime(task_data.get("started_at", ""))
        else:
            since = deserialize_datetime(task_data.get("finished_at", ""))
        return since or now

    def has_status_index(self):
        """
        Whether the status index covers every stored result.

        True once :meth:`rebuild_status_index` has run, or for a backend that
        had no stored result the first time it was asked: the index is
        written from the first task on, so there is nothing to rebuild. Until
        then the status counts are read from the result hashes, as they were
        before the index existed.

        A positive answer is remembered for the process; a negative one is
        asked again on every call, so a rebuild run from another process is
        picked up without a restart.
        """
        if self._status_index_built:
            return True

        client = self.get_client()
        built_key = get_status_index_built_key(self.key_prefix, self.alias)
        if client.exists(built_key):
            self._status_index_built = True
        elif not client.exists(get_results_index_key(self.key_prefix, self.alias)):
            client.set(built_key, "1")
            self._status_index_built = True
        return self._status_index_built

    def rebuild_status_index(self, batch_size=None):
        """
        Index every stored result under its status, and mark the index built.

        For the results a deployment stored before the index existed, and for
        anything that has since put the index out of step with the hashes.
        Workers can keep running throughout: each result is indexed by a
        script that reads its status again first, and one that moved on in
        between is left to the transition that moved it, which indexed it
        itself.

        Args:
            batch_size: Results per pipelined round trip. If None, uses the
                backend setting.

        Returns:
            Number of results indexed.
        """
        client = self.get_client()
        batch_size = batch_size or self.scan_batch_size
        index_task = client.register_script(_INDEX_TASK)
        now = timezone.now()
        indexed = 0

        pipeline = client.pipeline(transaction=False)
        queued = 0
        for task_id, task_data in self.iter_task_data(batch_size=batch_size):
            status = task_data.get("status")
            if status not in STATUS_INDEX_ORDER:
                continue
            queue_name = task_data.get("queue_name", "default")
            cutoff = self._status_index_cutoff(status, now)
            index_task(
                keys=[
                    get_result_key(self.key_prefix, self.alias, task_id),
                    *self._status_index_keys(queue_name),
                ],
                args=[
                    task_id,
                    status,
                    repr(self._status_since(task_data, status, now).timestamp()),
                    "" if cutoff is None else repr(cutoff),
                ],
                client=pipeline,
            )
            queued += 1
            if queued >= batch_size:
                indexed += pipeline.execute().count(1)
                queued = 0
        if queued:
            indexed += pipeline.execute().count(1)

        client.set(get_status_index_built_key(self.key_prefix, self.alias), "1")
        self._status_index_built = True
        logger.info(
            "Status index of the %r task backend rebuilt: %s result(s) indexed",
            self.alias,
            indexed,
        )
        return indexed

    def _read_status_index(self, queue_name=None):
        """
        The counts per status and the pending bounds, from the index.

        Entries older than the TTL of their status are pruned first: their
        hash has expired, and nothing else takes them out of the set.

        The READY set is scored by the time a task starts waiting, so the
        tasks that are due are the entries scored up to now, and the delayed
        tasks whose time has not come are the rest.

        Returns:
            Tuple of (counts dict, pending count, oldest waiting-since
            datetime, newest waiting-since datetime), like
            :meth:`_scan_status_counts`.
        """
        client = self.get_client()
        now = timezone.now()
        now_timestamp = now.timestamp()
        # An empty queue name means every queue, as it does for the scan.
        queue_name = queue_name or None
        pipeline = client.pipeline(transaction=False)

        count_positions = {}
        queued = 0
        for status in STATUS_INDEX_ORDER:
            key = get_status_index_key(self.key_prefix, self.alias, status, queue_name)
            cutoff = self._status_index_cutoff(status, now)
            if cutoff is not None:
                pipeline.zremrangebyscore(key, "-inf", cutoff)
                queued += 1
            pipeline.zcard(key)
            count_positions[status] = queued
            queued += 1

        ready_key = get_status_index_key(
            self.key_prefix, self.alias, TaskResultStatus.READY, queue_name
        )
        pipeline.zcount(ready_key, "-inf", now_timestamp)
        pipeline.zrangebyscore(
            ready_key, "-inf", now_timestamp, start=0, num=1, withscores=True
        )
        pipeline.zrevrangebyscore(
            ready_key, now_timestamp, "-inf", start=0, num=1, withscores=True
        )

        replies = pipeline.execute()
        counts = {
            status: replies[position] for status, position in count_positions.items()
        }
        pending_count = replies[-3]
        oldest = replies[-2]
        newest = replies[-1]
        return (
            counts,
            pending_count,
            deserialize_timestamp(oldest[0][1]) if oldest else None,
            deserialize_timestamp(newest[0][1]) if newest else None,
        )

    def _status_counts(self, queue_name=None):
        """
        The counts, the pending count and the pending bounds from the index,
        or from a scan until it is built.
        """
        if self.has_status_index():
            return self._read_status_index(queue_name)

        if not self._status_index_warned:
            self._status_index_warned = True
            logger.warning(
                "The status index of the %r task backend has not been built, so "
                "its status counts are read from every stored result. Run "
                "`manage.py rebuild_redis_status_index --backend %s` once.",
                self.alias,
                self.alias,
            )
        return self._scan_status_counts(queue_name)

    def get_status_counts(self, queue_name=None):
        """
        Get task counts by status.

        Read from the status index, at a cost independent of how many results
        are stored; from a pass over the stored results until the index has
        been built (see :meth:`has_status_index`).

        Args:
            queue_name: Optional queue name filter.

        Returns:
            Dict mapping status to count.
        """
        counts, _pending_count, _oldest, _newest = self._status_counts(queue_name)
        return counts

    def _scan_status_counts(self, queue_name=None):
        """
        One pass over the results index for the status-count based APIs.

        Returns the counts per status and, from the same scan, the number of
        pending tasks and the time the oldest and newest of them started
        waiting, as ``max(enqueued_at, run_after)`` datetimes, so a caller
        that wants both does not read the index twice. A pending task is a
        READY task that is due: one whose waiting time has come.

        Args:
            queue_name: Optional queue name filter.

        Returns:
            Tuple of (counts dict, pending count, oldest waiting-since
            datetime, newest waiting-since datetime). Both datetimes are None
            when no pending task was found.
        """
        counts = {
            TaskResultStatus.READY: 0,
            TaskResultStatus.RUNNING: 0,
            TaskResultStatus.SUCCESSFUL: 0,
            TaskResultStatus.FAILED: 0,
        }
        pending_count = 0
        oldest = newest = None
        now = timezone.now()

        for _task_id, task_data in self.iter_task_data():
            if queue_name and task_data.get("queue_name") != queue_name:
                continue

            status = task_data.get("status")
            if status in counts:
                counts[status] += 1

            if status == TaskResultStatus.READY:
                waiting_since = self._waiting_since(task_data)
                if waiting_since is not None and waiting_since > now:
                    continue
                pending_count += 1
                if waiting_since is not None:
                    if oldest is None or waiting_since < oldest:
                        oldest = waiting_since
                    if newest is None or waiting_since > newest:
                        newest = waiting_since

        return counts, pending_count, oldest, newest

    def get_queue_stats(self, queue_name=None):
        """
        Get queue statistics for a dashboard or an alert.

        Args:
            queue_name: Optional queue name filter.

        Returns:
            Dict with the counts per status (``pending_count``,
            ``running_count``, ``successful_count``, ``failed_count``), the
            number of delayed tasks not yet due (``delayed_count``), and the
            time the oldest and newest pending task started waiting
            (``oldest_pending_waiting_since``, ``newest_pending_waiting_since``):
            ``max(enqueued_at, run_after)``, None when there is none.

            A pending task is one a worker would pick up now: a READY task
            whose ``run_after`` is unset or has passed, as counted by
            ``get_pending_task_count()``. A READY task whose ``run_after``
            lies in the future is counted in ``delayed_count`` instead, so
            the two add up to the READY count of :meth:`get_status_counts`.
        """
        counts, pending_count, oldest, newest = self._status_counts(queue_name)
        ready_count = counts.get(TaskResultStatus.READY, 0)

        return {
            "pending_count": pending_count,
            "running_count": counts.get(TaskResultStatus.RUNNING, 0),
            "successful_count": counts.get(TaskResultStatus.SUCCESSFUL, 0),
            "failed_count": counts.get(TaskResultStatus.FAILED, 0),
            # The pipeline is not a transaction: a task enqueued between the
            # two counts must not make this negative.
            "delayed_count": max(ready_count - pending_count, 0),
            "oldest_pending_waiting_since": oldest,
            "newest_pending_waiting_since": newest,
        }

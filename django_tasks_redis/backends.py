"""
Redis/Valkey task backend implementation.
"""

import asyncio
import logging
import traceback
import uuid
import warnings
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
    get_delayed_key,
    get_redis_client,
    get_result_key,
    get_results_index_key,
    priority_to_level,
    serialize_datetime,
    serialize_json,
)

logger = logging.getLogger("django_tasks_redis")

# Claim a task for a run: move it to RUNNING and record the attempt, but only
# from one of the statuses the caller expects. Check and write in one step, or a
# worker that has read the message and an external trigger for the same task
# both see READY and both run it.
#
# KEYS[1] the task hash
# ARGV[1] the time of the attempt (ISO 8601)
# ARGV[2] the worker id, or "" to record none
# ARGV[3] the status to move to (RUNNING)
# ARGV[4..] the statuses the task may be claimed from
#
# Returns 1 when claimed, 0 when the task is in another status, -1 when there is
# no such task.
_CLAIM_TASK = """
local status = redis.call('HGET', KEYS[1], 'status')
if status == false then
    return -1
end
local allowed = false
for i = 4, #ARGV do
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
return 1
"""

# Read and write in one step, or two callers both see an executable task and
# both run it.
_TRANSITION_TASK_STATUS = """
local status = redis.call('HGET', KEYS[1], 'status')
if status == false then
    return 0
end
for i = 2, #ARGV do
    if status == ARGV[i] then
        redis.call('HSET', KEYS[1], 'status', ARGV[1])
        return 1
    end
end
return 0
"""


class RedisTaskBackend(BaseTaskBackend):
    """A task backend that uses Redis/Valkey for task queuing and storage."""

    supports_defer = True
    supports_async_task = True
    supports_get_result = True
    supports_priority = True

    # The broker a worker consumes tasks through. A subclass can name another
    # class here; it is built once per backend and reads its settings from it.
    broker_class = RedisStreamsBroker

    # Set on an instance once its deprecated get_auth_handler() override has
    # been reported, so the warning is emitted once per backend.
    _legacy_auth_handler_warned = False

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

    def get_auth_handler(self):
        """
        Get the authentication handler for task execution endpoints.

        .. deprecated:: 0.3
            Override :meth:`get_auth_handlers` instead. This method keeps
            working in 0.3 and is removed in 0.4. A subclass that still
            overrides it gets a :class:`DeprecationWarning` once, and its
            return value is wrapped into a one-element list passed to
            ``get_auth_handlers()``.

        The handler is a callable that takes a request and returns:
        - None if authentication succeeds
        - An HttpResponse with error details if authentication fails

        Returning None (the default) keeps the endpoints closed: they run and
        delete tasks, so they cannot be reachable without the project having
        said how to authenticate them.

        Returns:
            Callable or None
        """
        return None

    # Marks the implementation above as one of this library's own, so an
    # override written by a project can be told apart from it.
    get_auth_handler._is_library_auth_handler = True

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
        signs its requests. A deprecated :meth:`get_auth_handler` override is
        adapted here too, so a project written against 0.2 keeps being
        accepted without a second subclass.

        Returns:
            list of callables
        """
        handlers = []
        if self.broker is not None:
            # A broker that is not a TaskBroker has no handler, and the
            # Redis brokers are pull-only: they have nothing to authenticate.
            get_broker_handlers = getattr(self.broker, "get_auth_handlers", None)
            if get_broker_handlers is not None:
                handlers.extend(get_broker_handlers(endpoint) or [])

        handler = self._get_legacy_auth_handler()
        if handler is not None:
            handlers.append(handler)

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

    def _get_legacy_auth_handler(self):
        """
        Call a deprecated :meth:`get_auth_handler` override written by a project.

        Implementations this library ships are skipped: the no-op default
        above and the compat shims on the bundled backends, which would
        otherwise be counted twice.
        """
        implementation = type(self).get_auth_handler
        if getattr(implementation, "_is_library_auth_handler", False):
            return None

        if not self._legacy_auth_handler_warned:
            warnings.warn(
                f"{type(self).__name__}.get_auth_handler() is deprecated and "
                "will be removed in django-tasks-redis 0.4. Override "
                "get_auth_handlers() or configure AUTH_HANDLERS instead.",
                DeprecationWarning,
                stacklevel=3,
            )
            self._legacy_auth_handler_warned = True

        return self.get_auth_handler()

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

        claim = self.get_client().register_script(_CLAIM_TASK)
        outcome = claim(
            keys=[get_result_key(self.key_prefix, self.alias, task_id)],
            args=[
                serialize_datetime(timezone.now()),
                worker_id or "",
                TaskResultStatus.RUNNING,
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
            task_started.send(sender=self.__class__, task_result=task_result)
        except Exception as e:
            self._record_error(
                result_key,
                task_data,
                TaskError(
                    exception_class_path=f"{type(e).__module__}.{type(e).__qualname__}",
                    traceback=traceback.format_exc(),
                ),
            )
            logger.exception("Task could not be started: id=%s", task_id)
            raise

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
            self._record_error(result_key, task_data, error)

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

    def _record_error(self, result_key, task_data, error):
        """
        Persist a terminal FAILED state with `error` appended to the task.

        Args:
            result_key: Redis key of the task hash.
            task_data: Task data read before the failure.
            error: TaskError to append.
        """
        client = self.get_client()

        errors = deserialize_json(task_data.get("errors_json", "[]")) or []
        errors.append(
            {
                "exception_class_path": error.exception_class_path,
                "traceback": error.traceback,
            }
        )

        client.hset(
            result_key,
            mapping={
                "status": TaskResultStatus.FAILED,
                "errors_json": serialize_json(errors),
                "finished_at": serialize_datetime(timezone.now()),
            },
        )

        # Set TTL for completed task
        if self.completed_task_ttl > 0:
            client.expire(result_key, self.completed_task_ttl)

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
        transition = client.register_script(_TRANSITION_TASK_STATUS)

        return bool(
            transition(
                keys=[get_result_key(self.key_prefix, self.alias, task_id)],
                args=[to_status, *from_statuses],
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
        self._record_error(
            result_key,
            task_data,
            TaskError(
                exception_class_path=(
                    f"{TaskAbandoned.__module__}.{TaskAbandoned.__qualname__}"
                ),
                traceback=reason,
            ),
        )
        logger.error(
            "Task abandoned: id=%s path=%s reason=%s",
            task_id,
            task_data.get("task_path", ""),
            reason,
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
        # Fetch all task data and filter
        tasks = []
        for _task_id, task_data in self.iter_task_data():
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
        self.broker.requeue(task_data)

        return True

    def get_status_counts(self, queue_name=None):
        """
        Get task counts by status.

        Args:
            queue_name: Optional queue name filter.

        Returns:
            Dict mapping status to count.
        """
        counts = {
            TaskResultStatus.READY: 0,
            TaskResultStatus.RUNNING: 0,
            TaskResultStatus.SUCCESSFUL: 0,
            TaskResultStatus.FAILED: 0,
        }

        for _task_id, task_data in self.iter_task_data():
            if queue_name and task_data.get("queue_name") != queue_name:
                continue

            status = task_data.get("status")
            if status in counts:
                counts[status] += 1

        return counts

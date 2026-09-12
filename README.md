# django-tasks-redis

[![CI](https://github.com/tokibito/django-tasks-redis/actions/workflows/ci.yml/badge.svg)](https://github.com/tokibito/django-tasks-redis/actions/workflows/ci.yml)
[![PyPI version](https://badge.fury.io/py/django-tasks-redis.svg)](https://badge.fury.io/py/django-tasks-redis)
[![Python versions](https://img.shields.io/pypi/pyversions/django-tasks-redis.svg)](https://pypi.org/project/django-tasks-redis/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A Redis/Valkey-backed task queue backend for Django 6.0's built-in task framework.

## Features

- Full integration with Django 6.0's task framework (`django.tasks`)
- Redis Streams for reliable task queuing with consumer groups
- Support for both Redis and Valkey backends
- Delayed task execution with scheduled times
- Priority-based task processing
- Graceful shutdown: workers finish the running task before exiting on `SIGTERM`
- Crash recovery with automatic task reclaim
- Django Admin integration for task monitoring and management
- HTTP endpoints for external triggers (webhooks, Cloud Scheduler, etc.)

## Architecture

```mermaid
sequenceDiagram
    participant App as Application
    participant Backend as RedisTaskBackend
    participant Redis as Redis/Valkey
    participant Worker as Worker Process

    Note over App,Worker: Task Enqueue
    App->>Backend: task.enqueue(args, kwargs)
    Backend->>Backend: Validate & serialize args
    Note over Backend,Redis: One transaction
    Backend->>Redis: HSET task data (status=READY)
    Backend->>Redis: SADD to results index
    alt run_after in the future
        Backend->>Redis: ZADD to delayed set
    else Ready to run
        Backend->>Redis: XADD to priority stream
    end
    Backend-->>App: TaskResult (id, status=READY)

    Note over App,Worker: Task Execution
    Worker->>Redis: Promote due delayed tasks<br/>(ZREM + XADD in one script)
    Worker->>Redis: XREADGROUP (consumer group)<br/>(own pending messages, then new ones)
    Redis-->>Worker: Message with task_id
    Worker->>Redis: HGET task data
    Redis-->>Worker: Task data
    Worker->>Redis: Claim: status READY→RUNNING<br/>(one script; a lost claim is acknowledged and skipped)
    Worker->>Worker: Execute task function
    alt Success
        Worker->>Redis: HSET status=SUCCESSFUL,<br/>return_value, finished_at
    else Failure
        Worker->>Redis: HSET status=FAILED,<br/>errors, finished_at
    end
    Worker->>Redis: XACK + XDEL (acknowledge and delete the entry)

    Note over App,Worker: Crash Recovery
    Worker->>Redis: XPENDING + XCLAIM stale messages<br/>(claim_timeout exceeded)
    Redis-->>Worker: Messages reassigned to this consumer
    Worker->>Worker: Re-execute tasks<br/>(up to REDIS_MAX_DELIVERIES)
    Worker->>Redis: XGROUP DELCONSUMER idle consumers<br/>that hold nothing

    Note over App,Worker: Result Retrieval (Optional)
    App->>Backend: backend.get_result(task_id)
    Backend->>Redis: HGETALL task data
    Redis-->>Backend: Task data
    Backend-->>App: TaskResult (status, return_value, errors)
```

## Requirements

- Python 3.12+
- Django 6.0+
- Redis 5.0+ or Valkey 7.2+

## Installation

```bash
pip install django-tasks-redis
```

## Quick Start

1. Add `django_tasks_redis` to your `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    # ...
    "django_tasks_redis",
]
```

2. Configure the task backend in your Django settings:

```python
TASKS = {
    "default": {
        "BACKEND": "django_tasks_redis.RedisTaskBackend",
        "QUEUES": [],  # Empty list = allow all queue names
        "OPTIONS": {
            "REDIS_URL": "redis://localhost:6379/0",
        },
    },
}
```

> **Note**: `QUEUES` controls which queue names are allowed. If omitted, only `"default"` queue is allowed. Set `QUEUES: []` (empty list) to allow all queue names, or specify explicit names like `["default", "emails"]`.

3. Define a task:

```python
from django.tasks import task


@task
def send_email(to: str, subject: str, body: str):
    # Send email logic here
    pass
```

4. Enqueue the task:

```python
result = send_email.enqueue("user@example.com", "Hello", "World")
print(f"Task ID: {result.id}")
```

5. Run the worker:

```bash
python manage.py run_redis_tasks
```

## Configuration Options

```python
TASKS = {
    "default": {
        "BACKEND": "django_tasks_redis.RedisTaskBackend",
        "QUEUES": [],  # Empty list = allow all queue names
        "OPTIONS": {
            # Connection settings (use URL or individual settings)
            "REDIS_URL": "redis://localhost:6379/0",
            # Or use individual settings:
            # "REDIS_HOST": "localhost",
            # "REDIS_PORT": 6379,
            # "REDIS_DB": 0,
            # "REDIS_PASSWORD": None,
            # "REDIS_SSL": False,
            # "REDIS_SSL_CA_CERTS": "/path/to/ca.pem",  # CA cert path for TLS (self-signed CA). Requires REDIS_SSL=True (or a rediss:// URL).
            # Connection robustness (passed to redis-py)
            "REDIS_SOCKET_CONNECT_TIMEOUT": 5,  # Seconds to wait for a connection
            "REDIS_HEALTH_CHECK_INTERVAL": 30,  # Seconds before an idle connection is pinged
            # "REDIS_SOCKET_TIMEOUT": None,  # Seconds; must exceed REDIS_BLOCK_TIMEOUT (see below)
            # "REDIS_SOCKET_KEEPALIVE": None,  # Enable TCP keepalive
            # Behavior settings
            "REDIS_RESULT_TTL": 2592000,  # Result retention period (seconds), default 30 days
            "REDIS_COMPLETED_TASK_TTL": 2592000,  # Retention once finished, defaults to REDIS_RESULT_TTL
            "REDIS_KEY_PREFIX": "django_tasks",  # Redis key prefix
            "REDIS_CONSUMER_GROUP": "django_tasks_workers",  # Consumer group name
            "REDIS_CLAIM_TIMEOUT": 300,  # Stale message claim timeout (seconds)
            "REDIS_BLOCK_TIMEOUT": 5000,  # XREADGROUP block timeout (milliseconds)
            "REDIS_MAX_DELIVERIES": 5,  # Give up on a task started this many times without finishing (0 = never)
            "REDIS_SCAN_BATCH_SIZE": 500,  # Tasks read per round trip when walking the index
        },
    },
}
```

### Connection robustness

Without any timeout a worker blocked on a connection Redis no longer answers —
a restart, a network partition — waits as long as the kernel allows, and looks
idle while it does. `REDIS_SOCKET_CONNECT_TIMEOUT` bounds establishing a
connection and `REDIS_HEALTH_CHECK_INTERVAL` makes redis-py check an idle one
before reusing it.

`REDIS_SOCKET_TIMEOUT` has no bound by default, on purpose: it also applies to
the worker's blocking reads, so a value at or below `REDIS_BLOCK_TIMEOUT` makes
every idle wait raise `TimeoutError`. Set it above that, in seconds, or leave it
alone; the backend logs a warning at startup when the two do not fit. It is
passed to redis-py as `None` rather than left out, because redis-py 8 otherwise
applies a 5 second default of its own, the same length as the default block.

Setting any of the other options to `None` drops the default and lets redis-py
apply its own — useful when the value above is wrong for your environment.

Note that redis-py's default `Retry` multiplies the connect timeout. With
`REDIS_SOCKET_CONNECT_TIMEOUT=2` against an unroutable host, the default retry
policy in redis-py 8.x stretches a 2.0 s wait to roughly 25 s, so the actual
wait can be much longer than the configured value.

### Delivery guarantees

Tasks are delivered **at least once**. A worker that dies leaves its message
pending; another worker reclaims it after `REDIS_CLAIM_TIMEOUT` and runs it
again. Two consequences are worth planning for:

- **`REDIS_CLAIM_TIMEOUT` must be longer than your longest task.** A task still
  running after the timeout looks exactly like a dead worker at the queue
  level, and will be reclaimed and executed a second time.
- **Task functions should be idempotent.** A worker can die between finishing
  the work and recording the result, in which case the task runs again.

A task that has been started `REDIS_MAX_DELIVERIES` times without finishing is
given up on the next time its message is reclaimed: it is marked FAILED with a
`TaskAbandoned` error, so it shows up in the admin instead of being retried
forever. Only starts count, not deliveries, so a message a worker held without
running it does not use up an attempt. A task that already finished keeps its
result. Set it to `0` to disable the cap.

## Management Commands

### run_redis_tasks

Start a worker to process tasks:

```bash
python manage.py run_redis_tasks [options]

Options:
  --queue QUEUE_NAME        Process only tasks from specific queue
  --backend BACKEND_NAME    Backend name (default: default)
  --continuous              Continuous mode (don't exit)
  --interval SECONDS        Polling interval (default: 1)
  --max-tasks N             Maximum tasks to process (0=unlimited)
  --claim-interval SECS     Stale task claim interval (default: 60)
  --shutdown-timeout SECS   Maximum wait for the running task after SIGTERM/SIGINT
                            before forcing exit (0=wait indefinitely, default: 0)
  --no-graceful-shutdown    Do not install SIGTERM/SIGINT handlers
```

A worker handles one task at a time. Run several processes to process more,
each gets its own consumer in the group.

In `--continuous` mode the worker waits on the streams for up to
`REDIS_BLOCK_TIMEOUT` instead of polling, so `--interval` only applies when
that wait is disabled (`REDIS_BLOCK_TIMEOUT: 0`). The wait is taken in one
second steps, so a shutdown signal is noticed within about a second whatever
the block timeout. See [Graceful Shutdown](#graceful-shutdown).

### purge_completed_redis_tasks

Delete completed tasks:

```bash
python manage.py purge_completed_redis_tasks [options]

Options:
  --days N                Delete tasks completed N+ days ago
  --status STATUS         Target status (default: SUCCESSFUL,FAILED)
  --batch-size N          Tasks read per round trip (default: REDIS_SCAN_BATCH_SIZE)
  --dry-run               Only show count, don't delete
  --backend BACKEND_NAME  Backend name (default: default)
```

## Graceful Shutdown

When a worker is redeployed, the orchestrator (Kubernetes, Cloud Run, systemd,
Docker, supervisord, ...) sends `SIGTERM` and kills the process with `SIGKILL`
after a grace period. Without any handling, a task that happens to be running
at that moment is killed halfway through, and only runs again once the
stale-message sweep of another worker finds its message after
`REDIS_CLAIM_TIMEOUT`.

`run_redis_tasks` installs `SIGTERM` and `SIGINT` handlers by default:

1. On the first signal the worker stops fetching new tasks. A worker waiting
   on the streams stops waiting within about a second.
2. The task currently being executed keeps running until it finishes and its
   result is written to Redis, and its message is acknowledged.
3. The worker removes its consumer from the group and exits with status
   code 0.

```console
$ python manage.py run_redis_tasks --continuous
Starting Redis task worker: worker-1-3f2a9c11
  Backend: default
  Continuous: True
  Graceful shutdown: enabled (timeout=unlimited)
Processed task 1e2d5c0a: SUCCESSFUL
^C
Received SIGINT: no new tasks will be started. Waiting for the running task to finish (send the signal again to force exit).
Processed task 7b1f09d3: SUCCESSFUL

Shutdown complete (no task was interrupted).
Worker stopped. Processed 2 task(s).
```

### Shutdown timeout

By default the worker waits as long as the running task needs. Use
`--shutdown-timeout` to put an upper bound on it, so the process exits on its
own terms instead of being `SIGKILL`ed by the platform:

```bash
python manage.py run_redis_tasks --continuous --shutdown-timeout 25
```

If the task is still running when the timeout expires, the process exits
immediately with status code 1. The task is not lost: its message stays
pending and its hash `RUNNING`, and another worker reclaims and runs it again
after `REDIS_CLAIM_TIMEOUT`, so this is one of the cases the
[at-least-once guarantee](#delivery-guarantees) covers. Set the timeout to a
value slightly below the platform's termination grace period, and keep the
grace period longer than your longest task whenever possible.

Sending the signal a second time (for example pressing Ctrl-C twice) also
forces an immediate exit, with the same consequences.

### Cooperating from inside a task

Long running tasks can check whether a shutdown was requested and stop early,
so the worker does not have to wait for the whole task to complete:

```python
from django.tasks import task

from django_tasks_redis import is_shutdown_requested


@task
def import_rows(row_ids):
    processed = []
    for row_id in row_ids:
        if is_shutdown_requested():
            # Requeue the remaining work and return early
            import_rows.enqueue([i for i in row_ids if i not in processed])
            break
        handle(row_id)
        processed.append(row_id)
    return len(processed)
```

`is_shutdown_requested()` returns `False` when no worker with graceful shutdown
is active, so tasks using it stay safe to call from a web request, a test, or
the HTTP endpoints.

### Deployment examples

**Kubernetes** - set `terminationGracePeriodSeconds` longer than the worker's
shutdown timeout:

```yaml
spec:
  terminationGracePeriodSeconds: 60
  containers:
    - name: worker
      command:
        - python
        - manage.py
        - run_redis_tasks
        - --continuous
        - --shutdown-timeout=50
```

**systemd** - `TimeoutStopSec` controls how long systemd waits before
`SIGKILL`:

```ini
[Service]
ExecStart=/srv/app/venv/bin/python manage.py run_redis_tasks --continuous --shutdown-timeout=50
KillSignal=SIGTERM
TimeoutStopSec=60
Restart=always
```

**Docker / Docker Compose** - `docker stop` sends `SIGTERM` and waits for
`--time` (10 seconds by default):

```yaml
services:
  worker:
    command: python manage.py run_redis_tasks --continuous --shutdown-timeout=25
    stop_grace_period: 30s
```

Make sure the worker is PID 1 or that the signal reaches it (use the exec form
of `CMD`, or an init such as `tini`, rather than wrapping the command in a
shell script that swallows signals).

### Using it in your own worker loop

The shutdown handling is available as a public API, for custom worker loops:

```python
from django_tasks_redis import GracefulShutdown, executor

with GracefulShutdown(timeout=50) as shutdown:
    while not shutdown.is_set():
        results = executor.process_tasks(max_tasks=10, stop_event=shutdown)
        if not results and shutdown.wait(5):  # interruptible sleep
            break
```

| API | Description |
|-----|-------------|
| `GracefulShutdown(signals=None, timeout=0, on_signal=None, force_on_repeat=True)` | Context manager that installs the signal handlers |
| `shutdown.is_set()` | True once a shutdown has been requested |
| `shutdown.wait(seconds)` | Sleep, returning early (True) when a shutdown is requested |
| `shutdown.set()` | Request a shutdown programmatically |
| `executor.process_tasks(..., stop_event=...)` | Stop starting new tasks once the event is set |
| `broker.receive(..., wait_seconds=...)` | Stops waiting early while the active `GracefulShutdown` is set |
| `is_shutdown_requested()` | True if the active worker was asked to shut down |

The same API, with the same names, is in django-database-task.

## Django Admin

The package provides Django Admin integration for viewing and managing tasks:

- View task list with status, priority, queue
- Search a task by id
- Run selected tasks (requires `run_redistask`)
- Retry failed tasks (requires `run_redistask`)
- Delete tasks (requires `delete_redistask`)

The admin reads the `default` backend.

### Permissions

Tasks live in Redis, so `RedisTask` is an unmanaged model with no table. It
still takes a `migrate` run for its permissions to be created, after which they
are granted like any other model's:

| Permission | Grants |
| --- | --- |
| `view_redistask` | Read the task list and a task's detail page |
| `run_redistask` | Run and retry tasks |
| `delete_redistask` | Delete tasks from Redis |

Tasks cannot be added or edited through the admin, so no `add` or `change`
permission exists.

## HTTP Endpoints

Include the URLs in your project:

```python
from django.urls import include, path

urlpatterns = [
    # ...
    path("tasks/", include("django_tasks_redis.urls")),
]
```

Available endpoints:

- `POST /tasks/run/` - Process multiple tasks
- `POST /tasks/run-one/` - Process a single task
- `POST /tasks/execute/<task_id>/` - Execute specific task by ID
- `GET /tasks/status/<task_id>/` - Get task status
- `POST /tasks/purge/` - Purge completed tasks

These endpoints run tasks, expose their arguments and results, and delete task
history, so they answer `403` until the backend says how to authenticate them.
Override `get_auth_handler()` to open them. The handler returns `None` to let
the request through, or a response to refuse it:

```python
from django.conf import settings
from django.http import JsonResponse

from django_tasks_redis.backends import RedisTaskBackend


class MyTaskBackend(RedisTaskBackend):
    def get_auth_handler(self):
        def handler(request):
            if request.headers.get("X-Task-Token") != settings.TASK_ENDPOINT_TOKEN:
                return JsonResponse({"error": "Forbidden"}, status=403)
            return None

        return handler
```

Then point `BACKEND` at `myapp.backends.MyTaskBackend`. The endpoints are
`csrf_exempt`, so the handler is the only thing standing between the caller and
task execution: authenticate on something the caller has to prove, not on
anything the request can claim about itself.

`POST /tasks/run/` drains the whole queue in the request by default; pass
`max_tasks` to bound it.

## Public API

The `executor` module provides functions for programmatic task management:

```python
from django_tasks_redis import executor

# Process tasks
result = executor.process_one_task(queue_name="default")
results = executor.process_tasks(max_tasks=10)

# Execute specific task
result = executor.run_task_by_id(task_id, allow_retry=True)

# Get pending task count
count = executor.get_pending_task_count()

# Purge completed tasks
deleted = executor.purge_completed_tasks(days=7)
```

### The stream broker

The functions above are thin wrappers over the backend's broker, which is
where reading, acknowledging and reclaiming messages live. It has the shape of
a pull broker in django-database-task, so a worker loop written against one
package reads the same against the other:

```python
from django.tasks import task_backends

backend = task_backends["default"]
broker = backend.broker  # a django_tasks_redis.brokers.RedisStreamsBroker

for message in broker.receive(
    queue_name="default", wait_seconds=5, worker_id=worker_id
):
    backend.run_task(message.task_id, worker_id=worker_id)
    broker.ack(message)
```

| Method | What it does on a Redis stream |
|--------|--------------------------------|
| `receive(queue_name=None, max_messages=1, wait_seconds=0, worker_id=None)` | `XREADGROUP` as the consumer `worker_id`: the messages it already holds first, then new ones in priority order. Messages whose task is no longer `READY`, or whose task is gone, are acknowledged inside the call and not returned |
| `ack(message)` | `XACK` and `XDEL`. Until it is called the message stays pending for the consumer |
| `nack(message)` | Nothing. A pending entry is what a stream has instead of redelivery: the same consumer is served it again, or another worker takes it over once it has been idle for `REDIS_CLAIM_TIMEOUT` |
| `claim_stale_messages(worker_id, claim_timeout=None, max_deliveries=None)` | `XPENDING` and `XCLAIM`: take over what a dead consumer left, hand a task it left `RUNNING` back as `READY`, and give up on one started `REDIS_MAX_DELIVERIES` times. Consumers idle for the timeout that hold nothing are removed from the group |
| `remove_consumer(worker_id)` | `XGROUP DELCONSUMER` on every stream, for a worker on its way out. A consumer that still holds pending messages is kept for the sweep |

`worker_id` is the consumer name in the group, so it has to be the id the
worker keeps using: a message received as one consumer is only served again
to that consumer, or to whoever reclaims it.

There is no `notify()` step, unlike the brokers in django-database-task: the
backend writes to the stream when it enqueues, so the stream is the queue
rather than a notification about one. `broker_class` on the backend names
the class to build; a subclass of `RedisStreamsBroker` can change how any of
this is done.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for how to set up a development environment and what a pull request needs.

## License

MIT License

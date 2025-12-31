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
- Crash recovery with automatic task reclaim
- Django Admin integration for task monitoring and management
- HTTP endpoints for external triggers (webhooks, Cloud Scheduler, etc.)

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
        "OPTIONS": {
            "REDIS_URL": "redis://localhost:6379/0",
        },
    },
}
```

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

            # Behavior settings
            "REDIS_RESULT_TTL": 604800,  # Result retention period (seconds), default 7 days
            "REDIS_KEY_PREFIX": "django_tasks",  # Redis key prefix
            "REDIS_CONSUMER_GROUP": "django_tasks_workers",  # Consumer group name
            "REDIS_CLAIM_TIMEOUT": 300,  # Stale message claim timeout (seconds)
            "REDIS_BLOCK_TIMEOUT": 5000,  # XREADGROUP block timeout (milliseconds)
        },
    },
}
```

## Management Commands

### run_redis_tasks

Start a worker to process tasks:

```bash
python manage.py run_redis_tasks [options]

Options:
  --queue QUEUE_NAME      Process only tasks from specific queue
  --backend BACKEND_NAME  Backend name (default: default)
  --continuous            Continuous mode (don't exit)
  --interval SECONDS      Polling interval (default: 1)
  --max-tasks N           Maximum tasks to process (0=unlimited)
  --workers N             Number of worker threads (default: 1)
  --claim-interval SECS   Stale task claim interval (default: 60)
```

### purge_completed_redis_tasks

Delete completed tasks:

```bash
python manage.py purge_completed_redis_tasks [options]

Options:
  --days N                Delete tasks completed N+ days ago
  --status STATUS         Target status (default: SUCCESSFUL,FAILED)
  --batch-size N          Batch delete size (default: 1000)
  --dry-run               Only show count, don't delete
  --backend BACKEND_NAME  Backend name (default: default)
```

## Django Admin

The package provides Django Admin integration for viewing and managing tasks:

- View task list with status, priority, queue
- Filter by status, queue, backend
- Run selected tasks
- Retry failed tasks

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

## License

MIT License

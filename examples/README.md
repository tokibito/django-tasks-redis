# Django Tasks Redis - Example Project

This is an example Django project demonstrating the usage of `django-tasks-redis`.

## Prerequisites

- Python 3.12+
- Redis server running on `localhost:6379`
- Django 6.0+

## Setup

1. Install the package in development mode:

```bash
cd /path/to/django-tasks-redis
pip install -e ".[dev]"
```

2. Navigate to the examples directory:

```bash
cd examples
```

3. Run migrations:

```bash
python manage.py migrate
```

4. Create a superuser (optional, for admin access):

```bash
python manage.py createsuperuser
```

## Running the Demo

### Start the Development Server

```bash
python manage.py runserver
```

Visit http://localhost:8000/ to see the demo interface.

### Start the Task Worker

In a separate terminal:

```bash
python manage.py run_redis_tasks --continuous
```

This will start a worker that continuously processes tasks from the queue.

## Demo Features

### Web Interface (http://localhost:8000/)

- **Queue Statistics**: View real-time task counts by status
- **Enqueue Tasks**: Submit various types of tasks:
  - `add_numbers`: Simple arithmetic task
  - `slow_calculation`: Task that takes time to complete
  - `failing_task`: Task that always fails (for error handling demo)
  - `send_email`: Task in a specific queue

- **Check Task Status**: View detailed task results by ID

### Django Admin (http://localhost:8000/admin/)

Login with your superuser credentials to:
- View all tasks
- Filter by status and queue
- Run selected tasks
- Retry failed tasks

### API Endpoints

The package also exposes HTTP endpoints at `/tasks/`:

- `POST /tasks/run/` - Process multiple tasks
- `POST /tasks/run-one/` - Process a single task
- `POST /tasks/execute/<task_id>/` - Execute specific task
- `GET /tasks/status/<task_id>/` - Get task status
- `POST /tasks/purge/` - Purge completed tasks

## Management Commands

### run_redis_tasks

Start a worker to process tasks:

```bash
# Process tasks once and exit
python manage.py run_redis_tasks

# Run continuously
python manage.py run_redis_tasks --continuous

# Process specific queue
python manage.py run_redis_tasks --queue emails --continuous

# Limit number of tasks
python manage.py run_redis_tasks --max-tasks 10
```

### purge_completed_redis_tasks

Clean up old completed tasks:

```bash
# Delete tasks completed more than 7 days ago
python manage.py purge_completed_redis_tasks

# Delete tasks completed more than 1 day ago
python manage.py purge_completed_redis_tasks --days 1

# Dry run (show count without deleting)
python manage.py purge_completed_redis_tasks --dry-run

# Delete only successful tasks
python manage.py purge_completed_redis_tasks --status SUCCESSFUL
```

## Task Examples

See `demo_app/tasks.py` for task definitions:

```python
from django.tasks import task

@task
def add_numbers(x, y):
    return x + y

@task(priority=10)
def high_priority_task(message):
    return f"High priority: {message}"

@task(queue_name="emails")
def send_email(to, subject, body):
    return {"to": to, "subject": subject, "status": "sent"}

@task(takes_context=True)
def task_with_context(context, message):
    return {
        "message": message,
        "attempt": context.attempt,
    }
```

## Programmatic Usage

```python
from demo_app.tasks import add_numbers, send_email
from django_tasks_redis import executor

# Enqueue a task
result = add_numbers.enqueue(5, 3)
print(f"Task ID: {result.id}")

# Check status
task_data = executor.get_task_by_id(result.id)
print(f"Status: {task_data['status']}")

# Process tasks programmatically
processed = executor.process_tasks(max_tasks=10)
print(f"Processed {len(processed)} tasks")

# Get queue statistics
stats = executor.get_queue_stats()
print(f"Pending: {stats['pending_count']}")
```

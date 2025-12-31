"""
Test task definitions.
"""

from django.tasks import task


@task
def simple_task(x, y):
    """A simple task that adds two numbers."""
    return x + y


@task
def failing_task():
    """A task that always fails."""
    raise ValueError("This task always fails")


@task
def slow_task(seconds=1):
    """A task that takes time to complete."""
    import time

    time.sleep(seconds)
    return f"Slept for {seconds} seconds"


@task(priority=10)
def high_priority_task():
    """A high priority task."""
    return "high priority"


@task(priority=-10)
def low_priority_task():
    """A low priority task."""
    return "low priority"


@task(queue_name="emails")
def email_task(to, subject, body):
    """A task in a specific queue."""
    return {"to": to, "subject": subject, "body": body}


@task(takes_context=True)
def context_task(context, message):
    """A task that takes context."""
    return {
        "message": message,
        "attempt": context.attempt,
        "task_id": context.task_result.id,
    }

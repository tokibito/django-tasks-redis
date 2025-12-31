"""
Demo tasks for the example application.
"""

import time

from django.tasks import task


@task
def add_numbers(x, y):
    """Add two numbers together."""
    return x + y


@task
def slow_calculation(seconds=2):
    """A slow task that simulates work."""
    time.sleep(seconds)
    return f"Completed after {seconds} seconds"


@task
def failing_task():
    """A task that always fails (for testing error handling)."""
    raise ValueError("This task intentionally fails")


@task(priority=10)
def high_priority_task(message):
    """A high priority task."""
    return f"High priority: {message}"


@task(priority=-10)
def low_priority_task(message):
    """A low priority task."""
    return f"Low priority: {message}"


@task(queue_name="emails")
def send_email(to, subject, body):
    """Simulate sending an email."""
    # In a real application, this would send an actual email
    return {
        "to": to,
        "subject": subject,
        "body": body,
        "status": "sent",
    }


@task(takes_context=True)
def task_with_context(context, message):
    """A task that uses context information."""
    return {
        "message": message,
        "task_id": context.task_result.id,
        "attempt": context.attempt,
    }

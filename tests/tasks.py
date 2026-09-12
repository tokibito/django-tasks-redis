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


# Set by record_sigterm_handler_task, read by the command tests.
recorded_sigterm_handler = None


@task
def shutdown_signal_task(signal_name="SIGTERM"):
    """Task that sends a shutdown signal to its own process while running."""
    import os
    import signal as signal_module

    os.kill(os.getpid(), getattr(signal_module, signal_name))
    return f"sent {signal_name}"


@task
def record_sigterm_handler_task():
    """Task that records the SIGTERM handler installed while it runs."""
    import signal as signal_module

    from tests import tasks

    tasks.recorded_sigterm_handler = signal_module.getsignal(signal_module.SIGTERM)
    return "recorded"


@task
def shutdown_aware_task(iterations=10):
    """Task that signals itself and then stops its loop cooperatively."""
    import os
    import signal as signal_module

    from django_tasks_redis import is_shutdown_requested

    completed = 0
    for i in range(iterations):
        if i == 1:
            os.kill(os.getpid(), signal_module.SIGTERM)
        if is_shutdown_requested():
            break
        completed += 1
    return completed

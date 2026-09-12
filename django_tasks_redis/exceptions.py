"""
Exceptions specific to django-tasks-redis.
"""


class TaskAbandoned(Exception):
    """
    The queue gave up on a task instead of delivering it again.

    Recorded on the task result when a message exceeds ``REDIS_MAX_DELIVERIES``
    delivery attempts, so a task that cannot run is visible as FAILED instead of
    being redelivered forever.
    """

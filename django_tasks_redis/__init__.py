"""
django-tasks-redis: A Redis/Valkey-backed task queue backend for Django 6.0's task framework.
"""

from .backends import RedisTaskBackend
from .shutdown import GracefulShutdown, get_active_shutdown, is_shutdown_requested

__version__ = "0.4.0"

_EXECUTOR_EXPORTS = (
    "get_pending_task_count",
    "get_queue_stats",
    "get_task_counts",
)


def __getattr__(name):
    """Lazy import to avoid AppRegistryNotReady errors."""
    if name in _EXECUTOR_EXPORTS:
        from . import executor

        return getattr(executor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "GracefulShutdown",
    "RedisTaskBackend",
    "get_active_shutdown",
    "get_pending_task_count",
    "get_queue_stats",
    "get_task_counts",
    "is_shutdown_requested",
]

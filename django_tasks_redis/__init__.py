"""
django-tasks-redis: A Redis/Valkey-backed task queue backend for Django 6.0's task framework.
"""

from .backends import RedisTaskBackend
from .shutdown import GracefulShutdown, get_active_shutdown, is_shutdown_requested

__version__ = "0.2.1"
__all__ = [
    "GracefulShutdown",
    "RedisTaskBackend",
    "get_active_shutdown",
    "is_shutdown_requested",
]

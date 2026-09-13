"""
django-tasks-redis: A Redis/Valkey-backed task queue backend for Django 6.0's task framework.
"""

from .auth import (
    AUTH_ENDPOINTS,
    BaseAuthHandler,
    HMACAuth,
    SharedSecretAuth,
    StaffOnlyAuth,
    build_signature,
)
from .backends import RedisTaskBackend
from .shutdown import GracefulShutdown, get_active_shutdown, is_shutdown_requested

__version__ = "0.3.0"
__all__ = [
    "AUTH_ENDPOINTS",
    "BaseAuthHandler",
    "GracefulShutdown",
    "HMACAuth",
    "RedisTaskBackend",
    "SharedSecretAuth",
    "StaffOnlyAuth",
    "build_signature",
    "get_active_shutdown",
    "is_shutdown_requested",
]

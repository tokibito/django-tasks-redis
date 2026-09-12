"""
django-tasks-redis: A Redis/Valkey-backed task queue backend for Django 6.0's task framework.
"""

from .backends import RedisTaskBackend

__version__ = "0.2.0"
__all__ = ["RedisTaskBackend"]

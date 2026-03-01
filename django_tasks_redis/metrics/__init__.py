"""
Prometheus metrics integration for django-tasks-redis.

This module provides optional Prometheus metrics collection for monitoring
task queue performance. Requires the 'prometheus' extra:

    pip install django-tasks-redis[prometheus]

Enable metrics in your Django settings:

    TASKS = {
        "default": {
            "BACKEND": "django_tasks_redis.RedisTaskBackend",
            "OPTIONS": {
                "REDIS_URL": "redis://localhost:6379/0",
                "ENABLE_METRICS": True,
            },
        },
    }
"""

try:
    from prometheus_client import REGISTRY

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    REGISTRY = None

from .collectors import TaskMetricsCollector

__all__ = ["TaskMetricsCollector", "PROMETHEUS_AVAILABLE", "REGISTRY"]

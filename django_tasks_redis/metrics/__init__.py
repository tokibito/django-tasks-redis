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
    from prometheus_client import REGISTRY, Histogram

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False
    REGISTRY = None
    Histogram = None

from .collectors import RedisTaskMetricsCollector, TaskMetricsCollector

# Lazy-initialized histogram (only created when metrics are enabled)
_task_duration_histogram = None


def get_task_duration_histogram():
    """
    Get or create the task duration histogram.

    This is lazily initialized to avoid creating metrics when ENABLE_METRICS is False.
    Thread-safe due to Python's GIL.
    """
    global _task_duration_histogram

    if _task_duration_histogram is None and PROMETHEUS_AVAILABLE:
        _task_duration_histogram = Histogram(
            "django_tasks_duration_seconds",
            "Duration of task execution in seconds",
            labelnames=["backend", "queue", "status"],
            buckets=(
                0.01,
                0.05,
                0.1,
                0.5,
                1.0,
                2.5,
                5.0,
                10.0,
                30.0,
                60.0,
                120.0,
                300.0,
                float("inf"),
            ),
        )

    return _task_duration_histogram


__all__ = [
    "RedisTaskMetricsCollector",
    "TaskMetricsCollector",
    "PROMETHEUS_AVAILABLE",
    "REGISTRY",
    "get_task_duration_histogram",
]

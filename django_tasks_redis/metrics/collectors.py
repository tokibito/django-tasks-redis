"""
Prometheus metric collectors for django-tasks-redis.
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django_tasks_redis.backends import RedisTaskBackend

try:
    from prometheus_client import Counter, Gauge, Histogram
except ImportError:
    # Graceful fallback if prometheus-client not installed
    Counter = None
    Gauge = None
    Histogram = None

logger = logging.getLogger("django_tasks_redis.metrics")

# Global metric instances (singleton pattern)
# This prevents ValueError when multiple backend instances are created
# (e.g., in multi-process Django applications like WSGI/ASGI servers)
_metrics_registry = {}


class TaskMetricsCollector:
    """
    Collects Prometheus metrics for Redis task backend.

    Metrics exposed:
    - django_tasks_enqueued_total: Counter for enqueued tasks
    - django_tasks_completed_total: Counter for completed tasks
    - django_tasks_failed_total: Counter for failed tasks
    - django_tasks_queue_length: Gauge for current queue length
    - django_tasks_running: Gauge for currently running tasks
    - django_tasks_duration_seconds: Histogram for task execution duration
    """

    def __init__(self, backend: "RedisTaskBackend"):
        """
        Initialize metrics collector.

        Args:
            backend: The RedisTaskBackend instance to monitor.
        """
        if Counter is None:
            raise ImportError(
                "prometheus-client is not installed. "
                "Install with: pip install django-tasks-redis[prometheus]"
            )

        self.backend = backend
        self.backend_name = backend.alias

        # Use singleton pattern to avoid duplicate metric registration
        # Metrics are global and shared across all backend instances
        if not _metrics_registry:
            # Counters
            _metrics_registry["tasks_enqueued"] = Counter(
                "django_tasks_enqueued_total",
                "Total number of tasks enqueued",
                ["backend", "queue", "priority"],
            )

            _metrics_registry["tasks_completed"] = Counter(
                "django_tasks_completed_total",
                "Total number of tasks completed successfully",
                ["backend", "queue"],
            )

            _metrics_registry["tasks_failed"] = Counter(
                "django_tasks_failed_total",
                "Total number of tasks that failed",
                ["backend", "queue"],
            )

            # Gauges
            _metrics_registry["queue_length"] = Gauge(
                "django_tasks_queue_length",
                "Current number of tasks in queue by status",
                ["backend", "queue", "status"],
            )

            _metrics_registry["tasks_running"] = Gauge(
                "django_tasks_running",
                "Number of currently running tasks",
                ["backend", "queue"],
            )

            # Histogram
            _metrics_registry["task_duration"] = Histogram(
                "django_tasks_duration_seconds",
                "Task execution duration in seconds",
                ["backend", "queue", "status"],
                buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
            )

            logger.info("Prometheus metrics registered globally")

        # Reference the global metrics
        self.tasks_enqueued = _metrics_registry["tasks_enqueued"]
        self.tasks_completed = _metrics_registry["tasks_completed"]
        self.tasks_failed = _metrics_registry["tasks_failed"]
        self.queue_length = _metrics_registry["queue_length"]
        self.tasks_running = _metrics_registry["tasks_running"]
        self.task_duration = _metrics_registry["task_duration"]

        logger.info(
            "TaskMetricsCollector initialized for backend: %s", self.backend_name
        )

    def record_task_enqueued(self, queue_name: str, priority: int):
        """
        Record a task being enqueued.

        Args:
            queue_name: Name of the queue.
            priority: Task priority level.
        """
        self.tasks_enqueued.labels(
            backend=self.backend_name,
            queue=queue_name,
            priority=str(priority),
        ).inc()

    def record_task_completed(self, queue_name: str):
        """
        Record a task completing successfully.

        Args:
            queue_name: Name of the queue.
        """
        self.tasks_completed.labels(
            backend=self.backend_name,
            queue=queue_name,
        ).inc()

    def record_task_failed(self, queue_name: str):
        """
        Record a task failing.

        Args:
            queue_name: Name of the queue.
        """
        self.tasks_failed.labels(
            backend=self.backend_name,
            queue=queue_name,
        ).inc()

    def record_task_duration(self, queue_name: str, status: str, duration_seconds: float):
        """
        Record task execution duration.

        Args:
            queue_name: Name of the queue.
            status: Task final status (SUCCESSFUL or FAILED).
            duration_seconds: Duration in seconds.
        """
        self.task_duration.labels(
            backend=self.backend_name,
            queue=queue_name,
            status=status,
        ).observe(duration_seconds)

    def update_queue_lengths(self):
        """
        Update queue length gauges by fetching current status counts from Redis.

        This should be called periodically to update the gauge metrics.
        """
        try:
            # Get status counts for all queues
            status_counts = self.backend.get_status_counts()

            # Update gauges for each status
            for status, count in status_counts.items():
                self.queue_length.labels(
                    backend=self.backend_name,
                    queue="all",  # Aggregate across all queues
                    status=status,
                ).set(count)

        except Exception as e:
            logger.error("Failed to update queue length metrics: %s", e)

    def update_running_tasks(self, queue_name: str, count: int):
        """
        Update the number of running tasks for a queue.

        Args:
            queue_name: Name of the queue.
            count: Number of currently running tasks.
        """
        self.tasks_running.labels(
            backend=self.backend_name,
            queue=queue_name,
        ).set(count)

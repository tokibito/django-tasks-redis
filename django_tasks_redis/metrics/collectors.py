"""
Prometheus metric collectors for django-tasks-redis.

This collector queries Redis directly when Prometheus scrapes the metrics endpoint,
ensuring accurate metrics even when the web server process doesn't handle task execution.
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django_tasks_redis.backends import RedisTaskBackend

try:
    from prometheus_client.core import GaugeMetricFamily
    from prometheus_client.registry import Collector
except ImportError:
    # Graceful fallback if prometheus-client not installed
    Collector = None
    GaugeMetricFamily = None

logger = logging.getLogger("django_tasks_redis.metrics")


class RedisTaskMetricsCollector(Collector):
    """
    Custom Prometheus collector that queries Redis directly at scrape time.

    This approach ensures metrics are accurate regardless of which process
    is serving the metrics endpoint, since all data comes from Redis.

    Metrics exposed:
    - django_tasks_queue_length: Gauge for current queue length by status
    """

    def __init__(self, backend: "RedisTaskBackend"):
        """
        Initialize metrics collector.

        Args:
            backend: The RedisTaskBackend instance to monitor.
        """
        if Collector is None:
            raise ImportError(
                "prometheus-client is not installed. "
                "Install with: pip install django-tasks-redis[prometheus]"
            )

        self.backend = backend
        self.backend_name = backend.alias

        logger.info(
            "RedisTaskMetricsCollector initialized for backend: %s", self.backend_name
        )

    def collect(self):
        """
        Called by Prometheus client when metrics are scraped.

        Queries Redis directly to get current queue statistics.

        Yields:
            Prometheus metric families with current values from Redis.
        """
        try:
            # Get status counts from Redis
            status_counts = self.backend.get_status_counts()

            # Create gauge metric family for queue length
            queue_length = GaugeMetricFamily(
                "django_tasks_queue_length",
                "Current number of tasks in queue by status",
                labels=["backend", "status"],
            )

            # Add samples for each status
            for status, count in status_counts.items():
                queue_length.add_metric(
                    labels=[self.backend_name, status],
                    value=count,
                )

            yield queue_length

            logger.debug(
                "Collected metrics for backend %s: %s",
                self.backend_name,
                status_counts,
            )

        except Exception as e:
            logger.error("Failed to collect metrics from Redis: %s", e, exc_info=True)
            # Don't yield any metrics on error - Prometheus will use stale data


# Backward compatibility: keep old class name but log deprecation warning
class TaskMetricsCollector(RedisTaskMetricsCollector):
    """
    Deprecated: Use RedisTaskMetricsCollector instead.

    This class is kept for backward compatibility but will be removed in a future version.
    """

    def __init__(self, backend: "RedisTaskBackend"):
        logger.warning(
            "TaskMetricsCollector is deprecated. Use RedisTaskMetricsCollector instead."
        )
        super().__init__(backend)

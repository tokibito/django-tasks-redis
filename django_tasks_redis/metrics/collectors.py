"""
Prometheus metric collectors for django-tasks-redis.

This collector queries Redis directly when Prometheus scrapes the metrics endpoint,
ensuring accurate metrics even when the web server process doesn't handle task execution.
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from django_tasks_redis.backends import RedisTaskBackend

try:
    from prometheus_client.core import GaugeMetricFamily
    from prometheus_client.registry import Collector

    PROMETHEUS_INSTALLED = True
except ImportError:
    # Graceful fallback if prometheus-client not installed. `Collector`
    # must stay a valid base class — `class X(None)` raises TypeError at
    # import time, which would crash django.setup() for deployments with
    # ENABLE_METRICS=True but no prometheus-client, bypassing the guards.
    Collector = object
    GaugeMetricFamily = None
    PROMETHEUS_INSTALLED = False

logger = logging.getLogger("django_tasks_redis.metrics")


class RedisTaskMetricsCollector(Collector):
    """
    Custom Prometheus collector that queries Redis directly at scrape time.

    This approach ensures metrics are accurate regardless of which process
    is serving the metrics endpoint, since all data comes from Redis.

    Metrics exposed:
    - django_tasks_queue_length: Gauge for current queue length by status
    - django_tasks_queue_oldest_ready_age_seconds: Age of oldest READY task in seconds
    - django_tasks_queue_newest_ready_age_seconds: Age of newest READY task in seconds
    - django_tasks_duration_seconds: Histogram of task execution duration (recorded by worker)
    """

    def __init__(self, backend: "RedisTaskBackend"):
        """
        Initialize metrics collector.

        Args:
            backend: The RedisTaskBackend instance to monitor.
        """
        if not PROMETHEUS_INSTALLED:
            raise ImportError(
                "prometheus-client is not installed. "
                "Install with: pip install django-tasks-redis[prometheus]"
            )

        self.backend = backend
        self.backend_name = backend.alias

        logger.info(
            "RedisTaskMetricsCollector initialized for backend: %s", self.backend_name
        )

    def _metric_families(self):
        """Build the (empty) metric families this collector exposes."""
        queue_length = GaugeMetricFamily(
            "django_tasks_queue_length",
            "Current number of tasks in queue by status",
            labels=["backend", "status"],
        )
        oldest_age = GaugeMetricFamily(
            "django_tasks_queue_oldest_ready_age_seconds",
            "Age of the oldest READY task in seconds",
            labels=["backend"],
        )
        newest_age = GaugeMetricFamily(
            "django_tasks_queue_newest_ready_age_seconds",
            "Age of the newest READY task in seconds",
            labels=["backend"],
        )
        return queue_length, oldest_age, newest_age

    def describe(self):
        """
        Describe the metrics without collecting them.

        The default Prometheus REGISTRY has auto_describe enabled: without
        this method, REGISTRY.register() would call collect() — hitting
        Redis during backend construction, i.e. inside django.setup() for
        every process that defines a @task. Returning the (sample-less)
        families keeps duplicate registration detection working while
        guaranteeing registration never touches Redis.
        """
        return list(self._metric_families())

    def collect(self):
        """
        Called by Prometheus client when metrics are scraped.

        Queries Redis directly to get current queue statistics.

        Yields:
            Prometheus metric families with current values from Redis.
        """
        try:
            # Get status counts from Redis (O(1) when the status index is
            # built; falls back to a scan on unmigrated deployments)
            status_counts = self.backend.get_status_counts()

            queue_length, oldest_age, newest_age = self._metric_families()

            # Add samples for each status
            for status, count in status_counts.items():
                queue_length.add_metric(
                    labels=[self.backend_name, status],
                    value=count,
                )

            yield queue_length

            # Age metrics for READY tasks
            age_bounds = self._get_ready_age_bounds(status_counts)
            if age_bounds is not None:
                oldest, newest = age_bounds
                oldest_age.add_metric(labels=[self.backend_name], value=oldest)
                yield oldest_age
                newest_age.add_metric(labels=[self.backend_name], value=newest)
                yield newest_age

            logger.debug(
                "Collected metrics for backend %s: %s",
                self.backend_name,
                status_counts,
            )

        except Exception as e:
            logger.error("Failed to collect metrics from Redis: %s", e, exc_info=True)
            # Don't yield any metrics on error - Prometheus will use stale data

    # Cap for the legacy (index-less) age computation, so a scrape never
    # fetches an unbounded number of task hashes.
    LEGACY_AGE_FETCH_LIMIT = 1000

    def _get_ready_age_bounds(self, status_counts):
        """
        Get (oldest_age, newest_age) in seconds for READY tasks, or None.

        Uses the backend's READY status index when available (constant
        cost). On unmigrated deployments it falls back to fetching at most
        LEGACY_AGE_FETCH_LIMIT READY tasks; run the worker once or the
        rebuild_redis_task_index management command to build the index.
        """
        if self.backend.has_status_index():
            return self.backend.get_ready_age_bounds()

        ready_count = status_counts.get("READY", 0)
        if ready_count <= 0:
            return None

        if ready_count > self.LEGACY_AGE_FETCH_LIMIT:
            logger.warning(
                "Status index not built; READY age metrics computed from the "
                "%d most recent READY tasks out of %d. Build the index with "
                "the rebuild_redis_task_index management command.",
                self.LEGACY_AGE_FETCH_LIMIT,
                ready_count,
            )

        ready_tasks, _ = self.backend.get_all_tasks(
            status="READY",
            limit=min(ready_count, self.LEGACY_AGE_FETCH_LIMIT),
        )
        if not ready_tasks:
            return None

        from django_tasks_redis.utils import deserialize_datetime

        now = datetime.now(UTC)
        ages = []
        for task in ready_tasks:
            enqueued_at_str = task.get("enqueued_at", "")
            if not enqueued_at_str:
                continue
            try:
                enqueued_at = deserialize_datetime(enqueued_at_str)
            except Exception as e:
                logger.warning("Failed to parse enqueued_at for task: %s", e)
                continue
            if enqueued_at:
                if enqueued_at.tzinfo is None:
                    enqueued_at = enqueued_at.replace(tzinfo=UTC)
                ages.append((now - enqueued_at).total_seconds())

        if not ages:
            return None
        return max(ages), min(ages)


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

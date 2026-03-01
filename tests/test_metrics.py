"""
Tests for Prometheus metrics integration.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest
from django.tasks.base import TaskResultStatus


def _prometheus_available():
    """Check if prometheus-client is available."""
    try:
        import prometheus_client  # noqa: F401

        return True
    except ImportError:
        return False


class TestMetricsIntegration:
    """Test that metrics integration works correctly."""

    @pytest.mark.django_db
    def test_metrics_disabled_by_default(self):
        """Test that metrics are disabled by default."""
        from django_tasks_redis.backends import RedisTaskBackend

        backend = RedisTaskBackend(
            alias="default",
            params={
                "QUEUES": [],
                "OPTIONS": {
                    "REDIS_URL": "redis://localhost:6379/0",
                },
            },
        )

        # Metrics collector should not be created when disabled
        assert (
            not hasattr(backend, "_metrics_collector")
            or backend._metrics_collector is None
        )

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    @pytest.mark.django_db
    def test_metrics_enabled_when_configured(self):
        """Test that metrics collector is registered when configured."""
        from django_tasks_redis.backends import RedisTaskBackend

        with patch("django_tasks_redis.backends.logger") as mock_logger:
            _ = RedisTaskBackend(
                alias="test_metrics_enabled",
                params={
                    "QUEUES": [],
                    "OPTIONS": {
                        "REDIS_URL": "redis://localhost:6379/0",
                        "ENABLE_METRICS": True,
                    },
                },
            )

            # Should log info message when successfully registered
            # or debug if already registered (from duplicate metric names)
            assert mock_logger.info.called or mock_logger.debug.called


class TestRedisTaskMetricsCollector:
    """Test RedisTaskMetricsCollector functionality."""

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_collector_initialization(self):
        """Test that collector initializes correctly."""
        from django_tasks_redis.metrics.collectors import RedisTaskMetricsCollector

        mock_backend = Mock()
        mock_backend.alias = "test_backend"

        collector = RedisTaskMetricsCollector(mock_backend)

        assert collector.backend == mock_backend
        assert collector.backend_name == "test_backend"

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_collect_queries_redis(self):
        """Test that collect() queries Redis for current status."""
        from django_tasks_redis.metrics.collectors import RedisTaskMetricsCollector

        mock_backend = Mock()
        mock_backend.alias = "test"
        mock_backend.get_status_counts.return_value = {
            TaskResultStatus.READY: 10,
            TaskResultStatus.RUNNING: 2,
            TaskResultStatus.SUCCESSFUL: 100,
            TaskResultStatus.FAILED: 5,
        }

        collector = RedisTaskMetricsCollector(mock_backend)

        # Call collect (this is what Prometheus does when scraping)
        metrics = list(collector.collect())

        # Verify get_status_counts was called
        mock_backend.get_status_counts.assert_called_once()

        # Verify we got metric families back
        assert len(metrics) == 1
        assert metrics[0].name == "django_tasks_queue_length"

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_collect_handles_errors(self):
        """Test that collect() handles Redis errors gracefully."""
        from django_tasks_redis.metrics.collectors import RedisTaskMetricsCollector

        mock_backend = Mock()
        mock_backend.alias = "test"
        mock_backend.get_status_counts.side_effect = Exception("Redis error")

        collector = RedisTaskMetricsCollector(mock_backend)

        # Call collect - should not raise exception
        metrics = list(collector.collect())

        # Should return empty list on error
        assert len(metrics) == 0

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_collect_with_ready_tasks_age_metrics(self):
        """Test that collect() includes age metrics for READY tasks."""
        from django_tasks_redis.metrics.collectors import RedisTaskMetricsCollector
        from django_tasks_redis.utils import serialize_datetime

        mock_backend = Mock()
        mock_backend.alias = "test"
        mock_backend.get_status_counts.return_value = {
            TaskResultStatus.READY: 3,
            TaskResultStatus.RUNNING: 1,
        }

        # Create mock tasks with different ages
        now = datetime.now(timezone.utc)
        oldest_task_time = now - timedelta(minutes=10)
        middle_task_time = now - timedelta(minutes=5)
        newest_task_time = now - timedelta(minutes=1)

        mock_backend.get_all_tasks.return_value = (
            [
                {"enqueued_at": serialize_datetime(oldest_task_time)},
                {"enqueued_at": serialize_datetime(middle_task_time)},
                {"enqueued_at": serialize_datetime(newest_task_time)},
            ],
            3,
        )

        collector = RedisTaskMetricsCollector(mock_backend)

        # Call collect
        metrics = list(collector.collect())

        # Verify get_all_tasks was called with READY status
        mock_backend.get_all_tasks.assert_called_once_with(
            status="READY",
            limit=3
        )

        # Should return 3 metrics: queue_length, oldest_age, newest_age
        assert len(metrics) == 3

        metric_names = {m.name for m in metrics}
        assert "django_tasks_queue_length" in metric_names
        assert "django_tasks_queue_oldest_ready_age_seconds" in metric_names
        assert "django_tasks_queue_newest_ready_age_seconds" in metric_names

        # Check the age metrics values
        oldest_metric = next(
            m for m in metrics
            if m.name == "django_tasks_queue_oldest_ready_age_seconds"
        )
        newest_metric = next(
            m for m in metrics
            if m.name == "django_tasks_queue_newest_ready_age_seconds"
        )

        # Get the metric values (samples are stored in the metric family)
        oldest_samples = list(oldest_metric.samples)
        newest_samples = list(newest_metric.samples)

        assert len(oldest_samples) == 1
        assert len(newest_samples) == 1

        # Oldest should be ~600 seconds (10 minutes)
        assert 590 <= oldest_samples[0].value <= 610

        # Newest should be ~60 seconds (1 minute)
        assert 50 <= newest_samples[0].value <= 70

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_collect_without_ready_tasks_no_age_metrics(self):
        """Test that collect() doesn't include age metrics when no READY tasks."""
        from django_tasks_redis.metrics.collectors import RedisTaskMetricsCollector

        mock_backend = Mock()
        mock_backend.alias = "test"
        mock_backend.get_status_counts.return_value = {
            TaskResultStatus.RUNNING: 5,
            TaskResultStatus.SUCCESSFUL: 10,
        }

        collector = RedisTaskMetricsCollector(mock_backend)

        # Call collect
        metrics = list(collector.collect())

        # Should only return queue_length metric, no age metrics
        assert len(metrics) == 1
        assert metrics[0].name == "django_tasks_queue_length"

        # get_all_tasks should not be called when no READY tasks
        mock_backend.get_all_tasks.assert_not_called()

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_collect_with_invalid_enqueued_at(self):
        """Test that collect() handles tasks with invalid enqueued_at gracefully."""
        from django_tasks_redis.metrics.collectors import RedisTaskMetricsCollector
        from django_tasks_redis.utils import serialize_datetime

        mock_backend = Mock()
        mock_backend.alias = "test"
        mock_backend.get_status_counts.return_value = {
            TaskResultStatus.READY: 3,
        }

        now = datetime.now(timezone.utc)
        valid_task_time = now - timedelta(minutes=5)

        # Mix of valid and invalid tasks
        mock_backend.get_all_tasks.return_value = (
            [
                {"enqueued_at": serialize_datetime(valid_task_time)},
                {"enqueued_at": "invalid-datetime"},
                {"enqueued_at": ""},
            ],
            3,
        )

        collector = RedisTaskMetricsCollector(mock_backend)

        # Call collect - should not raise exception
        metrics = list(collector.collect())

        # Should still return metrics based on valid tasks
        metric_names = {m.name for m in metrics}
        assert "django_tasks_queue_length" in metric_names
        assert "django_tasks_queue_oldest_ready_age_seconds" in metric_names
        assert "django_tasks_queue_newest_ready_age_seconds" in metric_names

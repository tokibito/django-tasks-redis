"""
Tests for Prometheus metrics integration.
"""

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

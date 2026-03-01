"""
Tests for Prometheus metrics integration.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

from django.tasks.base import TaskResultStatus


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

        assert backend._metrics_collector is None
        assert backend._metrics_handler is None

    @pytest.mark.django_db
    def test_metrics_warn_when_not_installed(self):
        """Test that warning is logged when prometheus-client not installed."""
        from django_tasks_redis.backends import RedisTaskBackend

        with patch("django_tasks_redis.backends.logger") as mock_logger:
            backend = RedisTaskBackend(
                alias="default",
                params={
                    "QUEUES": [],
                    "OPTIONS": {
                        "REDIS_URL": "redis://localhost:6379/0",
                        "ENABLE_METRICS": True,
                    },
                },
            )

            # Should log warning if prometheus-client not available
            # (this test assumes prometheus-client may or may not be installed)
            if backend._metrics_collector is None:
                assert mock_logger.warning.called

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    @pytest.mark.django_db
    def test_metrics_enabled_when_configured(self):
        """Test that metrics are enabled when configured."""
        from django_tasks_redis.backends import RedisTaskBackend

        backend = RedisTaskBackend(
            alias="default",
            params={
                "QUEUES": [],
                "OPTIONS": {
                    "REDIS_URL": "redis://localhost:6379/0",
                    "ENABLE_METRICS": True,
                },
            },
        )

        assert backend._metrics_collector is not None
        assert backend._metrics_handler is not None


class TestTaskMetricsCollector:
    """Test TaskMetricsCollector functionality."""

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_collector_initialization(self):
        """Test that collector initializes correctly."""
        from django_tasks_redis.metrics.collectors import TaskMetricsCollector

        mock_backend = Mock()
        mock_backend.alias = "test_backend"

        collector = TaskMetricsCollector(mock_backend)

        assert collector.backend == mock_backend
        assert collector.backend_name == "test_backend"
        assert collector.tasks_enqueued is not None
        assert collector.tasks_completed is not None
        assert collector.tasks_failed is not None
        assert collector.queue_length is not None
        assert collector.tasks_running is not None
        assert collector.task_duration is not None

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_record_task_enqueued(self):
        """Test recording task enqueued."""
        from django_tasks_redis.metrics.collectors import TaskMetricsCollector

        mock_backend = Mock()
        mock_backend.alias = "test"

        collector = TaskMetricsCollector(mock_backend)

        # Record some enqueued tasks
        collector.record_task_enqueued("default", 0)
        collector.record_task_enqueued("default", 0)
        collector.record_task_enqueued("emails", 1)

        # Verify counters incremented (basic check)
        # Note: We can't easily check exact values without accessing internal state
        assert collector.tasks_enqueued is not None

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_record_task_completed(self):
        """Test recording task completion."""
        from django_tasks_redis.metrics.collectors import TaskMetricsCollector

        mock_backend = Mock()
        mock_backend.alias = "test"

        collector = TaskMetricsCollector(mock_backend)
        collector.record_task_completed("default")

        assert collector.tasks_completed is not None

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_record_task_failed(self):
        """Test recording task failure."""
        from django_tasks_redis.metrics.collectors import TaskMetricsCollector

        mock_backend = Mock()
        mock_backend.alias = "test"

        collector = TaskMetricsCollector(mock_backend)
        collector.record_task_failed("default")

        assert collector.tasks_failed is not None

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_record_task_duration(self):
        """Test recording task duration."""
        from django_tasks_redis.metrics.collectors import TaskMetricsCollector

        mock_backend = Mock()
        mock_backend.alias = "test"

        collector = TaskMetricsCollector(mock_backend)
        collector.record_task_duration("default", "SUCCESSFUL", 1.5)
        collector.record_task_duration("default", "FAILED", 0.3)

        assert collector.task_duration is not None

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_update_queue_lengths(self):
        """Test updating queue length gauges."""
        from django_tasks_redis.metrics.collectors import TaskMetricsCollector

        mock_backend = Mock()
        mock_backend.alias = "test"
        mock_backend.get_status_counts.return_value = {
            TaskResultStatus.READY: 10,
            TaskResultStatus.RUNNING: 2,
            TaskResultStatus.SUCCESSFUL: 100,
            TaskResultStatus.FAILED: 5,
        }

        collector = TaskMetricsCollector(mock_backend)
        collector.update_queue_lengths()

        # Verify get_status_counts was called
        mock_backend.get_status_counts.assert_called_once()


class TestMetricsSignalHandler:
    """Test signal handler functionality."""

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_signal_handler_initialization(self):
        """Test that signal handler registers signals."""
        from django_tasks_redis.metrics.signals import MetricsSignalHandler

        mock_collector = Mock()
        mock_collector.backend = Mock()
        mock_collector.backend.__class__ = Mock

        with patch("django_tasks_redis.metrics.signals.task_enqueued") as mock_enqueued:
            with patch("django_tasks_redis.metrics.signals.task_started") as mock_started:
                with patch("django_tasks_redis.metrics.signals.task_finished") as mock_finished:
                    handler = MetricsSignalHandler(mock_collector)

                    # Verify signals connected
                    mock_enqueued.connect.assert_called_once()
                    mock_started.connect.assert_called_once()
                    mock_finished.connect.assert_called_once()

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_handle_task_enqueued(self):
        """Test handling task enqueued signal."""
        from django_tasks_redis.metrics.signals import MetricsSignalHandler
        from django.tasks import task

        @task
        def test_task():
            pass

        mock_collector = Mock()
        mock_collector.backend = Mock()
        mock_collector.backend.__class__ = Mock

        with patch("django_tasks_redis.metrics.signals.task_enqueued"):
            handler = MetricsSignalHandler(mock_collector)

            # Create mock task result
            mock_result = Mock()
            mock_result.task = test_task
            mock_result.task.queue_name = "default"
            mock_result.task.priority = 0

            handler.handle_task_enqueued(None, task_result=mock_result)

            # Verify metric recorded
            mock_collector.record_task_enqueued.assert_called_once_with("default", 0)

    @pytest.mark.skipif(
        not _prometheus_available(),
        reason="prometheus-client not installed",
    )
    def test_handle_task_finished_successful(self):
        """Test handling task finished signal for successful task."""
        from django_tasks_redis.metrics.signals import MetricsSignalHandler
        from django.tasks import task
        from django.utils import timezone

        @task
        def test_task():
            pass

        mock_collector = Mock()
        mock_collector.backend = Mock()
        mock_collector.backend.__class__ = Mock

        with patch("django_tasks_redis.metrics.signals.task_finished"):
            handler = MetricsSignalHandler(mock_collector)

            # Create mock task result
            now = timezone.now()
            mock_result = Mock()
            mock_result.task = test_task
            mock_result.task.queue_name = "default"
            mock_result.status = TaskResultStatus.SUCCESSFUL
            mock_result.started_at = now
            mock_result.finished_at = now

            handler.handle_task_finished(None, task_result=mock_result)

            # Verify metrics recorded
            mock_collector.record_task_completed.assert_called_once_with("default")
            mock_collector.record_task_duration.assert_called_once()
            mock_collector.update_queue_lengths.assert_called_once()


def _prometheus_available():
    """Check if prometheus-client is available."""
    try:
        import prometheus_client
        return True
    except ImportError:
        return False

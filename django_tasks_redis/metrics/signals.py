"""
Signal handlers for instrumenting task lifecycle with Prometheus metrics.
"""

import logging
from typing import TYPE_CHECKING

from django.tasks.base import TaskResultStatus

if TYPE_CHECKING:
    from django.tasks.base import TaskResult

logger = logging.getLogger("django_tasks_redis.metrics")


class MetricsSignalHandler:
    """
    Handles Django task signals to update Prometheus metrics.

    Connects to task_enqueued, task_started, and task_finished signals
    to track task lifecycle events.
    """

    def __init__(self, metrics_collector):
        """
        Initialize signal handler.

        Args:
            metrics_collector: TaskMetricsCollector instance.
        """
        self.metrics = metrics_collector
        self._register_signals()

    def _register_signals(self):
        """Register signal handlers for task lifecycle events."""
        from django.tasks.signals import task_enqueued, task_finished, task_started

        task_enqueued.connect(
            self.handle_task_enqueued,
            sender=self.metrics.backend.__class__,
            weak=False,
        )

        task_started.connect(
            self.handle_task_started,
            sender=self.metrics.backend.__class__,
            weak=False,
        )

        task_finished.connect(
            self.handle_task_finished,
            sender=self.metrics.backend.__class__,
            weak=False,
        )

        logger.info("Metrics signal handlers registered")

    def handle_task_enqueued(self, sender, task_result: "TaskResult", **kwargs):
        """
        Handle task_enqueued signal.

        Args:
            sender: Signal sender (backend class).
            task_result: The TaskResult object.
        """
        try:
            queue_name = task_result.task.queue_name
            priority = task_result.task.priority

            self.metrics.record_task_enqueued(queue_name, priority)

            logger.debug(
                "Recorded task enqueued: queue=%s priority=%s",
                queue_name,
                priority,
            )
        except Exception as e:
            logger.error("Failed to record task enqueued metric: %s", e)

    def handle_task_started(self, sender, task_result: "TaskResult", **kwargs):
        """
        Handle task_started signal.

        Args:
            sender: Signal sender (backend class).
            task_result: The TaskResult object.
        """
        try:
            # Update queue length metrics when a task starts
            self.metrics.update_queue_lengths()

            logger.debug("Updated queue lengths on task start: task_id=%s", task_result.id)
        except Exception as e:
            logger.error("Failed to update metrics on task start: %s", e)

    def handle_task_finished(self, sender, task_result: "TaskResult", **kwargs):
        """
        Handle task_finished signal.

        Args:
            sender: Signal sender (backend class).
            task_result: The TaskResult object.
        """
        try:
            queue_name = task_result.task.queue_name
            status = task_result.status

            # Record completion/failure counter
            if status == TaskResultStatus.SUCCESSFUL:
                self.metrics.record_task_completed(queue_name)
            elif status == TaskResultStatus.FAILED:
                self.metrics.record_task_failed(queue_name)

            # Record task duration
            if task_result.started_at and task_result.finished_at:
                duration = (task_result.finished_at - task_result.started_at).total_seconds()
                self.metrics.record_task_duration(queue_name, status, duration)

            # Update queue length metrics
            self.metrics.update_queue_lengths()

            logger.debug(
                "Recorded task finished: queue=%s status=%s",
                queue_name,
                status,
            )
        except Exception as e:
            logger.error("Failed to record task finished metric: %s", e)

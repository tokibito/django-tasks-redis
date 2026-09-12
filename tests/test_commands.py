"""
Tests for management commands.
"""

import json
import os
import signal
import threading
import time
from io import StringIO

import pytest
from django.core.management import call_command
from django.tasks.base import TaskResultStatus

from django_tasks_redis import executor
from tests import tasks as test_tasks


@pytest.mark.django_db
class TestRunRedisTasksCommand:
    """Tests for run_redis_tasks management command."""

    def test_run_redis_tasks_empty(self, clean_redis):
        """Test running when no tasks available."""
        out = StringIO()
        call_command("run_redis_tasks", stdout=out)

        output = out.getvalue()
        assert "Starting Redis task worker" in output
        assert "No tasks available" in output

    def test_run_redis_tasks_with_tasks(self, clean_redis):
        """Test running with tasks available."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        simple_task.enqueue(3, 4)

        out = StringIO()
        call_command("run_redis_tasks", stdout=out)

        output = out.getvalue()
        assert "Starting Redis task worker" in output
        assert "SUCCESSFUL" in output
        assert "Processed 2 task(s)" in output

    def test_run_redis_tasks_max_tasks(self, clean_redis):
        """Test running with max-tasks limit."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 1)
        simple_task.enqueue(2, 2)
        simple_task.enqueue(3, 3)

        out = StringIO()
        call_command("run_redis_tasks", max_tasks=2, stdout=out)

        output = out.getvalue()
        assert "Reached max tasks limit (2)" in output
        assert "Processed 2 task(s)" in output

    def test_run_redis_tasks_queue_filter(self, clean_redis):
        """Test running with queue filter."""
        out = StringIO()
        call_command("run_redis_tasks", queue_name="emails", stdout=out)

        output = out.getvalue()
        assert "Queue: emails" in output

    def test_run_redis_tasks_with_failing_task(self, clean_redis):
        """Test running with a failing task."""
        from tests.tasks import failing_task

        failing_task.enqueue()

        out = StringIO()
        call_command("run_redis_tasks", stdout=out)

        output = out.getvalue()
        assert "FAILED" in output


@pytest.mark.django_db
class TestRunRedisTasksGracefulShutdown:
    def test_running_task_finishes_before_shutdown(self, clean_redis):
        """A task running when SIGTERM arrives is not interrupted."""
        from tests.tasks import shutdown_signal_task, simple_task

        signal_result = shutdown_signal_task.enqueue()
        simple_task.enqueue(1, 2)

        call_command("run_redis_tasks", stdout=StringIO())

        task_data = executor.get_task_by_id(signal_result.id)
        assert task_data["status"] == TaskResultStatus.SUCCESSFUL
        assert json.loads(task_data["return_value_json"]) == "sent SIGTERM"

    def test_no_new_task_is_started_after_signal(self, redis_backend, clean_redis):
        """Queued tasks are left untouched after a shutdown signal."""
        from tests.tasks import shutdown_signal_task, simple_task

        shutdown_signal_task.enqueue()
        next_result = simple_task.enqueue(1, 2)

        call_command("run_redis_tasks", stdout=StringIO())

        assert executor.get_task_by_id(next_result.id)["status"] == (
            TaskResultStatus.READY
        )
        # Its message was never read, so it is not pending for anyone.
        assert executor.fetch_task(worker_id="next-worker")["task_id"] == (
            next_result.id
        )

    def test_shutdown_is_reported(self, clean_redis):
        """The shutdown is reported on stdout."""
        from tests.tasks import shutdown_signal_task

        shutdown_signal_task.enqueue()

        out = StringIO()
        call_command("run_redis_tasks", stdout=out)
        output = out.getvalue()

        assert "Received SIGTERM" in output
        assert "Shutdown complete" in output
        assert "Processed 1 task(s)" in output

    def test_sigint_also_shuts_down_gracefully(self, clean_redis):
        """SIGINT is handled like SIGTERM, without a KeyboardInterrupt."""
        from tests.tasks import shutdown_signal_task, simple_task

        signal_result = shutdown_signal_task.enqueue(signal_name="SIGINT")
        next_result = simple_task.enqueue(1, 2)

        out = StringIO()
        call_command("run_redis_tasks", stdout=out)

        assert executor.get_task_by_id(signal_result.id)["status"] == (
            TaskResultStatus.SUCCESSFUL
        )
        assert executor.get_task_by_id(next_result.id)["status"] == (
            TaskResultStatus.READY
        )
        assert "Received SIGINT" in out.getvalue()

    def test_continuous_mode_stops_waiting_on_signal(self, clean_redis):
        """The blocking read is left as soon as a shutdown is requested."""
        pid = os.getpid()
        timer = threading.Timer(0.3, lambda: os.kill(pid, signal.SIGTERM))
        timer.daemon = True
        timer.start()

        started = time.monotonic()
        try:
            call_command(
                "run_redis_tasks",
                continuous=True,
                interval=60,
                stdout=StringIO(),
            )
        finally:
            timer.cancel()
        elapsed = time.monotonic() - started

        # The default REDIS_BLOCK_TIMEOUT is 5 seconds; the wait is taken in
        # one second steps so the signal is noticed within about one.
        assert elapsed < 3

    def test_signal_handlers_are_restored(self, clean_redis):
        """The original signal handlers are restored after the command."""
        original_term = signal.getsignal(signal.SIGTERM)
        original_int = signal.getsignal(signal.SIGINT)

        call_command("run_redis_tasks", stdout=StringIO())

        assert signal.getsignal(signal.SIGTERM) is original_term
        assert signal.getsignal(signal.SIGINT) is original_int

    def test_handler_is_installed_while_task_runs(self, clean_redis):
        """The graceful shutdown handler is active during task execution."""
        from tests.tasks import record_sigterm_handler_task

        original_term = signal.getsignal(signal.SIGTERM)
        test_tasks.recorded_sigterm_handler = None

        record_sigterm_handler_task.enqueue()
        call_command("run_redis_tasks", stdout=StringIO())

        assert test_tasks.recorded_sigterm_handler is not None
        assert test_tasks.recorded_sigterm_handler is not original_term

    def test_no_graceful_shutdown_option_skips_handlers(self, clean_redis):
        """--no-graceful-shutdown leaves the signal handlers untouched."""
        from tests.tasks import record_sigterm_handler_task

        original_term = signal.getsignal(signal.SIGTERM)
        test_tasks.recorded_sigterm_handler = None

        record_sigterm_handler_task.enqueue()
        out = StringIO()
        call_command("run_redis_tasks", no_graceful_shutdown=True, stdout=out)

        assert test_tasks.recorded_sigterm_handler is original_term
        assert "Graceful shutdown: disabled" in out.getvalue()

    def test_shutdown_timeout_is_reported(self, clean_redis):
        """The configured shutdown timeout is reported on startup."""
        out = StringIO()
        call_command("run_redis_tasks", shutdown_timeout=30.0, stdout=out)

        assert "Graceful shutdown: enabled (timeout=30.0s)" in out.getvalue()

    def test_unlimited_timeout_is_reported(self, clean_redis):
        out = StringIO()
        call_command("run_redis_tasks", stdout=out)

        assert "Graceful shutdown: enabled (timeout=unlimited)" in out.getvalue()

    def test_task_can_check_shutdown_state(self, clean_redis):
        """Task functions can stop early with is_shutdown_requested()."""
        from tests.tasks import shutdown_aware_task

        result = shutdown_aware_task.enqueue(iterations=100)

        call_command("run_redis_tasks", stdout=StringIO())

        task_data = executor.get_task_by_id(result.id)
        assert task_data["status"] == TaskResultStatus.SUCCESSFUL
        assert json.loads(task_data["return_value_json"]) < 100


@pytest.mark.django_db
class TestPurgeCompletedRedisTasksCommand:
    """Tests for purge_completed_redis_tasks management command."""

    def test_purge_dry_run(self, clean_redis):
        """Test purge with dry-run option."""
        out = StringIO()
        call_command("purge_completed_redis_tasks", dry_run=True, stdout=out)

        output = out.getvalue()
        assert "DRY RUN" in output
        assert "Would delete" in output

    def test_purge_completed_tasks(self, redis_backend, clean_redis):
        """Test purging completed tasks."""
        from datetime import timedelta

        from django.utils import timezone

        from django_tasks_redis import executor
        from django_tasks_redis.utils import get_result_key, serialize_datetime
        from tests.tasks import simple_task

        # Enqueue and run a task
        result = simple_task.enqueue(1, 2)
        executor.run_task_by_id(result.id)

        # Modify finished_at to be old
        client = redis_backend.get_client()
        result_key = get_result_key(
            redis_backend.key_prefix, redis_backend.alias, result.id
        )
        old_time = timezone.now() - timedelta(days=10)
        client.hset(result_key, "finished_at", serialize_datetime(old_time))

        out = StringIO()
        call_command("purge_completed_redis_tasks", days=7, stdout=out)

        output = out.getvalue()
        assert "Deleted 1 task(s)" in output

    def test_purge_uses_the_configured_batch_size_by_default(self, clean_redis):
        """Without --batch-size the REDIS_SCAN_BATCH_SIZE setting applies."""
        from unittest import mock

        with mock.patch(
            "django_tasks_redis.executor.purge_completed_tasks", return_value=0
        ) as purge:
            call_command("purge_completed_redis_tasks", stdout=StringIO())

        assert purge.call_args.kwargs["batch_size"] is None

    def test_purge_honours_batch_size(self, redis_backend, clean_redis):
        """--batch-size changes how tasks are read, not what is deleted."""
        from datetime import timedelta

        from django.utils import timezone

        from django_tasks_redis import executor
        from django_tasks_redis.utils import get_result_key, serialize_datetime
        from tests.tasks import simple_task

        client = redis_backend.get_client()
        old_time = timezone.now() - timedelta(days=10)
        for numbers in ((1, 1), (2, 2), (3, 3)):
            result = simple_task.enqueue(*numbers)
            executor.run_task_by_id(result.id)
            client.hset(
                get_result_key(
                    redis_backend.key_prefix, redis_backend.alias, result.id
                ),
                "finished_at",
                serialize_datetime(old_time),
            )

        out = StringIO()
        call_command("purge_completed_redis_tasks", days=7, batch_size=1, stdout=out)

        assert "Deleted 3 task(s)" in out.getvalue()

    def test_purge_with_status_filter(self, clean_redis):
        """Test purge with status filter."""
        out = StringIO()
        call_command(
            "purge_completed_redis_tasks",
            statuses=["SUCCESSFUL"],
            dry_run=True,
            stdout=out,
        )

        output = out.getvalue()
        assert "Statuses: SUCCESSFUL" in output

"""
Tests for management commands.
"""

from io import StringIO

import pytest
from django.core.management import call_command


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

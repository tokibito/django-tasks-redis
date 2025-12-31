"""
Tests for executor module.
"""

import pytest
from django.tasks.base import TaskResultStatus

from django_tasks_redis import executor


@pytest.mark.django_db
class TestExecutor:
    """Tests for executor functions."""

    def test_process_one_task(self, clean_redis):
        """Test processing a single task."""
        from tests.tasks import simple_task

        # Enqueue a task
        simple_task.enqueue(10, 20)

        # Process it
        result = executor.process_one_task()

        assert result is not None
        assert result.status == TaskResultStatus.SUCCESSFUL
        assert result.return_value == 30

    def test_process_one_task_empty(self, clean_redis):
        """Test processing when no tasks available."""
        result = executor.process_one_task()
        assert result is None

    def test_process_tasks(self, clean_redis):
        """Test processing multiple tasks."""
        from tests.tasks import simple_task

        # Enqueue multiple tasks
        simple_task.enqueue(1, 1)
        simple_task.enqueue(2, 2)
        simple_task.enqueue(3, 3)

        # Process all
        results = executor.process_tasks()

        assert len(results) == 3
        assert all(r.status == TaskResultStatus.SUCCESSFUL for r in results)

    def test_process_tasks_with_limit(self, clean_redis):
        """Test processing with max_tasks limit."""
        from tests.tasks import simple_task

        # Enqueue multiple tasks
        simple_task.enqueue(1, 1)
        simple_task.enqueue(2, 2)
        simple_task.enqueue(3, 3)

        # Process only 2
        results = executor.process_tasks(max_tasks=2)

        assert len(results) == 2

    def test_get_pending_task_count(self, clean_redis):
        """Test getting pending task count."""
        from tests.tasks import simple_task

        assert executor.get_pending_task_count() == 0

        simple_task.enqueue(1, 1)
        simple_task.enqueue(2, 2)

        assert executor.get_pending_task_count() == 2

    def test_run_task_by_id(self, clean_redis):
        """Test running a specific task by ID."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(100, 200)
        task_id = result.id

        final_result = executor.run_task_by_id(task_id)

        assert final_result is not None
        assert final_result.status == TaskResultStatus.SUCCESSFUL
        assert final_result.return_value == 300

    def test_run_task_by_id_not_found(self, clean_redis):
        """Test running a non-existent task."""
        from django.tasks.exceptions import TaskResultDoesNotExist

        with pytest.raises(TaskResultDoesNotExist):
            executor.run_task_by_id("non-existent-id")

    def test_run_task_by_id_not_ready(self, clean_redis):
        """Test running a task that's not in READY status."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        task_id = result.id

        # Run once
        executor.run_task_by_id(task_id)

        # Try to run again (should return None)
        second_result = executor.run_task_by_id(task_id)
        assert second_result is None

    def test_run_task_by_id_allow_retry(self, clean_redis):
        """Test retrying a failed task."""
        from tests.tasks import failing_task

        result = failing_task.enqueue()
        task_id = result.id

        # Run once (will fail)
        executor.run_task_by_id(task_id)

        # Retry without allow_retry (should return None)
        retry_result = executor.run_task_by_id(task_id, allow_retry=False)
        assert retry_result is None

        # Retry with allow_retry
        retry_result = executor.run_task_by_id(task_id, allow_retry=True)
        assert retry_result is not None
        assert retry_result.status == TaskResultStatus.FAILED

    def test_get_task_counts(self, clean_redis):
        """Test getting task counts by status."""
        from tests.tasks import failing_task, simple_task

        simple_task.enqueue(1, 1)
        simple_task.enqueue(2, 2)
        failing_task.enqueue()

        counts = executor.get_task_counts()

        assert counts[TaskResultStatus.READY] == 3

    def test_get_queue_stats(self, clean_redis):
        """Test getting queue statistics."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 1)
        simple_task.enqueue(2, 2)

        stats = executor.get_queue_stats()

        assert stats["pending_count"] == 2
        assert stats["running_count"] == 0

    def test_delete_task(self, clean_redis):
        """Test deleting a task."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        task_id = result.id

        # Verify it exists
        assert executor.get_task_by_id(task_id) is not None

        # Delete
        deleted = executor.delete_task(task_id)
        assert deleted is True

        # Verify it's gone
        assert executor.get_task_by_id(task_id) is None

    def test_delete_tasks(self, clean_redis):
        """Test deleting multiple tasks."""
        from tests.tasks import simple_task

        result1 = simple_task.enqueue(1, 1)
        result2 = simple_task.enqueue(2, 2)
        result3 = simple_task.enqueue(3, 3)

        deleted = executor.delete_tasks([result1.id, result2.id])
        assert deleted == 2

        # result3 should still exist
        assert executor.get_task_by_id(result3.id) is not None

    def test_reset_task_for_retry(self, clean_redis):
        """Test resetting a task for retry."""
        from tests.tasks import failing_task

        result = failing_task.enqueue()
        task_id = result.id

        # Run (will fail)
        executor.run_task_by_id(task_id)

        # Reset
        reset = executor.reset_task_for_retry(task_id)
        assert reset is True

        # Should be READY again
        task_data = executor.get_task_by_id(task_id)
        assert task_data["status"] == TaskResultStatus.READY

    def test_purge_completed_tasks(self, redis_backend, clean_redis):
        """Test purging completed tasks."""
        from datetime import timedelta

        from django.utils import timezone

        from tests.tasks import simple_task

        # Enqueue and run a task
        result = simple_task.enqueue(1, 2)
        executor.run_task_by_id(result.id)

        # Modify finished_at to be old (hack for testing)
        from django_tasks_redis.utils import get_result_key, serialize_datetime

        client = redis_backend.get_client()
        result_key = get_result_key(
            redis_backend.key_prefix, redis_backend.alias, result.id
        )
        old_time = timezone.now() - timedelta(days=10)
        client.hset(result_key, "finished_at", serialize_datetime(old_time))

        # Purge tasks older than 7 days
        deleted = executor.purge_completed_tasks(days=7)

        assert deleted == 1

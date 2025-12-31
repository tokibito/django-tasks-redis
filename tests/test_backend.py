"""
Tests for RedisTaskBackend.
"""

import pytest
from django.tasks.base import TaskResultStatus


@pytest.mark.django_db
class TestRedisTaskBackend:
    """Tests for the Redis task backend."""

    def test_enqueue_task(self, redis_backend, clean_redis):
        """Test enqueueing a task."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)

        assert result is not None
        assert result.id is not None
        assert result.status == TaskResultStatus.READY
        assert result.args == [1, 2]
        assert result.kwargs == {}

    def test_get_result(self, redis_backend, clean_redis):
        """Test retrieving a task result."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(3, 4)
        task_id = result.id

        retrieved = redis_backend.get_result(task_id)

        assert retrieved.id == task_id
        assert retrieved.status == TaskResultStatus.READY
        assert retrieved.args == [3, 4]

    def test_run_task_success(self, redis_backend, clean_redis):
        """Test running a task successfully."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(5, 6)
        task_id = result.id

        final_result = redis_backend.run_task(task_id, worker_id="test-worker")

        assert final_result.status == TaskResultStatus.SUCCESSFUL
        assert final_result.return_value == 11
        assert "test-worker" in final_result.worker_ids

    def test_run_task_failure(self, redis_backend, clean_redis):
        """Test running a task that fails."""
        from tests.tasks import failing_task

        result = failing_task.enqueue()
        task_id = result.id

        final_result = redis_backend.run_task(task_id, worker_id="test-worker")

        assert final_result.status == TaskResultStatus.FAILED
        assert len(final_result.errors) == 1
        assert "ValueError" in final_result.errors[0].exception_class_path

    def test_priority_task(self, redis_backend, clean_redis):
        """Test task with priority."""
        from tests.tasks import high_priority_task

        result = high_priority_task.enqueue()

        assert result is not None
        assert result.status == TaskResultStatus.READY

    def test_queue_name(self, redis_backend, clean_redis):
        """Test task with specific queue name."""
        from tests.tasks import email_task

        result = email_task.enqueue("test@example.com", "Subject", "Body")

        assert result is not None
        # Task should be stored with queue_name metadata

    def test_context_task(self, redis_backend, clean_redis):
        """Test task that takes context."""
        from tests.tasks import context_task

        result = context_task.enqueue("Hello")
        task_id = result.id

        final_result = redis_backend.run_task(task_id, worker_id="test-worker")

        assert final_result.status == TaskResultStatus.SUCCESSFUL
        return_value = final_result.return_value
        assert return_value["message"] == "Hello"
        assert return_value["attempt"] == 1
        assert return_value["task_id"] == task_id

    def test_get_all_tasks(self, redis_backend, clean_redis):
        """Test getting all tasks."""
        from tests.tasks import simple_task

        # Enqueue multiple tasks
        simple_task.enqueue(1, 1)
        simple_task.enqueue(2, 2)
        simple_task.enqueue(3, 3)

        tasks, total = redis_backend.get_all_tasks()

        assert total == 3
        assert len(tasks) == 3

    def test_get_status_counts(self, redis_backend, clean_redis):
        """Test getting status counts."""
        from tests.tasks import failing_task, simple_task

        # Create tasks in different states
        result1 = simple_task.enqueue(1, 1)
        simple_task.enqueue(2, 2)  # result2 remains READY
        result3 = failing_task.enqueue()

        # Run some tasks
        redis_backend.run_task(result1.id)  # SUCCESSFUL
        redis_backend.run_task(result3.id)  # FAILED

        counts = redis_backend.get_status_counts()

        assert counts[TaskResultStatus.READY] == 1
        assert counts[TaskResultStatus.SUCCESSFUL] == 1
        assert counts[TaskResultStatus.FAILED] == 1

    def test_delete_task_data(self, redis_backend, clean_redis):
        """Test deleting a task."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        task_id = result.id

        # Verify task exists
        assert redis_backend.get_task_data(task_id) is not None

        # Delete
        deleted = redis_backend.delete_task_data(task_id)
        assert deleted is True

        # Verify task is gone
        assert redis_backend.get_task_data(task_id) is None

    def test_reset_task_status(self, redis_backend, clean_redis):
        """Test resetting a failed task."""
        from tests.tasks import failing_task

        result = failing_task.enqueue()
        task_id = result.id

        # Run task (will fail)
        redis_backend.run_task(task_id)

        # Verify it's failed
        task_data = redis_backend.get_task_data(task_id)
        assert task_data["status"] == TaskResultStatus.FAILED

        # Reset
        reset = redis_backend.reset_task_status(task_id)
        assert reset is True

        # Verify it's ready again
        task_data = redis_backend.get_task_data(task_id)
        assert task_data["status"] == TaskResultStatus.READY

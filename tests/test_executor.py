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

    def test_get_tasks_filters_by_task_path_and_priority(self, clean_redis):
        """Test task path and priority narrowing the task listing."""
        from tests.tasks import high_priority_task, simple_task

        simple_task.enqueue(1, 1)
        high_priority_task.enqueue()

        tasks, total = executor.get_tasks(task_path="tests.tasks.simple_task")

        assert total == 1
        assert tasks[0]["task_path"] == "tests.tasks.simple_task"

        tasks, total = executor.get_tasks(priority="10")

        assert total == 1
        assert tasks[0]["priority"] == "10"

    def test_get_distinct_task_values(self, clean_redis):
        """Test getting the distinct values of several fields in one call."""
        from tests.tasks import email_task, simple_task

        simple_task.enqueue(1, 1)
        email_task.enqueue("to@example.com", "Subject", "Body")

        values = executor.get_distinct_task_values(("queue_name", "priority"))

        assert values["queue_name"] == {"default", "emails"}
        assert values["priority"] == {"0"}

    def test_get_queue_stats(self, clean_redis):
        """Test getting queue statistics."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 1)
        simple_task.enqueue(2, 2)

        stats = executor.get_queue_stats()

        assert stats["pending_count"] == 2
        assert stats["running_count"] == 0

    def test_get_queue_stats_waiting_since(self, redis_backend, clean_redis):
        """Test getting the waiting times of the oldest and newest READY task."""
        from datetime import timedelta

        from django.utils import timezone

        from django_tasks_redis.utils import get_result_key, serialize_datetime
        from tests.tasks import simple_task

        old_time = timezone.now() - timedelta(minutes=20)
        mid_time = timezone.now() - timedelta(minutes=10)
        new_time = timezone.now()

        results = [simple_task.enqueue(i, i) for i in range(3)]
        client = redis_backend.get_client()
        for result, value in zip(results, [old_time, mid_time, new_time], strict=True):
            result_key = get_result_key(
                redis_backend.key_prefix, redis_backend.alias, result.id
            )
            client.hset(result_key, "enqueued_at", serialize_datetime(value))

        # The hash was edited behind the index: rebuild it from the hashes.
        redis_backend.rebuild_status_index()

        stats = executor.get_queue_stats()

        assert stats["pending_count"] == 3
        assert stats["oldest_pending_waiting_since"] == old_time
        assert stats["newest_pending_waiting_since"] == new_time

    def test_get_queue_stats_delayed_waiting_since(self, redis_backend, clean_redis):
        """Test that a delayed task starts waiting at its run_after time."""
        from datetime import timedelta

        from django.utils import timezone

        from django_tasks_redis.utils import get_result_key, serialize_datetime
        from tests.tasks import simple_task

        run_after = timezone.now() + timedelta(hours=1)
        old_time = timezone.now() - timedelta(hours=2)

        result = simple_task.using(run_after=run_after).enqueue(1, 1)
        client = redis_backend.get_client()
        result_key = get_result_key(
            redis_backend.key_prefix, redis_backend.alias, result.id
        )
        client.hset(result_key, "enqueued_at", serialize_datetime(old_time))

        # The hash was edited behind the index: rebuild it from the hashes.
        redis_backend.rebuild_status_index()

        stats = executor.get_queue_stats()

        assert stats["pending_count"] == 1
        assert stats["delayed_count"] == 1
        assert stats["oldest_pending_waiting_since"] == run_after
        assert stats["newest_pending_waiting_since"] == run_after

    def test_get_queue_stats_past_run_after(self, redis_backend, clean_redis):
        """Test that a due task's waiting time starts at its run_after time."""
        from datetime import timedelta

        from django.utils import timezone

        from django_tasks_redis.utils import get_result_key, serialize_datetime
        from tests.tasks import simple_task

        old_time = timezone.now() - timedelta(minutes=20)
        due_time = timezone.now() - timedelta(minutes=10)

        result = simple_task.enqueue(1, 1)
        client = redis_backend.get_client()
        result_key = get_result_key(
            redis_backend.key_prefix, redis_backend.alias, result.id
        )
        client.hset(
            result_key,
            mapping={
                "enqueued_at": serialize_datetime(old_time),
                "run_after": serialize_datetime(due_time),
            },
        )

        # The hash was edited behind the index: rebuild it from the hashes.
        redis_backend.rebuild_status_index()

        stats = executor.get_queue_stats()

        assert stats["pending_count"] == 1
        assert stats["oldest_pending_waiting_since"] == due_time
        assert stats["newest_pending_waiting_since"] == due_time

    def test_get_queue_stats_past_run_after_other_offset(
        self, redis_backend, clean_redis
    ):
        """Test that a past run_after with another offset is not the waiting time."""
        from datetime import timedelta
        from datetime import timezone as datetime_timezone

        from django.utils import timezone

        from django_tasks_redis.utils import get_result_key, serialize_datetime
        from tests.tasks import simple_task

        enqueued_time = timezone.now()
        run_after = (enqueued_time - timedelta(hours=1)).astimezone(
            datetime_timezone(timedelta(hours=9))
        )

        result = simple_task.enqueue(1, 1)
        client = redis_backend.get_client()
        result_key = get_result_key(
            redis_backend.key_prefix, redis_backend.alias, result.id
        )
        client.hset(
            result_key,
            mapping={
                "enqueued_at": serialize_datetime(enqueued_time),
                "run_after": serialize_datetime(run_after),
            },
        )

        # The hash was edited behind the index: rebuild it from the hashes.
        redis_backend.rebuild_status_index()

        stats = executor.get_queue_stats()

        assert stats["pending_count"] == 1
        assert stats["oldest_pending_waiting_since"] == enqueued_time
        assert stats["newest_pending_waiting_since"] == enqueued_time

    def test_get_queue_stats_future_run_after_other_offset(
        self, redis_backend, clean_redis
    ):
        """Test that a future run_after with another offset is the waiting time."""
        from datetime import timedelta
        from datetime import timezone as datetime_timezone

        from django.utils import timezone

        from django_tasks_redis.utils import get_result_key, serialize_datetime
        from tests.tasks import simple_task

        enqueued_time = timezone.now()
        run_after = (enqueued_time + timedelta(hours=1)).astimezone(
            datetime_timezone(timedelta(hours=-4))
        )

        result = simple_task.enqueue(1, 1)
        client = redis_backend.get_client()
        result_key = get_result_key(
            redis_backend.key_prefix, redis_backend.alias, result.id
        )
        client.hset(
            result_key,
            mapping={
                "enqueued_at": serialize_datetime(enqueued_time),
                "run_after": serialize_datetime(run_after),
            },
        )

        # The hash was edited behind the index: rebuild it from the hashes.
        redis_backend.rebuild_status_index()

        stats = executor.get_queue_stats()

        assert stats["pending_count"] == 1
        assert stats["oldest_pending_waiting_since"] == run_after
        assert stats["newest_pending_waiting_since"] == run_after

    def test_get_queue_stats_no_pending(self, clean_redis):
        """Test that the waiting times are None when no task is READY."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        executor.process_one_task()

        stats = executor.get_queue_stats()

        assert stats["pending_count"] == 0
        assert stats["successful_count"] == 1
        assert stats["oldest_pending_waiting_since"] is None
        assert stats["newest_pending_waiting_since"] is None

    def test_get_queue_stats_queue_filter(self, redis_backend, clean_redis):
        """Test that a queue name scopes the counts and the waiting times."""
        from datetime import timedelta

        from django.utils import timezone

        from django_tasks_redis.utils import get_result_key, serialize_datetime
        from tests.tasks import email_task, simple_task

        old_time = timezone.now() - timedelta(minutes=20)
        new_time = timezone.now() - timedelta(minutes=10)

        default_result = simple_task.enqueue(1, 1)
        email_result = email_task.enqueue("to@example.com", "Hi", "body")
        client = redis_backend.get_client()
        for result, value in [
            (default_result, old_time),
            (email_result, new_time),
        ]:
            result_key = get_result_key(
                redis_backend.key_prefix, redis_backend.alias, result.id
            )
            client.hset(result_key, "enqueued_at", serialize_datetime(value))

        # The hash was edited behind the index: rebuild it from the hashes.
        redis_backend.rebuild_status_index()

        stats = executor.get_queue_stats(queue_name="emails")

        assert stats["pending_count"] == 1
        assert stats["oldest_pending_waiting_since"] == new_time
        assert stats["newest_pending_waiting_since"] == new_time

        stats = executor.get_queue_stats()

        assert stats["pending_count"] == 2
        assert stats["oldest_pending_waiting_since"] == old_time
        assert stats["newest_pending_waiting_since"] == new_time

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

    def test_purge_completed_tasks_filters_by_task_path(
        self, redis_backend, clean_redis
    ):
        """Purging with task_path deletes only that task path's results."""
        from datetime import timedelta

        from django.utils import timezone

        from django_tasks_redis.utils import get_result_key, serialize_datetime
        from tests.tasks import email_task, simple_task

        simple_result = simple_task.enqueue(1, 2)
        email_result = email_task.enqueue("a@example.com", "Hello", "Body")
        executor.run_task_by_id(simple_result.id)
        executor.run_task_by_id(email_result.id)

        client = redis_backend.get_client()
        old_time = timezone.now() - timedelta(days=10)
        for result in (simple_result, email_result):
            client.hset(
                get_result_key(
                    redis_backend.key_prefix, redis_backend.alias, result.id
                ),
                "finished_at",
                serialize_datetime(old_time),
            )

        deleted = executor.purge_completed_tasks(
            days=7, task_path="tests.tasks.simple_task"
        )

        assert deleted == 1
        assert executor.get_task_by_id(simple_result.id) is None
        assert executor.get_task_by_id(email_result.id) is not None


@pytest.mark.django_db
class TestProcessTasksStopEvent:
    def test_set_stop_event_starts_nothing(self, clean_redis):
        import threading

        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        stop_event = threading.Event()
        stop_event.set()

        assert executor.process_tasks(stop_event=stop_event) == []
        assert executor.get_task_by_id(result.id)["status"] == TaskResultStatus.READY

    def test_unset_stop_event_does_not_stop_processing(self, clean_redis):
        import threading

        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        simple_task.enqueue(3, 4)

        results = executor.process_tasks(stop_event=threading.Event())

        assert len(results) == 2

    def test_graceful_shutdown_is_a_stop_event(self, clean_redis):
        from django_tasks_redis import GracefulShutdown
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        shutdown = GracefulShutdown()
        shutdown.set()

        assert executor.process_tasks(stop_event=shutdown) == []

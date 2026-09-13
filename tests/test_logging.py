"""
Tests for the structured fields attached to task and worker log records.

Every record the backend or the worker command emits is expected to carry
its context as record attributes, so a JSON formatter can emit them as
fields an operator filters on rather than only baking them into the
message string.
"""

import logging

import pytest

from django_tasks_redis.utils import _elapsed_ms, task_log_fields


class TestTaskLogFields:
    """The helper that builds the structured fields mapping."""

    def test_fields_are_drawn_from_the_task_hash(self):
        data = {
            "task_id": "abc-123",
            "task_path": "myapp.tasks.send",
            "queue_name": "emails",
            "priority": "10",
            "backend_name": "default",
        }

        fields = task_log_fields(data, worker_id="host-abcd1234")

        assert fields["task_id"] == "abc-123"
        assert fields["task_path"] == "myapp.tasks.send"
        assert fields["queue_name"] == "emails"
        # Stored as a string in the Redis hash; surfaced as an int for
        # downstream filtering and JSON serialisation.
        assert fields["priority"] == 10
        assert isinstance(fields["priority"], int)
        assert fields["backend_alias"] == "default"
        assert fields["worker_id"] == "host-abcd1234"

    def test_worker_id_defaults_to_none(self):
        fields = task_log_fields({"task_id": "t", "task_path": "p"})

        assert fields["worker_id"] is None

    def test_extra_is_merged_last(self):
        fields = task_log_fields(
            {"task_id": "t", "task_path": "p"},
            worker_id="w",
            status="SUCCESSFUL",
            duration_ms=42,
            error_class="ValueError",
        )

        assert fields["status"] == "SUCCESSFUL"
        assert fields["duration_ms"] == 42
        assert fields["error_class"] == "ValueError"
        # Core fields survive the merge.
        assert fields["task_id"] == "t"

    def test_missing_values_become_none(self):
        fields = task_log_fields({})

        assert fields["task_id"] is None
        assert fields["task_path"] is None
        assert fields["queue_name"] is None
        assert fields["priority"] is None
        assert fields["backend_alias"] is None

    def test_non_numeric_priority_is_left_alone(self):
        # A bad value in the hash (corrupt data, a typo at write time)
        # must not raise when the record is built.
        fields = task_log_fields({"priority": "not-a-number"})

        assert fields["priority"] is None


class TestElapsedMs:
    """The wall-time helper used for ``duration_ms``."""

    def test_returns_a_round_number_of_milliseconds(self):
        import time

        start = time.monotonic() - 0.123  # ~123ms ago

        assert _elapsed_ms(start) == 123

    def test_is_zero_immediately_after_the_call(self):
        import time

        assert _elapsed_ms(time.monotonic()) == 0


@pytest.mark.django_db
class TestRunTaskLogging:
    """Every task record run_task() emits carries the structured fields."""

    def test_started_record_fires_with_fields(self, redis_backend, clean_redis, caplog):
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        task_id = result.id

        with caplog.at_level(logging.INFO, logger="django_tasks_redis"):
            redis_backend.run_task(task_id, worker_id="w-log")

        started = [
            r for r in caplog.records if r.getMessage().startswith("Task started")
        ]
        assert len(started) == 1
        record = started[0]
        assert record.task_id == task_id
        assert record.task_path.endswith("simple_task")
        assert record.worker_id == "w-log"
        assert record.backend_alias == redis_backend.alias

    def test_completed_record_has_status_and_duration(self, redis_backend, clean_redis, caplog):
        from tests.tasks import simple_task

        result = simple_task.enqueue(3, 4)

        with caplog.at_level(logging.INFO, logger="django_tasks_redis"):
            redis_backend.run_task(result.id, worker_id="w-log")

        completed = [
            r
            for r in caplog.records
            if r.getMessage().startswith("Task completed successfully")
        ]
        assert len(completed) == 1
        record = completed[0]
        assert record.status == "SUCCESSFUL"
        assert isinstance(record.duration_ms, int)
        assert record.duration_ms >= 0
        assert record.task_id == result.id

    def test_failed_record_has_status_duration_and_error_class(
        self, redis_backend, clean_redis, caplog
    ):
        from tests.tasks import failing_task

        result = failing_task.enqueue()

        with caplog.at_level(logging.INFO, logger="django_tasks_redis"):
            redis_backend.run_task(result.id, worker_id="w-log")

        failed = [r for r in caplog.records if r.getMessage().startswith("Task failed")]
        assert len(failed) == 1
        record = failed[0]
        assert record.status == "FAILED"
        assert isinstance(record.duration_ms, int)
        assert record.error_class.endswith("ValueError")

    def test_abandoned_record_carries_status_and_error_class(
        self, redis_backend, clean_redis, caplog
    ):
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 1)

        with caplog.at_level(logging.INFO, logger="django_tasks_redis"):
            abandoned = redis_backend.mark_task_failed(result.id, "test reason")

        assert abandoned is True
        abandoned_records = [
            r for r in caplog.records if r.getMessage().startswith("Task abandoned")
        ]
        assert len(abandoned_records) == 1
        record = abandoned_records[0]
        assert record.status == "FAILED"
        assert record.error_class.endswith("TaskAbandoned")
        assert record.task_id == result.id


@pytest.mark.django_db
class TestRunRedisTasksLogging:
    """Worker-level records carry the worker context."""

    def test_worker_started_and_finished_fire(self, clean_redis, caplog):
        from io import StringIO

        from django.core.management import call_command

        with caplog.at_level(logging.INFO, logger="django_tasks_redis"):
            call_command("run_redis_tasks", stdout=StringIO())

        started = [
            r for r in caplog.records if r.getMessage().startswith("Worker started")
        ]
        finished = [
            r for r in caplog.records if r.getMessage().startswith("Worker finished")
        ]
        assert len(started) == 1
        assert len(finished) == 1
        # The worker_id is generated for each run; both records must share it.
        assert started[0].worker_id == finished[0].worker_id
        assert started[0].backend_alias == "default"
        assert finished[0].tasks_processed == 0
        assert finished[0].tasks_failed == 0
        assert finished[0].exit_code == 0

    def test_finished_counts_failed_tasks(self, clean_redis, caplog):
        from io import StringIO

        from django.core.management import call_command

        from tests.tasks import failing_task, simple_task

        simple_task.enqueue(1, 2)
        failing_task.enqueue()

        with caplog.at_level(logging.INFO, logger="django_tasks_redis"):
            call_command("run_redis_tasks", stdout=StringIO())

        finished = [
            r for r in caplog.records if r.getMessage().startswith("Worker finished")
        ]
        assert len(finished) == 1
        # Two tasks ran: one succeeded, one failed. The counter is only for
        # failures, so a non-zero value proves it is wired up.
        assert finished[0].tasks_processed == 2
        assert finished[0].tasks_failed == 1

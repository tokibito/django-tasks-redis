"""
Tests for delivery guarantees: promotion, redelivery and stream cleanup.
"""

import json
from datetime import timedelta

import pytest
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from django_tasks_redis import executor
from django_tasks_redis.exceptions import TaskAbandoned
from django_tasks_redis.utils import (
    get_delayed_key,
    get_priority_stream_key,
    get_result_key,
    serialize_datetime,
)

DEAD_WORKER = "dead-worker"
LIVE_WORKER = "live-worker"


def normal_stream_key(backend, queue_name="default"):
    return get_priority_stream_key(
        backend.key_prefix, backend.alias, queue_name, "normal"
    )


def deliver_to(backend, worker_id, queue_name="default"):
    """Hand the next message to a consumer without acknowledging it."""
    client = backend.get_client()
    return client.xreadgroup(
        backend.consumer_group,
        worker_id,
        {normal_stream_key(backend, queue_name): ">"},
        count=1,
        block=None,
    )


def set_task_fields(backend, task_id, **fields):
    client = backend.get_client()
    client.hset(get_result_key(backend.key_prefix, backend.alias, task_id), **fields)


@pytest.mark.django_db
class TestDelayedTaskPromotion:
    """A due delayed task must reach the stream exactly once."""

    def make_due_delayed_task(self, backend):
        """Enqueue a delayed task and bring its due date forward."""
        from tests.tasks import simple_task

        result = simple_task.using(
            run_after=timezone.now() + timedelta(seconds=60)
        ).enqueue(4, 5)
        delayed_key = get_delayed_key(backend.key_prefix, backend.alias, "default")
        backend.get_client().zadd(delayed_key, {result.id: 0})
        return result

    def test_due_task_is_promoted_once_when_workers_race(
        self, redis_backend, clean_redis
    ):
        """Two workers scanning the same due task must not both queue it."""
        client = redis_backend.get_client()
        self.make_due_delayed_task(redis_backend)

        original_hgetall = client.hgetall
        raced = []

        def hgetall_racing_a_second_worker(*args, **kwargs):
            data = original_hgetall(*args, **kwargs)
            if not raced:
                raced.append(True)
                # A second worker runs the whole promotion in the window between
                # this one reading the task and queueing it.
                client.hgetall = original_hgetall
                executor._process_delayed_tasks(redis_backend)
                client.hgetall = hgetall_racing_a_second_worker
            return data

        client.hgetall = hgetall_racing_a_second_worker
        try:
            executor._process_delayed_tasks(redis_backend)
        finally:
            del client.hgetall

        assert raced, "the race was never exercised"
        assert client.xlen(normal_stream_key(redis_backend)) == 1

    def test_due_task_is_promoted(self, redis_backend, clean_redis):
        """A due delayed task reaches the stream and leaves the delayed set."""
        client = redis_backend.get_client()
        result = self.make_due_delayed_task(redis_backend)
        delayed_key = get_delayed_key(
            redis_backend.key_prefix, redis_backend.alias, "default"
        )

        executor._process_delayed_tasks(redis_backend)

        assert client.zscore(delayed_key, result.id) is None
        assert client.xlen(normal_stream_key(redis_backend)) == 1

    def test_task_that_is_no_longer_ready_is_not_promoted(
        self, redis_backend, clean_redis
    ):
        """A delayed task someone already ran is dropped, not queued."""
        client = redis_backend.get_client()
        result = self.make_due_delayed_task(redis_backend)
        set_task_fields(
            redis_backend, result.id, mapping={"status": TaskResultStatus.SUCCESSFUL}
        )
        delayed_key = get_delayed_key(
            redis_backend.key_prefix, redis_backend.alias, "default"
        )

        executor._process_delayed_tasks(redis_backend)

        assert client.zscore(delayed_key, result.id) is None
        assert client.xlen(normal_stream_key(redis_backend)) == 0

    def test_task_whose_result_expired_is_dropped(self, redis_backend, clean_redis):
        """A delayed entry with no result hash left leaves the delayed set."""
        client = redis_backend.get_client()
        result = self.make_due_delayed_task(redis_backend)
        client.delete(
            get_result_key(redis_backend.key_prefix, redis_backend.alias, result.id)
        )
        delayed_key = get_delayed_key(
            redis_backend.key_prefix, redis_backend.alias, "default"
        )

        executor._process_delayed_tasks(redis_backend)

        assert client.zscore(delayed_key, result.id) is None
        assert client.xlen(normal_stream_key(redis_backend)) == 0


@pytest.mark.django_db
class TestStreamCleanup:
    """Processed messages must not accumulate in the stream."""

    def test_processed_message_is_removed_from_the_stream(
        self, redis_backend, clean_redis
    ):
        from tests.tasks import simple_task

        client = redis_backend.get_client()
        stream_key = normal_stream_key(redis_backend)
        simple_task.enqueue(1, 2)
        assert client.xlen(stream_key) == 1

        executor.process_one_task()

        assert client.xlen(stream_key) == 0
        assert client.xpending(stream_key, redis_backend.consumer_group)["pending"] == 0

    def test_skipped_message_is_removed_from_the_stream(
        self, redis_backend, clean_redis
    ):
        """A message for a task that already ran is cleaned up too."""
        from tests.tasks import simple_task

        client = redis_backend.get_client()
        stream_key = normal_stream_key(redis_backend)
        result = simple_task.enqueue(1, 2)
        executor.run_task_by_id(result.id)

        assert executor.process_one_task() is None

        assert client.xlen(stream_key) == 0
        assert client.xpending(stream_key, redis_backend.consumer_group)["pending"] == 0


@pytest.mark.django_db
class TestBlockingFetch:
    """Waiting on the streams must work before anything was ever enqueued."""

    def test_blocking_fetch_creates_the_missing_groups(
        self, redis_backend, clean_redis
    ):
        """A worker started on a fresh Redis waits instead of failing."""
        client = redis_backend.get_client()

        assert executor.fetch_task(block=50) is None

        assert client.exists(normal_stream_key(redis_backend))

    def test_blocking_fetch_returns_queued_work(self, redis_backend, clean_redis):
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)

        task_data = executor.fetch_task(block=50)

        assert task_data is not None
        assert task_data["task_id"] == str(result.id)


@pytest.mark.django_db
class TestStaleTaskRecovery:
    """Messages a dead worker left pending must run again."""

    def test_stale_message_is_redelivered_and_executed(
        self, redis_backend, clean_redis
    ):
        """A worker that died before starting the task must not lose it."""
        from tests.tasks import simple_task

        simple_task.enqueue(4, 5)
        deliver_to(redis_backend, DEAD_WORKER)

        # Nothing new to read: the only message belongs to the dead consumer.
        assert executor.process_one_task(worker_id=LIVE_WORKER) is None

        claimed = executor.claim_stale_tasks(claim_timeout=0, worker_id=LIVE_WORKER)
        assert claimed == 1

        result = executor.process_one_task(worker_id=LIVE_WORKER)

        assert result is not None
        assert result.status == TaskResultStatus.SUCCESSFUL
        assert result.return_value == 9

    def test_task_interrupted_mid_run_is_executed_again(
        self, redis_backend, clean_redis
    ):
        """A RUNNING task whose worker died is not left hanging."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(2, 3)
        deliver_to(redis_backend, DEAD_WORKER)
        set_task_fields(
            redis_backend, result.id, mapping={"status": TaskResultStatus.RUNNING}
        )

        executor.claim_stale_tasks(claim_timeout=0, worker_id=LIVE_WORKER)

        # Only staleness can tell a dead worker from a slow one, so the claim is
        # what puts the task back in a runnable state.
        assert executor.get_task_by_id(result.id)["status"] == TaskResultStatus.READY

        final = executor.process_one_task(worker_id=LIVE_WORKER)

        assert final is not None
        assert final.status == TaskResultStatus.SUCCESSFUL
        assert final.return_value == 5

    def test_own_pending_message_does_not_rerun_a_running_task(
        self, redis_backend, clean_redis
    ):
        """A message this worker holds is not evidence that anything died.

        A blocking read wakes up on several streams at once and leaves the
        messages it did not return pending for this consumer. They come back
        through the same path a reclaimed message does, and must not be treated
        as a crashed run.
        """
        from tests.tasks import simple_task

        client = redis_backend.get_client()
        stream_key = normal_stream_key(redis_backend)
        result = simple_task.enqueue(1, 2)
        deliver_to(redis_backend, LIVE_WORKER)
        set_task_fields(
            redis_backend, result.id, mapping={"status": TaskResultStatus.RUNNING}
        )

        assert executor.process_one_task(worker_id=LIVE_WORKER) is None

        assert executor.get_task_by_id(result.id)["status"] == TaskResultStatus.RUNNING
        assert client.xlen(stream_key) == 0

    def test_finished_task_is_not_executed_again(self, redis_backend, clean_redis):
        """A reclaimed message whose task finished is dropped, not re-run."""
        from tests.tasks import simple_task

        client = redis_backend.get_client()
        stream_key = normal_stream_key(redis_backend)
        result = simple_task.enqueue(1, 2)
        deliver_to(redis_backend, DEAD_WORKER)
        executor.run_task_by_id(result.id)

        executor.claim_stale_tasks(claim_timeout=0, worker_id=LIVE_WORKER)

        assert executor.process_one_task(worker_id=LIVE_WORKER) is None
        assert client.xpending(stream_key, redis_backend.consumer_group)["pending"] == 0
        assert client.xlen(stream_key) == 0

    def test_message_being_processed_is_left_alone(self, redis_backend, clean_redis):
        """A message delivered moments ago is not stale."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        deliver_to(redis_backend, "worker-a")

        claimed = executor.claim_stale_tasks(claim_timeout=300, worker_id="worker-b")

        assert claimed == 0

    def test_message_is_abandoned_after_too_many_deliveries(
        self, redis_backend, clean_redis
    ):
        """A task that never completes is failed instead of retried forever."""
        from tests.tasks import simple_task

        client = redis_backend.get_client()
        stream_key = normal_stream_key(redis_backend)
        result = simple_task.enqueue(1, 2)
        deliver_to(redis_backend, DEAD_WORKER)

        claimed = executor.claim_stale_tasks(
            claim_timeout=0, worker_id=LIVE_WORKER, max_deliveries=1
        )

        assert claimed == 0
        assert client.xlen(stream_key) == 0
        assert client.xpending(stream_key, redis_backend.consumer_group)["pending"] == 0

        task_data = executor.get_task_by_id(result.id)
        assert task_data["status"] == TaskResultStatus.FAILED
        error = json.loads(task_data["errors_json"])[-1]
        assert error["exception_class_path"] == (
            "django_tasks_redis.exceptions.TaskAbandoned"
        )
        assert redis_backend.get_result(result.id).errors[-1].exception_class is (
            TaskAbandoned
        )


@pytest.mark.django_db
class TestWorkerResilience:
    """A task the worker cannot even start must not stop the worker."""

    def test_worker_survives_a_task_it_cannot_load(self, redis_backend, clean_redis):
        """A task_path a deploy removed is recorded, not fatal."""
        from io import StringIO

        from django.core.management import call_command

        from tests.tasks import simple_task

        client = redis_backend.get_client()
        result = simple_task.enqueue(1, 2)
        set_task_fields(
            redis_backend, result.id, mapping={"task_path": "tests.tasks.gone_away"}
        )

        call_command("run_redis_tasks", stdout=StringIO(), stderr=StringIO())

        task_data = executor.get_task_by_id(result.id)
        assert task_data["status"] == TaskResultStatus.FAILED
        assert "AttributeError" in task_data["errors_json"]
        # Left pending on purpose: the message is still recoverable.
        stream_key = normal_stream_key(redis_backend)
        assert client.xpending(stream_key, redis_backend.consumer_group)["pending"] == 1


@pytest.mark.django_db
class TestExternalTriggerClaim:
    """A task delivered twice by an external trigger must run once."""

    def test_second_claim_of_the_same_task_loses(self, redis_backend, clean_redis):
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)

        assert redis_backend.transition_task_status(
            result.id, TaskResultStatus.RUNNING, [TaskResultStatus.READY]
        )
        assert not redis_backend.transition_task_status(
            result.id, TaskResultStatus.RUNNING, [TaskResultStatus.READY]
        )

    def test_retry_keeps_the_error_history(self, redis_backend, clean_redis):
        """Retrying a failed task adds to its errors instead of erasing them."""
        from tests.tasks import failing_task

        result = failing_task.enqueue()
        executor.run_task_by_id(result.id)
        executor.run_task_by_id(result.id, allow_retry=True)

        task_data = executor.get_task_by_id(result.id)
        assert task_data["status"] == TaskResultStatus.FAILED
        assert len(json.loads(task_data["errors_json"])) == 2

    def test_missing_task_is_not_claimable(self, redis_backend, clean_redis):
        assert not redis_backend.transition_task_status(
            "does-not-exist", TaskResultStatus.RUNNING, [TaskResultStatus.READY]
        )


@pytest.mark.django_db
class TestRunAfterConstraint:
    """A task queued before it is due must not be dropped."""

    def test_task_not_due_yet_returns_to_the_delayed_set(
        self, redis_backend, clean_redis
    ):
        from tests.tasks import simple_task

        client = redis_backend.get_client()
        result = simple_task.enqueue(1, 2)
        run_after = timezone.now() + timedelta(seconds=60)
        set_task_fields(
            redis_backend,
            result.id,
            mapping={"run_after": serialize_datetime(run_after)},
        )

        assert executor.fetch_task() is None

        delayed_key = get_delayed_key(
            redis_backend.key_prefix, redis_backend.alias, "default"
        )
        assert client.zscore(delayed_key, result.id) == pytest.approx(
            run_after.timestamp()
        )
        assert client.xlen(normal_stream_key(redis_backend)) == 0

    def test_result_outlives_a_delay_longer_than_the_ttl(
        self, redis_backend, clean_redis
    ):
        """A task deferred past REDIS_RESULT_TTL must still be there when due."""
        from tests.tasks import simple_task

        client = redis_backend.get_client()
        delay = redis_backend.result_ttl * 2
        result = simple_task.using(
            run_after=timezone.now() + timedelta(seconds=delay)
        ).enqueue(1, 2)

        ttl = client.ttl(
            get_result_key(redis_backend.key_prefix, redis_backend.alias, result.id)
        )

        assert ttl > delay

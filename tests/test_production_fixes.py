"""
Tests for the production fixes:

1. Prometheus collector registration must never touch Redis, and status
   counts / READY-age metrics must cost a bounded number of commands
   independent of how many results are stored (per-status index).
2. Task streams are trimmed of acked history (XTRIM MINID at the consumer
   group's safe position) once they exceed REDIS_STREAM_MAXLEN; backlog
   and pending entries are never trimmed.
3. Stream consumers are cleaned up on shutdown, idle consumers are
   reaped, and stale-task claiming no longer creates throwaway consumers.
"""

from unittest.mock import Mock

import pytest
from django.tasks.base import TaskResultStatus

from django_tasks_redis import executor
from django_tasks_redis.utils import (
    get_priority_stream_key,
    get_status_index_synced_key,
)

from .tasks import email_task, failing_task, simple_task


def _prometheus_available():
    try:
        import prometheus_client  # noqa: F401

        return True
    except ImportError:
        return False


class CountingClient:
    """Proxy that counts every command/pipeline issued to a Redis client."""

    def __init__(self, client):
        self._client = client
        self.calls = 0

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if callable(attr):

            def wrapper(*args, **kwargs):
                self.calls += 1
                return attr(*args, **kwargs)

            return wrapper
        return attr


@pytest.fixture
def indexed_backend(redis_backend, clean_redis):
    """Backend with a freshly built status index."""
    redis_backend.rebuild_status_index()
    return redis_backend


def _make_backend(**extra_options):
    """Create a standalone backend against the test Redis."""
    from django_tasks_redis.backends import RedisTaskBackend

    options = {
        "REDIS_URL": "redis://localhost:6379/0",
        "REDIS_KEY_PREFIX": "django_tasks_test",
        "REDIS_RESULT_TTL": 3600,
    }
    options.update(extra_options)
    return RedisTaskBackend(alias="default", params={"QUEUES": [], "OPTIONS": options})


# ---------------------------------------------------------------------------
# Issue 1: metrics registration and O(1) status counts
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _prometheus_available(), reason="prometheus-client missing")
class TestCollectorRegistration:
    def test_describe_touches_no_backend(self):
        """describe() must not perform any backend (Redis) call."""
        from django_tasks_redis.metrics.collectors import RedisTaskMetricsCollector

        mock_backend = Mock()
        mock_backend.alias = "test"
        collector = RedisTaskMetricsCollector(mock_backend)
        mock_backend.reset_mock()

        families = collector.describe()

        assert mock_backend.method_calls == []
        names = {f.name for f in families}
        assert names == {
            "django_tasks_queue_length",
            "django_tasks_queue_oldest_ready_age_seconds",
            "django_tasks_queue_newest_ready_age_seconds",
        }
        # Described families carry no samples.
        assert all(f.samples == [] for f in families)

    def test_registration_performs_zero_redis_commands(self):
        """
        Registering with an auto-describing registry (the prometheus-client
        default) must not call collect() and therefore never touch Redis.
        """
        from prometheus_client import CollectorRegistry

        from django_tasks_redis.metrics.collectors import RedisTaskMetricsCollector

        mock_backend = Mock()
        mock_backend.alias = "test"
        collector = RedisTaskMetricsCollector(mock_backend)
        mock_backend.reset_mock()

        registry = CollectorRegistry(auto_describe=True)
        registry.register(collector)

        assert mock_backend.method_calls == []

    def test_duplicate_registration_still_detected(self):
        """describe() keeps duplicate-registration detection working."""
        from prometheus_client import CollectorRegistry

        from django_tasks_redis.metrics.collectors import RedisTaskMetricsCollector

        mock_backend = Mock()
        mock_backend.alias = "test"
        registry = CollectorRegistry(auto_describe=True)
        registry.register(RedisTaskMetricsCollector(mock_backend))

        with pytest.raises(ValueError, match="Duplicated"):
            registry.register(RedisTaskMetricsCollector(mock_backend))


@pytest.mark.django_db
class TestStatusIndex:
    def _counts(self, backend, **kwargs):
        counts = backend.get_status_counts(**kwargs)
        return {str(k): v for k, v in counts.items()}

    def test_counts_track_full_lifecycle(self, indexed_backend):
        backend = indexed_backend

        result = simple_task.enqueue(1, 2)
        assert self._counts(backend)["READY"] == 1

        executor.process_one_task()
        counts = self._counts(backend)
        assert counts["READY"] == 0
        assert counts["SUCCESSFUL"] == 1

        failing_task.enqueue()
        executor.process_one_task()
        counts = self._counts(backend)
        assert counts["FAILED"] == 1

        # Retry: FAILED -> READY
        failed_id = next(
            t["task_id"] for t in backend.get_all_tasks(status="FAILED")[0]
        )
        backend.reset_task_status(failed_id)
        counts = self._counts(backend)
        assert counts["FAILED"] == 0
        assert counts["READY"] == 1

        # Deletion removes from the index
        backend.delete_task_data(failed_id)
        backend.delete_task_data(result.id)
        counts = self._counts(backend)
        assert counts == {"READY": 0, "RUNNING": 0, "SUCCESSFUL": 0, "FAILED": 0}

    def test_counts_bounded_commands_independent_of_result_count(self, indexed_backend):
        """get_status_counts must not scale with the number of results."""
        backend = indexed_backend

        for _ in range(5):
            simple_task.enqueue(1, 2)

        counter = CountingClient(backend.get_client())
        backend._client = counter
        try:
            counts = self._counts(backend)
            calls_small = counter.calls
        finally:
            backend._client = counter._client
        assert counts["READY"] == 5

        for _ in range(45):
            simple_task.enqueue(1, 2)

        counter = CountingClient(backend.get_client())
        backend._client = counter
        try:
            counts = self._counts(backend)
            calls_large = counter.calls
        finally:
            backend._client = counter._client
        assert counts["READY"] == 50

        assert calls_large == calls_small
        assert calls_small <= 10

    def test_queue_scoped_counts(self, indexed_backend):
        backend = indexed_backend
        simple_task.enqueue(1, 2)
        email_task.enqueue("a@example.com", "hi", "body")

        assert self._counts(backend, queue_name="emails")["READY"] == 1
        assert self._counts(backend, queue_name="default")["READY"] == 1
        assert self._counts(backend)["READY"] == 2

    def test_legacy_fallback_when_index_not_built(self, redis_backend, clean_redis):
        backend = redis_backend
        simple_task.enqueue(1, 2)

        # Simulate an unmigrated deployment: no marker, no cached flag.
        client = backend.get_client()
        client.delete(get_status_index_synced_key(backend.key_prefix, backend.alias))
        for key in client.scan_iter(
            match=f"{backend.key_prefix}:{backend.alias}:status_index:*"
        ):
            client.delete(key)
        backend._status_index_synced = False

        assert backend.has_status_index() is False
        assert self._counts(backend)["READY"] == 1

    def test_rebuild_matches_reality(self, redis_backend, clean_redis):
        backend = redis_backend
        for _ in range(3):
            simple_task.enqueue(1, 2)
        executor.process_one_task()
        failing_task.enqueue()
        executor.process_one_task()  # may pick any READY task

        # Wipe the index and rebuild from the hashes.
        client = backend.get_client()
        for key in client.scan_iter(
            match=f"{backend.key_prefix}:{backend.alias}:status_index:*"
        ):
            client.delete(key)
        backend._status_index_synced = False

        indexed = backend.rebuild_status_index()
        assert indexed == 4
        assert backend.has_status_index() is True

        fast = self._counts(backend)

        # Compare against a ground-truth scan of the hashes.
        expected = {"READY": 0, "RUNNING": 0, "SUCCESSFUL": 0, "FAILED": 0}
        tasks, _total = backend.get_all_tasks(limit=1000)
        for task in tasks:
            expected[task["status"]] += 1
        assert fast == expected

    def test_ready_age_bounds(self, indexed_backend):
        backend = indexed_backend
        assert backend.get_ready_age_bounds() is None

        simple_task.enqueue(1, 2)
        simple_task.enqueue(3, 4)
        bounds = backend.get_ready_age_bounds()
        assert bounds is not None
        oldest, newest = bounds
        assert oldest >= newest >= 0.0
        assert oldest < 60.0

        executor.process_tasks()
        assert backend.get_ready_age_bounds() is None

    @pytest.mark.skipif(not _prometheus_available(), reason="prometheus-client missing")
    def test_collect_bounded_commands_with_index(self, indexed_backend):
        """A scrape must not perform one HGETALL per stored result."""
        from django_tasks_redis.metrics.collectors import RedisTaskMetricsCollector

        backend = indexed_backend
        for _ in range(50):
            simple_task.enqueue(1, 2)

        collector = RedisTaskMetricsCollector(backend)
        counter = CountingClient(backend.get_client())
        backend._client = counter
        try:
            metrics = list(collector.collect())
        finally:
            backend._client = counter._client

        assert counter.calls <= 12
        by_name = {m.name: m for m in metrics}
        length = by_name["django_tasks_queue_length"]
        ready_sample = next(s for s in length.samples if s.labels["status"] == "READY")
        assert ready_sample.value == 50
        assert "django_tasks_queue_oldest_ready_age_seconds" in by_name


# ---------------------------------------------------------------------------
# Issue 2: stream trimming
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestStreamTrimming:
    def test_default_maxlen(self, redis_backend):
        assert redis_backend.stream_maxlen == 10000

    def test_acked_history_is_trimmed(self, clean_redis, redis_connection):
        backend = _make_backend(REDIS_STREAM_MAXLEN=10, REDIS_STREAM_TRIM_INTERVAL=0)

        for i in range(300):
            backend.enqueue(simple_task, (i, i), {})

        stream_key = get_priority_stream_key(
            backend.key_prefix, backend.alias, "default", "normal"
        )
        # All 300 entries are an undelivered backlog: nothing may be trimmed.
        assert redis_connection.xlen(stream_key) == 300

        # Deliver and acknowledge everything, then trigger one more XADD.
        results = executor.process_tasks(worker_id="trim-worker")
        assert len(results) == 300
        backend.enqueue(simple_task, (1, 1), {})

        # The acked history is trimmed (XTRIM MINID ~ trims whole macro
        # nodes, so a partial node may survive) — but far below 301.
        assert redis_connection.xlen(stream_key) <= 150

        # And the freshly added entry is still deliverable.
        result = executor.process_one_task(worker_id="trim-worker")
        assert result is not None
        assert result.status == TaskResultStatus.SUCCESSFUL

    def test_backlog_is_never_trimmed(self, clean_redis, redis_connection):
        """A backlog larger than the target must never lose entries."""
        backend = _make_backend(REDIS_STREAM_MAXLEN=10, REDIS_STREAM_TRIM_INTERVAL=0)

        # Consume-and-ack a first batch so the group has acked history and
        # a last-delivered position (the trimmable region is non-empty).
        for i in range(20):
            backend.enqueue(simple_task, (i, i), {})
        assert len(executor.process_tasks(worker_id="backlog-worker")) == 20

        # Now pile up an undelivered backlog far beyond the target.
        for i in range(200):
            backend.enqueue(simple_task, (i, i), {})

        stream_key = get_priority_stream_key(
            backend.key_prefix, backend.alias, "default", "normal"
        )
        # Every backlog entry survives; only acked history may be gone.
        entries = redis_connection.xrange(stream_key, "-", "+")
        backlog_ids = {data["task_id"] for _id, data in entries}
        ready_tasks, total = backend.get_all_tasks(status="READY", limit=300)
        assert total == 200
        assert {t["task_id"] for t in ready_tasks} <= backlog_ids

    def test_trimming_disabled_with_none(self, clean_redis, redis_connection):
        backend = _make_backend(REDIS_STREAM_MAXLEN=None)

        for i in range(50):
            backend.enqueue(simple_task, (i, i), {})

        stream_key = get_priority_stream_key(
            backend.key_prefix, backend.alias, "default", "normal"
        )
        assert redis_connection.xlen(stream_key) == 50


# ---------------------------------------------------------------------------
# Issue 3: consumer lifecycle
# ---------------------------------------------------------------------------


def _consumers(client, backend, queue="default", level="normal"):
    stream_key = get_priority_stream_key(
        backend.key_prefix, backend.alias, queue, level
    )
    try:
        return {
            c["name"]: c
            for c in client.xinfo_consumers(stream_key, backend.consumer_group)
        }
    except Exception:
        return {}


@pytest.mark.django_db
class TestConsumerLifecycle:
    def test_process_worker_id_is_stable(self):
        assert executor._get_process_worker_id() == executor._get_process_worker_id()

    def test_cleanup_worker_removes_idle_consumer(self, redis_backend, clean_redis):
        backend = redis_backend
        simple_task.enqueue(1, 2)
        executor.process_one_task(worker_id="worker-a")

        client = backend.get_client()
        assert "worker-a" in _consumers(client, backend)

        requeued = executor.cleanup_worker(worker_id="worker-a")
        assert requeued == 0
        assert "worker-a" not in _consumers(client, backend)

    def test_cleanup_worker_requeues_pending_entries(self, redis_backend, clean_redis):
        backend = redis_backend
        simple_task.enqueue(1, 2)

        # Deliver without acking: the entry stays in worker-b's PEL.
        task_data = executor.fetch_task(worker_id="worker-b")
        assert task_data is not None
        client = backend.get_client()
        assert _consumers(client, backend)["worker-b"]["pending"] == 1

        requeued = executor.cleanup_worker(worker_id="worker-b")
        assert requeued == 1
        assert "worker-b" not in _consumers(client, backend)

        # The task is runnable again by anyone.
        result = executor.process_one_task(worker_id="worker-c")
        assert result is not None
        assert result.status == TaskResultStatus.SUCCESSFUL

    def test_reap_idle_consumers_spares_pending(self, redis_backend, clean_redis):
        backend = redis_backend
        simple_task.enqueue(1, 2)
        simple_task.enqueue(3, 4)

        # dead-idle: delivered and acked, holds nothing.
        executor.process_one_task(worker_id="dead-idle")
        # dead-pending: delivered, never acked.
        assert executor.fetch_task(worker_id="dead-pending") is not None

        client = backend.get_client()
        reaped = executor.reap_idle_consumers(min_idle_seconds=0)
        assert reaped == 1

        consumers = _consumers(client, backend)
        assert "dead-idle" not in consumers
        assert "dead-pending" in consumers  # never reap holders of pending

    def test_reap_respects_exclude(self, redis_backend, clean_redis):
        backend = redis_backend
        simple_task.enqueue(1, 2)
        executor.process_one_task(worker_id="me")

        reaped = executor.reap_idle_consumers(min_idle_seconds=0, exclude="me")
        assert reaped == 0
        assert "me" in _consumers(backend.get_client(), backend)

    def test_reap_disabled_via_option(self, clean_redis):
        backend = _make_backend(REDIS_CONSUMER_IDLE_TIMEOUT=None)
        assert backend.consumer_idle_timeout is None

    def test_claim_stale_tasks_requeues_without_throwaway_consumers(
        self, redis_backend, clean_redis
    ):
        backend = redis_backend
        result = simple_task.enqueue(1, 2)

        # Simulate a worker that died mid-task: delivered, status RUNNING,
        # never acked.
        task_data = executor.fetch_task(worker_id="dead-worker")
        assert task_data is not None
        client = backend.get_client()
        from django_tasks_redis.utils import get_result_key

        result_key = get_result_key(backend.key_prefix, backend.alias, result.id)
        client.hset(result_key, "status", TaskResultStatus.RUNNING)
        backend.update_status_index(
            client,
            result.id,
            "default",
            TaskResultStatus.READY,
            TaskResultStatus.RUNNING,
        )

        claimed = executor.claim_stale_tasks(claim_timeout=0, worker_id="reaper")
        assert claimed == 1

        # Status reset, no pending deliveries, no random consumers.
        assert client.hget(result_key, "status") == TaskResultStatus.READY
        stream_key = get_priority_stream_key(
            backend.key_prefix, backend.alias, "default", "normal"
        )
        assert client.xpending(stream_key, backend.consumer_group)["pending"] == 0
        assert set(_consumers(client, backend)) <= {"dead-worker", "reaper"}

        # And the task actually runs again — previously reclaimed tasks
        # were stranded in a throwaway consumer's PEL forever.
        rerun = executor.process_one_task(worker_id="worker-x")
        assert rerun is not None
        assert rerun.status == TaskResultStatus.SUCCESSFUL
        assert rerun.id == result.id

    def test_requeue_loser_of_ack_race_does_nothing(self, redis_backend, clean_redis):
        """
        Two concurrent requeuers (shutdown cleanup vs stale-claim cycle)
        must produce exactly one live copy: the atomic XACK guard makes
        the loser a no-op instead of adding a duplicate entry.
        """
        backend = redis_backend
        simple_task.enqueue(1, 2)

        task_data = executor.fetch_task(worker_id="racer-a")
        assert task_data is not None
        stream_key = task_data["_stream_key"]
        message_id = task_data["_message_id"]
        client = backend.get_client()

        msg_data = {
            "task_id": task_data["task_id"],
            "task_path": task_data["task_path"],
            "priority": task_data["priority"],
            "queue_name": task_data["queue_name"],
            "enqueued_at": task_data["enqueued_at"],
        }

        # Winner requeues (acks + re-adds one copy).
        assert (
            executor._requeue_claimed_message(
                backend, client, stream_key, message_id, msg_data
            )
            is True
        )
        # Loser tries the same message: XACK returns 0, nothing is added.
        assert (
            executor._requeue_claimed_message(
                backend, client, stream_key, message_id, msg_data
            )
            is False
        )

        # Exactly one live copy: process it once, then the queue is empty.
        assert executor.process_one_task(worker_id="racer-b") is not None
        assert executor.process_one_task(worker_id="racer-b") is None

    def test_claim_acks_already_finished_tasks(self, redis_backend, clean_redis):
        backend = redis_backend
        simple_task.enqueue(1, 2)

        # Deliver and fully process, but simulate a lost ack.
        task_data = executor.fetch_task(worker_id="worker-lostack")
        backend.run_task(task_data["task_id"], worker_id="worker-lostack")

        claimed = executor.claim_stale_tasks(claim_timeout=0, worker_id="reaper")
        assert claimed == 1

        client = backend.get_client()
        stream_key = get_priority_stream_key(
            backend.key_prefix, backend.alias, "default", "normal"
        )
        # Finished task was acked, not requeued.
        assert client.xpending(stream_key, backend.consumer_group)["pending"] == 0
        assert executor.process_one_task(worker_id="worker-x") is None


@pytest.mark.django_db
class TestWorkerCommandLifecycle:
    def test_worker_run_leaves_no_consumer_and_builds_index(
        self, redis_backend, clean_redis
    ):
        from django.core.management import call_command

        backend = redis_backend
        simple_task.enqueue(1, 2)

        # Simulate an unmigrated deployment.
        client = backend.get_client()
        client.delete(get_status_index_synced_key(backend.key_prefix, backend.alias))
        backend._status_index_synced = False

        call_command("run_redis_tasks")

        assert backend.has_status_index() is True
        assert self_counts(backend)["SUCCESSFUL"] == 1
        # Graceful exit removed the worker's consumer.
        assert _consumers(client, backend) == {}

    def test_rebuild_index_command(self, redis_backend, clean_redis):
        from django.core.management import call_command

        simple_task.enqueue(1, 2)
        client = redis_backend.get_client()
        client.delete(
            get_status_index_synced_key(redis_backend.key_prefix, redis_backend.alias)
        )
        redis_backend._status_index_synced = False

        call_command("rebuild_redis_task_index")
        assert redis_backend.has_status_index() is True
        assert self_counts(redis_backend)["READY"] == 1


def self_counts(backend, **kwargs):
    return {str(k): v for k, v in backend.get_status_counts(**kwargs).items()}

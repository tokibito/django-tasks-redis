"""
Tests for the status index the task counts are read from.

Every status write moves the task in the index within the same script or
transaction as the hash write, so the index and the hashes agree after any
interleaving of writers; the assertions compare the two directly.
"""

import threading

import pytest
from django.tasks.base import TaskResultStatus
from django.utils import timezone

from django_tasks_redis import executor
from django_tasks_redis.backends import _INDEX_TASK, STATUS_INDEX_ORDER
from django_tasks_redis.utils import (
    get_result_key,
    get_results_index_key,
    get_status_index_built_key,
    get_status_index_key,
)


def index_key(backend, status, queue_name=None):
    return get_status_index_key(backend.key_prefix, backend.alias, status, queue_name)


def index_score(backend, status, task_id, queue_name=None):
    """The task's score in the set of `status`, or None when it is not there."""
    return backend.get_client().zscore(index_key(backend, status, queue_name), task_id)


def statuses_holding(backend, task_id, queue_name="default"):
    """The statuses whose backend-wide and queue sets both hold the task."""
    held = set()
    for status in STATUS_INDEX_ORDER:
        backend_wide = index_score(backend, status, task_id) is not None
        queued = index_score(backend, status, task_id, queue_name) is not None
        assert backend_wide == queued, f"{status}: the two sets disagree"
        if backend_wide:
            held.add(status)
    return held


def wipe_index(backend):
    """Leave the hashes as they are and forget the index, marker included."""
    client = backend.get_client()
    keys = [
        key for key in client.keys(f"{backend.key_prefix}:*") if "status_index" in key
    ]
    if keys:
        client.delete(*keys)
    backend._status_index_built = False


def scan_counts(backend, queue_name=None):
    counts, _oldest, _newest = backend._scan_status_counts(queue_name)
    return counts


@pytest.mark.django_db
class TestStatusIndexWrites:
    """Every status write leaves the task in exactly one status' sets."""

    def test_enqueue_indexes_the_task_as_ready(self, redis_backend, clean_redis):
        """An enqueued task is READY, scored by its enqueue time."""
        from tests.tasks import simple_task

        before = timezone.now().timestamp()
        result = simple_task.enqueue(1, 2)
        after = timezone.now().timestamp()

        assert statuses_holding(redis_backend, result.id) == {TaskResultStatus.READY}
        score = index_score(redis_backend, TaskResultStatus.READY, result.id)
        assert before <= score <= after

    def test_delayed_task_is_scored_by_its_run_after(self, redis_backend, clean_redis):
        """A delayed task starts waiting when it comes due, not when enqueued."""
        from tests.tasks import simple_task

        run_after = timezone.now() + timezone.timedelta(minutes=10)
        result = simple_task.using(run_after=run_after).enqueue(1, 2)

        score = index_score(redis_backend, TaskResultStatus.READY, result.id)
        assert score == run_after.timestamp()

    def test_task_in_another_queue_is_indexed_under_that_queue(
        self, redis_backend, clean_redis
    ):
        """The queue sets are the task's own queue's, the backend-wide ones all."""
        from tests.tasks import email_task

        result = email_task.enqueue("to@example.com", "Hi", "body")

        assert statuses_holding(redis_backend, result.id, "emails") == {
            TaskResultStatus.READY
        }
        assert (
            index_score(redis_backend, TaskResultStatus.READY, result.id, "default")
            is None
        )

    def test_claim_moves_the_task_to_running(self, redis_backend, clean_redis):
        """A claimed task leaves READY and joins RUNNING, scored by the attempt."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        before = timezone.now().timestamp()

        assert redis_backend.claim_task(result.id, worker_id="w1")

        assert statuses_holding(redis_backend, result.id) == {TaskResultStatus.RUNNING}
        assert index_score(redis_backend, TaskResultStatus.RUNNING, result.id) >= before

    def test_lost_claim_leaves_the_index_alone(self, redis_backend, clean_redis):
        """A claim that finds the task in another status writes nothing."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        redis_backend.run_task(result.id)

        assert not redis_backend.claim_task(result.id)

        assert statuses_holding(redis_backend, result.id) == {
            TaskResultStatus.SUCCESSFUL
        }

    def test_successful_run_indexes_the_task_as_successful(
        self, redis_backend, clean_redis
    ):
        """A finished task is SUCCESSFUL, scored by finished_at."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        final = redis_backend.run_task(result.id)

        assert statuses_holding(redis_backend, result.id) == {
            TaskResultStatus.SUCCESSFUL
        }
        score = index_score(redis_backend, TaskResultStatus.SUCCESSFUL, result.id)
        assert score == final.finished_at.timestamp()

    def test_failed_run_indexes_the_task_as_failed(self, redis_backend, clean_redis):
        """A task whose run raised is FAILED, scored by finished_at."""
        from tests.tasks import failing_task

        result = failing_task.enqueue()
        final = redis_backend.run_task(result.id)

        assert statuses_holding(redis_backend, result.id) == {TaskResultStatus.FAILED}
        score = index_score(redis_backend, TaskResultStatus.FAILED, result.id)
        assert score == final.finished_at.timestamp()

    def test_retry_of_a_failed_task_moves_it_through_running(
        self, redis_backend, clean_redis
    ):
        """A retry claims the task out of FAILED and lands it in FAILED again."""
        from tests.tasks import failing_task

        result = failing_task.enqueue()
        redis_backend.run_task(result.id)

        assert redis_backend.claim_task(
            result.id, from_statuses=[TaskResultStatus.READY, TaskResultStatus.FAILED]
        )
        assert statuses_holding(redis_backend, result.id) == {TaskResultStatus.RUNNING}

    def test_released_task_is_ready_again(self, redis_backend, clean_redis):
        """A task handed back by the stale sweep is READY, scored by the release."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        redis_backend.claim_task(result.id, worker_id="dead")
        before = timezone.now().timestamp()

        assert redis_backend.transition_task_status(
            result.id, TaskResultStatus.READY, [TaskResultStatus.RUNNING]
        )

        assert statuses_holding(redis_backend, result.id) == {TaskResultStatus.READY}
        assert index_score(redis_backend, TaskResultStatus.READY, result.id) >= before

    def test_refused_transition_leaves_the_index_alone(
        self, redis_backend, clean_redis
    ):
        """A transition from a status the task is not in writes nothing."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)

        assert not redis_backend.transition_task_status(
            result.id, TaskResultStatus.FAILED, [TaskResultStatus.RUNNING]
        )

        assert statuses_holding(redis_backend, result.id) == {TaskResultStatus.READY}

    def test_abandoned_task_is_failed(self, redis_backend, clean_redis):
        """A task the queue gave up on is FAILED."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)

        assert redis_backend.mark_task_failed(result.id, "given up")

        assert statuses_holding(redis_backend, result.id) == {TaskResultStatus.FAILED}

    def test_reset_task_is_ready_again(self, redis_backend, clean_redis):
        """A task reset for a retry is READY, scored by the reset."""
        from tests.tasks import failing_task

        result = failing_task.enqueue()
        redis_backend.run_task(result.id)
        before = timezone.now().timestamp()

        assert redis_backend.reset_task_status(result.id)

        assert statuses_holding(redis_backend, result.id) == {TaskResultStatus.READY}
        assert index_score(redis_backend, TaskResultStatus.READY, result.id) >= before

    def test_deleted_task_leaves_every_set(self, redis_backend, clean_redis):
        """A deleted task is in no set at all."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        redis_backend.run_task(result.id)

        assert redis_backend.delete_task_data(result.id)

        assert statuses_holding(redis_backend, result.id) == set()

    def test_deleting_a_task_whose_hash_is_gone_sweeps_the_backend_wide_sets(
        self, redis_backend, clean_redis
    ):
        """Without the hash the queue is unknown; the backend-wide sets are swept."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        client = redis_backend.get_client()
        client.delete(
            get_result_key(redis_backend.key_prefix, redis_backend.alias, result.id)
        )

        assert not redis_backend.delete_task_data(result.id)

        assert index_score(redis_backend, TaskResultStatus.READY, result.id) is None
        assert not client.sismember(
            get_results_index_key(redis_backend.key_prefix, redis_backend.alias),
            result.id,
        )

    def test_purge_removes_purged_tasks_from_the_index(
        self, redis_backend, clean_redis
    ):
        """A purge goes through delete_task_data, so the index follows."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        redis_backend.run_task(result.id)

        assert executor.purge_completed_tasks(days=0) == 1

        assert statuses_holding(redis_backend, result.id) == set()
        assert redis_backend.get_status_counts()[TaskResultStatus.SUCCESSFUL] == 0


@pytest.mark.django_db
class TestStatusIndexCounts:
    """The counts come from the index and agree with the hashes."""

    def enqueue_a_mix(self, redis_backend):
        """Two queues, every status, and a delayed task."""
        from tests.tasks import email_task, failing_task, simple_task

        ready = [simple_task.enqueue(i, i) for i in range(3)]
        simple_task.using(
            run_after=timezone.now() + timezone.timedelta(minutes=5)
        ).enqueue(9, 9)
        running = simple_task.enqueue(4, 4)
        redis_backend.claim_task(running.id, worker_id="w1")
        for i in range(2):
            redis_backend.run_task(simple_task.enqueue(i, i).id)
        redis_backend.run_task(failing_task.enqueue().id)
        emails = [email_task.enqueue("to@example.com", "Hi", "body") for _ in range(2)]
        redis_backend.run_task(emails[0].id)
        return ready

    def test_counts_agree_with_a_scan(self, redis_backend, clean_redis):
        """Backend-wide and per queue, the index counts what a scan counts."""
        self.enqueue_a_mix(redis_backend)

        for queue_name in (None, "default", "emails", "nothing-here"):
            assert redis_backend.get_status_counts(queue_name) == scan_counts(
                redis_backend, queue_name
            ), queue_name

        counts = redis_backend.get_status_counts()
        assert counts == {
            TaskResultStatus.READY: 5,
            TaskResultStatus.RUNNING: 1,
            TaskResultStatus.SUCCESSFUL: 3,
            TaskResultStatus.FAILED: 1,
        }
        assert redis_backend.get_status_counts("emails") == {
            TaskResultStatus.READY: 1,
            TaskResultStatus.RUNNING: 0,
            TaskResultStatus.SUCCESSFUL: 1,
            TaskResultStatus.FAILED: 0,
        }

    def test_counts_are_read_from_the_index(self, redis_backend, clean_redis):
        """The hashes are not consulted once the index is built."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        assert redis_backend.get_status_counts()[TaskResultStatus.READY] == 1

        for queue_name in (None, "default"):
            redis_backend.get_client().zrem(
                index_key(redis_backend, TaskResultStatus.READY, queue_name), result.id
            )

        assert redis_backend.get_status_counts()[TaskResultStatus.READY] == 0

    def test_queue_stats_agree_with_a_scan(self, redis_backend, clean_redis):
        """The waiting-time bounds come out of the READY set's two ends."""
        self.enqueue_a_mix(redis_backend)

        for queue_name in (None, "default", "emails"):
            counts, oldest, newest = redis_backend._scan_status_counts(queue_name)
            stats = redis_backend.get_queue_stats(queue_name)
            assert stats["pending_count"] == counts[TaskResultStatus.READY]
            assert stats["oldest_pending_waiting_since"] == oldest, queue_name
            assert stats["newest_pending_waiting_since"] == newest, queue_name

    def test_expired_entries_are_dropped(self, redis_backend, clean_redis):
        """An entry older than its status' TTL has no hash and is not counted."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        client = redis_backend.get_client()
        # What expiry leaves behind: no hash, and the entry at its old score.
        client.delete(
            get_result_key(redis_backend.key_prefix, redis_backend.alias, result.id)
        )
        expired = timezone.now().timestamp() - redis_backend.result_ttl - 1
        for queue_name in (None, "default"):
            client.zadd(
                index_key(redis_backend, TaskResultStatus.READY, queue_name),
                {result.id: expired},
            )

        assert redis_backend.get_status_counts()[TaskResultStatus.READY] == 0
        assert redis_backend.get_status_counts("default")[TaskResultStatus.READY] == 0
        assert statuses_holding(redis_backend, result.id) == set()

    def test_a_write_prunes_the_set_it_adds_to(self, redis_backend, clean_redis):
        """Expired entries go when the set is written, not only when it is read."""
        from tests.tasks import simple_task

        stale = simple_task.enqueue(1, 2)
        client = redis_backend.get_client()
        expired = timezone.now().timestamp() - redis_backend.result_ttl - 1
        for queue_name in (None, "default"):
            client.zadd(
                index_key(redis_backend, TaskResultStatus.READY, queue_name),
                {stale.id: expired},
            )

        simple_task.enqueue(3, 4)

        assert statuses_holding(redis_backend, stale.id) == set()

    def test_entries_are_kept_when_results_never_expire(
        self, redis_backend, clean_redis
    ):
        """With a TTL of 0 nothing is pruned, since nothing expires."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        client = redis_backend.get_client()
        ancient = timezone.now().timestamp() - 10 * redis_backend.result_ttl
        for queue_name in (None, "default"):
            client.zadd(
                index_key(redis_backend, TaskResultStatus.READY, queue_name),
                {result.id: ancient},
            )

        original = redis_backend.result_ttl
        redis_backend.result_ttl = 0
        try:
            assert redis_backend.get_status_counts()[TaskResultStatus.READY] == 1
        finally:
            redis_backend.result_ttl = original


@pytest.mark.django_db
class TestStatusIndexBuild:
    """Until the index is built the counts are read from the hashes."""

    def test_a_fresh_backend_trusts_the_index_from_the_first_task(
        self, redis_backend, clean_redis
    ):
        """No stored result means nothing to rebuild: the marker is set."""
        from tests.tasks import simple_task

        redis_backend._status_index_built = False
        marker = get_status_index_built_key(
            redis_backend.key_prefix, redis_backend.alias
        )
        assert not redis_backend.get_client().exists(marker)

        simple_task.enqueue(1, 2)

        assert redis_backend.get_client().exists(marker)
        assert redis_backend.has_status_index()

    def test_stored_results_without_the_index_are_not_trusted(
        self, redis_backend, clean_redis
    ):
        """Results from before the index existed leave it unbuilt."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        wipe_index(redis_backend)

        assert not redis_backend.has_status_index()
        marker = get_status_index_built_key(
            redis_backend.key_prefix, redis_backend.alias
        )
        assert not redis_backend.get_client().exists(marker)

    def test_counts_fall_back_to_a_scan_until_built(
        self, redis_backend, clean_redis, caplog
    ):
        """An unbuilt index is not read; the scan answers, with a warning once."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        simple_task.enqueue(3, 4)
        wipe_index(redis_backend)
        redis_backend._status_index_warned = False

        with caplog.at_level("WARNING", logger="django_tasks_redis"):
            counts = redis_backend.get_status_counts()
            stats = redis_backend.get_queue_stats()

        assert counts[TaskResultStatus.READY] == 2
        assert stats["pending_count"] == 2
        warnings = [
            r for r in caplog.records if "rebuild_redis_status_index" in r.message
        ]
        assert len(warnings) == 1
        assert "'default'" in warnings[0].message

    def test_rebuild_indexes_the_stored_results(self, redis_backend, clean_redis):
        """A rebuild reads every hash and scores each by the time it entered its status."""
        from tests.tasks import failing_task, simple_task

        run_after = timezone.now() + timezone.timedelta(minutes=5)
        delayed = simple_task.using(run_after=run_after).enqueue(1, 1)
        ready = simple_task.enqueue(2, 2)
        done = redis_backend.run_task(simple_task.enqueue(3, 3).id)
        failed = redis_backend.run_task(failing_task.enqueue().id)
        running = simple_task.enqueue(4, 4)
        redis_backend.claim_task(running.id, worker_id="w1")
        before = scan_counts(redis_backend)
        wipe_index(redis_backend)

        indexed = redis_backend.rebuild_status_index()

        assert indexed == 5
        assert redis_backend.has_status_index()
        assert redis_backend.get_status_counts() == before
        assert index_score(redis_backend, TaskResultStatus.READY, delayed.id) == (
            run_after.timestamp()
        )
        ready_data = redis_backend.get_task_data(ready.id)
        assert index_score(redis_backend, TaskResultStatus.READY, ready.id) == (
            redis_backend._waiting_since(ready_data).timestamp()
        )
        assert index_score(redis_backend, TaskResultStatus.SUCCESSFUL, done.id) == (
            done.finished_at.timestamp()
        )
        assert index_score(redis_backend, TaskResultStatus.FAILED, failed.id) == (
            failed.finished_at.timestamp()
        )
        running_data = redis_backend.get_task_data(running.id)
        assert (
            index_score(redis_backend, TaskResultStatus.RUNNING, running.id)
            == (
                redis_backend._status_since(
                    running_data, TaskResultStatus.RUNNING, None
                )
            ).timestamp()
        )

    def test_rebuild_moves_a_task_the_index_had_wrong(self, redis_backend, clean_redis):
        """A stale entry under another status is swept by the rebuild."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        redis_backend.run_task(result.id)
        client = redis_backend.get_client()
        for queue_name in (None, "default"):
            client.zadd(
                index_key(redis_backend, TaskResultStatus.READY, queue_name),
                {result.id: timezone.now().timestamp()},
            )

        redis_backend.rebuild_status_index()

        assert statuses_holding(redis_backend, result.id) == {
            TaskResultStatus.SUCCESSFUL
        }

    def test_rebuild_honours_the_batch_size(self, redis_backend, clean_redis):
        """A batch smaller than the result count still indexes everything."""
        from tests.tasks import simple_task

        for i in range(5):
            simple_task.enqueue(i, i)
        wipe_index(redis_backend)

        assert redis_backend.rebuild_status_index(batch_size=2) == 5
        assert redis_backend.get_status_counts()[TaskResultStatus.READY] == 5

    def test_rebuild_leaves_a_task_that_moved_on_to_its_transition(
        self, redis_backend, clean_redis
    ):
        """The index script re-reads the status: a stale read indexes nothing."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        # Read as READY by the rebuild, then run by a worker before the
        # rebuild's script reaches it.
        redis_backend.run_task(result.id)

        client = redis_backend.get_client()
        outcome = client.register_script(_INDEX_TASK)(
            keys=[
                get_result_key(
                    redis_backend.key_prefix, redis_backend.alias, result.id
                ),
                *redis_backend._status_index_keys("default"),
            ],
            args=[
                result.id,
                TaskResultStatus.READY,
                repr(timezone.now().timestamp()),
                "",
            ],
        )

        assert outcome == 0
        assert statuses_holding(redis_backend, result.id) == {
            TaskResultStatus.SUCCESSFUL
        }

    def test_rebuild_drops_a_task_whose_hash_is_gone(self, redis_backend, clean_redis):
        """A hash that expired under the rebuild is taken out of every set."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        client = redis_backend.get_client()
        client.delete(
            get_result_key(redis_backend.key_prefix, redis_backend.alias, result.id)
        )

        outcome = client.register_script(_INDEX_TASK)(
            keys=[
                get_result_key(
                    redis_backend.key_prefix, redis_backend.alias, result.id
                ),
                *redis_backend._status_index_keys("default"),
            ],
            args=[
                result.id,
                TaskResultStatus.READY,
                repr(timezone.now().timestamp()),
                "",
            ],
        )

        assert outcome == -1
        assert statuses_holding(redis_backend, result.id) == set()

    def test_rebuild_is_picked_up_by_another_process(self, redis_backend, clean_redis):
        """A negative answer is asked again, so a rebuild elsewhere is seen."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        wipe_index(redis_backend)
        assert not redis_backend.has_status_index()

        # The other process: writes the marker without touching this instance.
        marker = get_status_index_built_key(
            redis_backend.key_prefix, redis_backend.alias
        )
        redis_backend.get_client().set(marker, "1")

        assert redis_backend.has_status_index()


@pytest.mark.django_db
class TestStatusIndexUnderConcurrency:
    """Concurrent workers leave the index agreeing with the hashes."""

    def test_workers_racing_for_tasks_keep_the_index_consistent(
        self, redis_backend, clean_redis
    ):
        """Several workers drain a queue at once; the index matches the hashes after."""
        from tests.tasks import failing_task, simple_task

        for i in range(20):
            simple_task.enqueue(i, i)
            if i % 4 == 0:
                failing_task.enqueue()

        def drain(worker_id):
            executor.process_tasks(worker_id=worker_id)

        workers = [
            threading.Thread(target=drain, args=(f"worker-{n}",)) for n in range(4)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        counts = redis_backend.get_status_counts()
        assert counts == scan_counts(redis_backend)
        assert counts[TaskResultStatus.READY] == 0
        assert counts[TaskResultStatus.RUNNING] == 0
        assert counts[TaskResultStatus.SUCCESSFUL] == 20
        assert counts[TaskResultStatus.FAILED] == 5

    def test_release_racing_a_finish_leaves_the_last_writer_in_charge(
        self, redis_backend, clean_redis
    ):
        """A stale sweep releasing a task its worker is finishing: the index
        follows whichever hash write landed last, so neither status set keeps
        a stale entry."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        redis_backend.claim_task(result.id, worker_id="slow")

        # The sweep gives up on the worker and hands the task back...
        assert redis_backend.transition_task_status(
            result.id, TaskResultStatus.READY, [TaskResultStatus.RUNNING]
        )
        # ...while the worker finishes it after all.
        task_data = redis_backend.get_task_data(result.id)
        redis_backend._record_error(
            result.id,
            task_data,
            __import__("django.tasks.base", fromlist=["TaskError"]).TaskError(
                exception_class_path="builtins.RuntimeError", traceback="boom"
            ),
        )

        assert (
            redis_backend.get_task_data(result.id)["status"] == TaskResultStatus.FAILED
        )
        assert statuses_holding(redis_backend, result.id) == {TaskResultStatus.FAILED}
        assert redis_backend.get_status_counts() == scan_counts(redis_backend)

"""
Tests for the stream broker: the interface a worker consumes tasks through.

The broker is what run_redis_tasks and the executor functions are built on,
so what matters here is the shape of receive() / ack() / nack() rather than
the outcome of a task run, which the executor tests cover.
"""

import time

import pytest
from django.conf import settings
from django.tasks.base import TaskResultStatus

from django_tasks_redis import executor
from django_tasks_redis.backends import RedisTaskBackend
from django_tasks_redis.brokers import BrokerMessage, PullBroker, RedisStreamsBroker
from django_tasks_redis.utils import get_priority_stream_key

WORKER = "broker-test-worker"


def pending(backend, stream_key):
    return backend.get_client().xpending(stream_key, backend.consumer_group)["pending"]


def stream_key_for(backend, priority_level="normal", queue_name="default"):
    return get_priority_stream_key(
        backend.key_prefix, backend.alias, queue_name, priority_level
    )


class TestBrokerMessage:
    def test_carries_the_task_id_and_the_handle(self):
        message = BrokerMessage("abc", handle=("stream", "1-0"), raw={"task_id": "abc"})

        assert message.task_id == "abc"
        assert message.handle == ("stream", "1-0")
        assert message.raw == {"task_id": "abc"}
        assert repr(message) == "<BrokerMessage task_id='abc'>"


class TestPullBrokerInterface:
    def test_receive_and_ack_must_be_implemented(self):
        broker = PullBroker(backend=None)

        with pytest.raises(NotImplementedError):
            broker.receive()
        with pytest.raises(NotImplementedError):
            broker.ack(BrokerMessage("abc"))

    def test_nack_and_close_are_optional(self):
        broker = PullBroker(backend=None)

        assert broker.nack(BrokerMessage("abc")) is None
        assert broker.close() is None


@pytest.mark.django_db
class TestBackendBroker:
    def test_backend_builds_a_stream_broker(self, redis_backend):
        broker = redis_backend.broker

        assert isinstance(broker, RedisStreamsBroker)
        assert isinstance(broker, PullBroker)
        assert broker.backend is redis_backend
        assert broker.options is redis_backend.options

    def test_broker_class_can_be_replaced(self):
        class RecordingBroker(RedisStreamsBroker):
            pass

        class RecordingBackend(RedisTaskBackend):
            broker_class = RecordingBroker

        backend = RecordingBackend(
            "recording", {"QUEUES": [], "OPTIONS": settings.REDIS_OPTIONS}
        )

        assert type(backend.broker) is RecordingBroker

    def test_broker_reads_its_settings_from_the_backend(self, redis_backend):
        broker = redis_backend.broker

        assert broker.consumer_group == redis_backend.consumer_group
        assert broker.stream_keys("emails") == [
            stream_key_for(redis_backend, level, "emails")
            for level in ("high", "normal", "low")
        ]


@pytest.mark.django_db
class TestReceive:
    def test_returns_a_message_for_a_queued_task(self, redis_backend, clean_redis):
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)

        messages = redis_backend.broker.receive(worker_id=WORKER)

        assert len(messages) == 1
        message = messages[0]
        assert isinstance(message, BrokerMessage)
        assert message.task_id == result.id
        stream_key, message_id = message.handle
        assert stream_key == stream_key_for(redis_backend)
        assert message_id
        assert message.raw["task_id"] == result.id

    def test_returns_nothing_when_the_queue_is_empty(self, redis_backend, clean_redis):
        assert redis_backend.broker.receive(worker_id=WORKER) == []

    def test_message_stays_pending_until_acknowledged(self, redis_backend, clean_redis):
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        broker = redis_backend.broker
        stream_key = stream_key_for(redis_backend)

        (message,) = broker.receive(worker_id=WORKER)
        assert pending(redis_backend, stream_key) == 1

        broker.ack(message)

        assert pending(redis_backend, stream_key) == 0
        assert redis_backend.get_client().xlen(stream_key) == 0

    def test_nacked_message_is_served_to_the_same_consumer_again(
        self, redis_backend, clean_redis
    ):
        """There is no redelivery to ask for; the pending entry is it."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        broker = redis_backend.broker

        (first,) = broker.receive(worker_id=WORKER)
        broker.nack(first)
        (again,) = broker.receive(worker_id=WORKER)

        assert again.handle == first.handle
        assert pending(redis_backend, stream_key_for(redis_backend)) == 1

    def test_other_consumers_do_not_see_a_pending_message(
        self, redis_backend, clean_redis
    ):
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        broker = redis_backend.broker

        assert len(broker.receive(worker_id=WORKER)) == 1
        assert broker.receive(worker_id="another-worker") == []

    def test_a_task_that_already_ran_is_acknowledged_not_returned(
        self, redis_backend, clean_redis
    ):
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        executor.run_task_by_id(result.id)
        stream_key = stream_key_for(redis_backend)

        assert redis_backend.broker.receive(worker_id=WORKER) == []

        assert pending(redis_backend, stream_key) == 0
        assert redis_backend.get_client().xlen(stream_key) == 0

    def test_messages_come_in_priority_order(self, redis_backend, clean_redis):
        from tests.tasks import high_priority_task, low_priority_task, simple_task

        low = low_priority_task.enqueue()
        normal = simple_task.enqueue(1, 2)
        high = high_priority_task.enqueue()

        messages = redis_backend.broker.receive(max_messages=3, worker_id=WORKER)

        assert [m.task_id for m in messages] == [high.id, normal.id, low.id]

    def test_max_messages_is_honoured(self, redis_backend, clean_redis):
        from tests.tasks import simple_task

        ids = [simple_task.enqueue(n, n).id for n in range(3)]
        broker = redis_backend.broker

        first = broker.receive(max_messages=2, worker_id=WORKER)
        for message in first:
            broker.ack(message)
        rest = broker.receive(max_messages=2, worker_id=WORKER)

        assert [m.task_id for m in first] == ids[:2]
        assert [m.task_id for m in rest] == ids[2:]

    def test_receive_reads_one_queue_when_asked(self, redis_backend, clean_redis):
        from tests.tasks import email_task, simple_task

        simple_task.enqueue(1, 2)
        email = email_task.enqueue("to", "subject", "body")
        broker = redis_backend.broker

        messages = broker.receive(queue_name="emails", worker_id=WORKER)

        assert [m.task_id for m in messages] == [email.id]

    def test_waiting_receive_returns_when_the_wait_is_up(
        self, redis_backend, clean_redis
    ):
        started = time.monotonic()

        messages = redis_backend.broker.receive(wait_seconds=0.1, worker_id=WORKER)

        assert messages == []
        assert time.monotonic() - started >= 0.1

    def test_waiting_receive_returns_queued_work(self, redis_backend, clean_redis):
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)

        messages = redis_backend.broker.receive(wait_seconds=1, worker_id=WORKER)

        assert [m.task_id for m in messages] == [result.id]

    def test_due_delayed_task_is_promoted_on_receive(self, redis_backend, clean_redis):
        from datetime import timedelta

        from django.utils import timezone

        from django_tasks_redis.utils import get_result_key, serialize_datetime
        from tests.tasks import simple_task

        result = simple_task.using(
            run_after=timezone.now() + timedelta(seconds=60)
        ).enqueue(1, 2)
        # Bring the due date forward, in the delayed set and in the hash.
        due = timezone.now() - timedelta(seconds=1)
        client = redis_backend.get_client()
        client.zadd(
            redis_backend.broker.delayed_key("default"), {result.id: due.timestamp()}
        )
        client.hset(
            get_result_key(redis_backend.key_prefix, redis_backend.alias, result.id),
            "run_after",
            serialize_datetime(due),
        )

        messages = redis_backend.broker.receive(worker_id=WORKER)

        assert [m.task_id for m in messages] == [result.id]


@pytest.mark.django_db
class TestClaimStaleMessages:
    def test_reclaimed_message_is_received_by_the_claiming_consumer(
        self, redis_backend, clean_redis
    ):
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        broker = redis_backend.broker
        assert len(broker.receive(worker_id="dead-worker")) == 1

        claimed = broker.claim_stale_messages(WORKER, claim_timeout=0)

        assert claimed == 1
        assert [m.task_id for m in broker.receive(worker_id=WORKER)] == [result.id]

    def test_fresh_message_is_not_reclaimed(self, redis_backend, clean_redis):
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        broker = redis_backend.broker
        broker.receive(worker_id="busy-worker")

        assert broker.claim_stale_messages(WORKER, claim_timeout=300) == 0


@pytest.mark.django_db
class TestExecutorCompatibility:
    """The executor functions keep their shape over the broker."""

    def test_fetch_task_keeps_the_message_handle_keys(self, redis_backend, clean_redis):
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)

        task_data = executor.fetch_task(worker_id=WORKER)

        assert task_data["task_id"] == result.id
        assert task_data["status"] == TaskResultStatus.READY
        assert task_data["_stream_key"] == stream_key_for(redis_backend)
        assert task_data["_message_id"]

    def test_process_one_task_acknowledges_through_the_broker(
        self, redis_backend, clean_redis
    ):
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        stream_key = stream_key_for(redis_backend)

        result = executor.process_one_task(worker_id=WORKER)

        assert result.status == TaskResultStatus.SUCCESSFUL
        assert pending(redis_backend, stream_key) == 0
        assert redis_backend.get_client().xlen(stream_key) == 0

    def test_claim_stale_tasks_wraps_the_broker(self, redis_backend, clean_redis):
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        redis_backend.broker.receive(worker_id="dead-worker")

        assert executor.claim_stale_tasks(claim_timeout=0, worker_id=WORKER) == 1

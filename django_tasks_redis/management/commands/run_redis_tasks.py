"""
Management command to run Redis task worker.

The worker receives from the backend's broker, runs the task each message
names, and acknowledges the message. It is the same loop `run_database_tasks`
runs against a pull broker in django-database-task.
"""

import logging
import signal
import time

from django.core.management.base import BaseCommand
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django.utils.translation import gettext_lazy as _

from django_tasks_redis.utils import generate_worker_id

logger = logging.getLogger("django_tasks_redis")


class Command(BaseCommand):
    help = _("Run a worker to process Redis tasks")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.should_stop = False

    def add_arguments(self, parser):
        parser.add_argument(
            "--queue",
            dest="queue_name",
            default=None,
            help=_("Process only tasks from specific queue"),
        )
        parser.add_argument(
            "--backend",
            dest="backend_name",
            default="default",
            help=_("Backend name (default: default)"),
        )
        parser.add_argument(
            "--continuous",
            action="store_true",
            default=False,
            help=_("Run continuously (don't exit when queue is empty)"),
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=1.0,
            help=_("Polling interval in seconds (default: 1.0)"),
        )
        parser.add_argument(
            "--max-tasks",
            type=int,
            default=0,
            help=_("Maximum number of tasks to process (0 = unlimited)"),
        )
        parser.add_argument(
            "--claim-interval",
            type=float,
            default=60.0,
            help=_("Stale task claim interval in seconds (default: 60.0)"),
        )

    def handle(self, *args, **options):
        queue_name = options["queue_name"]
        backend_name = options["backend_name"]
        continuous = options["continuous"]
        interval = options["interval"]
        max_tasks = options["max_tasks"]
        claim_interval = options["claim_interval"]

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        backend = task_backends[backend_name]
        broker = backend.broker
        worker_id = generate_worker_id()

        # Waiting removes up to `interval` of latency per task. Only continuous
        # workers wait; a one-shot run exits as soon as the queue is empty.
        wait_seconds = (backend.block_timeout or 0) / 1000 if continuous else 0

        self.stdout.write(
            self.style.SUCCESS(f"Starting Redis task worker: {worker_id}")
        )
        if queue_name:
            self.stdout.write(f"  Queue: {queue_name}")
        self.stdout.write(f"  Backend: {backend_name}")
        self.stdout.write(f"  Continuous: {continuous}")

        try:
            tasks_processed = self._process_tasks(
                backend=backend,
                broker=broker,
                queue_name=queue_name,
                worker_id=worker_id,
                continuous=continuous,
                interval=interval,
                wait_seconds=wait_seconds,
                max_tasks=max_tasks,
                claim_interval=claim_interval,
            )
        finally:
            broker.close()

        self.stdout.write(
            self.style.SUCCESS(f"Worker stopped. Processed {tasks_processed} task(s).")
        )

    def _process_tasks(
        self,
        backend,
        broker,
        queue_name,
        worker_id,
        continuous,
        interval,
        wait_seconds,
        max_tasks,
        claim_interval,
    ):
        tasks_processed = 0
        last_claim_time = time.time()

        while not self.should_stop:
            # Check if we should claim stale tasks
            current_time = time.time()
            if current_time - last_claim_time >= claim_interval:
                last_claim_time = current_time
                self._claim_stale_messages(broker, worker_id)

            try:
                messages = broker.receive(
                    queue_name=queue_name,
                    max_messages=1,
                    wait_seconds=wait_seconds,
                    worker_id=worker_id,
                )
            except Exception:
                logger.exception("Worker %s failed to receive a task", worker_id)
                self.stderr.write(
                    self.style.ERROR("Failed to receive a task, see the logs")
                )
                if not continuous:
                    break
                time.sleep(interval)
                continue

            if not messages:
                if not continuous:
                    self.stdout.write("No tasks available, exiting")
                    break

                # Wait before polling again, unless the receive already waited.
                if not wait_seconds:
                    time.sleep(interval)
                continue

            for message in messages:
                # A task that cannot even be started must not take the worker
                # down with it: its message stays pending and is handed out
                # again.
                try:
                    result = self._run_broker_message(
                        backend, broker, message, worker_id
                    )
                except Exception:
                    logger.exception("Worker %s failed to process a task", worker_id)
                    self.stderr.write(
                        self.style.ERROR("Failed to process a task, see the logs")
                    )
                    if not continuous:
                        return tasks_processed
                    time.sleep(interval)
                    continue

                if result is None:
                    continue

                tasks_processed += 1
                if max_tasks > 0 and tasks_processed >= max_tasks:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Reached max tasks limit ({max_tasks}), stopping"
                        )
                    )
                    return tasks_processed

        return tasks_processed

    def _run_broker_message(self, backend, broker, message, worker_id):
        """
        Run the task a broker message names, then acknowledge the message.

        An exception from the run leaves the message with the broker: for a
        stream that means it stays pending for this consumer, to be served
        again or reclaimed by another worker.

        Returns:
            The TaskResult, or None if the task was claimed by someone else
            between the read and the run and so did not run here.
        """
        try:
            result = backend.run_task(message.task_id, worker_id=worker_id)
        except Exception:
            broker.nack(message)
            raise

        # Acknowledged either way: the task ran, or another caller has it and
        # redelivering the message would not help.
        broker.ack(message)

        if result is None:
            self.stdout.write(
                f"Task {message.task_id[:8]} is not ready to run; nothing to do"
            )
            return None

        status_style = (
            self.style.SUCCESS
            if result.status == TaskResultStatus.SUCCESSFUL
            else self.style.ERROR
        )
        self.stdout.write(
            f"Processed task {result.id[:8]}: {status_style(result.status)}"
        )
        return result

    def _claim_stale_messages(self, broker, worker_id):
        """Take over the messages of workers that died, for this consumer."""
        try:
            claimed = broker.claim_stale_messages(worker_id)
        except Exception:
            logger.exception("Worker %s failed to claim stale tasks", worker_id)
            return

        if claimed > 0:
            self.stdout.write(f"Claimed {claimed} stale task(s)")

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        self.stdout.write(self.style.WARNING("\nReceived shutdown signal, stopping..."))
        self.should_stop = True

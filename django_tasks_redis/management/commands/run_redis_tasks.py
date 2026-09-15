"""
Management command to run Redis task worker.

The worker receives from the backend's broker, runs the task each message
names, and acknowledges the message. It is the same loop `run_database_tasks`
runs against a pull broker in django-database-task, with the same graceful
shutdown: on SIGTERM or SIGINT no new task is started, the running one is
finished and its result written, and the process exits.

The exit code the process leaves behind is 0 by default, so existing cron
lines and Kubernetes `Job`s see no change. `--empty-exit-code` and
`--failed-exit-code` opt in to scheduler-friendly codes a JP1, Hinemos,
Rundeck, cron or systemd timer can branch on; both default to 0 and are
clamped to 0–255, the range the operating system actually reports.
"""

import argparse
import logging
import sys
from contextlib import ExitStack
from time import monotonic

from django.core.management.base import BaseCommand
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django.utils.translation import gettext_lazy as _

from django_tasks_redis.backends import task_log_fields
from django_tasks_redis.shutdown import GracefulShutdown, signal_name
from django_tasks_redis.utils import generate_worker_id

logger = logging.getLogger("django_tasks_redis")


def _exit_code_argument(value):
    """Parse an exit-code option, rejecting what a shell cannot report."""
    try:
        code = int(value)
    except ValueError:
        # ArgumentTypeError is what argparse turns into a parser error
        # (exit code 2); a CommandError would escape run_from_argv()
        # uncaught as a traceback.
        raise argparse.ArgumentTypeError(
            f"Exit codes must be whole numbers, not {value!r}"
        ) from None
    if not 0 <= code <= 255:
        raise argparse.ArgumentTypeError(
            f"Exit codes must be between 0 and 255, not {code}"
        )
    return code


class Command(BaseCommand):
    help = _("Run a worker to process Redis tasks")

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
        parser.add_argument(
            "--shutdown-timeout",
            type=float,
            default=0.0,
            help=_(
                "Maximum seconds to wait for the running task after receiving "
                "SIGTERM/SIGINT before forcing exit (0=wait indefinitely, default: 0)"
            ),
        )
        parser.add_argument(
            "--no-graceful-shutdown",
            action="store_true",
            help=_(
                "Do not install SIGTERM/SIGINT handlers; the process is "
                "terminated immediately, even while a task is running"
            ),
        )
        parser.add_argument(
            "--empty-exit-code",
            type=_exit_code_argument,
            default=0,
            metavar="CODE",
            help=_(
                "Exit with this code when no task was processed, so a job "
                "scheduler can tell an idle run from a real one "
                "(0=exit normally, default: 0)"
            ),
        )
        parser.add_argument(
            "--failed-exit-code",
            type=_exit_code_argument,
            default=0,
            metavar="CODE",
            help=_(
                "Exit with this code when at least one task failed or could "
                "not be run. Takes precedence over --empty-exit-code "
                "(0=exit normally, default: 0)"
            ),
        )

    def handle(self, *args, **options):
        queue_name = options["queue_name"]
        backend_name = options["backend_name"]
        continuous = options["continuous"]
        interval = options["interval"]
        max_tasks = options["max_tasks"]
        claim_interval = options["claim_interval"]
        shutdown_timeout = options["shutdown_timeout"]
        graceful = not options["no_graceful_shutdown"]
        empty_exit_code = options["empty_exit_code"]
        failed_exit_code = options["failed_exit_code"]

        backend = task_backends[backend_name]
        broker = backend.broker
        worker_id = generate_worker_id()

        # Counted here rather than returned from the loop because a task can
        # fail at several depths (the run itself, the broker message that
        # named it) and every one of them feeds the Worker finished record.
        self.tasks_failed = 0

        logger.info(
            "Worker started: id=%s backend=%s",
            worker_id,
            backend_name,
            extra={
                "worker_id": worker_id,
                "backend_alias": backend_name,
                "queue_name": queue_name,
                "continuous": continuous,
            },
        )

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
        if graceful:
            timeout_label = (
                f"{shutdown_timeout}s" if shutdown_timeout > 0 else "unlimited"
            )
            self.stdout.write(f"  Graceful shutdown: enabled (timeout={timeout_label})")
        else:
            self.stdout.write("  Graceful shutdown: disabled")

        shutdown = GracefulShutdown(
            timeout=shutdown_timeout,
            on_signal=self._report_signal,
            # Signals are reported on stdout by the callback above.
            log_signals=False,
        )

        with ExitStack() as stack:
            if graceful:
                stack.enter_context(shutdown)
            # Callbacks run last-in first-out: the consumer is removed while
            # the broker is still open, then the broker is closed.
            stack.callback(broker.close)
            stack.callback(self._remove_consumer, broker, worker_id)
            tasks_processed = self._process_tasks(
                shutdown=shutdown,
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

        if shutdown.is_set():
            self.stdout.write(
                self.style.WARNING("\nShutdown complete (no task was interrupted).")
            )

        exit_code = self._exit_code(tasks_processed, empty_exit_code, failed_exit_code)

        # The Worker finished record is what an operator greps for in a JSON
        # log stream: counts and the exit code stay attached as fields rather
        # than only being written to stdout. The message matches database-
        # task's verbatim, so a plain-text reader cannot tell the libraries
        # apart from the line alone.
        logger.info(
            "Worker finished: id=%s processed=%d failed=%d",
            worker_id,
            tasks_processed,
            self.tasks_failed,
            extra={
                "worker_id": worker_id,
                "backend_alias": backend_name,
                "queue_name": queue_name,
                "tasks_processed": tasks_processed,
                "tasks_failed": self.tasks_failed,
                "exit_code": exit_code,
            },
        )

        self.stdout.write(
            self.style.SUCCESS(f"Worker stopped. Processed {tasks_processed} task(s).")
        )

        # Mirrors django-database-task's run_database_tasks: the failure
        # count comes after the run's total line, so a terminal reader sees
        # how much ran before how much failed. The Worker finished log
        # record carries the same number for a JSON operator.
        if self.tasks_failed:
            self.stdout.write(self.style.ERROR(f"Tasks failed: {self.tasks_failed}"))

        if exit_code:
            sys.exit(exit_code)

    def _exit_code(self, tasks_processed, empty_exit_code, failed_exit_code):
        """
        Work out what to report to whatever started the worker.

        Both codes default to 0, which leaves the run indistinguishable from
        any other successful command — the behaviour before these options
        existed. A failure wins over an idle run: a broker message the
        worker could not run at all counts as a failure without adding to
        the processed count, so both conditions can hold at once, and the
        failure is the one worth waking someone for.
        """
        if self.tasks_failed and failed_exit_code:
            return failed_exit_code
        if not tasks_processed and empty_exit_code:
            return empty_exit_code
        return 0

    def _process_tasks(
        self,
        shutdown,
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
        # No sweep on the first pass: the interval has to elapse first.
        next_claim = monotonic() + claim_interval

        while not shutdown.is_set():
            if monotonic() >= next_claim:
                next_claim = monotonic() + claim_interval
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
                if not continuous or shutdown.wait(interval):
                    break
                continue

            if not messages:
                if not continuous:
                    self.stdout.write("No tasks available, exiting")
                    break

                # Wait before polling again, unless the receive already waited.
                # The wait ends early when a shutdown is requested.
                if not wait_seconds and shutdown.wait(interval):
                    break
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
                    logger.exception(
                        "Worker %s failed to process a task",
                        worker_id,
                        # The stream entry already names the task, its queue
                        # and its priority, so the record carries the full
                        # field set without a round trip to Redis.
                        extra=task_log_fields(
                            message.raw, worker_id, backend_alias=backend.alias
                        ),
                    )
                    self.stderr.write(
                        self.style.ERROR("Failed to process a task, see the logs")
                    )
                    # The task is not part of `tasks_processed`: the broker
                    # message is nacked and handed out again. Count it as a
                    # failure for the Worker finished record either way.
                    self.tasks_failed += 1
                    if not continuous or shutdown.wait(interval):
                        return tasks_processed
                    continue

                if result is None:
                    continue

                tasks_processed += 1
                if result.status != TaskResultStatus.SUCCESSFUL:
                    self.tasks_failed += 1
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

    def _remove_consumer(self, broker, worker_id):
        """Take this worker's consumer out of the group on the way out."""
        try:
            broker.remove_consumer(worker_id)
        except Exception:
            # Housekeeping only: the sweep removes it later either way.
            logger.exception("Worker %s failed to remove its consumer", worker_id)

    def _claim_stale_messages(self, broker, worker_id):
        """Take over the messages of workers that died, for this consumer."""
        try:
            claimed = broker.claim_stale_messages(worker_id)
        except Exception:
            logger.exception("Worker %s failed to claim stale tasks", worker_id)
            return

        if claimed > 0:
            self.stdout.write(f"Claimed {claimed} stale task(s)")

    def _report_signal(self, signum, count):
        """Report a received shutdown signal (called from the signal handler)."""
        name = signal_name(signum)
        if count == 1:
            message = (
                f"\nReceived {name}: no new tasks will be started. "
                "Waiting for the running task to finish "
                "(send the signal again to force exit)."
            )
        else:
            message = f"\nReceived {name} again: forcing immediate exit."
        self.stdout.write(self.style.WARNING(message))
        self.stdout.flush()

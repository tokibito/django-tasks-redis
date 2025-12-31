"""
Management command to run Redis task worker.
"""

import signal
import time

from django.core.management.base import BaseCommand
from django.utils.translation import gettext_lazy as _

from django_tasks_redis import executor


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

        worker_id = executor._generate_worker_id()

        self.stdout.write(
            self.style.SUCCESS(f"Starting Redis task worker: {worker_id}")
        )
        if queue_name:
            self.stdout.write(f"  Queue: {queue_name}")
        self.stdout.write(f"  Backend: {backend_name}")
        self.stdout.write(f"  Continuous: {continuous}")

        tasks_processed = 0
        last_claim_time = time.time()

        while not self.should_stop:
            # Check if we should claim stale tasks
            current_time = time.time()
            if current_time - last_claim_time >= claim_interval:
                claimed = executor.claim_stale_tasks(backend_name=backend_name)
                if claimed > 0:
                    self.stdout.write(f"Claimed {claimed} stale task(s)")
                last_claim_time = current_time

            # Process one task
            result = executor.process_one_task(
                queue_name=queue_name,
                backend_name=backend_name,
                worker_id=worker_id,
            )

            if result is not None:
                tasks_processed += 1
                status_style = (
                    self.style.SUCCESS
                    if result.status == "SUCCESSFUL"
                    else self.style.ERROR
                )
                self.stdout.write(
                    f"Processed task {result.id[:8]}: {status_style(result.status)}"
                )

                # Check max_tasks limit
                if max_tasks > 0 and tasks_processed >= max_tasks:
                    self.stdout.write(
                        self.style.WARNING(
                            f"Reached max tasks limit ({max_tasks}), stopping"
                        )
                    )
                    break
            else:
                if not continuous:
                    self.stdout.write("No tasks available, exiting")
                    break

                # Wait before polling again
                time.sleep(interval)

        self.stdout.write(
            self.style.SUCCESS(f"Worker stopped. Processed {tasks_processed} task(s).")
        )

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals gracefully."""
        self.stdout.write(self.style.WARNING("\nReceived shutdown signal, stopping..."))
        self.should_stop = True

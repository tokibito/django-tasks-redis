"""
Management command to rebuild the status index of a backend.

The index is what the status counts and the queue statistics are read from.
It is written with every task from the first one on, so a new deployment
never needs this; a deployment that stored results before the index existed
runs it once, after every process is on a version that writes the index, and
reads its counts from the stored results until then.
"""

from django.core.management.base import BaseCommand
from django.utils.translation import gettext_lazy as _

from django_tasks_redis import executor


class Command(BaseCommand):
    help = _("Rebuild the status index the task counts are read from")

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=None,
            help=_("Tasks read per round trip (default: REDIS_SCAN_BATCH_SIZE)"),
        )
        parser.add_argument(
            "--backend",
            dest="backend_name",
            default="default",
            help=_("Backend name (default: default)"),
        )

    def handle(self, *args, **options):
        backend_name = options["backend_name"]

        self.stdout.write(f"Rebuilding the status index of backend: {backend_name}")

        count = executor.rebuild_status_index(
            backend_name=backend_name,
            batch_size=options["batch_size"],
        )

        self.stdout.write(self.style.SUCCESS(f"\nIndexed {count} task(s)"))

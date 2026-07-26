"""
Management command to purge completed Redis tasks.
"""

from django.core.management.base import BaseCommand
from django.tasks.base import TaskResultStatus
from django.utils.translation import gettext_lazy as _

from django_tasks_redis import executor


class Command(BaseCommand):
    help = _("Delete completed Redis tasks older than specified days")

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=7,
            help=_("Delete tasks completed more than N days ago (default: 7)"),
        )
        parser.add_argument(
            "--status",
            dest="statuses",
            action="append",
            choices=["SUCCESSFUL", "FAILED"],
            help=_(
                "Status to delete (can be specified multiple times, default: SUCCESSFUL,FAILED)"
            ),
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=1000,
            help=_("Batch size for deletion (default: 1000)"),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help=_("Only show count, don't actually delete"),
        )
        parser.add_argument(
            "--backend",
            dest="backend_name",
            default="default",
            help=_("Backend name (default: default)"),
        )

    def handle(self, *args, **options):
        days = options["days"]
        statuses = options["statuses"]
        dry_run = options["dry_run"]
        backend_name = options["backend_name"]
        batch_size = options["batch_size"]

        # Default statuses if not specified
        if not statuses:
            statuses = [TaskResultStatus.SUCCESSFUL, TaskResultStatus.FAILED]
        else:
            statuses = [TaskResultStatus(s) for s in statuses]

        self.stdout.write(f"Purging completed tasks from backend: {backend_name}")
        self.stdout.write(f"  Days threshold: {days}")
        self.stdout.write(f"  Statuses: {', '.join(str(s) for s in statuses)}")

        if dry_run:
            self.stdout.write(
                self.style.WARNING("  DRY RUN - no tasks will be deleted")
            )

        count = executor.purge_completed_tasks(
            backend_name=backend_name,
            days=days,
            statuses=statuses,
            batch_size=batch_size,
            dry_run=dry_run,
        )

        verb = "Would delete" if dry_run else "Deleted"
        self.stdout.write(self.style.SUCCESS(f"\n{verb} {count} task(s)"))

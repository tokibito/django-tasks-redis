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

            # Get count of tasks that would be deleted
            from django.tasks import task_backends
            from django.utils import timezone

            from django_tasks_redis.utils import (
                deserialize_datetime,
                get_result_key,
                get_results_index_key,
            )

            backend = task_backends[backend_name]
            client = backend.get_client()
            results_index_key = get_results_index_key(backend.key_prefix, backend_name)

            cutoff = timezone.now() - timezone.timedelta(days=days)
            would_delete = 0

            task_ids = client.smembers(results_index_key)
            for task_id in task_ids:
                result_key = get_result_key(backend.key_prefix, backend_name, task_id)
                task_data = client.hgetall(result_key)

                if not task_data:
                    continue

                status = task_data.get("status")
                if status not in [str(s) for s in statuses]:
                    continue

                finished_at = deserialize_datetime(task_data.get("finished_at", ""))
                if finished_at and finished_at < cutoff:
                    would_delete += 1

            self.stdout.write(
                self.style.SUCCESS(f"\nWould delete {would_delete} task(s)")
            )
        else:
            deleted_count = executor.purge_completed_tasks(
                backend_name=backend_name,
                days=days,
                statuses=statuses,
            )

            self.stdout.write(self.style.SUCCESS(f"\nDeleted {deleted_count} task(s)"))

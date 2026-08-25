"""
Management command to (re)build the per-status task index.

Deployments upgrading from a version without the status index should run
this once (or simply start a worker, which builds it automatically) so
that status counts and Prometheus scrapes become O(1) instead of scanning
every stored result.
"""

from django.core.management.base import BaseCommand
from django.tasks import task_backends
from django.utils.translation import gettext_lazy as _


class Command(BaseCommand):
    help = _("Rebuild the per-status task index used for fast status counts")

    def add_arguments(self, parser):
        parser.add_argument(
            "--backend",
            dest="backend_name",
            default="default",
            help=_("Backend name (default: default)"),
        )

    def handle(self, *args, **options):
        backend = task_backends[options["backend_name"]]
        indexed = backend.rebuild_status_index()
        self.stdout.write(
            self.style.SUCCESS(f"Status index rebuilt: {indexed} task(s) indexed")
        )

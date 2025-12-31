"""
Django Admin integration for Redis tasks.

This module provides a read-only admin interface for viewing
and managing Redis tasks. Since Redis tasks are not stored
in the database, this uses a custom approach with executor API.
"""

from django.contrib import admin, messages
from django.db import models
from django.tasks import task_backends
from django.tasks.base import TaskResultStatus
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from . import executor
from .utils import deserialize_datetime, deserialize_json


class RedisTask(models.Model):
    """
    Pseudo-model for Redis tasks in Django Admin.

    This model is not actually stored in the database.
    It's used to provide a Django Admin interface for Redis tasks.
    """

    class Meta:
        managed = False
        app_label = "django_tasks_redis"
        verbose_name = _("Redis Task")
        verbose_name_plural = _("Redis Tasks")


class RedisTaskAdmin(admin.ModelAdmin):
    """Admin interface for Redis tasks."""

    list_display = [
        "id_short",
        "task_path_short",
        "status_badge",
        "priority",
        "queue_name",
        "enqueued_at",
        "started_at",
        "finished_at",
    ]
    list_filter = ["status", "queue_name"]
    search_fields = ["id", "task_path"]
    ordering = ["-enqueued_at"]
    readonly_fields = [
        "id",
        "task_path",
        "status",
        "priority",
        "queue_name",
        "backend_name",
        "args_json",
        "kwargs_json",
        "run_after",
        "enqueued_at",
        "started_at",
        "finished_at",
        "last_attempted_at",
        "return_value_json",
        "errors_json",
        "worker_ids_json",
    ]
    actions = ["run_selected_tasks", "retry_failed_tasks"]

    # Track task data for display
    _task_cache = {}

    def has_add_permission(self, request):
        """Disable adding tasks from admin."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Enable deleting tasks."""
        return True

    def has_change_permission(self, request, obj=None):
        """Disable editing tasks (read-only view)."""
        return False

    def get_queryset(self, request):
        """Return empty queryset - we fetch from Redis directly."""
        return RedisTask.objects.none()

    def changelist_view(self, request, extra_context=None):
        """Override to fetch tasks from Redis."""
        # Get filter parameters
        queue_name = request.GET.get("queue_name")
        status = request.GET.get("status")

        # Fetch tasks from Redis
        backend_name = "default"  # Could be made configurable
        tasks, total = executor.get_tasks(
            backend_name=backend_name,
            queue_name=queue_name,
            status=status,
            limit=100,
        )

        # Store in cache for display
        self._task_cache = {t["task_id"]: t for t in tasks}

        # Create pseudo queryset for template
        extra_context = extra_context or {}
        extra_context["task_list"] = tasks
        extra_context["task_count"] = total

        return super().changelist_view(request, extra_context=extra_context)

    def id_short(self, obj):
        """Display shortened ID."""
        if hasattr(obj, "task_id"):
            return str(obj.task_id)[:8]
        return str(obj.pk)[:8] if obj.pk else ""

    id_short.short_description = _("ID")

    def task_path_short(self, obj):
        """Display shortened task path."""
        path = getattr(obj, "task_path", "")
        if len(path) > 40:
            return f"...{path[-37:]}"
        return path

    task_path_short.short_description = _("Task")

    def status_badge(self, obj):
        """Display status as a colored badge."""
        status = getattr(obj, "status", "")
        colors = {
            "READY": "#6c757d",
            "RUNNING": "#007bff",
            "SUCCESSFUL": "#28a745",
            "FAILED": "#dc3545",
        }
        color = colors.get(status, "#6c757d")
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; '
            'border-radius: 3px; font-size: 11px;">{}</span>',
            color,
            status,
        )

    status_badge.short_description = _("Status")

    @admin.action(description=_("Run selected tasks"))
    def run_selected_tasks(self, request, queryset):
        """Execute selected tasks that are in READY status."""
        # Get task IDs from request
        selected = request.POST.getlist("_selected_action")

        if not selected:
            self.message_user(
                request,
                _("No tasks selected."),
                messages.WARNING,
            )
            return

        success_count = 0
        fail_count = 0
        skipped_count = 0

        for task_id in selected:
            task_data = executor.get_task_by_id(task_id)
            if not task_data:
                skipped_count += 1
                continue

            if task_data.get("status") != TaskResultStatus.READY:
                skipped_count += 1
                continue

            try:
                result = executor.run_task_by_id(task_id, worker_id="admin")
                if result and result.status == TaskResultStatus.SUCCESSFUL:
                    success_count += 1
                else:
                    fail_count += 1
            except Exception:
                fail_count += 1

        msg_parts = []
        if success_count:
            msg_parts.append(_("%(count)d succeeded") % {"count": success_count})
        if fail_count:
            msg_parts.append(_("%(count)d failed") % {"count": fail_count})
        if skipped_count:
            msg_parts.append(_("%(count)d skipped (not READY)") % {"count": skipped_count})

        self.message_user(
            request,
            _("Task execution completed: %(results)s.") % {"results": ", ".join(str(p) for p in msg_parts)},
            messages.SUCCESS if fail_count == 0 else messages.WARNING,
        )

    @admin.action(description=_("Retry failed tasks"))
    def retry_failed_tasks(self, request, queryset):
        """Reset failed tasks to READY status and re-execute them."""
        selected = request.POST.getlist("_selected_action")

        if not selected:
            self.message_user(
                request,
                _("No tasks selected."),
                messages.WARNING,
            )
            return

        success_count = 0
        fail_count = 0
        skipped_count = 0

        for task_id in selected:
            task_data = executor.get_task_by_id(task_id)
            if not task_data:
                skipped_count += 1
                continue

            if task_data.get("status") != TaskResultStatus.FAILED:
                skipped_count += 1
                continue

            try:
                # Reset and run
                result = executor.run_task_by_id(
                    task_id, worker_id="admin-retry", allow_retry=True
                )
                if result and result.status == TaskResultStatus.SUCCESSFUL:
                    success_count += 1
                else:
                    fail_count += 1
            except Exception:
                fail_count += 1

        msg_parts = []
        if success_count:
            msg_parts.append(_("%(count)d succeeded") % {"count": success_count})
        if fail_count:
            msg_parts.append(_("%(count)d failed again") % {"count": fail_count})
        if skipped_count:
            msg_parts.append(_("%(count)d skipped (not FAILED)") % {"count": skipped_count})

        self.message_user(
            request,
            _("Retry completed: %(results)s.") % {"results": ", ".join(str(p) for p in msg_parts)},
            messages.SUCCESS if fail_count == 0 else messages.WARNING,
        )


# Register the admin
admin.site.register(RedisTask, RedisTaskAdmin)

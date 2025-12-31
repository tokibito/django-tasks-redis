"""
Django Admin integration for Redis tasks.

This module provides a read-only admin interface for viewing
and managing Redis tasks. Since Redis tasks are not stored
in the database, this uses a custom approach with executor API.
"""

from django.contrib import admin, messages
from django.contrib.admin.views.main import ChangeList
from django.core.paginator import Paginator
from django.db import models
from django.shortcuts import render
from django.tasks.base import TaskResultStatus
from django.urls import path
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from . import executor


class RedisTaskObject:
    """
    Wrapper class for Redis task data to make it look like a Django model object.
    """

    def __init__(self, task_data, meta):
        self._data = task_data
        self._meta = meta
        self.pk = task_data.get("task_id")
        self.task_id = task_data.get("task_id")
        self.task_path = task_data.get("task_path", "")
        self.status = task_data.get("status", "")
        self.priority = task_data.get("priority", 0)
        self.queue_name = task_data.get("queue_name", "default")
        self.enqueued_at = task_data.get("enqueued_at", "")
        self.started_at = task_data.get("started_at", "")
        self.finished_at = task_data.get("finished_at", "")
        self.args_json = task_data.get("args_json", "")
        self.kwargs_json = task_data.get("kwargs_json", "")
        self.return_value_json = task_data.get("return_value_json", "")
        self.errors_json = task_data.get("errors_json", "")
        self.worker_ids_json = task_data.get("worker_ids_json", "")

    def __str__(self):
        return self.task_id or ""

    def serializable_value(self, field_name):
        return getattr(self, field_name, None)


class RedisTaskPaginator(Paginator):
    """Custom paginator for Redis tasks."""

    def __init__(self, task_objects, per_page, total_count):
        self._total_count = total_count
        super().__init__(task_objects, per_page)

    @property
    def count(self):
        return self._total_count


class RedisTaskChangeList(ChangeList):
    """Custom ChangeList that loads data from Redis instead of database."""

    def get_queryset(self, request):
        # Return empty queryset - we'll override get_results
        return self.model_admin.model._default_manager.none()

    def get_results(self, request):
        # Fetch tasks from Redis
        page_num = int(request.GET.get("p", 0))
        per_page = self.list_per_page

        # Get status filter if any
        status_filter = request.GET.get("status")

        tasks, total = executor.get_tasks(
            backend_name="default",
            status=status_filter,
            offset=page_num * per_page,
            limit=per_page,
        )

        # Get meta from the model
        meta = self.model._meta

        # Convert to wrapper objects
        self.result_list = [RedisTaskObject(t, meta) for t in tasks]
        self.result_count = total
        self.show_full_result_count = True
        self.show_admin_actions = True
        self.full_result_count = total
        self.can_show_all = False
        self.multi_page = total > per_page
        self.paginator = RedisTaskPaginator(self.result_list, per_page, total)


class RedisTask(models.Model):
    """
    Pseudo-model for Redis tasks in Django Admin.

    This model is not actually stored in the database.
    It's used to provide a Django Admin interface for Redis tasks.
    """

    task_id = models.CharField(max_length=36, primary_key=True)

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
        "get_priority",
        "get_queue_name",
        "get_enqueued_at",
    ]
    list_per_page = 50
    search_fields = ["task_id"]
    actions = ["run_selected_tasks", "retry_failed_tasks", "delete_selected_tasks"]

    def has_add_permission(self, request):
        """Disable adding tasks from admin."""
        return False

    def has_delete_permission(self, request, obj=None):
        """Enable deleting tasks."""
        return True

    def has_change_permission(self, request, obj=None):
        """Disable editing tasks (read-only view)."""
        return False

    def get_urls(self):
        """Add custom URL for task detail view."""
        urls = super().get_urls()
        custom_urls = [
            path(
                "<str:task_id>/detail/",
                self.admin_site.admin_view(self.task_detail_view),
                name="django_tasks_redis_redistask_detail",
            ),
        ]
        return custom_urls + urls

    def get_object(self, request, object_id, from_field=None):
        """Fetch task from Redis instead of database."""
        task_data = executor.get_task_by_id(object_id)
        if task_data:
            return RedisTaskObject(task_data, self.model._meta)
        return None

    def change_view(self, request, object_id, form_url="", extra_context=None):
        """Redirect to custom detail view."""
        from django.shortcuts import redirect

        return redirect("admin:django_tasks_redis_redistask_detail", task_id=object_id)

    def task_detail_view(self, request, task_id):
        """Custom view for task details."""
        task_data = executor.get_task_by_id(task_id)

        context = {
            **self.admin_site.each_context(request),
            "title": _("Task Detail"),
            "task": task_data,
            "task_id": task_id,
            "opts": self.model._meta,
            "has_change_permission": False,
            "original": task_id,
        }
        return render(
            request, "admin/django_tasks_redis/redistask/detail.html", context
        )

    def get_changelist(self, request, **kwargs):
        """Return custom ChangeList class."""
        return RedisTaskChangeList

    def id_short(self, obj):
        """Display shortened ID."""
        task_id = getattr(obj, "task_id", None) or getattr(obj, "pk", None)
        return str(task_id)[:8] if task_id else ""

    id_short.short_description = _("ID")

    def task_path_short(self, obj):
        """Display shortened task path."""
        path = getattr(obj, "task_path", "") or ""
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

    def get_priority(self, obj):
        """Display priority."""
        return getattr(obj, "priority", 0)

    get_priority.short_description = _("Priority")

    def get_queue_name(self, obj):
        """Display queue name."""
        return getattr(obj, "queue_name", "default")

    get_queue_name.short_description = _("Queue")

    def get_enqueued_at(self, obj):
        """Display enqueued timestamp."""
        return getattr(obj, "enqueued_at", "")

    get_enqueued_at.short_description = _("Enqueued At")

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
            msg_parts.append(
                _("%(count)d skipped (not READY)") % {"count": skipped_count}
            )

        self.message_user(
            request,
            _("Task execution completed: %(results)s.")
            % {"results": ", ".join(str(p) for p in msg_parts)},
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
            msg_parts.append(
                _("%(count)d skipped (not FAILED)") % {"count": skipped_count}
            )

        self.message_user(
            request,
            _("Retry completed: %(results)s.")
            % {"results": ", ".join(str(p) for p in msg_parts)},
            messages.SUCCESS if fail_count == 0 else messages.WARNING,
        )

    @admin.action(description=_("Delete selected tasks"))
    def delete_selected_tasks(self, request, queryset):
        """Delete selected tasks from Redis."""
        selected = request.POST.getlist("_selected_action")

        if not selected:
            self.message_user(
                request,
                _("No tasks selected."),
                messages.WARNING,
            )
            return

        deleted_count = executor.delete_tasks(selected)

        self.message_user(
            request,
            _("%(count)d task(s) deleted.") % {"count": deleted_count},
            messages.SUCCESS,
        )


# Register the admin
admin.site.register(RedisTask, RedisTaskAdmin)

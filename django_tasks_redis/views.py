"""
HTTP endpoints for Redis task operations.

These views provide HTTP API for external triggers like webhooks,
Cloud Scheduler, etc.
"""

from django.http import JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from . import executor


@method_decorator(csrf_exempt, name="dispatch")
class RunTasksView(View):
    """Process multiple tasks."""

    def post(self, request):
        queue_name = request.POST.get("queue_name")
        backend_name = request.POST.get("backend_name", "default")
        max_tasks = int(request.POST.get("max_tasks", 0))

        results = executor.process_tasks(
            queue_name=queue_name,
            backend_name=backend_name,
            max_tasks=max_tasks,
        )

        return JsonResponse(
            {
                "processed": len(results),
                "tasks": [{"id": str(r.id), "status": r.status} for r in results],
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class RunOneTaskView(View):
    """Process a single task."""

    def post(self, request):
        queue_name = request.POST.get("queue_name")
        backend_name = request.POST.get("backend_name", "default")

        result = executor.process_one_task(
            queue_name=queue_name,
            backend_name=backend_name,
        )

        if result is None:
            return JsonResponse({"processed": False, "message": "No tasks available"})

        return JsonResponse(
            {
                "processed": True,
                "task": {"id": str(result.id), "status": result.status},
            }
        )


@method_decorator(csrf_exempt, name="dispatch")
class ExecuteTaskView(View):
    """Execute a specific task by ID (for Cloud Tasks, webhooks, etc.)."""

    def post(self, request, task_id):
        backend_name = request.POST.get("backend_name", "default")
        allow_retry = request.POST.get("allow_retry", "false").lower() == "true"

        try:
            result = executor.run_task_by_id(
                task_id=str(task_id),
                backend_name=backend_name,
                allow_retry=allow_retry,
            )
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=404)

        if result is None:
            return JsonResponse(
                {"error": "Task not in executable status"},
                status=400,
            )

        return JsonResponse(
            {
                "task": {"id": str(result.id), "status": result.status},
            }
        )


class TaskStatusView(View):
    """Get task status by ID."""

    def get(self, request, task_id):
        backend_name = request.GET.get("backend_name", "default")

        task = executor.get_task_by_id(str(task_id), backend_name=backend_name)

        if task is None:
            return JsonResponse({"error": "Task not found"}, status=404)

        return JsonResponse({"task": task})


@method_decorator(csrf_exempt, name="dispatch")
class PurgeCompletedTasksView(View):
    """Purge completed tasks."""

    def post(self, request):
        backend_name = request.POST.get("backend_name", "default")
        days = int(request.POST.get("days", 7))

        deleted_count = executor.purge_completed_tasks(
            backend_name=backend_name,
            days=days,
        )

        return JsonResponse({"deleted": deleted_count})

"""
HTTP endpoints for Redis task operations.

These views provide HTTP API for external triggers like webhooks,
Cloud Scheduler, etc.

Every endpoint runs the backend's authentication handler first. That handler is
None by default, which keeps the endpoints closed: they execute and delete
tasks, so a project has to say how they are authenticated before they answer.
See RedisTaskBackend.get_auth_handler().
"""

from django.http import JsonResponse
from django.tasks import task_backends
from django.tasks.exceptions import InvalidTaskBackend
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from . import executor


def _int_param(params, name, default):
    """Read an integer parameter, or None when the caller sent something else."""
    raw = params.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


class TaskEndpointMixin:
    """Authenticate a request against the backend it addresses."""

    def get_backend_name(self, request):
        """Read the backend the request targets, the same way the view does."""
        if request.method == "POST":
            return request.POST.get("backend_name", "default")
        return request.GET.get("backend_name", "default")

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            # Reading POST consumes the stream for a multipart body, so cache
            # the raw body first: a handler verifying a signature needs it.
            _ = request.body

        try:
            backend = task_backends[self.get_backend_name(request)]
        except InvalidTaskBackend:
            return JsonResponse({"error": "Unknown backend"}, status=400)

        # A backend that is not a RedisTaskBackend has no handler, and is
        # just as closed.
        get_auth_handler = getattr(backend, "get_auth_handler", None)
        handler = get_auth_handler() if get_auth_handler else None
        if handler is None:
            return JsonResponse(
                {
                    "error": "Task endpoints are disabled. Override "
                    "get_auth_handler() on the task backend to enable them."
                },
                status=403,
            )

        response = handler(request)
        if response is not None:
            return response

        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class RunTasksView(TaskEndpointMixin, View):
    """Process multiple tasks."""

    def post(self, request):
        queue_name = request.POST.get("queue_name")
        backend_name = request.POST.get("backend_name", "default")
        max_tasks = _int_param(request.POST, "max_tasks", 0)
        if max_tasks is None:
            return JsonResponse({"error": "max_tasks must be an integer"}, status=400)

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
class RunOneTaskView(TaskEndpointMixin, View):
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
class ExecuteTaskView(TaskEndpointMixin, View):
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


class TaskStatusView(TaskEndpointMixin, View):
    """Get task status by ID."""

    def get(self, request, task_id):
        backend_name = request.GET.get("backend_name", "default")

        task = executor.get_task_by_id(str(task_id), backend_name=backend_name)

        if task is None:
            return JsonResponse({"error": "Task not found"}, status=404)

        return JsonResponse({"task": task})


@method_decorator(csrf_exempt, name="dispatch")
class PurgeCompletedTasksView(TaskEndpointMixin, View):
    """Purge completed tasks."""

    def post(self, request):
        backend_name = request.POST.get("backend_name", "default")
        days = _int_param(request.POST, "days", 7)
        if days is None:
            return JsonResponse({"error": "days must be an integer"}, status=400)

        deleted_count = executor.purge_completed_tasks(
            backend_name=backend_name,
            days=days,
        )

        return JsonResponse({"deleted": deleted_count})

"""
HTTP endpoints for Redis task operations.

These views provide HTTP API for external triggers like webhooks,
Cloud Scheduler, etc.

Every endpoint runs the backend's authentication handlers first. The
backend decides how requests are authenticated by returning handlers from
``get_auth_handlers()`` (see ``RedisTaskBackend.get_auth_handlers``). Each
handler takes the request and returns ``None`` to accept it, or a response
to reject it. The handlers are tried in order and the request is accepted as
soon as one of them accepts it. When every handler rejects the request, the
first rejection is returned.

When the backend returns no handlers, every endpoint answers 403: they run
and delete tasks, so a project has to say how they are authenticated before
they answer. See ``RedisTaskBackend.get_auth_handlers()``.
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

    # Endpoint name passed to get_auth_handlers(), so a handler can be
    # configured for a subset of the endpoints. See auth.AUTH_ENDPOINTS.
    auth_endpoint = None

    def get_backend_name(self, request):
        """Read the backend the request targets, the same way the view does."""
        if request.method == "POST":
            return request.POST.get("backend_name", "default")
        return request.GET.get("backend_name", "default")

    def _get_backend_auth_handlers(self, backend):
        """Return the handlers the backend provides, or [] for none."""
        # A backend that is not a RedisTaskBackend has no get_auth_handlers,
        # and its endpoints are just as closed: treat that the same as an
        # empty handler list.
        get_auth_handlers = getattr(backend, "get_auth_handlers", None)
        if get_auth_handlers is None:
            return []
        return list(get_auth_handlers(self.auth_endpoint) or [])

    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            # Reading POST consumes the stream for a multipart body, so cache
            # the raw body first: a handler verifying a signature needs it.
            _ = request.body

        try:
            backend = task_backends[self.get_backend_name(request)]
        except InvalidTaskBackend:
            return JsonResponse({"error": "Unknown backend"}, status=400)

        handlers = self._get_backend_auth_handlers(backend)
        if not handlers:
            # An empty handler list keeps the endpoints closed, the same way
            # 0.2.0 did: the endpoints run and delete tasks, so they cannot
            # be reachable without the backend having said how to authenticate
            # them.
            return JsonResponse(
                {
                    "error": "Task endpoints are disabled. Override "
                    "get_auth_handlers() on the task backend, or set the "
                    "AUTH_HANDLERS option, to enable them."
                },
                status=403,
            )

        first_error = None
        for handler in handlers:
            error_response = handler(request)
            if error_response is None:
                # Accepted: hand off to the view, do not look at the
                # rejection a previous handler returned.
                return super().dispatch(request, *args, **kwargs)
            if first_error is None:
                first_error = error_response

        # No handler accepted. Return the first rejection, so the caller
        # sees the same error whether the backend had one handler or many.
        if first_error is not None:
            return first_error

        return super().dispatch(request, *args, **kwargs)


@method_decorator(csrf_exempt, name="dispatch")
class RunTasksView(TaskEndpointMixin, View):
    """Process multiple tasks."""

    auth_endpoint = "run"

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

    auth_endpoint = "run_one"

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

    auth_endpoint = "execute"

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

    auth_endpoint = "status"

    def get(self, request, task_id):
        backend_name = request.GET.get("backend_name", "default")

        task = executor.get_task_by_id(str(task_id), backend_name=backend_name)

        if task is None:
            return JsonResponse({"error": "Task not found"}, status=404)

        return JsonResponse({"task": task})


@method_decorator(csrf_exempt, name="dispatch")
class PurgeCompletedTasksView(TaskEndpointMixin, View):
    """Purge completed tasks."""

    auth_endpoint = "purge"

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

"""
Views for the demo application.
"""

from django.http import JsonResponse
from django.shortcuts import render
from django.tasks import task_backends

from . import tasks


def index(request):
    """Home page with task submission form."""
    return render(request, "demo_app/index.html")


def enqueue_add(request):
    """Enqueue an add_numbers task."""
    x = int(request.GET.get("x", 1))
    y = int(request.GET.get("y", 2))

    result = tasks.add_numbers.enqueue(x, y)

    return JsonResponse(
        {
            "task_id": result.id,
            "status": result.status,
            "message": f"Enqueued add_numbers({x}, {y})",
        }
    )


def enqueue_slow(request):
    """Enqueue a slow_calculation task."""
    seconds = int(request.GET.get("seconds", 2))

    result = tasks.slow_calculation.enqueue(seconds)

    return JsonResponse(
        {
            "task_id": result.id,
            "status": result.status,
            "message": f"Enqueued slow_calculation({seconds})",
        }
    )


def enqueue_failing(request):
    """Enqueue a failing_task."""
    result = tasks.failing_task.enqueue()

    return JsonResponse(
        {
            "task_id": result.id,
            "status": result.status,
            "message": "Enqueued failing_task()",
        }
    )


def enqueue_email(request):
    """Enqueue a send_email task."""
    to = request.GET.get("to", "test@example.com")
    subject = request.GET.get("subject", "Test Subject")
    body = request.GET.get("body", "Test Body")

    result = tasks.send_email.enqueue(to, subject, body)

    return JsonResponse(
        {
            "task_id": result.id,
            "status": result.status,
            "message": f"Enqueued send_email to {to}",
        }
    )


def task_result(request, task_id):
    """Get a task result by ID."""
    backend = task_backends["default"]

    try:
        result = backend.get_result(task_id)
        return render(
            request,
            "demo_app/result.html",
            {
                "result": result,
            },
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=404)


def task_status_json(request, task_id):
    """Get task status as JSON."""
    backend = task_backends["default"]

    try:
        result = backend.get_result(task_id)
        return JsonResponse(
            {
                "id": result.id,
                "status": result.status,
                "enqueued_at": result.enqueued_at.isoformat() if result.enqueued_at else None,
                "started_at": result.started_at.isoformat() if result.started_at else None,
                "finished_at": result.finished_at.isoformat() if result.finished_at else None,
                "return_value": result._return_value if result.status == "SUCCESSFUL" else None,
                "errors": [
                    {"class": e.exception_class_path, "traceback": e.traceback}
                    for e in result.errors
                ],
            }
        )
    except Exception as e:
        return JsonResponse({"error": str(e)}, status=404)


def queue_stats(request):
    """Get queue statistics."""
    from django_tasks_redis import executor

    stats = executor.get_queue_stats()
    counts = executor.get_task_counts()

    return JsonResponse(
        {
            "stats": stats,
            "counts": {str(k): v for k, v in counts.items()},
        }
    )

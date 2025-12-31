"""
URL patterns for Redis task HTTP endpoints.

Include these URLs in your project:

    from django.urls import include, path

    urlpatterns = [
        # ...
        path("tasks/", include("django_tasks_redis.urls")),
    ]
"""

from django.urls import path

from . import views

app_name = "django_tasks_redis"

urlpatterns = [
    path("run/", views.RunTasksView.as_view(), name="run_tasks"),
    path("run-one/", views.RunOneTaskView.as_view(), name="run_one_task"),
    path(
        "execute/<uuid:task_id>/", views.ExecuteTaskView.as_view(), name="execute_task"
    ),
    path("status/<uuid:task_id>/", views.TaskStatusView.as_view(), name="task_status"),
    path("purge/", views.PurgeCompletedTasksView.as_view(), name="purge_completed"),
]

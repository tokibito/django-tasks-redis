"""
URL patterns for demo app.
"""

from django.urls import path

from . import views

app_name = "demo_app"

urlpatterns = [
    path("", views.index, name="index"),
    path("enqueue/add/", views.enqueue_add, name="enqueue_add"),
    path("enqueue/slow/", views.enqueue_slow, name="enqueue_slow"),
    path("enqueue/failing/", views.enqueue_failing, name="enqueue_failing"),
    path("enqueue/email/", views.enqueue_email, name="enqueue_email"),
    path("result/<uuid:task_id>/", views.task_result, name="task_result"),
    path("status/<uuid:task_id>/", views.task_status_json, name="task_status"),
    path("stats/", views.queue_stats, name="queue_stats"),
]

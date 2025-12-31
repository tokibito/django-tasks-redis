"""
URL configuration for example project.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("demo_app.urls")),
    path("tasks/", include("django_tasks_redis.urls")),
]

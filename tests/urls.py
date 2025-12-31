"""
URL configuration for tests.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("tasks/", include("django_tasks_redis.urls")),
]

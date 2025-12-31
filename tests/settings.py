"""
Django settings for tests.
"""

SECRET_KEY = "test-secret-key-for-django-tasks-redis"

DEBUG = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django_tasks_redis",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

USE_TZ = True

TIME_ZONE = "UTC"

TASKS = {
    "default": {
        "BACKEND": "django_tasks_redis.RedisTaskBackend",
        "QUEUES": [],  # Empty list = allow all queue names
        "OPTIONS": {
            "REDIS_URL": "redis://localhost:6379/0",
            "REDIS_KEY_PREFIX": "django_tasks_test",
            "REDIS_RESULT_TTL": 3600,  # 1 hour for tests
        },
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

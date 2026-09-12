"""
Django settings for tests.
"""

import os

# Set REDIS_URL to run the suite against another server, e.g. a throwaway
# container on a spare port instead of the developer's own Redis.
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

SECRET_KEY = "test-secret-key-for-django-tasks-redis"

DEBUG = True

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.admin",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django_tasks_redis",
]

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "tests.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

USE_TZ = True

TIME_ZONE = "UTC"

REDIS_OPTIONS = {
    "REDIS_URL": REDIS_URL,
    "REDIS_KEY_PREFIX": "django_tasks_test",
    "REDIS_RESULT_TTL": 3600,  # 1 hour for tests
}

TASKS = {
    "default": {
        "BACKEND": "tests.backends.TokenAuthRedisTaskBackend",
        "QUEUES": [],  # Empty list = allow all queue names
        "OPTIONS": REDIS_OPTIONS,
    },
    # No authentication handler, so its HTTP endpoints stay closed.
    "closed": {
        "BACKEND": "django_tasks_redis.RedisTaskBackend",
        "QUEUES": [],
        "OPTIONS": REDIS_OPTIONS,
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

"""
Task backend for the demo project.
"""

from django.conf import settings
from django.http import JsonResponse

from django_tasks_redis.backends import RedisTaskBackend


class TokenAuthRedisTaskBackend(RedisTaskBackend):
    """
    Opens the HTTP task endpoints to callers holding a shared secret.

    A real project would verify a signature over request.body instead.
    """

    def get_auth_handler(self):
        def handler(request):
            if request.headers.get("X-Task-Token") != settings.TASK_ENDPOINT_TOKEN:
                return JsonResponse({"error": "Forbidden"}, status=403)
            return None

        return handler

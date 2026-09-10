"""
Task backend used by the test project.
"""

from django.http import JsonResponse

from django_tasks_redis.backends import RedisTaskBackend

TASK_ENDPOINT_TOKEN = "test-endpoint-token"


class TokenAuthRedisTaskBackend(RedisTaskBackend):
    """
    Backend that opens the HTTP endpoints to a shared secret.

    Mirrors what a project has to do to use them: the endpoints run and delete
    tasks, so they stay closed until a backend says how to authenticate them.
    """

    def get_auth_handler(self):
        def handler(request):
            if request.headers.get("X-Task-Token") != TASK_ENDPOINT_TOKEN:
                return JsonResponse({"error": "Forbidden"}, status=403)
            return None

        return handler


class UnbuildableRedisTaskBackend(RedisTaskBackend):
    def __init__(self, alias, params):
        raise RuntimeError("REDIS_URL is missing")

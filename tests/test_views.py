"""
Tests for views module.
"""

import warnings

import pytest
from django.conf import settings
from django.http import JsonResponse
from django.tasks import task_backends
from django.test import Client, override_settings

from django_tasks_redis import executor
from django_tasks_redis.backends import RedisTaskBackend


class LegacyHandlerBackend(RedisTaskBackend):
    """Backend that still overrides the deprecated get_auth_handler()."""

    def get_auth_handler(self):
        def handler(request):
            if request.headers.get("X-Task-Token") != "legacy-token":
                return JsonResponse({"error": "Forbidden"}, status=403)
            return None

        return handler


@pytest.mark.django_db
class TestTaskEndpointAuth:
    """Tests for the authentication guarding every endpoint."""

    @pytest.mark.parametrize(
        ("method", "url"),
        [
            ("post", "/tasks/run/"),
            ("post", "/tasks/run-one/"),
            ("post", "/tasks/purge/"),
            ("get", "/tasks/status/00000000-0000-0000-0000-000000000000/"),
            ("post", "/tasks/execute/00000000-0000-0000-0000-000000000000/"),
        ],
    )
    def test_endpoint_rejects_unauthenticated_request(self, clean_redis, method, url):
        """Every endpoint refuses a request the backend did not authenticate."""
        client = Client()

        response = getattr(client, method)(url)

        assert response.status_code == 403

    def test_endpoint_is_closed_when_backend_has_no_auth_handler(self, clean_redis):
        """A backend that returns no handler keeps its endpoints closed."""
        client = Client()

        response = client.post("/tasks/run/", {"backend_name": "closed"})

        assert response.status_code == 403
        assert "get_auth_handlers" in response.json()["error"]
        assert "AUTH_HANDLERS" in response.json()["error"]

    def test_unknown_backend_is_rejected(self, clean_redis, auth_client):
        """An unknown backend name is refused instead of raising."""
        response = auth_client.post("/tasks/run/", {"backend_name": "nope"})

        assert response.status_code == 400

    def test_misconfigured_backend_does_not_look_like_a_typo(self, clean_redis):
        broken = {
            **settings.TASKS,
            "broken": {
                "BACKEND": "tests.backends.UnbuildableRedisTaskBackend",
                "QUEUES": [],
                "OPTIONS": {},
            },
        }

        with override_settings(TASKS=broken), pytest.raises(RuntimeError):
            Client().post("/tasks/run/", {"backend_name": "broken"})

    @pytest.mark.parametrize(
        ("data", "content_type"),
        [
            ({"max_tasks": "1"}, None),  # multipart, the test client default
            ('{"max_tasks": 1}', "application/json"),
            ("max_tasks=1", "application/x-www-form-urlencoded"),
        ],
    )
    def test_auth_handler_can_read_the_request_body(
        self, clean_redis, data, content_type
    ):
        """Reading POST parses a multipart stream and makes request.body raise."""
        backend = task_backends["default"]
        seen = {}

        def handler(request):
            seen["body"] = request.body
            return None

        backend.get_auth_handlers = lambda endpoint=None: [handler]
        try:
            post = {} if content_type is None else {"content_type": content_type}
            response = Client().post("/tasks/run/", data, **post)
        finally:
            del backend.get_auth_handlers

        assert response.status_code == 200
        assert b"max_tasks" in seen["body"]

    def test_empty_handler_list_keeps_endpoint_closed(self, clean_redis):
        """An empty handler list is the closed-by-default signal, not open."""
        backend = task_backends["default"]
        backend.get_auth_handlers = lambda endpoint=None: []
        try:
            response = Client(headers={"x-task-token": "test-endpoint-token"}).post(
                "/tasks/run/"
            )
        finally:
            del backend.get_auth_handlers

        assert response.status_code == 403

    def test_first_accepting_handler_wins(self, clean_redis):
        """A request is accepted as soon as one handler accepts it."""
        backend = task_backends["default"]
        called = []

        def reject(request):
            called.append("reject")
            return JsonResponse({"error": "no"}, status=401)

        def accept(request):
            called.append("accept")
            return None

        backend.get_auth_handlers = lambda endpoint=None: [reject, accept]
        try:
            response = Client().post("/tasks/run/")
        finally:
            del backend.get_auth_handlers

        assert response.status_code == 200
        # The reject handler ran, the accept handler ran, no third handler did.
        assert called == ["reject", "accept"]

    def test_first_rejection_returned_when_all_reject(self, clean_redis):
        """When every handler rejects, the first rejection is the response."""
        backend = task_backends["default"]

        def first_reject(request):
            return JsonResponse({"error": "first"}, status=401)

        def second_reject(request):
            return JsonResponse({"error": "second"}, status=403)

        backend.get_auth_handlers = lambda endpoint=None: [first_reject, second_reject]
        try:
            response = Client().post("/tasks/run/")
        finally:
            del backend.get_auth_handlers

        assert response.status_code == 401
        assert response.json() == {"error": "first"}

    def test_endpoints_option_excludes_a_handler_from_a_view(self, clean_redis):
        """A handler scoped to one endpoint does not fire on another."""
        # A bare RedisTaskBackend whose only handler is scoped to "purge".
        # /tasks/run/ has no matching handler, so it falls through to the
        # closed 403.
        scoped = {
            **settings.TASKS,
            "scoped": {
                "BACKEND": "django_tasks_redis.RedisTaskBackend",
                "QUEUES": [],
                "OPTIONS": {
                    **settings.TASKS["default"]["OPTIONS"],
                    "AUTH_HANDLERS": [
                        {
                            "HANDLER": "django_tasks_redis.auth.SharedSecretAuth",
                            "OPTIONS": {"TOKEN": "test-endpoint-token"},
                            "ENDPOINTS": ["purge"],
                        }
                    ],
                },
            },
        }

        with override_settings(TASKS=scoped):
            # /tasks/run/ has no matching handler, so the endpoint stays closed.
            run_response = Client(headers={"x-task-token": "test-endpoint-token"}).post(
                "/tasks/run/", {"backend_name": "scoped"}
            )
            assert run_response.status_code == 403

            # /tasks/purge/ shares the token header, but SharedSecretAuth
            # reads Authorization by default, not X-Task-Token. So even on the
            # matching endpoint, the handler rejects and the first rejection
            # is returned.
            purge_response = Client(
                headers={"x-task-token": "test-endpoint-token"}
            ).post("/tasks/purge/", {"backend_name": "scoped", "days": "0"})
            assert purge_response.status_code == 401
            assert "Missing Authorization header" in purge_response.content.decode()

    def test_auth_handlers_option_opens_endpoints_without_a_subclass(self, clean_redis):
        """Configuring AUTH_HANDLERS is enough to open the endpoints."""
        from django_tasks_redis.auth import SharedSecretAuth

        configured = {
            **settings.TASKS,
            "configured": {
                "BACKEND": "django_tasks_redis.RedisTaskBackend",
                "QUEUES": [],
                "OPTIONS": {
                    **settings.TASKS["default"]["OPTIONS"],
                    "AUTH_HANDLERS": [SharedSecretAuth({"TOKEN": "shared-secret"})],
                },
            },
        }

        with override_settings(TASKS=configured):
            # Wrong token: rejected by the configured handler.
            bad = Client(headers={"authorization": "Bearer wrong"}).post(
                "/tasks/run/", {"backend_name": "configured"}
            )
            assert bad.status_code == 401
            assert "Invalid token" in bad.content.decode()

            # Right token: accepted.
            good = Client(headers={"authorization": "Bearer shared-secret"}).post(
                "/tasks/run/", {"backend_name": "configured"}
            )
            assert good.status_code == 200

    def test_deprecated_get_auth_handler_override_still_works_with_warning(
        self, clean_redis
    ):
        """A subclass still overriding get_auth_handler() emits a warning
        and its return value is used as the sole handler."""

        legacy = {
            **settings.TASKS,
            "legacy": {
                "BACKEND": "tests.test_views.LegacyHandlerBackend",
                "QUEUES": [],
                "OPTIONS": settings.TASKS["default"]["OPTIONS"],
            },
        }

        with (
            override_settings(TASKS=legacy),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            bad = Client().post("/tasks/run/", {"backend_name": "legacy"})
            assert bad.status_code == 403

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert deprecations, "expected a DeprecationWarning"
        assert "get_auth_handler()" in str(deprecations[0].message)
        assert "get_auth_handlers" in str(deprecations[0].message)

        # And the legacy handler is still actually called: the right token
        # reaches the view.
        with override_settings(TASKS=legacy):
            good = Client(headers={"x-task-token": "legacy-token"}).post(
                "/tasks/run/", {"backend_name": "legacy"}
            )
        assert good.status_code == 200

    def test_deprecation_warning_emitted_once_per_backend(self, clean_redis):
        """The warning fires on the first use, not on every request."""
        legacy = {
            **settings.TASKS,
            "legacy": {
                "BACKEND": "tests.test_views.LegacyHandlerBackend",
                "QUEUES": [],
                "OPTIONS": settings.TASKS["default"]["OPTIONS"],
            },
        }

        client = Client()
        with (
            override_settings(TASKS=legacy),
            warnings.catch_warnings(record=True) as caught,
        ):
            warnings.simplefilter("always")
            for _ in range(3):
                client.post("/tasks/run/", {"backend_name": "legacy"})

        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
        assert len(deprecations) == 1


@pytest.mark.django_db
class TestTaskEndpointInput:
    """Bad input must be answered, not raised."""

    @pytest.mark.parametrize(
        ("url", "data"),
        [
            ("/tasks/run/", {"max_tasks": "many"}),
            ("/tasks/purge/", {"days": "forever"}),
        ],
    )
    def test_non_numeric_parameter_is_rejected(
        self, clean_redis, auth_client, url, data
    ):
        response = auth_client.post(url, data)

        assert response.status_code == 400
        assert "error" in response.json()

    def test_status_of_unknown_task_is_not_found(self, clean_redis, auth_client):
        """A well-formed id that is not in Redis reaches the view's own 404."""
        response = auth_client.get(
            "/tasks/status/8ad0a2f6-0e04-4c0e-8b1e-9a6f5c2d3e4b/"
        )

        assert response.status_code == 404
        assert response.json()["error"] == "Task not found"

    def test_executing_an_unknown_task_is_not_found(self, clean_redis, auth_client):
        response = auth_client.post(
            "/tasks/execute/8ad0a2f6-0e04-4c0e-8b1e-9a6f5c2d3e4b/"
        )

        assert response.status_code == 404


@pytest.mark.django_db
class TestRunTasksView:
    """Tests for RunTasksView."""

    def test_run_tasks_empty(self, clean_redis, auth_client):
        """Test running tasks when queue is empty."""
        response = auth_client.post("/tasks/run/")

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] == 0
        assert data["tasks"] == []

    def test_run_tasks(self, clean_redis, auth_client):
        """Test running tasks."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        simple_task.enqueue(3, 4)

        response = auth_client.post("/tasks/run/")

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] == 2
        assert len(data["tasks"]) == 2

    def test_run_tasks_with_max(self, clean_redis, auth_client):
        """Test running tasks with max_tasks limit."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 1)
        simple_task.enqueue(2, 2)
        simple_task.enqueue(3, 3)

        response = auth_client.post("/tasks/run/", {"max_tasks": "2"})

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] == 2


@pytest.mark.django_db
class TestRunOneTaskView:
    """Tests for RunOneTaskView."""

    def test_run_one_task_empty(self, clean_redis, auth_client):
        """Test running one task when queue is empty."""
        response = auth_client.post("/tasks/run-one/")

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] is False

    def test_run_one_task(self, clean_redis, auth_client):
        """Test running one task."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(10, 20)

        response = auth_client.post("/tasks/run-one/")

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] is True
        assert data["task"]["id"] == str(result.id)
        assert data["task"]["status"] == "SUCCESSFUL"


@pytest.mark.django_db
class TestExecuteTaskView:
    """Tests for ExecuteTaskView."""

    def test_execute_task(self, clean_redis, auth_client):
        """Test executing a specific task."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(5, 10)

        response = auth_client.post(f"/tasks/execute/{result.id}/")

        assert response.status_code == 200
        data = response.json()
        assert data["task"]["id"] == str(result.id)
        assert data["task"]["status"] == "SUCCESSFUL"

    def test_execute_task_not_found(self, clean_redis, auth_client):
        """Test executing a non-existent task."""
        response = auth_client.post("/tasks/execute/non-existent-id/")

        assert response.status_code == 404

    def test_execute_task_already_run(self, clean_redis, auth_client):
        """Test executing an already completed task."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        executor.run_task_by_id(result.id)

        response = auth_client.post(f"/tasks/execute/{result.id}/")

        assert response.status_code == 400
        data = response.json()
        assert "error" in data


@pytest.mark.django_db
class TestTaskStatusView:
    """Tests for TaskStatusView."""

    def test_task_status(self, clean_redis, auth_client):
        """Test getting task status."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)

        response = auth_client.get(f"/tasks/status/{result.id}/")

        assert response.status_code == 200
        data = response.json()
        assert data["task"]["task_id"] == str(result.id)
        assert data["task"]["status"] == "READY"

    def test_task_status_not_found(self, clean_redis, auth_client):
        """Test getting status of non-existent task."""
        response = auth_client.get("/tasks/status/non-existent-id/")

        assert response.status_code == 404


@pytest.mark.django_db
class TestPurgeCompletedTasksView:
    """Tests for PurgeCompletedTasksView."""

    def test_purge_completed_tasks(self, redis_backend, clean_redis, auth_client):
        """Test purging completed tasks."""
        from datetime import timedelta

        from django.utils import timezone

        from django_tasks_redis.utils import get_result_key, serialize_datetime
        from tests.tasks import simple_task

        # Enqueue and run a task
        result = simple_task.enqueue(1, 2)
        executor.run_task_by_id(result.id)

        # Modify finished_at to be old
        client_redis = redis_backend.get_client()
        result_key = get_result_key(
            redis_backend.key_prefix, redis_backend.alias, result.id
        )
        old_time = timezone.now() - timedelta(days=10)
        client_redis.hset(result_key, "finished_at", serialize_datetime(old_time))

        response = auth_client.post("/tasks/purge/", {"days": "7"})

        assert response.status_code == 200
        data = response.json()
        assert data["deleted"] == 1

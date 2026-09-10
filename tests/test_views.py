"""
Tests for views module.
"""

import pytest
from django.conf import settings
from django.tasks import task_backends
from django.test import Client, override_settings

from django_tasks_redis import executor


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
        assert "get_auth_handler" in response.json()["error"]

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

        backend.get_auth_handler = lambda: handler
        try:
            post = {} if content_type is None else {"content_type": content_type}
            response = Client().post("/tasks/run/", data, **post)
        finally:
            del backend.get_auth_handler

        assert response.status_code == 200
        assert b"max_tasks" in seen["body"]


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

"""
Tests for views module.
"""

import pytest
from django.test import Client

from django_tasks_redis import executor


@pytest.mark.django_db
class TestRunTasksView:
    """Tests for RunTasksView."""

    def test_run_tasks_empty(self, clean_redis):
        """Test running tasks when queue is empty."""
        client = Client()
        response = client.post("/tasks/run/")

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] == 0
        assert data["tasks"] == []

    def test_run_tasks(self, clean_redis):
        """Test running tasks."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        simple_task.enqueue(3, 4)

        client = Client()
        response = client.post("/tasks/run/")

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] == 2
        assert len(data["tasks"]) == 2

    def test_run_tasks_with_max(self, clean_redis):
        """Test running tasks with max_tasks limit."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 1)
        simple_task.enqueue(2, 2)
        simple_task.enqueue(3, 3)

        client = Client()
        response = client.post("/tasks/run/", {"max_tasks": "2"})

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] == 2


@pytest.mark.django_db
class TestRunOneTaskView:
    """Tests for RunOneTaskView."""

    def test_run_one_task_empty(self, clean_redis):
        """Test running one task when queue is empty."""
        client = Client()
        response = client.post("/tasks/run-one/")

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] is False

    def test_run_one_task(self, clean_redis):
        """Test running one task."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(10, 20)

        client = Client()
        response = client.post("/tasks/run-one/")

        assert response.status_code == 200
        data = response.json()
        assert data["processed"] is True
        assert data["task"]["id"] == str(result.id)
        assert data["task"]["status"] == "SUCCESSFUL"


@pytest.mark.django_db
class TestExecuteTaskView:
    """Tests for ExecuteTaskView."""

    def test_execute_task(self, clean_redis):
        """Test executing a specific task."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(5, 10)

        client = Client()
        response = client.post(f"/tasks/execute/{result.id}/")

        assert response.status_code == 200
        data = response.json()
        assert data["task"]["id"] == str(result.id)
        assert data["task"]["status"] == "SUCCESSFUL"

    def test_execute_task_not_found(self, clean_redis):
        """Test executing a non-existent task."""
        client = Client()
        response = client.post("/tasks/execute/non-existent-id/")

        assert response.status_code == 404

    def test_execute_task_already_run(self, clean_redis):
        """Test executing an already completed task."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        executor.run_task_by_id(result.id)

        client = Client()
        response = client.post(f"/tasks/execute/{result.id}/")

        assert response.status_code == 400
        data = response.json()
        assert "error" in data


@pytest.mark.django_db
class TestTaskStatusView:
    """Tests for TaskStatusView."""

    def test_task_status(self, clean_redis):
        """Test getting task status."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)

        client = Client()
        response = client.get(f"/tasks/status/{result.id}/")

        assert response.status_code == 200
        data = response.json()
        assert data["task"]["task_id"] == str(result.id)
        assert data["task"]["status"] == "READY"

    def test_task_status_not_found(self, clean_redis):
        """Test getting status of non-existent task."""
        client = Client()
        response = client.get("/tasks/status/non-existent-id/")

        assert response.status_code == 404


@pytest.mark.django_db
class TestPurgeCompletedTasksView:
    """Tests for PurgeCompletedTasksView."""

    def test_purge_completed_tasks(self, redis_backend, clean_redis):
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

        client = Client()
        response = client.post("/tasks/purge/", {"days": "7"})

        assert response.status_code == 200
        data = response.json()
        assert data["deleted"] == 1

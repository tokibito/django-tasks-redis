"""
Tests for admin module.
"""

import pytest
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, RequestFactory

from django_tasks_redis import executor
from django_tasks_redis.admin import (
    PriorityFilter,
    QueueNameFilter,
    RedisTaskAdmin,
    RedisTaskObject,
    StatusFilter,
    TaskPathFilter,
)
from django_tasks_redis.models import RedisTask


@pytest.fixture
def admin_site():
    """Get admin site."""
    return AdminSite()


@pytest.fixture
def redis_task_admin(admin_site):
    """Get RedisTaskAdmin instance."""
    return RedisTaskAdmin(RedisTask, admin_site)


@pytest.fixture
def admin_user(db):
    """Create and return admin user."""
    User = get_user_model()
    return User.objects.create_superuser(
        username="admin", email="admin@example.com", password="password"
    )


@pytest.fixture
def admin_client(admin_user):
    """Get logged-in admin client."""
    client = Client()
    client.force_login(admin_user)
    return client


@pytest.fixture
def staff_user(db):
    """Staff user without any task permission."""
    User = get_user_model()
    return User.objects.create_user(
        username="staff",
        email="staff@example.com",
        password="password",
        is_staff=True,
    )


@pytest.fixture
def staff_client(staff_user):
    """Logged-in client for a staff user with no task permission."""
    client = Client()
    client.force_login(staff_user)
    return client


@pytest.fixture
def grant():
    """Grant task permissions to a user and return them with a fresh cache.

    has_perm() caches its answers on the user instance, so a user handed back
    from create_user() would keep saying no.
    """

    def _grant(user, *codenames):
        perms = Permission.objects.filter(
            content_type__app_label="django_tasks_redis",
            codename__in=codenames,
        )
        assert len(perms) == len(codenames), f"unknown permission in {codenames}"
        user.user_permissions.add(*perms)
        return get_user_model().objects.get(pk=user.pk)

    return _grant


class TestRedisTaskObject:
    """Tests for RedisTaskObject wrapper class."""

    def test_init(self):
        """Test initialization."""
        task_data = {
            "task_id": "test-123",
            "task_path": "myapp.tasks.my_task",
            "status": "READY",
            "priority": 10,
            "queue_name": "default",
        }
        obj = RedisTaskObject(task_data, None)

        assert obj.pk == "test-123"
        assert obj.task_id == "test-123"
        assert obj.task_path == "myapp.tasks.my_task"
        assert obj.status == "READY"
        assert obj.priority == 10
        assert obj.queue_name == "default"

    def test_str(self):
        """Test string representation."""
        task_data = {"task_id": "test-456"}
        obj = RedisTaskObject(task_data, None)

        assert str(obj) == "test-456"

    def test_serializable_value(self):
        """Test serializable_value method."""
        task_data = {"task_id": "test-789", "status": "RUNNING"}
        obj = RedisTaskObject(task_data, None)

        assert obj.serializable_value("task_id") == "test-789"
        assert obj.serializable_value("status") == "RUNNING"
        assert obj.serializable_value("nonexistent") is None


class TestRedisTaskAdmin:
    """Tests for RedisTaskAdmin."""

    def test_has_add_permission(self, redis_task_admin):
        """Test that add permission is disabled."""
        request = RequestFactory().get("/")
        assert redis_task_admin.has_add_permission(request) is False

    def test_has_change_permission(self, redis_task_admin):
        """Test that change permission is disabled."""
        request = RequestFactory().get("/")
        assert redis_task_admin.has_change_permission(request) is False

    def test_delete_permission_follows_the_user(
        self, redis_task_admin, admin_user, staff_user
    ):
        """Deleting tasks takes the delete permission, like any model."""
        request = RequestFactory().get("/")

        request.user = staff_user
        assert redis_task_admin.has_delete_permission(request) is False

        request.user = admin_user
        assert redis_task_admin.has_delete_permission(request) is True

    def test_run_permission_follows_the_user(
        self, redis_task_admin, admin_user, staff_user
    ):
        """Running tasks takes the run permission."""
        request = RequestFactory().get("/")

        request.user = staff_user
        assert redis_task_admin.has_run_permission(request) is False

        request.user = admin_user
        assert redis_task_admin.has_run_permission(request) is True

    def test_unprivileged_staff_gets_no_destructive_actions(
        self, redis_task_admin, staff_user
    ):
        """A staff user without task permissions cannot run or delete them."""
        request = RequestFactory().get("/")
        request.user = staff_user

        assert redis_task_admin.get_actions(request) == {}

    def test_builtin_delete_action_is_not_offered(self, redis_task_admin, admin_user):
        """Django's queryset delete would report success while doing nothing."""
        request = RequestFactory().get("/")
        request.user = admin_user

        assert "delete_selected" not in redis_task_admin.get_actions(request)


@pytest.mark.django_db
class TestRedisTaskPermissions:
    """The permissions the admin checks have to exist to be grantable."""

    def test_permissions_are_created_for_the_pseudo_model(self):
        """RedisTask lives in models.py, so migrate creates its permissions."""
        codenames = set(
            Permission.objects.filter(
                content_type__app_label="django_tasks_redis"
            ).values_list("codename", flat=True)
        )

        assert codenames == {"view_redistask", "run_redistask", "delete_redistask"}

    def test_no_permission_is_offered_for_what_the_admin_refuses(self):
        """Tasks cannot be added or edited, so those permissions do not exist."""
        codenames = set(
            Permission.objects.filter(
                content_type__app_label="django_tasks_redis"
            ).values_list("codename", flat=True)
        )

        assert "add_redistask" not in codenames
        assert "change_redistask" not in codenames

    def test_view_permission_opens_the_changelist(self, staff_user, grant):
        """A granted view permission is enough to read the task list."""
        user = grant(staff_user, "view_redistask")
        client = Client()
        client.force_login(user)

        response = client.get("/admin/django_tasks_redis/redistask/")

        assert response.status_code == 200

    def test_view_permission_opens_the_detail_view(
        self, staff_user, grant, clean_redis
    ):
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        user = grant(staff_user, "view_redistask")
        client = Client()
        client.force_login(user)

        response = client.get(
            f"/admin/django_tasks_redis/redistask/{result.id}/detail/"
        )

        assert response.status_code == 200

    def test_view_permission_alone_offers_no_actions(
        self, redis_task_admin, staff_user, grant
    ):
        """Reading tasks does not imply running or deleting them."""
        request = RequestFactory().get("/")
        request.user = grant(staff_user, "view_redistask")

        assert redis_task_admin.get_actions(request) == {}

    def test_run_permission_offers_only_the_run_actions(
        self, redis_task_admin, staff_user, grant
    ):
        request = RequestFactory().get("/")
        request.user = grant(staff_user, "view_redistask", "run_redistask")

        actions = redis_task_admin.get_actions(request)

        assert set(actions) == {"run_selected_tasks", "retry_failed_tasks"}

    def test_delete_permission_offers_only_the_delete_action(
        self, redis_task_admin, staff_user, grant
    ):
        request = RequestFactory().get("/")
        request.user = grant(staff_user, "view_redistask", "delete_redistask")

        actions = redis_task_admin.get_actions(request)

        assert set(actions) == {"delete_selected_tasks"}

    def test_id_short(self, redis_task_admin):
        """Test shortened ID display."""
        task_data = {"task_id": "12345678-1234-1234-1234-123456789012"}
        obj = RedisTaskObject(task_data, None)

        result = redis_task_admin.id_short(obj)
        assert result == "12345678"

    def test_task_path_short(self, redis_task_admin):
        """Test shortened task path display."""
        task_data = {"task_path": "myapp.tasks.my_task"}
        obj = RedisTaskObject(task_data, None)

        result = redis_task_admin.task_path_short(obj)
        assert result == "myapp.tasks.my_task"

    def test_task_path_short_long(self, redis_task_admin):
        """Test shortened task path for long paths."""
        long_path = "myapp.tasks.submodule.another.deeply.nested.my_very_long_task_name"
        task_data = {"task_path": long_path}
        obj = RedisTaskObject(task_data, None)

        result = redis_task_admin.task_path_short(obj)
        assert len(result) <= 40
        assert result.startswith("...")

    def test_status_badge(self, redis_task_admin):
        """Test status badge HTML."""
        for status in ["READY", "RUNNING", "SUCCESSFUL", "FAILED"]:
            task_data = {"status": status}
            obj = RedisTaskObject(task_data, None)

            result = redis_task_admin.status_badge(obj)
            assert status in result
            assert "background-color" in result

    def test_get_priority(self, redis_task_admin):
        """Test priority display."""
        task_data = {"priority": 10}
        obj = RedisTaskObject(task_data, None)

        result = redis_task_admin.get_priority(obj)
        assert result == 10

    def test_get_queue_name(self, redis_task_admin):
        """Test queue name display."""
        task_data = {"queue_name": "emails"}
        obj = RedisTaskObject(task_data, None)

        result = redis_task_admin.get_queue_name(obj)
        assert result == "emails"

    def test_get_object(self, redis_task_admin, clean_redis):
        """Test getting object from Redis."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)
        request = RequestFactory().get("/")

        obj = redis_task_admin.get_object(request, result.id)

        assert obj is not None
        assert obj.task_id == str(result.id)

    def test_get_object_not_found(self, redis_task_admin, clean_redis):
        """Test getting non-existent object."""
        request = RequestFactory().get("/")
        obj = redis_task_admin.get_object(request, "non-existent-id")
        assert obj is None


@pytest.mark.django_db
class TestRedisTaskAdminViews:
    """Tests for RedisTaskAdmin views."""

    def test_changelist_view(self, admin_client, clean_redis):
        """Test changelist view."""
        response = admin_client.get("/admin/django_tasks_redis/redistask/")
        assert response.status_code == 200

    def test_changelist_view_with_tasks(self, admin_client, clean_redis):
        """Test changelist view with tasks."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        simple_task.enqueue(3, 4)

        response = admin_client.get("/admin/django_tasks_redis/redistask/")
        assert response.status_code == 200

    def test_detail_view(self, admin_client, clean_redis):
        """Test task detail view."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)

        response = admin_client.get(
            f"/admin/django_tasks_redis/redistask/{result.id}/detail/"
        )
        assert response.status_code == 200
        assert str(result.id) in response.content.decode()

    def test_detail_view_requires_view_permission(self, staff_client, clean_redis):
        """Being staff is not enough to read a task's arguments and errors."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)

        response = staff_client.get(
            f"/admin/django_tasks_redis/redistask/{result.id}/detail/"
        )

        assert response.status_code == 403

    def test_detail_view_not_found(self, admin_client, clean_redis):
        """Test task detail view for non-existent task."""
        response = admin_client.get(
            "/admin/django_tasks_redis/redistask/non-existent-id/detail/"
        )
        assert response.status_code == 200
        assert "Task Not Found" in response.content.decode()

    def test_change_view_redirects_to_detail(self, admin_client, clean_redis):
        """Test that change view redirects to detail view."""
        from tests.tasks import simple_task

        result = simple_task.enqueue(1, 2)

        response = admin_client.get(
            f"/admin/django_tasks_redis/redistask/{result.id}/change/"
        )
        assert response.status_code == 302
        assert f"{result.id}/detail/" in response.url


@pytest.mark.django_db
class TestRedisTaskAdminActions:
    """Tests for RedisTaskAdmin actions."""

    def test_run_selected_tasks(self, admin_client, clean_redis):
        """Test run selected tasks action."""
        from tests.tasks import simple_task

        result1 = simple_task.enqueue(1, 2)
        result2 = simple_task.enqueue(3, 4)

        response = admin_client.post(
            "/admin/django_tasks_redis/redistask/",
            {
                "action": "run_selected_tasks",
                "_selected_action": [result1.id, result2.id],
            },
        )

        assert response.status_code == 302  # Redirect after action

        # Verify tasks were run
        task1 = executor.get_task_by_id(result1.id)
        task2 = executor.get_task_by_id(result2.id)
        assert task1["status"] == "SUCCESSFUL"
        assert task2["status"] == "SUCCESSFUL"

    def test_retry_failed_tasks(self, admin_client, clean_redis):
        """Test retry failed tasks action."""
        from tests.tasks import failing_task

        result = failing_task.enqueue()
        executor.run_task_by_id(result.id)

        # Verify it's failed
        task = executor.get_task_by_id(result.id)
        assert task["status"] == "FAILED"

        response = admin_client.post(
            "/admin/django_tasks_redis/redistask/",
            {
                "action": "retry_failed_tasks",
                "_selected_action": [result.id],
            },
        )

        assert response.status_code == 302

    def test_delete_selected_tasks(self, admin_client, clean_redis):
        """Test delete selected tasks action."""
        from tests.tasks import simple_task

        result1 = simple_task.enqueue(1, 2)
        result2 = simple_task.enqueue(3, 4)
        result3 = simple_task.enqueue(5, 6)

        response = admin_client.post(
            "/admin/django_tasks_redis/redistask/",
            {
                "action": "delete_selected_tasks",
                "_selected_action": [result1.id, result2.id],
            },
        )

        assert response.status_code == 302

        # Verify tasks were deleted
        assert executor.get_task_by_id(result1.id) is None
        assert executor.get_task_by_id(result2.id) is None
        assert executor.get_task_by_id(result3.id) is not None


@pytest.mark.django_db
class TestRedisTaskAdminFilters:
    """Tests for admin list filters."""

    def test_list_filter_configured(self, redis_task_admin):
        """Test that list_filter includes all four filters."""
        assert StatusFilter in redis_task_admin.list_filter
        assert QueueNameFilter in redis_task_admin.list_filter
        assert TaskPathFilter in redis_task_admin.list_filter
        assert PriorityFilter in redis_task_admin.list_filter

    def test_status_filter_lookups(self, redis_task_admin, clean_redis):
        """Test that StatusFilter returns correct lookups."""
        from tests.tasks import failing_task, simple_task

        simple_task.enqueue(1, 2)
        result = failing_task.enqueue()
        executor.run_task_by_id(result.id)

        request = RequestFactory().get("/")
        f = StatusFilter(request, {}, RedisTask, redis_task_admin)
        lookups = f.lookups(request, redis_task_admin)
        values = [v for v, _ in lookups]

        assert "READY" in values
        assert "FAILED" in values

    def test_queue_name_filter_lookups(self, redis_task_admin, clean_redis):
        """Test that QueueNameFilter returns correct lookups."""
        from tests.tasks import email_task, simple_task

        simple_task.enqueue(1, 2)
        email_task.enqueue("a@b.com", "Hi", "Body")

        request = RequestFactory().get("/")
        f = QueueNameFilter(request, {}, RedisTask, redis_task_admin)
        lookups = f.lookups(request, redis_task_admin)
        values = [v for v, _ in lookups]

        assert "default" in values
        assert "emails" in values

    def test_task_path_filter_lookups(self, redis_task_admin, clean_redis):
        """Test that TaskPathFilter returns correct lookups."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)

        request = RequestFactory().get("/")
        f = TaskPathFilter(request, {}, RedisTask, redis_task_admin)
        lookups = f.lookups(request, redis_task_admin)
        values = [v for v, _ in lookups]

        assert "tests.tasks.simple_task" in values

    def test_priority_filter_lookups(self, redis_task_admin, clean_redis):
        """Test that PriorityFilter returns correct lookups."""
        from tests.tasks import high_priority_task, simple_task

        simple_task.enqueue(1, 2)
        high_priority_task.enqueue()

        request = RequestFactory().get("/")
        f = PriorityFilter(request, {}, RedisTask, redis_task_admin)
        lookups = f.lookups(request, redis_task_admin)
        values = [v for v, _ in lookups]

        assert "0" in values
        assert "10" in values

    def test_changelist_filter_by_status(self, admin_client, clean_redis):
        """Test filtering changelist by status."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)
        result2 = simple_task.enqueue(3, 4)
        executor.run_task_by_id(result2.id)

        response = admin_client.get("/admin/django_tasks_redis/redistask/?status=READY")
        assert response.status_code == 200

    def test_changelist_filter_by_queue_name(self, admin_client, clean_redis):
        """Test filtering changelist by queue name."""
        from tests.tasks import email_task, simple_task

        simple_task.enqueue(1, 2)
        email_task.enqueue("a@b.com", "Hi", "Body")

        response = admin_client.get(
            "/admin/django_tasks_redis/redistask/?queue_name=emails"
        )
        assert response.status_code == 200

    def test_changelist_filter_by_task_path(self, admin_client, clean_redis):
        """Test filtering changelist by task path."""
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)

        response = admin_client.get(
            "/admin/django_tasks_redis/redistask/?task_path=tests.tasks.simple_task"
        )
        assert response.status_code == 200

    def test_changelist_filter_by_priority(self, admin_client, clean_redis):
        """Test filtering changelist by priority."""
        from tests.tasks import high_priority_task, simple_task

        simple_task.enqueue(1, 2)
        high_priority_task.enqueue()

        response = admin_client.get("/admin/django_tasks_redis/redistask/?priority=10")
        assert response.status_code == 200

    def test_changelist_combined_filters(self, admin_client, clean_redis):
        """Test filtering changelist with multiple filters."""
        from tests.tasks import email_task, simple_task

        simple_task.enqueue(1, 2)
        email_task.enqueue("a@b.com", "Hi", "Body")

        response = admin_client.get(
            "/admin/django_tasks_redis/redistask/?status=READY&queue_name=default"
        )
        assert response.status_code == 200

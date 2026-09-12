"""
Tests for utils module.
"""

from datetime import UTC, datetime
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from django_tasks_redis.utils import (
    deserialize_datetime,
    deserialize_json,
    get_delayed_key,
    get_priority_stream_key,
    get_redis_client,
    get_result_key,
    get_results_index_key,
    get_stream_key,
    priority_to_level,
    serialize_datetime,
    serialize_json,
)


class TestSerializeDatetime:
    """Tests for datetime serialization."""

    def test_serialize_datetime(self):
        """Test serializing a datetime."""
        dt = datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)
        result = serialize_datetime(dt)
        assert result == "2024-01-15T10:30:00+00:00"

    def test_serialize_datetime_none(self):
        """Test serializing None."""
        result = serialize_datetime(None)
        assert result == ""


class TestDeserializeDatetime:
    """Tests for datetime deserialization."""

    def test_deserialize_datetime(self):
        """Test deserializing a datetime."""
        result = deserialize_datetime("2024-01-15T10:30:00+00:00")
        assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)

    def test_deserialize_datetime_empty(self):
        """Test deserializing empty string."""
        result = deserialize_datetime("")
        assert result is None

    def test_deserialize_datetime_none_string(self):
        """Test deserializing falsy value."""
        result = deserialize_datetime(None)
        assert result is None

    def test_naive_value_is_comparable_under_use_tz(self):
        """A value written with USE_TZ off still compares with timezone.now()."""
        result = deserialize_datetime("2024-01-15T10:30:00")

        assert timezone.is_aware(result)
        assert result < timezone.now()

    @override_settings(USE_TZ=False)
    def test_aware_value_is_comparable_without_use_tz(self):
        """A value written by a process that had USE_TZ on, read by one without."""
        result = deserialize_datetime("2024-01-15T10:30:00+00:00")

        assert timezone.is_naive(result)
        assert result < timezone.now()

    def test_normalising_preserves_the_instant(self):
        """The conversion changes the representation, not the point in time."""
        written = timezone.now()

        assert deserialize_datetime(serialize_datetime(written)) == written


class TestSerializeJson:
    """Tests for JSON serialization."""

    def test_serialize_json_dict(self):
        """Test serializing a dict."""
        result = serialize_json({"key": "value"})
        assert result == '{"key": "value"}'

    def test_serialize_json_list(self):
        """Test serializing a list."""
        result = serialize_json([1, 2, 3])
        assert result == "[1, 2, 3]"

    def test_serialize_json_primitives(self):
        """Test serializing primitive values."""
        assert serialize_json(42) == "42"
        assert serialize_json("hello") == '"hello"'
        assert serialize_json(True) == "true"
        assert serialize_json(None) == "null"


class TestDeserializeJson:
    """Tests for JSON deserialization."""

    def test_deserialize_json_dict(self):
        """Test deserializing a dict."""
        result = deserialize_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_deserialize_json_list(self):
        """Test deserializing a list."""
        result = deserialize_json("[1, 2, 3]")
        assert result == [1, 2, 3]

    def test_deserialize_json_empty(self):
        """Test deserializing empty string."""
        result = deserialize_json("")
        assert result is None

    def test_deserialize_json_none(self):
        """Test deserializing None."""
        result = deserialize_json(None)
        assert result is None


class TestKeyGenerators:
    """Tests for Redis key generators."""

    def test_get_stream_key(self):
        """Test stream key generation."""
        result = get_stream_key("prefix", "backend", "queue")
        assert result == "prefix:backend:queue:stream"

    def test_get_priority_stream_key(self):
        """Test priority stream key generation."""
        result = get_priority_stream_key("prefix", "backend", "queue", "high")
        assert result == "prefix:backend:queue:stream:high"

    def test_get_result_key(self):
        """Test result key generation."""
        result = get_result_key("prefix", "backend", "task-123")
        assert result == "prefix:backend:result:task-123"

    def test_get_delayed_key(self):
        """Test delayed key generation."""
        result = get_delayed_key("prefix", "backend", "queue")
        assert result == "prefix:backend:queue:delayed"

    def test_get_results_index_key(self):
        """Test results index key generation."""
        result = get_results_index_key("prefix", "backend")
        assert result == "prefix:backend:results_index"


class TestGetRedisClient:
    """Tests for Redis client construction from backend options."""

    @mock.patch("django_tasks_redis.utils.redis")
    def test_url_without_ca_certs(self, mock_redis):
        """URL config does not pass ssl_ca_certs when unset."""
        get_redis_client({"REDIS_URL": "redis://localhost:6379/0"})
        args, kwargs = mock_redis.Redis.from_url.call_args
        assert args == ("redis://localhost:6379/0",)
        assert kwargs["decode_responses"] is True
        assert "ssl_ca_certs" not in kwargs

    @mock.patch("django_tasks_redis.utils.redis")
    def test_url_with_ca_certs(self, mock_redis):
        """URL config passes ssl_ca_certs when REDIS_SSL_CA_CERTS is set."""
        get_redis_client(
            {
                "REDIS_URL": "rediss://localhost:6379/0",
                "REDIS_SSL_CA_CERTS": "/path/to/ca.pem",
            }
        )
        args, kwargs = mock_redis.Redis.from_url.call_args
        assert args == ("rediss://localhost:6379/0",)
        assert kwargs["decode_responses"] is True
        assert kwargs["ssl_ca_certs"] == "/path/to/ca.pem"

    @mock.patch("django_tasks_redis.utils.redis")
    def test_params_without_ca_certs(self, mock_redis):
        """Individual params config does not pass ssl_ca_certs when unset."""
        get_redis_client({"REDIS_HOST": "localhost"})
        _, kwargs = mock_redis.Redis.call_args
        assert "ssl_ca_certs" not in kwargs
        assert "ssl" not in kwargs

    @mock.patch("django_tasks_redis.utils.redis")
    def test_params_with_ca_certs(self, mock_redis):
        """Individual params config passes ssl_ca_certs when set."""
        get_redis_client(
            {
                "REDIS_HOST": "localhost",
                "REDIS_SSL": True,
                "REDIS_SSL_CA_CERTS": "/path/to/ca.pem",
            }
        )
        _, kwargs = mock_redis.Redis.call_args
        assert kwargs["ssl_ca_certs"] == "/path/to/ca.pem"
        assert kwargs["ssl"] is True

    @mock.patch("django_tasks_redis.utils.redis")
    def test_params_ca_certs_without_ssl_warns(self, mock_redis, caplog):
        """Warn when ssl_ca_certs is set but REDIS_SSL is not enabled."""
        with caplog.at_level("WARNING", logger="django_tasks_redis"):
            get_redis_client(
                {
                    "REDIS_HOST": "localhost",
                    "REDIS_SSL_CA_CERTS": "/path/to/ca.pem",
                }
            )
        assert "REDIS_SSL_CA_CERTS is set but REDIS_SSL is not enabled" in caplog.text

    @mock.patch("django_tasks_redis.utils.redis")
    def test_url_ca_certs_without_rediss_warns(self, mock_redis, caplog):
        """Warn when ssl_ca_certs is set but the URL is not rediss://."""
        with caplog.at_level("WARNING", logger="django_tasks_redis"):
            get_redis_client(
                {
                    "REDIS_URL": "redis://localhost:6379/0",
                    "REDIS_SSL_CA_CERTS": "/path/to/ca.pem",
                }
            )
        assert "is not a rediss:// URL" in caplog.text

    @mock.patch("django_tasks_redis.utils.redis")
    def test_url_rediss_with_ca_certs_no_warning(self, mock_redis, caplog):
        """No warning when ssl_ca_certs is set with a rediss:// URL."""
        with caplog.at_level("WARNING", logger="django_tasks_redis"):
            get_redis_client(
                {
                    "REDIS_URL": "rediss://localhost:6379/0",
                    "REDIS_SSL_CA_CERTS": "/path/to/ca.pem",
                }
            )
        assert caplog.text == ""


class TestConnectionOptions:
    """Tests for the connection settings passed to redis-py."""

    @mock.patch("django_tasks_redis.utils.redis")
    def test_url_passes_connection_options(self, mock_redis):
        """URL config passes the connection settings it is given."""
        get_redis_client(
            {
                "REDIS_URL": "redis://localhost:6379/0",
                "REDIS_SOCKET_TIMEOUT": 30,
                "REDIS_SOCKET_CONNECT_TIMEOUT": 5,
                "REDIS_SOCKET_KEEPALIVE": True,
                "REDIS_HEALTH_CHECK_INTERVAL": 30,
            }
        )
        _, kwargs = mock_redis.Redis.from_url.call_args
        assert kwargs["socket_timeout"] == 30
        assert kwargs["socket_connect_timeout"] == 5
        assert kwargs["socket_keepalive"] is True
        assert kwargs["health_check_interval"] == 30

    @mock.patch("django_tasks_redis.utils.redis")
    def test_params_pass_connection_options(self, mock_redis):
        """Individual params config passes the connection settings too."""
        get_redis_client(
            {
                "REDIS_HOST": "localhost",
                "REDIS_SOCKET_CONNECT_TIMEOUT": 5,
            }
        )
        _, kwargs = mock_redis.Redis.call_args
        assert kwargs["socket_connect_timeout"] == 5

    @mock.patch("django_tasks_redis.utils.redis")
    def test_socket_timeout_stays_unset(self, mock_redis):
        """It applies to blocking reads, so it cannot have a default."""
        get_redis_client({"REDIS_URL": "redis://localhost:6379/0"})
        _, kwargs = mock_redis.Redis.from_url.call_args
        assert "socket_timeout" not in kwargs

    @mock.patch("django_tasks_redis.utils.redis")
    def test_connection_is_bounded_by_default(self, mock_redis):
        """An unconfigured client still notices a connection that went away."""
        get_redis_client({"REDIS_URL": "redis://localhost:6379/0"})
        _, kwargs = mock_redis.Redis.from_url.call_args
        assert kwargs["socket_connect_timeout"] == 5
        assert kwargs["health_check_interval"] == 30

    @mock.patch("django_tasks_redis.utils.redis")
    def test_defaults_can_be_turned_off(self, mock_redis):
        """None drops the kwarg; health_check_interval=0 still disables the check."""
        get_redis_client(
            {
                "REDIS_URL": "redis://localhost:6379/0",
                "REDIS_SOCKET_CONNECT_TIMEOUT": None,
                "REDIS_HEALTH_CHECK_INTERVAL": 0,
            }
        )
        _, kwargs = mock_redis.Redis.from_url.call_args
        assert "socket_connect_timeout" not in kwargs
        assert kwargs["health_check_interval"] == 0


class TestPriorityToLevel:
    """Tests for priority to level conversion."""

    def test_priority_high(self):
        """Test positive priority returns 'high'."""
        assert priority_to_level(10) == "high"
        assert priority_to_level(1) == "high"
        assert priority_to_level(100) == "high"

    def test_priority_normal(self):
        """Test zero priority returns 'normal'."""
        assert priority_to_level(0) == "normal"

    def test_priority_low(self):
        """Test negative priority returns 'low'."""
        assert priority_to_level(-1) == "low"
        assert priority_to_level(-10) == "low"
        assert priority_to_level(-100) == "low"

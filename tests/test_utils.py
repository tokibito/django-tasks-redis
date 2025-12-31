"""
Tests for utils module.
"""

from datetime import UTC, datetime

from django_tasks_redis.utils import (
    deserialize_datetime,
    deserialize_json,
    get_delayed_key,
    get_priority_stream_key,
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

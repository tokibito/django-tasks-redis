"""
Utility functions for Redis connection and data handling.
"""

import json
from datetime import datetime
from typing import Any

import redis


def get_redis_client(options: dict) -> redis.Redis:
    """
    Create a Redis client from backend options.

    Supports both URL-based and individual parameter configuration.

    Args:
        options: Backend OPTIONS dict containing connection settings.

    Returns:
        redis.Redis: Configured Redis client instance.
    """
    if "REDIS_URL" in options:
        url = options["REDIS_URL"]
        # SSL is determined by the URL scheme (rediss:// for SSL)
        return redis.Redis.from_url(url, decode_responses=True)

    host = options.get("REDIS_HOST", "localhost")
    port = options.get("REDIS_PORT", 6379)
    db = options.get("REDIS_DB", 0)
    password = options.get("REDIS_PASSWORD")
    ssl = options.get("REDIS_SSL", False)

    return redis.Redis(
        host=host,
        port=port,
        db=db,
        password=password,
        ssl=ssl,
        decode_responses=True,
    )


def serialize_datetime(dt: datetime | None) -> str:
    """
    Serialize a datetime to ISO format string.

    Args:
        dt: Datetime object or None.

    Returns:
        ISO format string or empty string if None.
    """
    if dt is None:
        return ""
    return dt.isoformat()


def deserialize_datetime(value: str) -> datetime | None:
    """
    Deserialize an ISO format string to datetime.

    Args:
        value: ISO format string or empty string.

    Returns:
        Datetime object or None if empty.
    """
    if not value:
        return None
    return datetime.fromisoformat(value)


def serialize_json(value: Any) -> str:
    """
    Serialize a value to JSON string.

    Args:
        value: Any JSON-serializable value.

    Returns:
        JSON string.
    """
    return json.dumps(value)


def deserialize_json(value: str) -> Any:
    """
    Deserialize a JSON string to Python value.

    Args:
        value: JSON string.

    Returns:
        Python value.
    """
    if not value:
        return None
    return json.loads(value)


def get_stream_key(key_prefix: str, backend_name: str, queue_name: str) -> str:
    """
    Generate Redis Stream key for a queue.

    Args:
        key_prefix: Key prefix from settings.
        backend_name: Backend name.
        queue_name: Queue name.

    Returns:
        Stream key string.
    """
    return f"{key_prefix}:{backend_name}:{queue_name}:stream"


def get_priority_stream_key(
    key_prefix: str, backend_name: str, queue_name: str, priority_level: str
) -> str:
    """
    Generate Redis Stream key for a priority queue.

    Priority levels: high, normal, low

    Args:
        key_prefix: Key prefix from settings.
        backend_name: Backend name.
        queue_name: Queue name.
        priority_level: Priority level (high, normal, low).

    Returns:
        Stream key string.
    """
    return f"{key_prefix}:{backend_name}:{queue_name}:stream:{priority_level}"


def get_result_key(key_prefix: str, backend_name: str, task_id: str) -> str:
    """
    Generate Redis Hash key for task result.

    Args:
        key_prefix: Key prefix from settings.
        backend_name: Backend name.
        task_id: Task UUID string.

    Returns:
        Result key string.
    """
    return f"{key_prefix}:{backend_name}:result:{task_id}"


def get_delayed_key(key_prefix: str, backend_name: str, queue_name: str) -> str:
    """
    Generate Redis Sorted Set key for delayed tasks.

    Args:
        key_prefix: Key prefix from settings.
        backend_name: Backend name.
        queue_name: Queue name.

    Returns:
        Delayed tasks key string.
    """
    return f"{key_prefix}:{backend_name}:{queue_name}:delayed"


def get_results_index_key(key_prefix: str, backend_name: str) -> str:
    """
    Generate Redis Set key for tracking all result keys.

    Args:
        key_prefix: Key prefix from settings.
        backend_name: Backend name.

    Returns:
        Results index key string.
    """
    return f"{key_prefix}:{backend_name}:results_index"


def priority_to_level(priority: int) -> str:
    """
    Convert numeric priority to level string.

    Args:
        priority: Numeric priority value.

    Returns:
        Priority level: 'high' (priority > 0), 'normal' (priority == 0), 'low' (priority < 0).
    """
    if priority > 0:
        return "high"
    elif priority < 0:
        return "low"
    return "normal"

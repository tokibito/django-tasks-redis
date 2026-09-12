"""
Utility functions for Redis connection and data handling.
"""

import json
import logging
import socket
import uuid
from datetime import datetime
from typing import Any

import redis
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("django_tasks_redis")

# Without any of these, a half-open connection - a Redis restart, a network
# partition - blocks a worker for as long as the kernel allows. REDIS_SOCKET_TIMEOUT
# has no bound by default: it covers blocking reads too, so anything below
# REDIS_BLOCK_TIMEOUT makes every fetch raise.
CONNECTION_OPTIONS = {
    "REDIS_SOCKET_TIMEOUT": ("socket_timeout", None),
    "REDIS_SOCKET_CONNECT_TIMEOUT": ("socket_connect_timeout", 5),
    "REDIS_SOCKET_KEEPALIVE": ("socket_keepalive", None),
    "REDIS_HEALTH_CHECK_INTERVAL": ("health_check_interval", 30),
}

# Options passed to redis-py even when they are None. For the others None means
# "let redis-py decide", but redis-py 8 decides on a 5 second socket_timeout,
# the same length as the worker's default XREADGROUP block: the read is cut off
# the moment the block would have returned, and every idle wait raises.
UNBOUNDED_WHEN_NONE = frozenset({"REDIS_SOCKET_TIMEOUT"})


def generate_worker_id() -> str:
    """
    Generate a unique worker id.

    It doubles as the consumer name in the stream's consumer group, so the
    host name is in it for an operator reading XINFO CONSUMERS.
    """
    return f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


def get_connection_options(options: dict) -> dict:
    resolved = {}
    for option, (kwarg, default) in CONNECTION_OPTIONS.items():
        value = options.get(option, default)
        if value is not None or option in UNBOUNDED_WHEN_NONE:
            resolved[kwarg] = value
    return resolved


def get_redis_client(options: dict) -> redis.Redis:
    """
    Create a Redis client from backend options.

    Supports both URL-based and individual parameter configuration.

    Args:
        options: Backend OPTIONS dict containing connection settings.

    Returns:
        redis.Redis: Configured Redis client instance.
    """
    connection_kwargs = get_connection_options(options)

    # Add ssl_ca_certs only when REDIS_SSL_CA_CERTS is specified (self-signed CA).
    ssl_ca_certs = options.get("REDIS_SSL_CA_CERTS")
    if ssl_ca_certs:
        connection_kwargs["ssl_ca_certs"] = ssl_ca_certs

    if "REDIS_URL" in options:
        url = options["REDIS_URL"]
        # SSL is determined by the URL scheme (rediss:// for SSL)
        if ssl_ca_certs and not url.startswith("rediss://"):
            logger.warning(
                "REDIS_SSL_CA_CERTS is set but REDIS_URL is not a rediss:// URL; "
                "the CA certificate will be ignored. Use a rediss:// URL to enable SSL."
            )
        return redis.Redis.from_url(url, decode_responses=True, **connection_kwargs)

    host = options.get("REDIS_HOST", "localhost")
    port = options.get("REDIS_PORT", 6379)
    db = options.get("REDIS_DB", 0)
    password = options.get("REDIS_PASSWORD")
    ssl = options.get("REDIS_SSL", False)
    if ssl:
        connection_kwargs["ssl"] = ssl
    elif ssl_ca_certs:
        logger.warning(
            "REDIS_SSL_CA_CERTS is set but REDIS_SSL is not enabled; "
            "the CA certificate will be ignored. Set REDIS_SSL=True to enable SSL."
        )

    return redis.Redis(
        host=host,
        port=port,
        db=db,
        password=password,
        decode_responses=True,
        **connection_kwargs,
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

    The result is always comparable with timezone.now(): a value written under
    a different USE_TZ is converted rather than returned as is, so a setting
    change or a producer and a consumer that disagree cannot raise "can't
    compare offset-naive and offset-aware datetimes" deep in a worker.

    Args:
        value: ISO format string or empty string.

    Returns:
        Datetime object or None if empty.
    """
    if not value:
        return None

    parsed = datetime.fromisoformat(value)
    # Read and write a naive value in the current time zone, which is what
    # wrote it, so the instant survives the conversion either way.
    if settings.USE_TZ and timezone.is_naive(parsed):
        return timezone.make_aware(parsed)
    if not settings.USE_TZ and timezone.is_aware(parsed):
        return timezone.make_naive(parsed)
    return parsed


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

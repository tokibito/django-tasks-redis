"""
pytest configuration and fixtures for django-tasks-redis tests.
"""

import pytest
import redis as redis_lib

from django.tasks import task_backends


@pytest.fixture(scope="session")
def redis_connection():
    """Create a Redis connection for the test session."""
    client = redis_lib.Redis(
        host="localhost",
        port=6379,
        db=0,
        decode_responses=True,
    )
    yield client
    client.close()


@pytest.fixture
def redis_backend():
    """Get the configured Redis backend."""
    backend = task_backends["default"]
    # Reset cached client to get fresh connection
    backend._client = None
    return backend


@pytest.fixture
def redis_client(redis_backend):
    """Get a Redis client."""
    return redis_backend.get_client()


@pytest.fixture
def clean_redis(redis_connection, redis_backend):
    """Ensure Redis is clean before and after test."""
    # Clean before
    pattern = f"{redis_backend.key_prefix}:*"
    keys = redis_connection.keys(pattern)
    if keys:
        redis_connection.delete(*keys)

    yield

    # Clean after
    keys = redis_connection.keys(pattern)
    if keys:
        redis_connection.delete(*keys)

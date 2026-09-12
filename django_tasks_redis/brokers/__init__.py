"""
Brokers: the interface a worker consumes tasks through.

The shape is the one django-database-task gives its pull brokers, so a worker
loop written against one package reads the same against the other:

- receive() hands out messages, each naming a task.
- ack() tells the broker a message is dealt with.
- nack() gives a message back for another delivery.

Here the only broker is the Redis stream the tasks are queued on. Unlike the
brokers in django-database-task it has no notify() step: the backend writes
to the stream when it enqueues, so the stream is the queue rather than a
notification about one.
"""

from .base import BrokerMessage, PullBroker
from .streams import RedisStreamsBroker

__all__ = ["BrokerMessage", "PullBroker", "RedisStreamsBroker"]

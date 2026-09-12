"""
Base classes for brokers a worker polls for messages.

Mirrors ``django_database_task.brokers`` so the two packages present the same
consuming interface. Only the pull side is here: a task backend on Redis has
nothing to notify, since enqueueing writes to the stream directly.
"""


class BrokerMessage:
    """
    One message received from a pull broker.

    Attributes:
        task_id: Id of the task the message refers to.
        handle: Broker specific value the broker needs to acknowledge the
            message. For a Redis stream it is the ``(stream_key, message_id)``
            pair.
        raw: The original message, for logging and debugging.
    """

    def __init__(self, task_id, handle=None, raw=None):
        self.task_id = task_id
        self.handle = handle
        self.raw = raw

    def __repr__(self):
        return f"<{type(self).__name__} task_id={self.task_id!r}>"


class PullBroker:
    """
    Base class for brokers a worker polls for messages.

    A worker calls receive() in a loop, executes each task, then ack()s the
    message. A message that is never acknowledged is delivered again, so
    the worker can crash without losing the task.

    Args:
        backend: The task backend the broker belongs to.
        options: The backend's OPTIONS dict.
    """

    def __init__(self, backend, options=None):
        self.backend = backend
        self.options = options or {}

    def receive(self, queue_name=None, max_messages=1, wait_seconds=0):
        """
        Wait for messages and return them.

        Args:
            queue_name: Queue to read from, or None for every queue the
                backend serves.
            max_messages: How many messages to return at most.
            wait_seconds: How long to wait for a message before giving up.
                Zero polls without waiting.

        Returns:
            list of BrokerMessage
        """
        raise NotImplementedError(f"{type(self).__name__} must implement receive().")

    def ack(self, message):
        """Tell the broker the message is dealt with, so it is not resent."""
        raise NotImplementedError(f"{type(self).__name__} must implement ack().")

    def nack(self, message, delay=None):
        """
        Give the message back to the broker so it is delivered again.

        Args:
            message: The message to return.
            delay: Seconds to wait before the message becomes visible
                again, if the broker supports it.
        """

    def close(self):
        """Release whatever the broker holds open. Called by workers."""

"""
Tests for --empty-exit-code / --failed-exit-code on run_redis_tasks.

The exit code the command leaves behind is the signal a cron / JP1 /
systemd timer reads, so this is mostly a behavioural contract: the
right code per outcome, and nothing surprising when nothing was asked
for.

A Redis error during XREADGROUP is the case worth pinning: it is an
infrastructure fault (the connection died, NOGROUP, the network
blipped) and must not be counted as a task failure.
"""

import argparse
import logging
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command

from django_tasks_redis.management.commands.run_redis_tasks import (
    Command,
    _exit_code_argument,
)


class TestExitCodeArgument:
    """The argparse type used by both options."""

    def test_accepts_zero(self):
        assert _exit_code_argument("0") == 0

    def test_accepts_the_max(self):
        assert _exit_code_argument("255") == 255

    def test_rejects_a_negative_code(self):
        with pytest.raises(argparse.ArgumentTypeError, match="between 0 and 255"):
            _exit_code_argument("-1")

    def test_rejects_an_oversized_code(self):
        with pytest.raises(argparse.ArgumentTypeError, match="between 0 and 255"):
            _exit_code_argument("256")

    def test_rejects_a_non_integer(self):
        with pytest.raises(argparse.ArgumentTypeError, match="whole numbers"):
            _exit_code_argument("not-a-number")


class TestExitCodeSelection:
    """The helper that picks which code the command exits with."""

    def test_zero_when_neither_option_is_set(self):
        command = Command()
        command.tasks_failed = 0
        assert (
            command._exit_code(tasks_processed=0, empty_exit_code=0, failed_exit_code=0)
            == 0
        )

    def test_zero_after_a_successful_run_when_neither_option_is_set(self):
        command = Command()
        command.tasks_failed = 0
        assert (
            command._exit_code(tasks_processed=5, empty_exit_code=0, failed_exit_code=0)
            == 0
        )

    def test_empty_code_when_nothing_ran(self):
        command = Command()
        command.tasks_failed = 0
        assert (
            command._exit_code(tasks_processed=0, empty_exit_code=4, failed_exit_code=0)
            == 4
        )

    def test_zero_after_a_run_when_only_empty_code_is_set(self):
        command = Command()
        command.tasks_failed = 0
        assert (
            command._exit_code(tasks_processed=3, empty_exit_code=4, failed_exit_code=0)
            == 0
        )

    def test_failed_code_when_a_task_failed(self):
        command = Command()
        command.tasks_failed = 1
        assert (
            command._exit_code(tasks_processed=1, empty_exit_code=4, failed_exit_code=1)
            == 1
        )

    def test_failed_code_wins_when_both_conditions_hold(self):
        command = Command()
        # Nothing was processed (could not run it at all) AND a failure was
        # counted — the failure wins.
        command.tasks_failed = 1
        assert (
            command._exit_code(tasks_processed=0, empty_exit_code=4, failed_exit_code=1)
            == 1
        )

    def test_zero_when_failure_code_is_unset_even_if_a_task_failed(self):
        command = Command()
        command.tasks_failed = 1
        # Default behaviour is preserved: a failure that the user did not
        # opt into leaves the exit code alone.
        assert (
            command._exit_code(tasks_processed=1, empty_exit_code=0, failed_exit_code=0)
            == 0
        )


@pytest.mark.django_db
class TestCommandExitCode:
    """End-to-end: drive call_command() and observe the SystemExit."""

    def test_default_options_exit_zero_when_idle(self, clean_redis):
        # sys.exit(0) returns normally rather than raising SystemExit, so
        # the assertion is that no exception escapes the command.
        call_command("run_redis_tasks", stdout=StringIO())

    def test_default_options_exit_zero_when_a_task_succeeds(self, clean_redis):
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)

        # Same as above: a 0 exit code is not an exception.
        call_command("run_redis_tasks", stdout=StringIO())

    def test_empty_exit_code_fires_when_nothing_ran(self, clean_redis):
        with pytest.raises(SystemExit) as excinfo:
            call_command(
                "run_redis_tasks",
                empty_exit_code=4,
                stdout=StringIO(),
            )

        assert excinfo.value.code == 4

    def test_empty_exit_code_is_suppressed_when_a_task_ran(self, clean_redis):
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)

        # A task ran, so the empty exit code is suppressed: exit 0,
        # which means no SystemExit.
        call_command(
            "run_redis_tasks",
            empty_exit_code=4,
            stdout=StringIO(),
        )

    def test_failed_exit_code_fires_when_a_task_failed(self, clean_redis):
        from tests.tasks import failing_task

        failing_task.enqueue()

        with pytest.raises(SystemExit) as excinfo:
            call_command(
                "run_redis_tasks",
                failed_exit_code=1,
                stdout=StringIO(),
            )

        assert excinfo.value.code == 1

    def test_failed_exit_code_takes_precedence_over_empty(self, clean_redis, caplog):
        # The real shape of "both conditions hold": the broker message
        # named a task the worker could not run at all (its code no longer
        # imports, say), so nothing was processed AND a failure was
        # recorded — failed wins.
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)

        with caplog.at_level(logging.INFO, logger="django_tasks_redis"):
            with mock.patch(
                "django_tasks_redis.backends.RedisTaskBackend.run_task",
                side_effect=ImportError("task module is gone"),
            ):
                with pytest.raises(SystemExit) as excinfo:
                    call_command(
                        "run_redis_tasks",
                        empty_exit_code=4,
                        failed_exit_code=1,
                        stdout=StringIO(),
                    )

        assert excinfo.value.code == 1

        finished = [
            r for r in caplog.records if r.getMessage().startswith("Worker finished")
        ]
        assert len(finished) == 1
        assert finished[0].tasks_processed == 0
        assert finished[0].tasks_failed == 1

    def test_tasks_failed_is_written_after_the_worker_stopped_line(self, clean_redis):
        from tests.tasks import failing_task

        failing_task.enqueue()

        out = StringIO()
        with pytest.raises(SystemExit):
            call_command("run_redis_tasks", failed_exit_code=1, stdout=out)

        output = out.getvalue()
        assert "Tasks failed: 1" in output
        assert "Worker stopped. Processed 1 task(s)." in output
        assert output.index("Tasks failed: 1") > output.index(
            "Worker stopped. Processed 1 task(s)."
        )


@pytest.mark.django_db
class TestInfrastructureErrorsAreNotCounted:
    """A fetch that raises is logged at ERROR but does not become a task failure."""

    def test_broker_receive_failure_does_not_set_a_failed_exit_code(self, clean_redis):
        # The connection error is an infrastructure fault, not a task
        # outcome, so the failed exit code does not fire: exit 0,
        # no SystemExit. The worker still logs the failure at ERROR (an
        # operator needs to see it), which is covered in test_logging.py
        # — asserting on it here is brittle when earlier tests redirect
        # stdout through call_command (test_reliability.py does).
        with mock.patch(
            "django_tasks_redis.brokers.streams.RedisStreamsBroker.receive",
            side_effect=ConnectionError("redis went away"),
        ):
            call_command(
                "run_redis_tasks",
                failed_exit_code=1,
                stdout=StringIO(),
            )


@pytest.mark.django_db
class TestWorkerFinishedExitCode:
    """The Worker finished log record carries the actual exit code."""

    def test_logged_exit_code_matches_what_the_process_exits_with(
        self, clean_redis, caplog
    ):
        from tests.tasks import simple_task

        simple_task.enqueue(1, 2)

        with caplog.at_level(logging.INFO, logger="django_tasks_redis"):
            # A task ran, so the empty code is suppressed: exit 0,
            # no SystemExit raised.
            call_command(
                "run_redis_tasks",
                empty_exit_code=4,
                stdout=StringIO(),
            )

        finished = [
            r for r in caplog.records if r.getMessage().startswith("Worker finished")
        ]
        assert len(finished) == 1
        # The idle exit code is not what the process exited with (a task ran,
        # so the empty code was suppressed) — the field matches the real one.
        assert finished[0].exit_code == 0
        assert finished[0].tasks_processed == 1
        assert finished[0].tasks_failed == 0

    def test_logged_exit_code_is_the_idle_code_when_nothing_ran(
        self, clean_redis, caplog
    ):
        with caplog.at_level(logging.INFO, logger="django_tasks_redis"):
            with pytest.raises(SystemExit) as excinfo:
                call_command(
                    "run_redis_tasks",
                    empty_exit_code=4,
                    stdout=StringIO(),
                )

        assert excinfo.value.code == 4

        finished = [
            r for r in caplog.records if r.getMessage().startswith("Worker finished")
        ]
        assert len(finished) == 1
        assert finished[0].exit_code == 4
        assert finished[0].tasks_processed == 0
        assert finished[0].tasks_failed == 0

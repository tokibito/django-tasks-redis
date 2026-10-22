"""
The ``--workers`` option over real workers, against Redis.

test_supervisor.py drives the supervisor over stub processes and
test_commands.py checks what the command hands it. Here the workers are
the command itself, started as ``python -m django run_redis_tasks
--workers 2`` against the test Redis, where the consumer group hands each
message to one consumer, so two workers can run two tasks at once.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from django.tasks.base import TaskResultStatus

from django_tasks_redis import executor

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.django_db
class TestWorkers:
    def start_command(self, *args):
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "tests.settings",
            # The output is read after the command exits, but a crash
            # before then must not lose what was written so far.
            "PYTHONUNBUFFERED": "1",
        }
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(
            [sys.executable, "-m", "django", "run_redis_tasks", *args],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )

    def finish(self, process, timeout=60):
        try:
            output, _ = process.communicate(timeout=timeout)
        except BaseException:
            process.kill()
            process.communicate()
            raise
        return output

    def wait_until(self, process, condition, timeout=60, what="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if condition():
                return
            if process.poll() is not None:
                output, _ = process.communicate()
                pytest.fail(
                    f"The command exited with {process.returncode} early:\n{output}"
                )
            time.sleep(0.05)
        pytest.fail(f"{what} not met within {timeout} seconds")

    def statuses(self, task_ids):
        return sorted(
            executor.get_task_by_id(task_id)["status"] for task_id in task_ids
        )

    def test_two_workers_run_two_tasks_at_once_and_stop_on_the_signal(
        self, clean_redis
    ):
        from tests.tasks import slow_task

        task_ids = [slow_task.enqueue(seconds=3).id for _ in range(2)]
        if sys.platform == "win32":
            sig, expected_name = signal.CTRL_BREAK_EVENT, "SIGBREAK"
        else:
            sig, expected_name = signal.SIGTERM, "SIGTERM"

        process = self.start_command(
            "--continuous", "--interval", "0.2", "--workers", "2"
        )
        try:
            self.wait_until(
                process,
                lambda: self.statuses(task_ids) == [TaskResultStatus.RUNNING] * 2,
                what="both tasks running at once",
            )
            process.send_signal(sig)
        except BaseException:
            process.kill()
            process.communicate()
            raise
        output = self.finish(process)

        assert process.returncode == 0, output
        assert self.statuses(task_ids) == [TaskResultStatus.SUCCESSFUL] * 2
        assert f"Received {expected_name}: stopping 2 worker(s)" in output
        assert output.count("Worker stopped. Processed 1 task(s).") == 2
        assert "Shutdown complete: every worker exited." in output

    def test_two_workers_drain_the_queue_and_exit(self, clean_redis):
        from tests.tasks import slow_task

        task_ids = [slow_task.enqueue(seconds=1).id for _ in range(4)]

        process = self.start_command("--workers", "2", "--empty-exit-code", "4")
        output = self.finish(process)

        assert process.returncode == 0, output
        assert self.statuses(task_ids) == [TaskResultStatus.SUCCESSFUL] * 4
        assert output.count("Starting Redis task worker:") == 2
        assert output.count("No tasks available, exiting") == 2
        assert "Every worker has exited." in output

    def test_an_idle_run_reports_the_empty_exit_code(self, clean_redis):
        process = self.start_command("--workers", "2", "--empty-exit-code", "4")
        output = self.finish(process)

        assert process.returncode == 4, output
        assert output.count("Worker stopped. Processed 0 task(s).") == 2

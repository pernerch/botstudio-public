from contextlib import contextmanager
from contextvars import ContextVar
import subprocess
from time import monotonic


_cancellation = ContextVar("command_cancellation", default=None)


class CommandCancelled(RuntimeError):
    pass


@contextmanager
def command_control(checker):
    token = _cancellation.set(checker)
    try:
        yield
    finally:
        _cancellation.reset(token)


def check_cancelled():
    checker = _cancellation.get()
    if checker is not None and checker():
        raise CommandCancelled("cancelled")


def run_command(command, *, timeout, check=False, capture_output=False, **kwargs):
    if _cancellation.get() is None:
        return subprocess.run(
            command, timeout=timeout, check=check,
            capture_output=capture_output, **kwargs,
        )
    check_cancelled()
    if capture_output:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    with subprocess.Popen(command, **kwargs) as process:
        deadline = monotonic() + timeout
        try:
            while True:
                control_started = monotonic()
                check_cancelled()
                # Pause gates may block; paused time must not consume the command timeout.
                deadline += monotonic() - control_started
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            process.kill()
            process.communicate(timeout=1)
            raise
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if check:
            result.check_returncode()
        return result

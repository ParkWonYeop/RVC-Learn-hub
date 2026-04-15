"""Subprocess execution without a shell, with cancellation and bounded shutdown."""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import signal
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .workspace import ensure_within


class ProcessRunnerError(RuntimeError):
    """Base class for isolated subprocess failures."""


class ProcessCancelled(ProcessRunnerError):
    """Raised after a subprocess is terminated for cooperative cancellation."""


class ProcessTimedOut(ProcessRunnerError):
    """Raised after a subprocess exceeds its configured timeout."""


class ProcessFailed(ProcessRunnerError):
    def __init__(self, argv: Sequence[str], returncode: int) -> None:
        super().__init__(f"process exited with code {returncode}: {argv[0]}")
        self.argv = tuple(argv)
        self.returncode = returncode


OutputCallback = Callable[[str, bytes], Awaitable[None] | None]


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    argv: tuple[str, ...]
    cwd: Path
    workspace_root: Path
    stdout_path: Path
    stderr_path: Path
    env: Mapping[str, str] | None = None
    timeout_seconds: float | None = None
    terminate_grace_seconds: float = 10.0
    check: bool = True


@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout_path: Path
    stderr_path: Path


class SafeSubprocessRunner:
    async def run(
        self,
        spec: ProcessSpec,
        cancellation: asyncio.Event,
        *,
        output_callback: OutputCallback | None = None,
    ) -> ProcessResult:
        _validate_spec(spec)
        if cancellation.is_set():
            raise ProcessCancelled("process was cancelled before start")

        spec.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        spec.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        environment = safe_environment(spec.env)

        with spec.stdout_path.open("ab") as stdout_file, spec.stderr_path.open("ab") as stderr_file:
            try:
                process = await asyncio.create_subprocess_exec(
                    *spec.argv,
                    cwd=str(spec.cwd),
                    env=environment,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
            except OSError as exc:
                raise ProcessRunnerError(f"cannot start executable: {spec.argv[0]}") from exc

            assert process.stdout is not None
            assert process.stderr is not None
            stdout_task = asyncio.create_task(
                _pump(process.stdout, stdout_file, "stdout", output_callback)
            )
            stderr_task = asyncio.create_task(
                _pump(process.stderr, stderr_file, "stderr", output_callback)
            )
            process_task = asyncio.create_task(process.wait())
            cancellation_task = asyncio.create_task(cancellation.wait())
            timeout_task = (
                asyncio.create_task(asyncio.sleep(spec.timeout_seconds))
                if spec.timeout_seconds is not None
                else None
            )
            reason: str | None = None
            output_failure: BaseException | None = None
            try:
                watched: set[asyncio.Task[Any]] = {
                    process_task,
                    cancellation_task,
                    stdout_task,
                    stderr_task,
                }
                if timeout_task is not None:
                    watched.add(timeout_task)
                while True:
                    done, _ = await asyncio.wait(
                        watched,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if cancellation_task in done:
                        reason = "cancelled"
                        await _terminate_process_group(process, spec.terminate_grace_seconds)
                        break
                    failed_pump = next(
                        (
                            task
                            for task in (stdout_task, stderr_task)
                            if task in done
                            and not task.cancelled()
                            and task.exception() is not None
                        ),
                        None,
                    )
                    if failed_pump is not None:
                        output_failure = failed_pump.exception()
                        reason = "output_callback"
                        await _terminate_process_group(process, spec.terminate_grace_seconds)
                        break
                    if process_task in done:
                        break
                    if timeout_task is not None and timeout_task in done:
                        reason = "timeout"
                        await _terminate_process_group(process, spec.terminate_grace_seconds)
                        break
                    # A pipe can reach EOF immediately before process.wait().
                    # Stop watching that normally completed pump and continue.
                    watched.difference_update(
                        task
                        for task in (stdout_task, stderr_task)
                        if task in done and task.exception() is None
                    )
                returncode = await process_task
            except asyncio.CancelledError:
                await _terminate_process_group(process, spec.terminate_grace_seconds)
                await asyncio.gather(process_task, return_exceptions=True)
                raise
            finally:
                cancellation_task.cancel()
                if timeout_task is not None:
                    timeout_task.cancel()
                await asyncio.gather(
                    cancellation_task,
                    *(() if timeout_task is None else (timeout_task,)),
                    return_exceptions=True,
                )
                pump_results = await asyncio.gather(
                    stdout_task,
                    stderr_task,
                    return_exceptions=True,
                )
                if output_failure is None:
                    output_failure = next(
                        (
                            result
                            for result in pump_results
                            if isinstance(result, BaseException)
                            and not isinstance(result, asyncio.CancelledError)
                        ),
                        None,
                    )

        result = ProcessResult(spec.argv, returncode, spec.stdout_path, spec.stderr_path)
        if reason == "cancelled":
            raise ProcessCancelled(f"process cancelled: {spec.argv[0]}")
        if output_failure is not None:
            raise output_failure
        if reason == "timeout":
            raise ProcessTimedOut(f"process timed out: {spec.argv[0]}")
        if spec.check and returncode != 0:
            raise ProcessFailed(spec.argv, returncode)
        return result


async def _pump(
    stream: asyncio.StreamReader,
    destination: Any,
    channel: str,
    callback: OutputCallback | None,
) -> None:
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            break
        destination.write(chunk)
        destination.flush()
        if callback is not None:
            response = callback(channel, chunk)
            if inspect.isawaitable(response):
                await response


async def _terminate_process_group(
    process: asyncio.subprocess.Process, grace_seconds: float
) -> None:
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            process.terminate()
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            process.kill()
        except ProcessLookupError:
            return
    await process.wait()


_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "AUTHORIZATION", "CREDENTIAL")


def safe_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
    }
    for key, value in (extra or {}).items():
        if not _ENV_KEY.fullmatch(key):
            raise ProcessRunnerError(f"invalid environment key: {key!r}")
        if any(marker in key.upper() for marker in _SECRET_MARKERS):
            raise ProcessRunnerError(
                f"secret environment key is not allowed in RVC subprocess: {key}"
            )
        if "\x00" in value:
            raise ProcessRunnerError(f"environment value for {key} contains NUL")
        environment[key] = value
    return environment


def _validate_spec(spec: ProcessSpec) -> None:
    if not spec.argv or not spec.argv[0]:
        raise ProcessRunnerError("argv must contain an executable")
    if any(not isinstance(arg, str) or "\x00" in arg for arg in spec.argv):
        raise ProcessRunnerError("argv entries must be NUL-free strings")
    ensure_within(spec.cwd, spec.workspace_root)
    ensure_within(spec.stdout_path, spec.workspace_root)
    ensure_within(spec.stderr_path, spec.workspace_root)
    if not spec.cwd.is_dir():
        raise ProcessRunnerError(f"working directory does not exist: {spec.cwd}")
    if spec.timeout_seconds is not None and spec.timeout_seconds <= 0:
        raise ProcessRunnerError("timeout_seconds must be greater than zero")
    if spec.terminate_grace_seconds <= 0:
        raise ProcessRunnerError("terminate_grace_seconds must be greater than zero")

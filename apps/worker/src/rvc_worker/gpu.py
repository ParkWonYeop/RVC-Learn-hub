"""Safe, bounded GPU telemetry collection through nvidia-smi."""

from __future__ import annotations

import math
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GpuSnapshot:
    index: int
    uuid: str | None
    name: str
    memory_total_mb: int
    memory_used_mb: int
    utilization_percent: float
    temperature_celsius: float


@dataclass(frozen=True, slots=True)
class GpuCollection:
    """One bounded GPU inventory observation.

    ``available`` means that the telemetry query itself completed and returned
    semantically valid output.  A successful empty query is therefore
    distinguishable from an unavailable or malformed ``nvidia-smi`` result.
    """

    gpus: tuple[GpuSnapshot, ...]
    available: bool
    error: str | None = None


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class NvidiaSmiCollector:
    QUERY_FIELDS = (
        "index",
        "uuid",
        "name",
        "memory.total",
        "memory.used",
        "utilization.gpu",
        "temperature.gpu",
    )

    def __init__(
        self,
        *,
        executable: Path | None = None,
        timeout_seconds: float = 5.0,
        run_command: RunCommand = subprocess.run,
    ) -> None:
        discovered = shutil.which("nvidia-smi") if executable is None else str(executable)
        self.executable = Path(discovered).resolve() if discovered else None
        self.timeout_seconds = timeout_seconds
        self._run_command = run_command

    def collect(self) -> GpuCollection:
        if self.executable is None:
            return GpuCollection((), False, "nvidia-smi is not installed")
        argv: Sequence[str] = (
            str(self.executable),
            f"--query-gpu={','.join(self.QUERY_FIELDS)}",
            "--format=csv,noheader,nounits",
        )
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LANG": "C",
            "LC_ALL": "C",
        }
        try:
            result = self._run_command(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return GpuCollection((), False, f"nvidia-smi failed: {type(exc).__name__}")
        if result.returncode != 0:
            return GpuCollection((), False, f"nvidia-smi exited with code {result.returncode}")
        try:
            lines = tuple(line for line in result.stdout.splitlines() if line.strip())
            if len(lines) > 64:
                raise ValueError("GPU inventory exceeds 64 entries")
            snapshots = tuple(_parse_line(line) for line in lines)
            _validate_inventory(snapshots)
        except ValueError as exc:
            return GpuCollection((), False, f"invalid nvidia-smi output: {exc}")
        return GpuCollection(snapshots, True)


def _parse_line(line: str) -> GpuSnapshot:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) != 7:
        raise ValueError("expected seven CSV fields")
    try:
        index = int(parts[0])
        memory_total_mb = _integral_metric(parts[3])
        memory_used_mb = _integral_metric(parts[4])
        utilization_percent = float(parts[5])
        temperature_celsius = float(parts[6])
    except (OverflowError, ValueError) as exc:
        raise ValueError("GPU metrics contain a non-numeric value") from exc

    uuid = parts[1] if parts[1] not in {"", "N/A"} else None
    name = parts[2]
    if not 0 <= index <= 1_023:
        raise ValueError("GPU index is outside the supported range")
    if not name or len(name) > 256:
        raise ValueError("GPU name is empty or too long")
    if uuid is not None and len(uuid) > 256:
        raise ValueError("GPU UUID is too long")
    if memory_total_mb <= 0:
        raise ValueError("GPU total memory must be positive")
    if memory_used_mb < 0 or memory_used_mb > memory_total_mb:
        raise ValueError("GPU used memory is outside the supported range")
    if not math.isfinite(utilization_percent) or not 0 <= utilization_percent <= 100:
        raise ValueError("GPU utilization is outside the supported range")
    if not math.isfinite(temperature_celsius) or not -273.15 <= temperature_celsius <= 1_000:
        raise ValueError("GPU temperature is outside the supported range")
    return GpuSnapshot(
        index=index,
        uuid=uuid,
        name=name,
        memory_total_mb=memory_total_mb,
        memory_used_mb=memory_used_mb,
        utilization_percent=utilization_percent,
        temperature_celsius=temperature_celsius,
    )


def _integral_metric(value: str) -> int:
    parsed = float(value)
    if not math.isfinite(parsed) or not parsed.is_integer():
        raise ValueError("GPU memory metric is not a finite integer")
    return int(parsed)


def _validate_inventory(snapshots: tuple[GpuSnapshot, ...]) -> None:
    indices = [snapshot.index for snapshot in snapshots]
    if len(set(indices)) != len(indices):
        raise ValueError("GPU indexes are not unique")
    uuids = [snapshot.uuid for snapshot in snapshots if snapshot.uuid is not None]
    if len(set(uuids)) != len(uuids):
        raise ValueError("GPU UUIDs are not unique")

"""RVC train-log and TensorBoard scalar normalization."""

from __future__ import annotations

import importlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class MetricParserError(RuntimeError):
    """Raised when optional metric input cannot be read safely."""


@dataclass(frozen=True, slots=True)
class ParsedTrainingMetric:
    key: str
    value: float
    epoch: int | None = None
    step: int | None = None
    source: str = "train_log"


_TRAIN_EPOCH = re.compile(r"Train Epoch:\s*(?P<epoch>[0-9]+)\s*\[(?P<percent>[0-9.]+)%\]")
_EPOCH_DONE = re.compile(r"====>\s*Epoch:\s*(?P<epoch>[0-9]+)")
_STEP_AND_LR = re.compile(
    r"\[\s*(?P<step>[0-9]+)\s*,\s*(?P<lr>[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:e[+-]?[0-9]+)?)\s*\]",
    re.IGNORECASE,
)
_LOSSES = re.compile(
    r"loss_disc=(?P<loss_disc>[+-]?[0-9.]+),\s*"
    r"loss_gen=(?P<loss_gen>[+-]?[0-9.]+),\s*"
    r"loss_fm=(?P<loss_fm>[+-]?[0-9.]+),\s*"
    r"loss_mel=(?P<loss_mel>[+-]?[0-9.]+),\s*"
    r"loss_kl=(?P<loss_kl>[+-]?[0-9.]+)"
)

_TENSORBOARD_TAGS = {
    "loss/g/total": "loss_g_total",
    "loss/d/total": "loss_d_total",
    "loss/g/fm": "loss_fm",
    "loss/g/mel": "loss_mel",
    "loss/g/kl": "loss_kl",
    "learning_rate": "learning_rate",
    "grad_norm_g": "grad_norm_g",
    "grad_norm_d": "grad_norm_d",
}


class TrainingLogParser:
    """Stateful parser that associates loss lines with their preceding epoch/step."""

    def __init__(self) -> None:
        self.current_epoch: int | None = None
        self.current_step: int | None = None

    def feed(self, line: str) -> tuple[ParsedTrainingMetric, ...]:
        epoch_match = _TRAIN_EPOCH.search(line)
        if epoch_match:
            self.current_epoch = int(epoch_match.group("epoch"))
            return (
                self._metric("current_epoch", float(self.current_epoch)),
                self._metric("epoch_progress_percent", float(epoch_match.group("percent"))),
            )

        step_match = _STEP_AND_LR.search(line)
        if step_match:
            self.current_step = int(step_match.group("step"))
            return (
                self._metric("step", float(self.current_step)),
                self._metric("learning_rate", float(step_match.group("lr"))),
            )

        loss_match = _LOSSES.search(line)
        if loss_match:
            values = {key: float(value) for key, value in loss_match.groupdict().items()}
            total = values["loss_gen"] + values["loss_fm"] + values["loss_mel"] + values["loss_kl"]
            return (
                self._metric("loss_d_total", values["loss_disc"]),
                self._metric("loss_g_adversarial", values["loss_gen"]),
                self._metric("loss_fm", values["loss_fm"]),
                self._metric("loss_mel", values["loss_mel"]),
                self._metric("loss_kl", values["loss_kl"]),
                self._metric("loss_g_total", total),
            )

        completed_match = _EPOCH_DONE.search(line)
        if completed_match:
            self.current_epoch = int(completed_match.group("epoch"))
            return (self._metric("epoch_completed", float(self.current_epoch)),)
        return ()

    def _metric(self, key: str, value: float) -> ParsedTrainingMetric:
        if not math.isfinite(value):
            raise MetricParserError(f"non-finite RVC metric rejected: {key}")
        return ParsedTrainingMetric(
            key=key,
            value=value,
            epoch=self.current_epoch,
            step=self.current_step,
        )


def parse_training_log(lines: list[str] | tuple[str, ...]) -> tuple[ParsedTrainingMetric, ...]:
    parser = TrainingLogParser()
    return tuple(metric for line in lines for metric in parser.feed(line))


def normalize_tensorboard_scalar(tag: str, value: float, step: int) -> ParsedTrainingMetric | None:
    """Map only the reviewed RVC scalar tags into the central metric vocabulary."""

    key = _TENSORBOARD_TAGS.get(tag)
    if key is None:
        return None
    if step < 0 or not math.isfinite(value):
        raise MetricParserError("TensorBoard scalar contains an invalid step or value")
    return ParsedTrainingMetric(key=key, value=value, step=step, source="tensorboard")


def read_tensorboard_scalars(
    log_directory: Path,
    *,
    after_step: int = -1,
    max_records: int = 10_000,
) -> tuple[ParsedTrainingMetric, ...]:
    """Read supported scalar tags lazily from an RVC TensorBoard event directory.

    TensorBoard is supplied by the pinned RVC runtime rather than the lightweight
    control-plane Worker package.  A clear error is returned when it is absent.
    """

    if not log_directory.is_dir() or log_directory.is_symlink():
        raise MetricParserError("TensorBoard log directory is missing or unsafe")
    if isinstance(max_records, bool) or not 1 <= max_records <= 1_000_000:
        raise MetricParserError("TensorBoard scalar record limit is invalid")
    try:
        module = importlib.import_module("tensorboard.backend.event_processing.event_accumulator")
    except ImportError as exc:
        raise MetricParserError("TensorBoard parser is not installed in the RVC runtime") from exc

    accumulator_class: Any = module.EventAccumulator
    accumulator: Any = accumulator_class(
        str(log_directory),
        size_guidance={"scalars": max_records},
    )
    try:
        accumulator.Reload()
        available_tags = set(accumulator.Tags().get("scalars", []))
        metrics: list[ParsedTrainingMetric] = []
        for tag in sorted(_TENSORBOARD_TAGS):
            if tag not in available_tags:
                continue
            for event in accumulator.Scalars(tag):
                step = int(event.step)
                if step <= after_step:
                    continue
                normalized = normalize_tensorboard_scalar(tag, float(event.value), step)
                if normalized is not None:
                    if len(metrics) >= max_records:
                        raise MetricParserError("TensorBoard scalar record limit was exceeded")
                    metrics.append(normalized)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise MetricParserError("cannot parse TensorBoard event data") from exc
    return tuple(sorted(metrics, key=lambda item: (item.step or 0, item.key)))

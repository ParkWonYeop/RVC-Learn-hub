"""Pure RVC version-aware pretrained path resolution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rvc_orchestrator_contracts import RVCVersion, SampleRate

from .workspace import ensure_within


class PretrainedResolutionError(ValueError):
    """Raised for an unsupported or unsafe pretrained combination."""


@dataclass(frozen=True, slots=True)
class PretrainedPair:
    generator: Path
    discriminator: Path


def feature_directory(version: RVCVersion | str) -> str:
    value = _enum_value(version)
    if value == "v1":
        return "3_feature256"
    if value == "v2":
        return "3_feature768"
    raise PretrainedResolutionError(f"unsupported RVC version: {value}")


def resolve_pretrained(
    repository_root: Path,
    version: RVCVersion | str,
    sample_rate: SampleRate | str,
    use_f0: bool,
    *,
    require_files: bool = False,
) -> PretrainedPair:
    root = repository_root.expanduser().resolve()
    version_value = _enum_value(version)
    rate_value = _enum_value(sample_rate)
    if version_value == "v1":
        pretrained_root = root / "assets" / "pretrained"
    elif version_value == "v2":
        pretrained_root = root / "assets" / "pretrained_v2"
    else:
        raise PretrainedResolutionError(f"unsupported RVC version: {version_value}")
    if rate_value not in {"40k", "48k"}:
        raise PretrainedResolutionError(f"unsupported sample rate: {rate_value}")
    prefix = "f0" if use_f0 else ""
    pair = PretrainedPair(
        generator=ensure_within(pretrained_root / f"{prefix}G{rate_value}.pth", root),
        discriminator=ensure_within(pretrained_root / f"{prefix}D{rate_value}.pth", root),
    )
    if require_files:
        missing = [str(path) for path in (pair.generator, pair.discriminator) if not path.is_file()]
        if missing:
            raise PretrainedResolutionError(f"pretrained files are missing: {', '.join(missing)}")
    return pair


def validate_custom_pretrained(
    allowed_root: Path, generator: Path, discriminator: Path
) -> PretrainedPair:
    root = allowed_root.expanduser().resolve()
    pair = PretrainedPair(
        ensure_within(generator.expanduser(), root),
        ensure_within(discriminator.expanduser(), root),
    )
    if not pair.generator.is_file() or not pair.discriminator.is_file():
        raise PretrainedResolutionError("custom pretrained G/D files must both exist")
    return pair


def _enum_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw).lower()

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import wave
from dataclasses import dataclass
from pathlib import Path

from rvc_orchestrator_contracts import InferencePresetConfig

from ..config import Settings
from ..models import Preset, TestSet, TestSetItem
from ..schemas import (
    PresetRead,
    TestSetItemRead,
    TestSetItemUploadInitRequest,
    TestSetRead,
)


class InvalidTestSetWav(ValueError):
    def __init__(self, failure_code: str) -> None:
        super().__init__("test set item is not a supported bounded PCM WAV")
        self.failure_code = failure_code


@dataclass(frozen=True, slots=True)
class WavInspection:
    sample_rate_hz: int
    channels: int
    duration_seconds: float


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def preset_document(config: InferencePresetConfig) -> dict[str, object]:
    return config.model_dump(mode="json")


def preset_to_read(preset: Preset) -> PresetRead:
    return PresetRead(
        id=preset.id,
        family_id=preset.family_id,
        name=preset.name,
        revision=preset.revision,
        config=InferencePresetConfig.model_validate(preset.config_json),
        config_sha256=preset.config_sha256,
        created_at=preset.created_at,
        updated_at=preset.updated_at,
    )


def test_set_item_manifest_document(item: TestSetItem) -> dict[str, object]:
    """Return the storage-neutral, identifier-stable item manifest projection."""

    return {
        "item_key": item.item_key,
        "display_name": item.display_name,
        "sort_order": item.sort_order,
        "filename": f"{item.item_key}.wav",
        "original_filename": item.original_filename,
        "size_bytes": item.size_bytes,
        "sha256": item.sha256,
        "mime_type": item.mime_type,
        "sample_rate_hz": item.sample_rate_hz,
        "channels": item.channels,
        "duration_seconds": item.duration_seconds,
        "license_reference": item.license_reference,
        "provenance_reference": item.provenance_reference,
    }


def build_test_set_manifest_document(
    test_set: TestSet,
    items: list[TestSetItem],
) -> dict[str, object]:
    ordered = sorted(items, key=lambda item: (item.sort_order, item.item_key, item.id))
    return {
        "schema_version": 1,
        "test_set": {
            "name": test_set.name,
            "revision": test_set.revision,
        },
        "items": [test_set_item_manifest_document(item) for item in ordered],
    }


def build_sample_plan_document(
    test_set: TestSet,
    items: list[TestSetItem],
    inference_config: InferencePresetConfig,
) -> dict[str, object]:
    ordered = sorted(items, key=lambda item: (item.sort_order, item.item_key, item.id))
    inference = preset_document(inference_config)
    return {
        "schema_version": 1,
        "test_set": {
            "id": test_set.id,
            "family_id": test_set.family_id,
            "revision": test_set.revision,
            "manifest_sha256": test_set.manifest_sha256,
        },
        "items": [
            {
                "test_set_item_id": item.id,
                **test_set_item_manifest_document(item),
            }
            for item in ordered
        ],
        "inference_config": inference,
        "inference_config_sha256": canonical_sha256(inference),
    }


def test_set_upload_request_fingerprint(payload: TestSetItemUploadInitRequest) -> str:
    document = payload.model_dump(mode="json", exclude={"idempotency_key"})
    return canonical_sha256(document)


def test_set_temporary_object_key(test_set_id: str, upload_id: str) -> str:
    return f"test-sets/staging/{test_set_id}/{upload_id}"


def test_set_item_object_key(test_set_id: str, upload_id: str) -> str:
    return f"test-sets/verified/{test_set_id}/items/{upload_id}.wav"


def test_set_manifest_object_key(test_set_id: str) -> str:
    return f"test-sets/verified/{test_set_id}/manifest.json"


def derive_test_set_upload_token(
    upload_session_id: str,
    expires_at_timestamp: int,
    settings: Settings,
) -> str:
    message = f"test-set-upload\x1f{upload_session_id}\x1f{expires_at_timestamp}".encode()
    digest = hmac.new(
        settings.worker_token_pepper.get_secret_value().encode("utf-8"),
        message,
        hashlib.sha256,
    ).digest()
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"rvct_{encoded}"


def inspect_pcm_wav(path: Path, settings: Settings) -> WavInspection:
    try:
        with path.open("rb") as source:
            header = source.read(12)
    except OSError as exc:
        raise InvalidTestSetWav("wav_read_failed") from exc
    if len(header) != 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        raise InvalidTestSetWav("invalid_wav_signature")

    try:
        with wave.open(str(path), mode="rb") as audio:
            if audio.getcomptype() != "NONE":
                raise InvalidTestSetWav("compressed_wav_not_allowed")
            channels = audio.getnchannels()
            sample_rate = audio.getframerate()
            sample_width = audio.getsampwidth()
            frame_count = audio.getnframes()
            if channels < 1 or channels > settings.test_set_max_channels:
                raise InvalidTestSetWav("wav_channel_limit")
            if not (
                settings.test_set_min_sample_rate_hz
                <= sample_rate
                <= settings.test_set_max_sample_rate_hz
            ):
                raise InvalidTestSetWav("wav_sample_rate_limit")
            if sample_width not in {1, 2, 3, 4}:
                raise InvalidTestSetWav("wav_sample_width_not_supported")
            if frame_count <= 0:
                raise InvalidTestSetWav("empty_wav")
            duration = frame_count / sample_rate
            if duration > settings.test_set_max_duration_seconds:
                raise InvalidTestSetWav("wav_duration_limit")

            decoded_bytes = 0
            frames_remaining = frame_count
            while frames_remaining:
                requested = min(frames_remaining, 65_536)
                chunk = audio.readframes(requested)
                if not chunk:
                    break
                decoded_bytes += len(chunk)
                frames_remaining -= len(chunk) // (channels * sample_width)
            expected_bytes = frame_count * channels * sample_width
            if frames_remaining != 0 or decoded_bytes != expected_bytes:
                raise InvalidTestSetWav("truncated_wav_pcm")
    except InvalidTestSetWav:
        raise
    except (EOFError, wave.Error, OSError) as exc:
        raise InvalidTestSetWav("invalid_wav_structure") from exc

    return WavInspection(
        sample_rate_hz=sample_rate,
        channels=channels,
        duration_seconds=duration,
    )


def test_set_to_read(
    test_set: TestSet,
    items: list[TestSetItem],
    *,
    items_included: bool = True,
) -> TestSetRead:
    ordered = sorted(items, key=lambda item: (item.sort_order, item.item_key, item.id))
    return TestSetRead(
        id=test_set.id,
        family_id=test_set.family_id,
        name=test_set.name,
        revision=test_set.revision,
        description=test_set.description,
        status=test_set.status,  # type: ignore[arg-type]
        manifest_sha256=test_set.manifest_sha256,
        item_count=test_set.item_count,
        failure_code=test_set.failure_code,
        items_included=items_included,
        items=[TestSetItemRead.model_validate(item) for item in ordered],
        finalized_at=test_set.finalized_at,
        created_at=test_set.created_at,
        updated_at=test_set.updated_at,
    )

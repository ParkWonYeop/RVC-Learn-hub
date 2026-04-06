from __future__ import annotations

import hashlib
import io
import json
import math
import os
import stat
import struct
import wave
import zipfile
from pathlib import Path

import pytest

from rvc_manager_api.dataset_ingestion import (
    DatasetLimitExceededError,
    DestinationExistsError,
    IngestionLimits,
    NoUsableAudioError,
    UnsafeArchiveError,
    UnsafePathError,
    _k_weighting_coefficients,
    ingest_dataset,
)


def pcm_wav_bytes(
    samples: list[int],
    *,
    sample_rate: int = 8_000,
    channels: int = 1,
    sample_width: int = 2,
) -> bytes:
    output = io.BytesIO()
    with wave.open(output, mode="wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(sample_width)
        audio.setframerate(sample_rate)
        if sample_width == 1:
            frame_data = bytes(sample + 128 for sample in samples)
        elif sample_width == 2:
            frame_data = struct.pack(f"<{len(samples)}h", *samples)
        elif sample_width == 3:
            frame_data = b"".join(
                sample.to_bytes(3, byteorder="little", signed=True) for sample in samples
            )
        else:
            frame_data = struct.pack(f"<{len(samples)}i", *samples)
        audio.writeframes(frame_data)
    return output.getvalue()


def write_zip(
    path: Path,
    members: list[tuple[str, bytes]],
    *,
    compression: int = zipfile.ZIP_STORED,
) -> None:
    with zipfile.ZipFile(path, mode="w", compression=compression) as archive:
        for name, content in members:
            archive.writestr(name, content)


def ingest(root: Path, source: Path, destination_name: str = "published"):
    return ingest_dataset(
        source,
        job_temp_root=root,
        destination=root / destination_name,
    )


def test_single_pcm_wav_is_streamed_flattened_analyzed_and_serialized(tmp_path: Path) -> None:
    samples = [0, 32_767, -32_768, 1_000]
    source = tmp_path / "한글 (speaker) TAKE.WAV"
    source.write_bytes(pcm_wav_bytes(samples))

    result = ingest(tmp_path, source)

    canonical = result.flat_directory / "000001.wav"
    assert canonical.read_bytes() == source.read_bytes()
    assert [path.name for path in result.flat_directory.iterdir()] == ["000001.wav"]
    assert result.manifest.file_count == 1
    assert result.manifest.total_bytes == source.stat().st_size
    entry = result.manifest.files[0]
    assert entry.source_path == source.name
    assert entry.canonical_path == "prepared_flat/000001.wav"
    assert entry.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert entry.inspection.status == "validated_pcm"
    assert entry.inspection.duration_seconds == pytest.approx(4 / 8_000)
    assert entry.inspection.sample_rate_hz == 8_000
    assert entry.inspection.channels == 1
    assert entry.inspection.sample_width_bytes == 2
    assert entry.inspection.peak_ratio == 1.0
    assert entry.inspection.clipping_ratio == 0.5
    assert entry.inspection.silence_ratio == 0.25
    assert entry.inspection.rms_ratio == pytest.approx(
        (sum(sample * sample for sample in samples) / len(samples)) ** 0.5 / 32_768
    )
    assert result.quality_report.schema_version == 3
    assert result.quality_report.pcm_quality is not None
    assert result.quality_report.pcm_quality.algorithm == "pcm-sample-weighted-v1"
    assert result.quality_report.pcm_quality.validated_file_count == 1
    assert result.quality_report.pcm_quality.sample_count == 4
    assert result.quality_report.pcm_quality.clipping_ratio == 0.5
    assert result.quality_report.pcm_quality.silence_ratio == 0.25
    assert result.quality_report.pcm_quality.rms_ratio == pytest.approx(
        (sum(sample * sample for sample in samples) / len(samples)) ** 0.5 / 32_768
    )
    assert result.quality_report.pcm_quality.silence_threshold_dbfs == -50.0
    loudness = result.quality_report.pcm_quality.loudness
    assert loudness.algorithm == "itu-r-bs1770-4-mono-stereo-v1"
    assert loudness.scope == "global-gate-over-per-file-complete-blocks-v1"
    assert loudness.block_duration_ms == 400
    assert loudness.block_overlap_percent == 75
    assert loudness.absolute_gate_lufs == -70.0
    assert loudness.relative_gate_lu == -10.0
    assert loudness.analyzed_file_count == 1
    assert loudness.block_count == 0
    assert loudness.gated_block_count == 0
    assert loudness.integrated_lufs is None
    assert loudness.unavailable_reason == "insufficient_duration"
    assert json.loads(result.manifest_path.read_text()) == result.manifest.to_dict()
    assert json.loads(result.quality_report_path.read_text()) == result.quality_report.to_dict()
    assert result.manifest.to_json().endswith("\n")
    assert result.quality_report.to_json().endswith("\n")


@pytest.mark.parametrize("extension", [".flac", ".mp3", ".m4a", ".ogg", ".aac"])
def test_non_wav_formats_are_explicitly_decoder_pending(tmp_path: Path, extension: str) -> None:
    source = tmp_path / f"speaker{extension.upper()}"
    source.write_bytes(b"decoder-validation-is-not-yet-available")

    result = ingest(tmp_path, source)

    entry = result.manifest.files[0]
    assert entry.extension == extension
    assert entry.canonical_path == f"prepared_flat/000001{extension}"
    assert entry.inspection.status == "decoder_pending"
    assert entry.inspection.duration_seconds is None
    assert result.quality_report.decoder_pending_count == 1
    assert result.quality_report.pcm_quality is None


def test_pcm_quality_uses_exact_sample_weighting_across_widths_and_channels(
    tmp_path: Path,
) -> None:
    mono_16bit = pcm_wav_bytes([32_767, 0], sample_width=2, channels=1)
    stereo_8bit = pcm_wav_bytes([64] * 8, sample_width=1, channels=2)
    source = tmp_path / "weighted.zip"
    write_zip(
        source,
        [
            ("a/short-mono.wav", mono_16bit),
            ("b/long-stereo.wav", stereo_8bit),
        ],
    )

    result = ingest(tmp_path, source)
    aggregate = result.quality_report.pcm_quality

    assert aggregate is not None
    assert aggregate.validated_file_count == 2
    assert aggregate.sample_count == 10
    assert aggregate.clipping_ratio == pytest.approx(1 / 10)
    assert aggregate.silence_ratio == pytest.approx(1 / 10)
    expected_normalized_square_sum = (32_767 / 32_768) ** 2 + 8 * (64 / 128) ** 2
    assert aggregate.rms_ratio == pytest.approx((expected_normalized_square_sum / 10) ** 0.5)
    assert aggregate.loudness.integrated_lufs is None
    assert aggregate.loudness.unavailable_reason == "insufficient_duration"


def test_pcm_loudness_matches_bs1770_coefficients_and_reference_tone(
    tmp_path: Path,
) -> None:
    shelf, high_pass = _k_weighting_coefficients(48_000)
    assert (shelf.b0, shelf.b1, shelf.b2, shelf.a1, shelf.a2) == pytest.approx(
        (
            1.53512485958697,
            -2.69169618940638,
            1.19839281085285,
            -1.69065929318241,
            0.73248077421585,
        ),
        abs=1e-14,
    )
    assert (
        high_pass.b0,
        high_pass.b1,
        high_pass.b2,
        high_pass.a1,
        high_pass.a2,
    ) == pytest.approx(
        (1.0, -2.0, 1.0, -1.99004745483398, 0.99007225036621),
        abs=1e-14,
    )

    sample_rate = 48_000
    samples = [
        round(32_767 * 0.1 * math.sin(2 * math.pi * 1_000 * index / sample_rate))
        for index in range(sample_rate * 3)
    ]
    source = tmp_path / "reference-tone.wav"
    source.write_bytes(pcm_wav_bytes(samples, sample_rate=sample_rate))

    loudness = ingest(tmp_path, source).quality_report.pcm_quality

    assert loudness is not None
    assert loudness.loudness.analyzed_file_count == 1
    assert loudness.loudness.block_count == 27
    assert loudness.loudness.gated_block_count == 27
    assert loudness.loudness.integrated_lufs == pytest.approx(-23.003511150303, abs=1e-9)
    assert loudness.loudness.unavailable_reason is None


def test_pcm_loudness_uses_global_gate_without_averaging_file_lufs(tmp_path: Path) -> None:
    sample_rate = 48_000

    def tone(amplitude: float) -> bytes:
        return pcm_wav_bytes(
            [
                round(32_767 * amplitude * math.sin(2 * math.pi * 1_000 * index / sample_rate))
                for index in range(sample_rate)
            ],
            sample_rate=sample_rate,
        )

    source = tmp_path / "global-gate.zip"
    write_zip(source, [("a-loud.wav", tone(0.1)), ("b-quiet.wav", tone(0.001))])

    aggregate = ingest(tmp_path, source).quality_report.pcm_quality

    assert aggregate is not None
    assert aggregate.loudness.analyzed_file_count == 2
    assert aggregate.loudness.block_count == 14
    assert aggregate.loudness.gated_block_count == 7
    assert aggregate.loudness.integrated_lufs == pytest.approx(-23.003542866848, abs=1e-9)


@pytest.mark.parametrize(
    ("samples", "sample_rate", "channels", "expected_reason", "expected_blocks"),
    [
        ([0] * 48_000, 48_000, 1, "below_absolute_gate", 7),
        ([1_000] * 4_000, 4_000, 1, "unsupported_sample_rate", 0),
        ([1_000] * 24_000, 8_000, 3, "unsupported_channel_layout", 0),
    ],
)
def test_pcm_loudness_unavailable_states_are_explicit_and_finite(
    tmp_path: Path,
    samples: list[int],
    sample_rate: int,
    channels: int,
    expected_reason: str,
    expected_blocks: int,
) -> None:
    source = tmp_path / f"unavailable-{expected_reason}.wav"
    source.write_bytes(pcm_wav_bytes(samples, sample_rate=sample_rate, channels=channels))

    aggregate = ingest(tmp_path, source).quality_report.pcm_quality

    assert aggregate is not None
    assert aggregate.loudness.integrated_lufs is None
    assert aggregate.loudness.gated_block_count == 0
    assert aggregate.loudness.block_count == expected_blocks
    assert aggregate.loudness.unavailable_reason == expected_reason


def test_zip_is_recursively_collected_sorted_flattened_and_deduplicated(tmp_path: Path) -> None:
    wav_data = pcm_wav_bytes([0, 1_000, -1_000, 0])
    source = tmp_path / "dataset.zip"
    write_zip(
        source,
        [
            ("z/deep/copy.wav", wav_data),
            ("docs/readme.txt", b"not audio"),
            ("folder/.hidden.wav", wav_data),
            ("__MACOSX/._resource.wav", b"metadata"),
            ("nested/.DS_Store", b"finder metadata"),
            ("b/song.MP3", b"ID3-pending-decoder"),
            ("a/original.wav", wav_data),
        ],
    )

    first = ingest(tmp_path, source, "first")
    second = ingest(tmp_path, source, "second")

    assert [entry.canonical_path for entry in first.manifest.files] == [
        "prepared_flat/000001.wav",
        "prepared_flat/000002.mp3",
    ]
    assert [entry.source_path for entry in first.manifest.files] == [
        "a/original.wav",
        "b/song.MP3",
    ]
    assert first.manifest.to_json() == second.manifest.to_json()
    assert first.quality_report.to_json() == second.quality_report.to_json()
    assert first.quality_report.source_file_entries == 7
    assert first.quality_report.included_count == 2
    assert first.quality_report.decoder_pending_count == 1
    assert {(entry.source_path, entry.reason) for entry in first.quality_report.skipped} == {
        ("__MACOSX/._resource.wav", "macos_metadata"),
        ("docs/readme.txt", "non_audio"),
        ("folder/.hidden.wav", "hidden"),
        ("nested/.DS_Store", "macos_metadata"),
    }
    duplicate = first.quality_report.duplicates[0]
    assert duplicate.source_path == "z/deep/copy.wav"
    assert duplicate.duplicate_of == "prepared_flat/000001.wav"
    assert sorted(path.name for path in first.flat_directory.iterdir()) == [
        "000001.wav",
        "000002.mp3",
    ]


@pytest.mark.parametrize(
    "member_name",
    [
        "../escape.wav",
        "nested/../../escape.wav",
        "/absolute.wav",
        "C:/windows.wav",
        "nested\\..\\escape.wav",
    ],
)
def test_zip_path_escape_is_rejected_without_partial_publish(
    tmp_path: Path, member_name: str
) -> None:
    source = tmp_path / "malicious.zip"
    write_zip(source, [(member_name, pcm_wav_bytes([0, 1]))])
    destination = tmp_path / "published"
    outside = tmp_path.parent / "escape.wav"
    outside_before = outside.read_bytes() if outside.exists() else None

    with pytest.raises(UnsafeArchiveError):
        ingest_dataset(source, job_temp_root=tmp_path, destination=destination)

    assert not destination.exists()
    assert list(tmp_path.glob(".published.staging-*")) == []
    assert (outside.read_bytes() if outside.exists() else None) == outside_before


@pytest.mark.parametrize("file_type", [stat.S_IFLNK, stat.S_IFIFO])
def test_zip_symlink_and_special_file_members_are_rejected(tmp_path: Path, file_type: int) -> None:
    source = tmp_path / "special.zip"
    member = zipfile.ZipInfo("nested/audio.wav")
    member.create_system = 3
    member.external_attr = (file_type | 0o777) << 16
    with zipfile.ZipFile(source, mode="w") as archive:
        archive.writestr(member, b"outside-target")

    with pytest.raises(UnsafeArchiveError):
        ingest(tmp_path, source)

    assert not (tmp_path / "published").exists()


def test_encrypted_member_flag_is_rejected_before_decompression(tmp_path: Path) -> None:
    source = tmp_path / "encrypted.zip"
    write_zip(source, [("voice.mp3", b"encrypted-payload")])
    archive_bytes = bytearray(source.read_bytes())
    local_header = archive_bytes.index(b"PK\x03\x04")
    central_header = archive_bytes.index(b"PK\x01\x02")
    local_flags = struct.unpack_from("<H", archive_bytes, local_header + 6)[0]
    central_flags = struct.unpack_from("<H", archive_bytes, central_header + 8)[0]
    struct.pack_into("<H", archive_bytes, local_header + 6, local_flags | 0x1)
    struct.pack_into("<H", archive_bytes, central_header + 8, central_flags | 0x1)
    source.write_bytes(archive_bytes)

    with pytest.raises(UnsafeArchiveError, match="encrypted"):
        ingest(tmp_path, source)

    assert not (tmp_path / "published").exists()


def test_duplicate_member_names_are_rejected_case_insensitively(tmp_path: Path) -> None:
    source = tmp_path / "duplicate-members.zip"
    with zipfile.ZipFile(source, mode="w") as archive:
        archive.writestr("nested/Voice.wav", pcm_wav_bytes([0, 1]))
        archive.writestr("nested/voice.wav", pcm_wav_bytes([0, 2]))

    with pytest.raises(UnsafeArchiveError, match="duplicate member"):
        ingest(tmp_path, source)


def test_corrupt_member_crc_aborts_without_partial_publish(tmp_path: Path) -> None:
    source = tmp_path / "bad-crc.zip"
    write_zip(source, [("voice.mp3", b"CRC protected payload")])
    archive_bytes = bytearray(source.read_bytes())
    local_header = archive_bytes.index(b"PK\x03\x04")
    name_length = struct.unpack_from("<H", archive_bytes, local_header + 26)[0]
    extra_length = struct.unpack_from("<H", archive_bytes, local_header + 28)[0]
    payload_offset = local_header + 30 + name_length + extra_length
    archive_bytes[payload_offset] ^= 0x01
    source.write_bytes(archive_bytes)

    with pytest.raises(UnsafeArchiveError, match="integrity"):
        ingest(tmp_path, source)

    assert not (tmp_path / "published").exists()
    assert list(tmp_path.glob(".published.staging-*")) == []


def test_entry_count_limit_applies_before_any_extraction(tmp_path: Path) -> None:
    source = tmp_path / "too-many.zip"
    write_zip(source, [("a.mp3", b"a"), ("b.mp3", b"b")])
    limits = IngestionLimits(max_entries=1)

    with pytest.raises(DatasetLimitExceededError, match="max_entries"):
        ingest_dataset(
            source,
            job_temp_root=tmp_path,
            destination=tmp_path / "published",
            limits=limits,
        )

    assert not (tmp_path / "published").exists()


def test_per_file_and_total_metadata_limits_apply_to_all_zip_files(tmp_path: Path) -> None:
    per_file_source = tmp_path / "per-file.zip"
    write_zip(per_file_source, [("ignored.txt", b"12345")])
    per_file_limits = IngestionLimits(
        max_file_uncompressed_bytes=4,
        max_total_uncompressed_bytes=10,
    )
    with pytest.raises(DatasetLimitExceededError, match="max_file"):
        ingest_dataset(
            per_file_source,
            job_temp_root=tmp_path,
            destination=tmp_path / "per-file-output",
            limits=per_file_limits,
        )

    total_source = tmp_path / "total.zip"
    write_zip(total_source, [("a.txt", b"123"), ("b.txt", b"456")])
    total_limits = IngestionLimits(
        max_file_uncompressed_bytes=10,
        max_total_uncompressed_bytes=5,
    )
    with pytest.raises(DatasetLimitExceededError, match="max_total"):
        ingest_dataset(
            total_source,
            job_temp_root=tmp_path,
            destination=tmp_path / "total-output",
            limits=total_limits,
        )


def test_compression_ratio_limit_rejects_highly_compressible_member(tmp_path: Path) -> None:
    source = tmp_path / "ratio.zip"
    write_zip(
        source,
        [("voice.mp3", b"0" * 4_096)],
        compression=zipfile.ZIP_DEFLATED,
    )
    limits = IngestionLimits(max_compression_ratio=2.0)

    with pytest.raises(DatasetLimitExceededError, match="compression_ratio"):
        ingest_dataset(
            source,
            job_temp_root=tmp_path,
            destination=tmp_path / "published",
            limits=limits,
        )


def test_broken_wav_is_reported_and_removed_when_other_audio_is_usable(tmp_path: Path) -> None:
    source = tmp_path / "mixed.zip"
    good_wav = pcm_wav_bytes([0, 1_000, -1_000, 0])
    write_zip(
        source,
        [
            ("a-broken.wav", b"not-a-wave-file"),
            ("b-good.wav", good_wav),
        ],
    )

    result = ingest(tmp_path, source)

    assert result.manifest.file_count == 1
    assert (result.flat_directory / "000001.wav").read_bytes() == good_wav
    assert result.quality_report.rejected[0].source_path == "a-broken.wav"
    assert result.quality_report.rejected[0].reason == "invalid_wav"
    assert result.quality_report.rejected[0].detail is not None
    assert "invalid WAV" in result.quality_report.rejected[0].detail


def test_only_broken_wav_fails_without_publishing_and_carries_report(tmp_path: Path) -> None:
    source = tmp_path / "broken.wav"
    source.write_bytes(b"RIFF-truncated")

    with pytest.raises(NoUsableAudioError) as error:
        ingest(tmp_path, source)

    assert error.value.report.included_count == 0
    assert error.value.report.rejected[0].source_path == "broken.wav"
    assert not (tmp_path / "published").exists()
    assert list(tmp_path.glob(".published.staging-*")) == []


def test_existing_destination_is_never_overwritten(tmp_path: Path) -> None:
    source = tmp_path / "audio.wav"
    source.write_bytes(pcm_wav_bytes([0, 1]))
    destination = tmp_path / "published"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("existing data")

    with pytest.raises(DestinationExistsError):
        ingest_dataset(source, job_temp_root=tmp_path, destination=destination)

    assert sentinel.read_text() == "existing data"


def test_source_and_destination_must_not_escape_or_follow_symlinks(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.wav"
    outside.write_bytes(pcm_wav_bytes([0, 1]))
    source_link = tmp_path / "linked.wav"
    source_link.symlink_to(outside)

    with pytest.raises(UnsafePathError):
        ingest_dataset(
            source_link,
            job_temp_root=tmp_path,
            destination=tmp_path / "linked-output",
        )
    with pytest.raises(UnsafePathError):
        ingest_dataset(
            outside,
            job_temp_root=tmp_path,
            destination=tmp_path / "outside-output",
        )
    with pytest.raises(UnsafePathError):
        ingest_dataset(
            "../outside.wav",
            job_temp_root=tmp_path,
            destination=tmp_path / "traversal-output",
        )

    source_link.unlink()
    outside.unlink()


def test_destination_parent_symlink_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "audio.wav"
    source.write_bytes(pcm_wav_bytes([0, 1]))
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(UnsafePathError):
        ingest_dataset(
            source,
            job_temp_root=tmp_path,
            destination=linked_parent / "published",
        )

    assert not (real_parent / "published").exists()


def test_limits_validate_configuration() -> None:
    with pytest.raises(ValueError):
        IngestionLimits(copy_chunk_bytes=0)
    with pytest.raises(ValueError):
        IngestionLimits(max_compression_ratio=float("inf"))
    with pytest.raises(ValueError):
        IngestionLimits(silence_threshold_dbfs=0)


def test_publish_leaves_no_lock_or_candidate_directory(tmp_path: Path) -> None:
    source = tmp_path / "audio.wav"
    source.write_bytes(pcm_wav_bytes([0, 1]))

    result = ingest(tmp_path, source)

    assert not (tmp_path / ".published.publish.lock").exists()
    assert not (result.destination / ".candidates").exists()
    assert set(path.name for path in result.destination.iterdir()) == {
        "manifest.json",
        "prepared_flat",
        "quality_report.json",
    }
    assert os.path.commonpath([result.destination, tmp_path]) == str(tmp_path)

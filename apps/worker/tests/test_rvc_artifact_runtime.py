from __future__ import annotations

import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from rvc_worker.index_builder import (
    IndexBuildError,
    IndexBuildLimits,
    IndexDependencies,
    build_index,
    build_index_command,
    build_rvc_index,
)
from rvc_worker.rvc_commands import RVC_REVIEWED_COMMIT
from rvc_worker.small_model import (
    SmallModelExtractionError,
    SmallModelRuntime,
    build_small_model_command,
    extract_small_model,
)


class FakeArray:
    def __init__(self, rows: list[list[float]], *, dtype: str = "float32") -> None:
        self.rows = rows
        self.dtype = dtype
        columns = len(rows[0]) if rows else 0
        self.shape = (len(rows), columns)

    def __getitem__(self, key: object) -> FakeArray:
        if isinstance(key, slice):
            return FakeArray(self.rows[key], dtype=self.dtype)
        if isinstance(key, list):
            return FakeArray([self.rows[index] for index in key], dtype=self.dtype)
        raise TypeError(f"unsupported fake array key: {type(key).__name__}")


class FakeFinite:
    def __init__(self, value: bool) -> None:
        self.value = value

    def all(self) -> bool:
        return self.value


class FakeRandomGenerator:
    def __init__(self, seed: int) -> None:
        self.seed = seed

    def permutation(self, size: int) -> list[int]:
        values = list(range(size))
        return list(reversed(values)) if self.seed % 2 else values


class FakeRandom:
    def __init__(self, seeds: list[int]) -> None:
        self.seeds = seeds

    def default_rng(self, seed: int) -> FakeRandomGenerator:
        self.seeds.append(seed)
        return FakeRandomGenerator(seed)


class FakeNumpy:
    float32 = "float32"
    floating = "floating"

    def __init__(self, arrays: dict[str, FakeArray]) -> None:
        self.arrays = arrays
        self.loads: list[tuple[str, bool]] = []
        self.saves: list[tuple[list[float], bool]] = []
        self.seeds: list[int] = []
        self.random = FakeRandom(self.seeds)

    def load(self, path: str, *, allow_pickle: bool) -> FakeArray:
        self.loads.append((path, allow_pickle))
        return self.arrays[path]

    def concatenate(self, arrays: list[FakeArray], *, axis: int) -> FakeArray:
        if axis != 0:
            raise AssertionError("only row concatenation is expected")
        return FakeArray([row for array in arrays for row in array.rows])

    def ascontiguousarray(self, array: FakeArray, *, dtype: str) -> FakeArray:
        return FakeArray([list(row) for row in array.rows], dtype=dtype)

    def issubdtype(self, dtype: str, expected: str) -> bool:
        return expected == self.floating and dtype in {"float16", "float32", "float64"}

    def isfinite(self, array: FakeArray) -> FakeFinite:
        return FakeFinite(all(math.isfinite(value) for row in array.rows for value in row))

    def save(self, stream: Any, array: FakeArray, *, allow_pickle: bool) -> None:
        first_values = [row[0] for row in array.rows]
        self.saves.append((first_values, allow_pickle))
        stream.write((",".join(str(value) for value in first_values) or "empty").encode())


class FakeIndex:
    def __init__(self) -> None:
        self.is_trained = False
        self.ntotal = 0
        self.trained_rows: list[list[float]] = []

    def train(self, features: FakeArray) -> None:
        self.trained_rows = features.rows
        self.is_trained = True

    def add(self, features: FakeArray) -> None:
        self.ntotal += len(features.rows)


class FakeFaiss:
    def __init__(self) -> None:
        self.factories: list[tuple[int, str]] = []
        self.index = FakeIndex()
        self.ivf = SimpleNamespace(nprobe=0)
        self.writes: list[tuple[str, int]] = []

    def index_factory(self, dimension: int, description: str) -> FakeIndex:
        self.factories.append((dimension, description))
        return self.index

    def extract_index_ivf(self, index: FakeIndex) -> SimpleNamespace:
        if index is not self.index:
            raise AssertionError("unexpected fake index")
        return self.ivf

    def write_index(self, index: FakeIndex, path: str) -> None:
        self.writes.append((path, index.ntotal))
        Path(path).write_bytes(f"index:{index.ntotal}".encode())


class FakeMiniBatchKMeans:
    def __init__(self, calls: list[dict[str, object]], **kwargs: object) -> None:
        self.calls = calls
        self.kwargs = kwargs
        self.calls.append(kwargs)
        self.cluster_centers_ = FakeArray([])

    def fit(self, features: FakeArray) -> FakeMiniBatchKMeans:
        count = int(self.kwargs["n_clusters"])
        self.cluster_centers_ = FakeArray(features.rows[:count])
        return self


class FakeKMeansFactory:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> FakeMiniBatchKMeans:
        return FakeMiniBatchKMeans(self.calls, **kwargs)


def feature_rows(count: int, dimension: int, *, start: int = 0) -> list[list[float]]:
    return [[float(start + index)] * dimension for index in range(count)]


class IndexRuntimeTests(unittest.TestCase):
    def test_builds_shell_free_index_cli_arguments(self) -> None:
        command = build_index_command(
            "/opt/rvc-python/bin/python",
            Path("/srv/rvc-job/work/rvc/logs/voice"),
            "voice",
            "v2",
            seed=42,
            cpu_workers=8,
        )
        self.assertEqual(
            command[:3],
            (
                "/opt/rvc-python/bin/python",
                "-m",
                "rvc_worker.index_builder",
            ),
        )
        self.assertEqual(command[command.index("--version") + 1], "v2")
        self.assertEqual(command[command.index("--seed") + 1], "42")
        self.assertEqual(command[command.index("--cpu-workers") + 1], "8")

    def test_builds_deterministic_v1_outputs_without_heavy_dependencies(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            features = root / "3_feature256"
            features.mkdir()
            paths = [features / "b.npy", features / "a.npy"]
            for path in paths:
                path.write_bytes(b"safe-npy-placeholder")
            numpy = FakeNumpy(
                {
                    str(features / "a.npy"): FakeArray(feature_rows(2, 256, start=10)),
                    str(features / "b.npy"): FakeArray(feature_rows(1, 256, start=20)),
                }
            )
            faiss = FakeFaiss()

            result = build_index(
                features,
                root,
                "voice_a",
                "v1",
                seed=17,
                dependencies=IndexDependencies(numpy=numpy, faiss=faiss),
            )

            self.assertEqual([Path(call[0]).name for call in numpy.loads], ["a.npy", "b.npy"])
            self.assertTrue(all(allow_pickle is False for _, allow_pickle in numpy.loads))
            self.assertEqual(numpy.seeds, [17])
            self.assertEqual(numpy.saves, [([20.0, 11.0, 10.0], False)])
            self.assertEqual(faiss.factories, [(256, "IVF1,Flat")])
            self.assertEqual(faiss.ivf.nprobe, 1)
            self.assertEqual(faiss.index.ntotal, 3)
            self.assertEqual(result.source_rows, 3)
            self.assertEqual(result.indexed_rows, 3)
            self.assertFalse(result.used_kmeans)
            self.assertEqual(result.dimension, 256)
            self.assertTrue(result.total_features.is_file())
            self.assertTrue(result.trained_index.name.startswith("trained_IVF1_Flat"))
            self.assertTrue(result.added_index.name.startswith("added_IVF1_Flat"))

    def test_v2_uses_seeded_kmeans_above_configured_threshold(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            features = root / "3_feature768"
            features.mkdir()
            source = features / "features.npy"
            source.write_bytes(b"safe-npy-placeholder")
            numpy = FakeNumpy({str(source): FakeArray(feature_rows(3, 768))})
            faiss = FakeFaiss()
            kmeans = FakeKMeansFactory()

            result = build_rvc_index(
                root,
                "voice_v2",
                "v2",
                seed=9,
                cpu_workers=3,
                limits=IndexBuildLimits(kmeans_threshold_rows=2, kmeans_clusters=2),
                dependencies=IndexDependencies(
                    numpy=numpy,
                    faiss=faiss,
                    mini_batch_kmeans=kmeans,
                ),
            )

            self.assertTrue(result.used_kmeans)
            self.assertEqual(result.source_rows, 3)
            self.assertEqual(result.indexed_rows, 2)
            self.assertEqual(result.dimension, 768)
            self.assertEqual(faiss.factories, [(768, "IVF1,Flat")])
            self.assertEqual(kmeans.calls[0]["random_state"], 9)
            self.assertEqual(kmeans.calls[0]["n_clusters"], 2)
            self.assertEqual(kmeans.calls[0]["n_init"], 1)

    def test_rejects_unsafe_or_invalid_feature_inputs_before_faiss(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            features = root / "features"
            features.mkdir()
            source = features / "bad.npy"
            source.write_bytes(b"placeholder")
            faiss = FakeFaiss()

            with self.subTest("dimension"):
                numpy = FakeNumpy({str(source): FakeArray(feature_rows(1, 255))})
                with self.assertRaisesRegex(IndexBuildError, "dimension 256"):
                    build_index(
                        features,
                        root,
                        "safe",
                        "v1",
                        dependencies=IndexDependencies(numpy=numpy, faiss=faiss),
                    )

            with self.subTest("non-finite"):
                rows = feature_rows(1, 256)
                rows[0][0] = math.nan
                numpy = FakeNumpy({str(source): FakeArray(rows)})
                with self.assertRaisesRegex(IndexBuildError, "NaN or infinity"):
                    build_index(
                        features,
                        root,
                        "safe",
                        "v1",
                        dependencies=IndexDependencies(numpy=numpy, faiss=faiss),
                    )

            with self.subTest("row limit"):
                numpy = FakeNumpy({str(source): FakeArray(feature_rows(2, 256))})
                with self.assertRaisesRegex(IndexBuildError, "row count exceeds limit"):
                    build_index(
                        features,
                        root,
                        "safe",
                        "v1",
                        limits=IndexBuildLimits(max_rows_per_file=1),
                        dependencies=IndexDependencies(numpy=numpy, faiss=faiss),
                    )

            with self.subTest("name"):
                numpy = FakeNumpy({str(source): FakeArray(feature_rows(1, 256))})
                with self.assertRaisesRegex(IndexBuildError, "safe RVC path"):
                    build_index(
                        features,
                        root,
                        "../escape",
                        "v1",
                        dependencies=IndexDependencies(numpy=numpy, faiss=faiss),
                    )

            with self.subTest("symlink"):
                source.unlink()
                target = root / "outside.npy"
                target.write_bytes(b"outside")
                source.symlink_to(target)
                numpy = FakeNumpy({str(source): FakeArray(feature_rows(1, 256))})
                with self.assertRaisesRegex(IndexBuildError, "regular non-symlink"):
                    build_index(
                        features,
                        root,
                        "safe",
                        "v1",
                        dependencies=IndexDependencies(numpy=numpy, faiss=faiss),
                    )


class SmallModelRuntimeTests(unittest.TestCase):
    def _tree(self, root: Path) -> tuple[Path, Path, Path]:
        repository = root / "rvc"
        (repository / "infer/lib/train").mkdir(parents=True)
        (repository / "infer/lib/train/process_ckpt.py").write_text("# official\n")
        (repository / "assets/weights").mkdir(parents=True)
        checkpoint = root / "G_12.pth"
        checkpoint.write_bytes(b"large-training-checkpoint")
        output_directory = root / "outputs"
        output_directory.mkdir()
        return repository, checkpoint, output_directory / "final_small_model.pth"

    def _runtime(
        self,
        calls: list[tuple[str, str, str, str, str, str]],
        *,
        metadata_override: dict[str, object] | None = None,
        copy_checkpoint: bool = False,
    ) -> SmallModelRuntime:
        metadata_by_path: dict[Path, dict[str, object]] = {}

        def extractor(
            checkpoint: str,
            name: str,
            sample_rate: str,
            use_f0: str,
            info: str,
            version: str,
        ) -> object:
            calls.append((checkpoint, name, sample_rate, use_f0, info, version))
            output = Path("assets/weights") / f"{name}.pth"
            output.write_bytes(
                Path(checkpoint).read_bytes() if copy_checkpoint else b"official-extracted-model"
            )
            metadata: dict[str, object] = {
                "weight": {"decoder.weight": object()},
                "config": [
                    1025,
                    32,
                    192,
                    192,
                    768,
                    2,
                    6,
                    3,
                    0,
                    "1",
                    [],
                    [],
                    [],
                    512,
                    [],
                    109,
                    256,
                    40_000 if sample_rate == "40k" else 48_000,
                ],
                "info": info or "Extracted model.",
                "version": version,
                "sr": sample_rate,
                "f0": int(use_f0),
            }
            metadata.update(metadata_override or {})
            metadata_by_path[output.resolve()] = metadata
            return "Success."

        def loader(path: Path) -> object:
            return metadata_by_path[path.resolve()]

        return SmallModelRuntime(extractor=extractor, metadata_loader=loader)

    def test_calls_official_function_and_atomically_publishes_verified_model(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repository, checkpoint, output = self._tree(root)
            calls: list[tuple[str, str, str, str, str, str]] = []
            original_cwd = Path.cwd()

            result = extract_small_model(
                repository,
                checkpoint,
                output,
                "voice_a",
                "48k",
                True,
                "v2",
                info="job 42",
                runtime=self._runtime(calls),
                revision_reader=lambda _: RVC_REVIEWED_COMMIT,
            )

            self.assertEqual(Path.cwd(), original_cwd)
            self.assertEqual(calls[0][0], str(checkpoint))
            self.assertTrue(calls[0][1].startswith("orchestrator_"))
            self.assertEqual(calls[0][2:], ("48k", "1", "job 42", "v2"))
            self.assertEqual(output.read_bytes(), b"official-extracted-model")
            self.assertEqual(checkpoint.read_bytes(), b"large-training-checkpoint")
            self.assertNotEqual(result.sha256, result.source_checkpoint_sha256)
            self.assertEqual(result.repository_commit, RVC_REVIEWED_COMMIT)
            self.assertEqual(result.version, "v2")
            self.assertEqual(result.sample_rate, "48k")
            self.assertFalse(list((repository / "assets/weights").glob("orchestrator_*.pth")))

    def test_explicit_reviewed_projection_marker_replaces_git_only_for_typed_runner(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repository, checkpoint, output = self._tree(root)
            (repository / ".rvc-reviewed-commit").write_text(
                RVC_REVIEWED_COMMIT + "\n", encoding="ascii"
            )
            (repository / ".orchestrator-projection.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "rvc_commit_hash": RVC_REVIEWED_COMMIT,
                        "files": [],
                    }
                ),
                encoding="utf-8",
            )
            calls: list[tuple[str, str, str, str, str, str]] = []

            result = extract_small_model(
                repository,
                checkpoint,
                output,
                "voice_a",
                "40k",
                False,
                "v1",
                runtime=self._runtime(calls),
                allow_reviewed_projection=True,
            )

            self.assertEqual(result.repository_commit, RVC_REVIEWED_COMMIT)
            self.assertEqual(output.read_bytes(), b"official-extracted-model")

    def test_metadata_failure_preserves_existing_output_and_removes_stage(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repository, checkpoint, output = self._tree(root)
            output.write_bytes(b"previous-good-model")
            calls: list[tuple[str, str, str, str, str, str]] = []
            runtime = self._runtime(calls, metadata_override={"version": "v1"})

            with self.assertRaisesRegex(SmallModelExtractionError, "version metadata"):
                extract_small_model(
                    repository,
                    checkpoint,
                    output,
                    "voice_a",
                    "48k",
                    True,
                    "v2",
                    runtime=runtime,
                    revision_reader=lambda _: RVC_REVIEWED_COMMIT,
                )

            self.assertEqual(output.read_bytes(), b"previous-good-model")
            self.assertFalse(list((repository / "assets/weights").glob("orchestrator_*.pth")))

    def test_rejects_a_byte_for_byte_checkpoint_copy(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repository, checkpoint, output = self._tree(root)
            calls: list[tuple[str, str, str, str, str, str]] = []

            with self.assertRaisesRegex(SmallModelExtractionError, "byte-identical"):
                extract_small_model(
                    repository,
                    checkpoint,
                    output,
                    "voice_a",
                    "40k",
                    False,
                    "v1",
                    runtime=self._runtime(calls, copy_checkpoint=True),
                    revision_reader=lambda _: RVC_REVIEWED_COMMIT,
                )

            self.assertFalse(output.exists())
            self.assertTrue(checkpoint.is_file())

    def test_rejects_symlink_checkpoint_and_unreviewed_revision(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            repository, checkpoint, output = self._tree(root)
            linked_checkpoint = root / "linked.pth"
            linked_checkpoint.symlink_to(checkpoint)
            calls: list[tuple[str, str, str, str, str, str]] = []
            with self.assertRaisesRegex(SmallModelExtractionError, "regular non-symlink"):
                extract_small_model(
                    repository,
                    linked_checkpoint,
                    output,
                    "voice_a",
                    "40k",
                    True,
                    "v1",
                    runtime=self._runtime(calls),
                    revision_reader=lambda _: RVC_REVIEWED_COMMIT,
                )
            with self.assertRaisesRegex(SmallModelExtractionError, "not been reviewed"):
                build_small_model_command(
                    "python",
                    repository,
                    checkpoint,
                    output,
                    "voice_a",
                    "40k",
                    True,
                    "v1",
                    expected_commit="0" * 40,
                )

    def test_builds_validated_shell_free_cli_arguments(self) -> None:
        root = Path("/srv/rvc-job")
        command = build_small_model_command(
            "/opt/venv/bin/python",
            root / "upstream",
            root / "logs/voice/G_10.pth",
            root / "outputs/final_small_model.pth",
            "voice_01",
            "40k",
            False,
            "v1",
            info="checkpoint 10",
        )
        self.assertEqual(command[:3], ("/opt/venv/bin/python", "-m", "rvc_worker.small_model"))
        self.assertEqual(command[command.index("--use-f0") + 1], "0")
        self.assertEqual(command[command.index("--sample-rate") + 1], "40k")
        self.assertEqual(command[command.index("--version") + 1], "v1")
        self.assertEqual(
            command[command.index("--expected-commit") + 1],
            RVC_REVIEWED_COMMIT,
        )
        with self.assertRaisesRegex(SmallModelExtractionError, "absolute"):
            build_small_model_command(
                "python",
                Path("relative/repo"),
                root / "G_1.pth",
                root / "final.pth",
                "voice",
                "40k",
                True,
                "v1",
            )
        with self.assertRaisesRegex(SmallModelExtractionError, "safe RVC path"):
            build_small_model_command(
                "python",
                root / "repo",
                root / "G_1.pth",
                root / "final.pth",
                "../voice",
                "40k",
                True,
                "v1",
            )


if __name__ == "__main__":
    unittest.main()

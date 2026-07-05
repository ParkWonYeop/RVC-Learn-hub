from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_VERIFIER = ROOT / "tools/verify_release_source.py"


def _git_check_ignore(path: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "check-ignore", "--no-index", "-v", path],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_release_source_routes_are_not_hidden_by_gitignore() -> None:
    required_sources = (
        "apps/web/src/app/bff/artifacts/[artifactId]/download/route.ts",
        "apps/web/src/app/bff/jobs/[jobId]/artifacts/route.ts",
        "apps/web/src/app/bff/datasets/uploads/init/route.ts",
        "apps/web/src/app/bff/samples/[sampleId]/download/route.ts",
    )

    for relative in required_sources:
        assert (ROOT / relative).is_file(), relative
        result = _git_check_ignore(relative)
        assert result.returncode == 1, result.stdout or result.stderr


def test_runtime_output_directories_remain_ignored_at_repository_root() -> None:
    ignored_outputs = (
        "artifacts/release-probe.bin",
        "build/release-probe.bin",
        "data/release-probe.bin",
        "dist/release-probe.bin",
        "secrets/release-probe.bin",
        "workspaces/release-probe.bin",
    )

    for relative in ignored_outputs:
        result = _git_check_ignore(relative)
        assert result.returncode == 0, (relative, result.stdout, result.stderr)


def test_release_output_ignore_rules_are_explicitly_root_scoped() -> None:
    gitignore = set((ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())
    dockerignore = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())

    for directory in ("artifacts", "data", "secrets", "workspaces"):
        assert f"/{directory}/" in gitignore
        assert f"{directory}/" not in gitignore
        assert f"/{directory}" in dockerignore
        assert directory not in dockerignore


def test_release_source_verifier_rejects_ignored_application_source(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _write_minimal_release_tree(repo)
    route = repo / "apps/web/src/app/bff/artifacts/item/route.ts"
    route.parent.mkdir(parents=True)
    route.write_text("export const GET = () => null;\n", encoding="utf-8")
    (repo / "apps/web/node_modules/dependency").mkdir(parents=True)
    (repo / "apps/web/node_modules/dependency/cache.js").write_text("generated\n", encoding="utf-8")
    (repo / ".gitignore").write_text("artifacts/\nnode_modules/\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    rejected = subprocess.run(
        ["python3", str(SOURCE_VERIFIER), "--repo-root", str(repo)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode != 0
    assert "apps/web/src/app/bff/artifacts/item/route.ts" in rejected.stderr

    subprocess.run(
        ["git", "-C", str(repo), "add", "-f", route.relative_to(repo).as_posix()],
        check=True,
    )
    tracked_but_ignored = subprocess.run(
        ["python3", str(SOURCE_VERIFIER), "--repo-root", str(repo)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert tracked_but_ignored.returncode != 0
    assert "apps/web/src/app/bff/artifacts/item/route.ts" in tracked_but_ignored.stderr

    (repo / ".gitignore").write_text("/artifacts/\nnode_modules/\n", encoding="utf-8")
    accepted = subprocess.run(
        ["python3", str(SOURCE_VERIFIER), "--repo-root", str(repo)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert "Release source ignore closure verified" in accepted.stdout


def _write_minimal_release_tree(root: Path) -> None:
    for directory in (
        "apps",
        "docs",
        "infra",
        "installers",
        "packages",
        "supply-chain",
        "tests",
        "tools",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
        (root / directory / "tracked-source.txt").write_text(f"{directory}\n", encoding="utf-8")
    for filename in (
        ".dockerignore",
        ".env.example",
        ".gitignore",
        "AGENTS.md",
        "CHECKLIST.md",
        "Makefile",
        "README.md",
        "pyproject.toml",
        "requirements-dev.txt",
    ):
        (root / filename).write_text(f"{filename}\n", encoding="utf-8")

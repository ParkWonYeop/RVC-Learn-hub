# Offline RVC Worker runtime packaging foundation

This directory builds a separate real-RVC Worker image. It does not change or
replace `apps/worker/Dockerfile`, which remains the lightweight Agent/Fake-runner
image. The runtime foundation is deliberately fail-closed and is not a released
GPU training image yet. The explicit `native` runner connects canonical Dataset
download/materialization and core preprocess/extract/train/index/export functions
through a job-local source projection. Fixed-TestSet sample inference is not
merely a future stub: the guarded PM/Harvest/CREPE/RMVPE driver, canonical Sample
publication, and completion path are connected. No real GPU/no-network matrix has
passed, however. The build manifest and image therefore continue to carry
pre-qualification `GPU_SMOKE_VERIFIED=false` and `PROFILE_STAGE_SET_VERIFIED=false`.
Only external evidence for the exact built image can create a release activation;
the current unqualified bundle carries `RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false`,
advertises no inference F0 capability, and reports
`fixed_test_set_inference_ready=false`.

## Reviewed compatibility candidate

| Layer | Fixed value |
|---|---|
| RVC source | `7ef19867780cf703841ebafb565a4e47d1ea86ff` |
| Platform | Linux x86_64 |
| Base family | `pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime` plus mandatory reviewed amd64 SHA-256 digest |
| Python | 3.11 |
| Torch | `2.6.0+cu124` |
| Torchvision | `0.21.0+cu124` |
| Torchaudio | `2.6.0+cu124` |
| CUDA wheel/runtime | cu124 / 12.4 |
| cuDNN | major 9 |
| fairseq | 0.12.2 wheel from an operator-reviewed exact One-sixth/fairseq commit |
| FAISS CPU | 1.7.4 |

The base image argument must include both the exact tag above and an
operator-reviewed `@sha256:<64 lowercase hex>` digest. The builder refuses a
locally loaded image of another architecture and runs Docker with
`--network=none --pull=false`.

This is a Python 3.11 release-runtime candidate, not a claim that upstream supports
the complete matrix. At the reviewed RVC commit, `pyproject.toml` fixes Torch
2.4.0/Torchvision 0.19.0/Torchaudio 2.4.0. The source verifier keeps those values
as reviewed upstream source markers so that a silently changed archive is rejected;
they are not the release runtime lock. The separate `runtime.lock.env` and
wheelhouse manifest require the 2.6.0/cu124 family above. The upstream metadata
also carries older NumPy, Numba, llvmlite and FAISS pins. `requirements-py311.txt`
relaxes those packages and
uses `fairseq @ git+https://github.com/One-sixth/fairseq.git` without a commit.
Installing either file directly would therefore resolve mutable or conflicting
dependencies. The build never does that. It requires an operator-created
`requirements.lock` in which every direct and transitive package has an exact
version and one or more wheel SHA-256 hashes.

The Agent now uses `httpx>=0.27,<1` for asynchronous streaming. The verifier
therefore requires a compatible fixed `httpx` wheel and the reviewed transport
closure (`httpcore`, `anyio`, `certifi`, `idna`, `h11`, `sniffio`, and
`typing-extensions`). The Docker build imports HTTPX, Pydantic, PyYAML, the
contracts package and the installed Worker wheel before it can succeed.

The Worker uses Pydantic 2, while upstream WebUI metadata includes old UI/API
dependencies such as FastAPI 0.88. A reviewed lock should contain the RVC
training/import subset rather than blindly combining the full WebUI dependency
set. The image build and later GPU stage smoke tests are the gate for that
selection.

## Required offline inputs

No script here downloads source, wheels, models, FFmpeg, or a base image.
Operators provide three roots. All JSON manifests reject duplicate/unknown
fields, symlinks, path traversal, missing files, byte-size differences,
checksum differences, unreviewed license identifiers and non-HTTPS provenance.
The builder first verifies the original inputs, copies only regular verified
files into a private mode-0700 snapshot, verifies the snapshot again, and uses
only that snapshot for extraction and the Docker context. Replacement of an
input between initial verification and copying is therefore rejected.

### 1. RVC source archive

The source manifest has this strict shape; placeholders below are not accepted.

```json
{
  "schema_version": 1,
  "kind": "rvc-source",
  "repository": "https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI",
  "commit": "7ef19867780cf703841ebafb565a4e47d1ea86ff",
  "archive": {
    "file": "rvc-source.tar.gz",
    "root": "Retrieval-based-Voice-Conversion-WebUI-7ef19867780cf703841ebafb565a4e47d1ea86ff",
    "sha256": "<64 lowercase hex>",
    "size": 123,
    "unpacked_size": 456,
    "source": "https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/archive/7ef19867780cf703841ebafb565a4e47d1ea86ff.tar.gz"
  },
  "license": {
    "spdx": "MIT",
    "source": "https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/7ef19867780cf703841ebafb565a4e47d1ea86ff/LICENSE"
  }
}
```

The verifier permits only regular files/directories under one declared archive
root. It also checks the reviewed dependency markers and training entrypoints.
The source archive hash and provenance manifest replace mutable branch or Git
clone operations. The image contains a narrow `git rev-parse HEAD` shim because
the existing Worker profile boundary verifies a revision through Git; that shim
returns the reviewed commit only when `/opt/rvc-webui/.rvc-reviewed-commit`
matches. It is not a general Git client.

### 2. Wheelhouse and hash lock

The wheelhouse contains only:

- `wheelhouse-manifest.json`;
- `requirements.lock`;
- the wheels listed by both files.

Each lock entry uses `normalized-name==exact-version
--hash=sha256:<wheel-hash>`. URLs, VCS references, environment markers,
unpinned entries, source distributions, unlisted files and missing hashes are
rejected. Every wheel is opened as ZIP, its METADATA name/version and Python
3.11 linux_x86_64 tag are checked, and its manifest record must include
`file`, `project`, `version`, `sha256`, `size`, `license`, and `source`.

The wheelhouse manifest fixes Python/platform/CUDA and the Torch family, FAISS,
and fairseq version. Its fairseq object additionally records the exact 40-hex
One-sixth/fairseq commit; the fairseq wheel source URL must contain that commit.
The fairseq commit is operator-reviewed input because upstream's Python 3.11
requirements do not pin one. At minimum the lock must also contain Hatchling,
setuptools, Pydantic and PyYAML for deterministic Agent wheel creation. A fixed
training subset is required for preprocessing, HuBERT/fairseq extraction,
supported F0 methods, training metrics and FAISS indexing: NumPy, SciPy, Numba,
llvmlite, librosa, soundfile, pydub, resampy, joblib, matplotlib, Pillow,
praat-parselmouth, pyworld, torchcrepe, tensorboard/TensorboardX, tqdm, sympy and
ffmpeg-python. All of their transitive dependencies must also appear as hashed
wheel lock entries. Pip runs with
`--no-index --require-hashes --only-binary=:all:`.

### 3. Asset root

`assets-manifest.json` targets the reviewed RVC commit and lists every file in
the supplied root. Every record has:

```json
{
  "path": "assets/hubert/hubert_base.pt",
  "sha256": "<64 lowercase hex>",
  "size": 123,
  "license": "LicenseRef-Operator-Reviewed",
  "source": "https://reviewed.example/source",
  "executable": false
}
```

The required set is:

- `assets/hubert/hubert_base.pt` and `assets/rmvpe/rmvpe.pt`;
- v1 and v2 `G`, `D`, `f0G`, `f0D` weights for 40k and 48k;
- mute WAV/features/F0 fixtures used by `training_inputs.py`;
- `runtime/crepe/full.pth`, the only CREPE model path accepted by the native
  inference driver;
- operator-supplied static `runtime/bin/ffmpeg` and `ffprobe` executables.

`runtime/crepe/full.pth` is part of the strict asset manifest and the
build-generated private projection inventory. A CREPE inference verifies the
fixed path and the same `O_NOFOLLOW` descriptor bytes before, during, and after
the subprocess, loads the state dictionary with explicit `weights_only=True`,
strictly binds it to `torchcrepe.Crepe("full")`, and pre-binds
`torchcrepe.infer.model/capacity`. `TORCH_HOME` is attempt-private and Hugging
Face/Transformers offline mode remains enabled, so a cache miss cannot trigger a
download. The same-attempt small model also uses explicit `weights_only=True`.
Reviewed HuBERT/RMVPE operator bytes require exact manifest evidence and use the
separate explicit `weights_only=False` trust mode; no global Torch loader override
may collapse these per-call policies.

An upstream MIT source license does not establish redistribution rights for
models. A real release must review and record every weight/tool license and
source. `UNKNOWN`, `NOASSERTION`, `TBD` and empty values are rejected.

## Verify and build

Verification does not need Docker or a GPU:

```bash
infra/worker/runtime/build-runtime-image.sh \
  --source-archive /offline/source/rvc-source.tar.gz \
  --source-manifest /offline/source/source-manifest.json \
  --wheelhouse /offline/wheelhouse \
  --assets /offline/assets \
  --verify-only
```

For a build, use a clean repository with a 40-hex committed HEAD on an amd64
Docker daemon, preload the reviewed base digest locally, and add:

```bash
infra/worker/runtime/build-runtime-image.sh \
  --source-archive /offline/source/rvc-source.tar.gz \
  --source-manifest /offline/source/source-manifest.json \
  --wheelhouse /offline/wheelhouse \
  --assets /offline/assets \
  --base-image 'pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:<reviewed-amd64-digest>' \
  --tag rvc-orchestrator-worker:<version> \
  --output-manifest /offline/output/rvc-runtime-build.env
```

The generated build manifest and image labels record the base, source,
wheelhouse, asset, fairseq and RVC pins. The builder also inventories every
allowlisted source, config and model input used by the native private projection.
Worker, contracts and runtime-helper files are exported from the exact Git commit
rather than recursively copied from the host working tree. Ignored caches and
dirty bytes therefore cannot be hidden under the committed revision label. The
build is fixed to `linux/amd64`; a non-amd64 daemon fails before image creation.
`projection-manifest.json` fixes each path, byte size, SHA-256 and source mode;
its SHA-256 is repeated in `projection-manifest.sha256`, the runtime build
manifest as `RVC_PROJECTION_MANIFEST_SHA256`, and the image's
`org.rvc-orchestrator.rvc.projection.sha256` label. Worker startup and every claim
require all four provenance layers and the current source bytes to agree.

`GPU_SMOKE_VERIFIED=false` is intentional, as is
`PROFILE_STAGE_SET_VERIFIED=false`: the image proves offline packaging and CPU
imports, not a complete production stage profile. The Dockerfile performs an
import/asset/FFmpeg CPU-level preflight during its network-disabled build. At
container startup, the entrypoint runs the same preflight without
`--allow-no-gpu`, so a real-runtime Worker refuses to start without a visible
CUDA device. While the GPU smoke flag is false it also requires
`RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED=true`; the installer writes that value
only after `--allow-unverified-gpu-runtime` is given explicitly.

The 2.6.0/cu124 lock is therefore still not a released runtime. The actual amd64
base digest has not been selected and reviewed in this repository, and the full
GPU/no-network, vulnerability, container, SBOM, redistribution-license, and
clean-host lifecycle gates remain open.

## Optional Worker bundle inclusion

The installer bundle includes a real runtime only when the image, original
asset root/manifest, and build manifest all agree:

```bash
installers/worker/build-bundle.sh \
  --version <version> \
  --self-contained \
  --include-rvc-runtime-image rvc-orchestrator-worker:<version> \
  --rvc-runtime-assets /offline/assets \
  --rvc-runtime-asset-manifest /offline/assets/assets-manifest.json \
  --rvc-runtime-build-manifest /offline/output/rvc-runtime-build.env
```

The runtime image must use the exact tag selected by the installer. The bundle
builder exports its infra, installer, verifier, documentation and supply-chain
inputs from the same clean committed Git revision instead of copying mutable host
working-tree bytes. It validates the complete runtime build-manifest schema even
before qualification, re-hashes every asset, checks all provenance labels including the
projection-manifest SHA-256, verifies amd64, and then stores a Docker image
archive plus asset/build manifests. Generic `--include-image` cannot duplicate
the runtime image. The resulting manifest carries
`RVC_PROJECTION_MANIFEST_SHA256`, sets `RVC_NATIVE_RUNNER_AVAILABLE=true`, and
keeps `RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false`. The disabled activation is
also explicitly stored as mode `0444`; it does not depend on the host checkout's
file mode.

After the real matrix has run, provide both the strict qualification JSON and its
49-report evidence archive. The bundle builder validates them against the exact
post-build image ID and runtime/asset manifests, then generates (never accepts)
the activation projection:

```bash
installers/worker/build-bundle.sh \
  --version <version> \
  --self-contained \
  --include-rvc-runtime-image rvc-orchestrator-worker:<version> \
  --rvc-runtime-assets /offline/assets \
  --rvc-runtime-asset-manifest /offline/assets/assets-manifest.json \
  --rvc-runtime-build-manifest /offline/output/rvc-runtime-build.env \
  --rvc-runtime-qualification /offline/review/runtime-qualification.json \
  --rvc-runtime-qualification-evidence /offline/review/runtime-evidence.tar.gz
```

The generated `runtime-activation.json` is mode `0444`, mounted at the fixed
read-only container path, and rechecked against the installed image, asset,
qualification, and evidence bytes. Environment, YAML, and CLI inputs cannot
choose another activation path. See `docs/RUNTIME_QUALIFICATION.md` for the exact
case IDs and schema.

Bundle format v2 is a closed-world image contract. A self-contained Worker has
exactly one `runtime` image; pre-load archive hash/size, Docker-save inventory,
reference, image/config digest, linux/amd64 and release labels are checked, then
the loaded identity is checked again. Installed start/restart/run/create paths repeat
manifest/environment/loaded-identity verification and use
`RVC_IMAGE_PULL_POLICY=never`. A partial bundle instead records
`SELF_CONTAINED=false` and an empty v2 image inventory; it is not an air-gapped
runtime. The Worker package does not provide an automated rollback script.

## Release gates still open

CPU-level/static validation is not GPU training validation. Do not promote the
image until all gates in `docs/RVC_RUNTIME_MATRIX.md` pass, including:

- digest review of the actual amd64 2.6.0/CUDA 12.4 base and complete
  vulnerability, container, license, and SBOM review;
- fairseq/FAISS/Torch/cuDNN imports on the target GPU host;
- v1/v2 × 40k/48k × F0/non-F0 one-epoch smoke tests;
- RMVPE CPU/GPU and multi-GPU device routing;
- checkpoint, deployable weight, feature matrix and index semantics;
- full GPU/no-network matrix validation of the guarded job-local native adapter,
  manifest-pinned CREPE pre-bind, and all four Fixed-TestSet F0 methods;
- a complete 49-case qualification/evidence archive reviewed for the exact image
  and projected through the installer without any editable activation flag;
- clean-VM offline rebuild and Worker installer lifecycle tests.

# RVC runtime 호환 및 출시 gate

마지막 검증: 2026-07-13

이 문서는 Worker Agent 컨테이너와 실제 GPU 학습 runtime을 구분한다. 현재
`apps/worker/Dockerfile`은 protocol/Fake runner 검증용 Agent image이며 PyTorch,
CUDA user-space library, FAISS, fairseq, RVC source와 model asset을 포함하지 않는다.
따라서 GPU가 Compose에 노출돼도 이 image만으로 실제 학습할 수 없다.

dev.20 Manager 후보는 application/dependency 8개 image를 self-contained archive로 묶었지만,
Worker dev.20은 image/runtime이 없는 config-only partial이다. RVC source/asset, Torch/CUDA runtime과
49-case qualification 증적을 포함하지 않았으므로 아래 GPU/native gate는 계속 모두 닫혀 있다.

## 검토한 upstream 기준

- Repository: [RVC-Project/Retrieval-based-Voice-Conversion-WebUI](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI)
- Command adapter pin: `7ef19867780cf703841ebafb565a4e47d1ea86ff`
- Commit 시각: 2024-11-24T23:09:44+08:00
- Upstream `pyproject.toml`: Python `^3.9`, Torch/Torchaudio `2.4.0`,
  Torchvision `0.19.0`
- [PyTorch 공식 이전 버전 표](https://pytorch.org/get-started/previous-versions/)는
  Torch/Torchvision/Torchaudio `2.6.0/0.21.0/2.6.0`의 CUDA 12.4 wheel 조합을 제공한다.
- [NVIDIA Container Toolkit 지원 표](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/supported-platforms.html)는
  Ubuntu 22.04를 지원 대상으로 포함한다.

Upstream의 일반 `requirements.txt`는 `numba==0.56.4`, `llvmlite==0.39.0`,
`numpy==1.23.5` 같은 오래된 조합을 포함하고 Torch 자체는 고정하지 않는다. 반면
Poetry metadata는 Torch 2.4.0을 고정한다. source verifier는 이 2.4 marker를 reviewed
commit archive의 변경 감지 기준으로 계속 검사하지만 release runtime에 설치하지 않는다.
release 후보는 별도 exact lock과 hashed wheelhouse를 사용하며, 두 upstream 파일을 섞거나
현재 최신 Python/CUDA로 자동 갱신하지 않는다.

## 첫 GPU runtime 검증 후보

| 계층 | 후보 | 상태 |
|---|---|---|
| OS/container | Ubuntu 22.04 x86_64 | base family 선택, 실제 amd64 digest 미고정 |
| Python | 3.11 | offline packaging 후보, image build/GPU matrix 미검증 |
| Torch | `2.6.0+cu124` | exact 후보 lock, 실제 GPU/취약점 검토 미완료 |
| Torchvision | `0.21.0+cu124` | exact 후보 lock |
| Torchaudio | `2.6.0+cu124` | exact 후보 lock |
| CUDA wheel/runtime | cu124 / 12.4 | 공식 wheel 조합 확인, 실제 GPU smoke 미검증 |
| Base family | `pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime` | reviewed linux/amd64 digest 미선정 |
| RVC source | `7ef1986…` | CLI source 검토 및 command snapshot 완료 |
| FAISS/fairseq | upstream pin 기반 | build/import/GPU 학습 미검증 |

위 표는 출시 조합이 아니라 첫 검증 후보다. exact runtime lock과 verifier는 구현됐지만
Docker base의 실제 linux/amd64 digest, 완전한 wheel/asset hash 입력과 T4/Ampere 이상 NVIDIA
GPU의 v1/v2 40k/48k, F0/non-F0 조합을 아직 실행하지 않았다. vulnerability/container/secret
scan과 파일별 재배포 라이선스 검토도 남아 있으므로 release image로 표시하지 않는다.
실제 runtime image build는 40-hex clean committed orchestrator source, release source closure와
amd64 Docker daemon을 요구한다. Worker/contract/runtime build byte는 working tree `cp -R`가 아니라
해당 commit의 Git archive에서만 가져오므로 ignored cache나 dirty source가 image label의 commit과
어긋날 수 없다. 외부 source/wheel/asset의 `--verify-only` 경로는 Docker/GPU 없이 계속 사용할 수 있다.
Self-contained Worker bundle byte도 같은 commit의 Git archive에서만 stage하며 runtime build manifest의
전체 스키마와 release/orchestrator identity를 qualification 전에도 검증한다. Disabled와 qualified
activation은 모두 archive에서 `0444`다. 이 상태만으로 GPU/profile/Sample gate를 열지는 않는다.
[PyTorch serialization 문서](https://docs.pytorch.org/docs/stable/notes/serialization.html)의
2.6 기본 동작에 의존하지 않고 loader마다 `weights_only`를 명시한다. `>=2.6`이라는 버전 숫자나
checksum만으로 operator-trusted pickle의 안전성과 전체 RVC 호환성이 증명됐다고 보지 않는다.

## 필요한 model asset

실제 profile/native mode는 최소 다음 asset을 시작 시 검증해야 한다. `native`는
`source_root/assets-manifest.json`의 commit, file size, SHA-256과 executable mode를 생성
시점과 Job claim 직전에 다시 확인한다. code/config와 아래 asset 전체는 build-generated
projection manifest에도 기록되고 manifest hash는 image label/runtime manifest에 결박된다.

- `assets/hubert/hubert_base.pt`
- `assets/rmvpe/rmvpe.pt` (RMVPE 사용 시)
- `runtime/crepe/full.pth` (CREPE `full` capacity의 고정 offline model)
- `assets/pretrained/{f0,}G40k.pth`, `{f0,}D40k.pth`
- `assets/pretrained/{f0,}G48k.pth`, `{f0,}D48k.pth`
- v2의 동일 matrix under `assets/pretrained_v2`
- upstream `logs/mute`의 version/sample-rate별 mute fixture

각 파일은 source URL, license/redistribution 판단, byte size와 SHA-256을 별도 asset
manifest에 기록해야 한다. 공식 source가 MIT라는 사실만으로 외부 model weight의
재배포 권리를 추정하지 않는다. 권리와 checksum이 확정되기 전 installer는 asset을
bundle에 포함하지 않고 operator가 검증된 cache를 제공하도록 해야 한다.

## 출시 전 필수 검증

Worker의 guarded `native` mode는 reviewed source를 attempt-local allowlist projection으로
복제하고 preprocess/F0/feature/train/index/small-model 단계를 연결한다. claim 직전 현재
visible GPU index, source commit과 asset manifest를 다시 확인하며 profile/native 모두
lease-bound Dataset materializer를 사용한다. 이는 공유 checkout에 쓰지 않는 실행 경계와
argv/timeout/cancel 계약을 검증한 것이며, 아래 GPU/runtime gate를 통과했다는 뜻은 아니다.
첫 private projection도 build manifest의 expected size/mode/hash와 일치하는 FD byte만
원자 게시하고 이후 stage마다 다시 검사한다.
중앙 TestSet 원장과 lease-bound claim/item download, Worker atomic PCM materializer는 구현됐다.
Manager는 ready manifest object, Job sample-plan, canonical item ledger identity/namespace와
capability/inference method를 claim 때 다시 검증하고 item GET을 current Worker/lease/attempt에
결박한다. Worker도 external 307 credential/cookie 분리, exact size/SHA/PCM과 전체 디렉터리
replay를 검증한다. Pinned RVC Pipeline 기반 PM/Harvest/RMVPE inference, model/index/input/operator
asset FD/SHA 재검증, shell-free subprocess, deterministic manifest/publication, canonical Artifact와
중앙 Sample 등록/completion도 fixture 수준으로 연결했다. CREPE도 고정
`runtime/crepe/full.pth`를 strict asset manifest/private projection에서 검증하고
`torchcrepe.Crepe("full")`에 `weights_only=True` strict state dict를 pre-bind하는 경로가
연결됐다. 같은-attempt small model은 `weights_only=True`, manifest-verified HuBERT/RMVPE
operator byte는 명시적 `weights_only=False`로 분리하며 attempt-private `TORCH_HOME`과 offline
환경으로 network/cache fallback을 막는다. 실제 2.6.0/cu124 image의 GPU/no-network smoke는
남아 있다. Builder-generated qualification projection과 production factory/capability 경로는
연결됐지만 현재 실제 qualification 증적은 없다. 따라서 현재 Agent는
`supported_inference_f0_methods=[]`, `fixed_test_set_inference_ready=false`를 광고해 Manager가
sample Job을 배정하지 않으며 방어적으로 workspace 생성 전에도 거부한다. 그러므로
`AUTO_SAMPLE_JOBS_ENABLED=false`, `RVC_GPU_SMOKE_VERIFIED=false`,
`PROFILE_STAGE_SET_VERIFIED=false`, `RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false`를 유지한다.

1. 모든 base image를 manifest digest로 고정하고 amd64를 확인한다.
2. exact Torch `2.6.0+cu124`/Torchvision `0.21.0+cu124`/Torchaudio `2.6.0+cu124`
   wheelhouse와 호출별 serialization trust, RVC/fairseq/FAISS/torchcrepe 호환성을 검증한다.
3. network 없는 rebuild에 사용할 wheel/image/asset cache와 내부 checksum을 만들고, clean committed
   orchestrator Git archive와 amd64 build host에서 같은 image identity가 나오는지 검증한다.
4. `torch.cuda.is_available()`, cuDNN, fairseq, FAISS, ffmpeg import/smoke를 수행한다.
5. v1/v2 40k/48k 및 F0/non-F0 command snapshot과 1-epoch GPU smoke를 수행한다.
6. RMVPE CPU/GPU와 multi-GPU shard ID가 실제 visible device에 맞는지 확인한다.
7. `G_*.pth`/`D_*.pth`, `weights/<exp>.pth`, `total_fea.npy`, `added_*.index`를
   의미별로 검증한다.
8. ordered 고정 TestSet 전체에 대해 `pm|harvest|crepe|rmvpe`, v1/v2·40k/48k·index 조합의
   inference output을 검증한다. no-index 조합은 `index_rate=0`만 사용하며, canonical
   Artifact→Sample 등록→Job completion을 확인한다.
9. image closure v2가 self-contained Manager의 exact 8 roles와 Worker의 exact 1 runtime role,
   version-scoped dependency alias, `pull_policy=never`, load 전후/start/rollback identity를
   보존하는지 실제 archive와 clean host에서 검증한다.
10. 설치 bundle의 asset manifest, SBOM, vulnerability/container/secret scan, license report와
    외부 SHA-256을 검증한다.
11. 위 결과를 `RUNTIME_QUALIFICATION.md`의 exact 49-case report archive로 만들고 runtime
    image/build/asset identity에 결박한 qualification projection이 설치/start/Worker binding을
    통과하는지 확인한다.

이 gate가 모두 통과하기 전 offline runtime bundle은 guarded 개발 후보이며 실제 RVC
학습 설치 파일로 출시하지 않는다. 설치기는 `RVC_NATIVE_RUNNER_AVAILABLE=true`인 bundle만
native로 선택하고, 현재 `RVC_GPU_SMOKE_VERIFIED=false`에서는
`--allow-unverified-gpu-runtime` 없이는 시작하지 않는다.

# RVC Orchestrator Worker

GPU 학습 서버에서 Manager의 작업 lease를 받아 격리된 workspace에서 실행하는
outbound agent다.

기본 runner는 실제 학습을 하지 않는 `fake`다. 기존 allowlist command profile은
`RVC_RUNNER_MODE=profile`, reviewed offline runtime의 typed adapter는
`RVC_RUNNER_MODE=native`로 각각 명시해야 한다. `native`는 commit을 설정으로 바꿀 수 없고
`7ef19867780cf703841ebafb565a4e47d1ea86ff`만 허용하며, source root는 기본
`/opt/rvc-webui`인 절대 경로로만 구성할 수 있다.

Worker는 모든 산출물을 로컬 URI 메타데이터로 등록하지 않는다. lease/attempt에
묶인 upload session을 만든 뒤, Manager가 제공한 Local 또는 S3/MinIO URL로 원본
byte를 streaming PUT하고 finalize를 호출한다. Manager의 전체 크기·SHA-256 검증과
canonical 저장이 성공한 artifact만 Job 완료 조건으로 인정된다. PUT과 finalize는
취소 가능하며, 재시도 시 같은 멱등키를 사용한다. 로그·metric도 먼저 private disk
spool에 기록한 뒤 at-least-once로 재전송한다.

실제 runner는 claim의 `dataset_transfer`에 포함된 Manager 상대 경로에서
`prepared_flat.zip`을 받는다. Worker bearer/lease/attempt는 Manager 요청에만 전송하고,
S3 307은 한 번만 수동 처리해 외부 host에 Authorization을 전달하지 않는다. HTTPS에서
HTTP로의 downgrade, userinfo/fragment/redirect chain, Content-Length·size·SHA 불일치를
거부하고 mode `0600` partial을 fsync한 뒤 원자 게시한다. validation과 extraction은
canonical archive도 다시 traversal/symlink/duplicate/CRC/file-count/byte/compression
상한으로 검사하며 `inputs/prepared_flat`만 materialize한다.

실제 profile/native 학습은 RVC/CUDA runtime과 검증된 model asset이 모두 있어야 한다.
`native` 생성과 각 claim 직전 asset manifest 전체 byte/hash, source commit과 build-generated
projection manifest를 검증하며 claim의 training 및 RMVPE GPU ID를 그 시점의
`nvidia-smi` capability index와 다시 대조한다. 불일치하면 subprocess나 Dataset stage 전에
Job을 실패시킨다.

`rvc_worker.native_runner.PinnedRvcRunner`는 실제 stage 연결을 위한 typed adapter다.
검토한 commit의 `infer`, `configs`, pretrained/HuBERT/RMVPE와 mute asset만 attempt의
`work/rvc`로 복제한다. source path/size/SHA-256/mode는 image build 때 만들어 image label과
bundle에 결박한 inventory여야 한다. 복제 시 각 source를 `O_NOFOLLOW`로 열고 같은 file
descriptor에서 검증한 byte만 mode `0444` private tree에 원자 게시하며, 각 stage 전 private
inventory도 다시 검증한다. upstream 명령은 이 projection 안에서만 실행하고 공유 image
checkout의 `logs`, `assets/weights`, `weights`에는 쓰지 않는다.
preprocess, optional 병렬 F0, v1/v2 feature, training input 준비, G/D pair, deterministic
index CLI, 기존 deployable weight 또는 공식 small-model 추출, checksum/config/environment
manifest를 연결하며 각 stage output을 workspace 내부 regular file로 재검증한다.

Dataset download/checksum/materialization dependency는 profile/native Worker stage 앞에
동일하게 연결된다. `native`에서 `auto_inference_samples.enabled=false`이면
Dataset→preprocess→F0/feature→train→checkpoint/index/small model→evaluation/manifest 전체
stage를 실행한다. sample-enabled 경로는 lease-bound ordered TestSet PCM을 원자 materialize하고,
pinned RVC Pipeline으로 PM/Harvest/CREPE/RMVPE를 실행한다. 모델·인덱스·입력·operator asset은
`O_NOFOLLOW` FD와 SHA-256으로 다시 검증하며 shell 없는 격리 subprocess, 전체 deadline,
cancel join과 출력 byte/duration 상한을 적용한다. 결과 model/index/WAV에는 runtime
image/asset digest, native manifest/request SHA-256과 역할을 붙여 Manager upload/finalize 뒤
각 논리 Sample을 등록한다. 동일 PCM은 한 canonical Artifact로 dedupe하되 item별 등록 순서는
보존한다. 단일 출력은 256 MiB/600초, 한 attempt의 논리 출력 합계는 2 GiB/3,600초를 넘을 수
없고 driver·publication·Manager 원장 경계에서 각각 다시 확인한다.

CREPE는 고정 `runtime/crepe/full.pth`를 strict asset manifest와 build-generated private
projection의 exact inventory로 요구한다. runner와 driver는 이 경로를 `O_NOFOLLOW` FD의
size/SHA-256과 함께 inference 전·중·후에 재검증한다. driver는 명시적
`torch.load(..., weights_only=True)`로 strict state dict를 읽어
`torchcrepe.Crepe("full")`에 적용하고 `eval()`/device 이동 뒤
`torchcrepe.infer.model`과 `capacity="full"`을 pre-bind한다. attempt-private
`TORCH_HOME`, `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`을 사용하므로 package cache나
network fallback을 허용하지 않는다. 같은-attempt small model도 `weights_only=True`, manifest로
검증한 operator HuBERT/RMVPE byte는 별도 `weights_only=False` trust mode만 사용하며 전역
loader override로 두 경계를 합치지 않는다.

upstream `pyproject.toml`의 Torch 2.4/Torchvision 0.19/Torchaudio 2.4 marker는 reviewed source
archive 검증 기준으로 남는다. release runtime 후보 lock은 별도로 Torch `2.6.0+cu124`,
Torchvision `0.21.0+cu124`, Torchaudio `2.6.0+cu124`, CUDA runtime `12.4`다. 실제 amd64 base
digest, GPU/no-network 전체 matrix, 취약점·container·라이선스 검토는 아직 완료되지 않았다.
Production factory는 builder-generated mode `0444` activation, 현재 asset hash와 dependency
binding이 모두 일치할 때만 Sample dependency와 네 inference F0 capability를 연다. Activation
경로는 고정이며 env/YAML/CLI override는 거부한다. 현재 실제 qualification 증적이 없으므로
Agent는 계속 빈 inference F0 목록과 `fixed_test_set_inference_ready=false`를 광고하고,
Manager의 `AUTO_SAMPLE_JOBS_ENABLED=false`와 runtime/image/bundle의
`RVC_GPU_SMOKE_VERIFIED=false`, `PROFILE_STAGE_SET_VERIFIED=false`,
`RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false`를 유지한다. safe-loader 코드와 후보 lock이 있다는
이유만으로 이 gate를 열지 않는다.

Agent/Fake image의 Python dependency는 `requirements.lock`의 exact version으로 wheel을
만든 뒤 runtime stage에서 `--no-index`로 설치한다. 실제 RVC image는 이 lock을 재사용하지
않고 `infra/worker/runtime`의 별도 hash·license 검증 wheelhouse를 사용한다.

`rvc_worker.rvc_commands`에는 공식 upstream commit
`7ef19867780cf703841ebafb565a4e47d1ea86ff`에서 확인한 전처리, 5종 training F0,
v1/v2 HuBERT feature, multi-GPU 학습 argv 빌더가 있다. `shell=True` 문자열을
복사하지 않으며 Worker가 보고한 GPU ID 밖의 요청을 subprocess 생성 전에
거부한다. `rvc_worker.training_inputs`는 정렬된 `filelist.txt`와 job-local
`config.json`을 만들고, `rvc_worker.training_metrics`는 train log 및 TensorBoard
scalar를 중앙 metric 이름으로 정규화한다. 설치 예시 profile은 문법 확인용 최소
파일이다. guarded native mode도 CREPE를 포함한 전체 GPU/no-network matrix를 검증한 release가 아니므로
installer/runtime의 acknowledgement와 open gate를 제거해서는 안 된다.

self-contained Worker installer의 image closure v2는 `runtime` image 정확히 하나만 허용한다.
archive/reference/image ID/config digest/linux-amd64/release label을 load 전후에 검증하고,
설치된 start/restart와 rollback에서도 manifest·env·loaded identity를 다시 확인한다. 이 경로는
`RVC_IMAGE_PULL_POLICY=never`이므로 registry pull로 누락 또는 변조된 image를 보충하지 않는다.
partial bundle은 빈 v2 image inventory와 `SELF_CONTAINED=false`를 명시하며 실제 runtime
installer가 아니다.

active Job의 시스템 메트릭은 claim/session 시작 직후 새로 관측하고 이후
`SYSTEM_TELEMETRY_INTERVAL_SECONDS` 주기로 heartbeat와 독립적으로 private spool에 남긴다.
`system.gpu.telemetry_available=1`이면서 `system.gpu.count=0`이면 GPU 질의는 성공했지만 장치가
없는 것이고, availability가 0이면 `nvidia-smi` 실행·파싱·의미 검증에 실패한 것이다. 잘못된
GPU 값은 heartbeat를 죽이지 않고 unavailable로 fail-safe 처리한다. 필수 spool 저장 실패는
`failed / telemetry_persistence_failed`이며, 정상 terminal 전에는 producer를 봉인한 뒤 마지막
best-effort flush를 수행한다. Manager가 일시적으로 503을 반환하면 bounded pending record는
terminal watermark 아래 late replay를 위해 보존한다.

## 단계 실패와 재시도 경계

`StageExecutor`는 모든 stage를 정확히 한 번만 호출한다. training, checkpoint, index 또는
다른 stage가 실패해도 같은 attempt에서 stage 전체를 자동 재실행하지 않는다. 부분 산출물을
이어 붙일 수 있는 유일한 retry는 이미 원자·멱등 경계를 가진 Dataset download와 Artifact
upload 내부의 설정된 횟수뿐이다. telemetry 전송의 일시 장애는 private durable spool에
보류하고 후속 flush/restart에서 재전송하며 stage를 다시 실행하지 않는다.

terminal 실패는 exception class 이름이나 원문 대신 다음 고정 code를 사용한다.

| code | 의미와 retry 정책 |
|---|---|
| `stage_timeout` | 해당 subprocess timeout; 같은 attempt 자동 retry 없음 |
| `stage_process_failed` | subprocess nonzero/start 실패; 같은 attempt 자동 retry 없음 |
| `exhausted_transient` | Dataset/Artifact 또는 Manager transient 경계가 허용 횟수를 소진함 |
| `stage_integrity_failed` | workspace, Dataset, RVC source/asset/output 무결성 실패; nonretryable |
| `stage_configuration_invalid` | claim/RVC/GPU 설정 불일치; nonretryable |
| `worker_runtime_unready` | Worker의 pinned runtime/asset 준비 증명을 하지 못함; nonretryable |
| `stage_remote_rejected` | Manager의 deterministic protocol 거부; nonretryable |
| `telemetry_persistence_failed` | 필수 telemetry를 local spool에 보존하지 못함; nonretryable |
| `stage_internal_error` | 알 수 없는 예외의 fail-closed fallback; nonretryable |

cancel, shutdown과 lease loss는 오류 분류보다 우선하며 Job은 `cancelled`가 된다. 각 typed
오류의 `cause`도 고정 enum이며 terminal `error_message`는 stage와 일반 동작만 설명한다.
exception 원문, argv, 실행 파일/local workspace path, Manager/object URL query와 token은
terminal payload나 Agent log에 복사하지 않는다. 사용자가 다시 실행하려면 Manager의 retry로
새 `JobAttempt`를 생성해야 하며 이전 attempt 산출물과 telemetry는 보존한다.

주요 환경 변수:

- `MANAGER_URL`
- `WORKER_NAME`
- `WORKER_TOKEN_FILE` (권장) 또는 `WORKER_TOKEN`
- `WORKER_CREDENTIAL_PATH` (기본 `DATA_ROOT/credentials/worker.json`)
- `DATA_ROOT`
- `RVC_RUNNER_MODE=fake|profile|native` (기본 `fake`)
- `RVC_PROFILE_PATH` (`profile` 모드에서 필수)
- `RVC_NATIVE_SOURCE_ROOT` (`native` 기본 `/opt/rvc-webui`, 절대 경로)
- `RVC_NATIVE_PYTHON_EXECUTABLE` (기본 현재 Python, runtime image는 `/opt/conda/bin/python`)
- `RVC_NATIVE_CPU_WORKERS`, `RVC_NATIVE_DEVICE`, `RVC_NATIVE_USE_HALF`
- `RVC_NATIVE_{PREPROCESS,EXTRACTION,TRAINING,INDEX,SMALL_MODEL}_TIMEOUT_SECONDS`
  (0보다 큰 finite 숫자만 허용)
- `RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED` (runtime image 직접 시작 시 명시적 gate)
- `HEARTBEAT_INTERVAL_SECONDS`
- `SYSTEM_TELEMETRY_INTERVAL_SECONDS` (기본 60초, 허용 범위 10~3600초; heartbeat와 독립)
- `POLL_INTERVAL_SECONDS`
- `LEASE_RENEW_INTERVAL_SECONDS`
- `TELEMETRY_SPOOL_MAX_BYTES` (기본 256 MiB)
- `ARTIFACT_UPLOAD_TIMEOUT_SECONDS` (PUT/finalize 기본 3600초)
- `ARTIFACT_UPLOAD_MAX_ATTEMPTS` (기본 3)
- `ARTIFACT_MAX_OBJECT_BYTES` (기본 5 GiB)
- `ARTIFACT_MAX_FILES_PER_ATTEMPT` (기본 256)
- `ARTIFACT_MAX_TOTAL_BYTES_PER_ATTEMPT` (기본 100 GiB)
- `ARTIFACT_CHECKPOINT_RETENTION` (G/D 각각 최신 20개)
- `DATASET_DOWNLOAD_TIMEOUT_SECONDS` (기본 3600초)
- `DATASET_DOWNLOAD_MAX_ATTEMPTS` (기본 3)
- `DATASET_MAX_ARCHIVE_BYTES` (기본·상한 5 GiB)
- `DATASET_MAX_ENTRIES` (기본 10,000)
- `DATASET_MAX_FILE_BYTES` / `DATASET_MAX_TOTAL_BYTES` (기본 2/20 GiB)
- `DATASET_MAX_COMPRESSION_RATIO` (기본 200)

실행:

```bash
python -m rvc_worker --config /etc/rvc-worker/config.yaml
python -m rvc_worker --config /etc/rvc-worker/config.yaml --check
```

Worker token은 Agent를 중지해 idle/no-active-lease를 보장한 뒤 회전한다. `--rotate-token`은
prepare 응답을 credential schema v2 파일에 fsync한 뒤 activate하고 old token의 즉시 폐기를
확인한다. admin emergency revoke 뒤에는 기존 credential metadata와 bootstrap token을 보존한
host에서 inactive-only `--re-enroll`을 실행한다. 두 명령은 token을 stdout/log에 출력하지 않는다.

```bash
python -m rvc_worker --config /etc/rvc-worker/config.yaml --rotate-token
python -m rvc_worker --config /etc/rvc-worker/config.yaml --re-enroll
```

테스트는 저장소 루트에서 contracts source와 Worker source를 함께 경로에 둔 뒤
실행할 수 있다.

```bash
PYTHONPATH=packages/contracts/src:apps/worker/src \
  python -m unittest discover -s apps/worker/tests -t apps/worker -v

# typed pinned-stage adapter fixture만 실행
.venv/bin/pytest -q apps/worker/tests/test_native_runner.py

# PM/Harvest/CREPE/RMVPE safe-loader와 manifest/replay 증거 경계
.venv/bin/pytest -q apps/worker/tests/test_native_inference.py

# Dataset download/redirect/cancel/archive 방어
.venv/bin/pytest -q apps/worker/tests/test_dataset_transfer.py

# 단계별 typed error, 취소 우선순위와 no-replay 정책
.venv/bin/pytest -q apps/worker/tests/test_stage_errors.py
```

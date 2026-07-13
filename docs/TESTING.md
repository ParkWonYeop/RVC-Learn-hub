# 테스트 안내

## 빠른 검증

저장소 루트에서 다음 명령을 실행한다.

```bash
make check
```

이 명령은 다음을 순서대로 검증한다.

- Python Ruff
- Python mypy strict
- localhost socket이 필요 없는 Python unit/integration test
- Frontend Vitest, ESLint와 Next.js production build
- installer/infra와 opt-in recovery fixture의 shell 문법
- Git whitespace 오류

HTTP E2E, Docker image build/Compose 기동, 실제 installer, recovery Docker drill과 GPU/RVC 학습은
`make check` 범위가 아니며 아래의 별도 명령과 사용자 [테스트 가이드](TEST_GUIDE.md)를 따른다.

## localhost Manager↔Fake Worker HTTP protocol E2E

```bash
make test-e2e
```

이 테스트는 임시 SQLite와 실제 `127.0.0.1` Uvicorn, 명시적으로 opt-in한 Fake Worker fixture를
사용한다. 제한된 sandbox에서는 localhost bind 권한이 필요할 수 있다. Docker/GPU/native runtime,
설치형 production Manager 통합을 검증하지 않는다. 검증 범위는 다음과 같다.

- Fake Worker의 명시적 opt-in과 production 차단
- Worker bootstrap 등록, token hash와 0600 credential 저장
- server-owned Dataset upload/finalize, canonical binary GET과 Experiment/Job 생성
- atomic claim, attempt, lease renew와 전체 상태 전이
- 같은 Dataset의 PM/Harvest/RMVPE 조건 Job 세 개를 세 실제 Agent가 동시에 독립 claim·완료
- log/metric/artifact ingestion과 completion gate
- Job이 아직 `training`일 때 native-like runner callback의 sanitized stdout log,
  `current_epoch`와 `loss_g_total`이 실제 HTTP tail 조회에 보임
- 같은 training 중 visible GPU utilization, `system.gpu.telemetry_available=1`과
  `system.disk_free_bytes`가 HTTP API에 보임
- terminal 뒤 exclusive watermark가 저장 count와 일치하고 healthy Manager에서 pending/
  dead-letter가 비어 있음
- model/index 전 completion gate
- 완료 후 lease release와 상태 event 순서

현재 E2E는 token prepare/activate·revoke·re-enroll, 실제 heartbeat/terminal CAS conflict와
batch replay 멱등성을 직접 증명하지 않는다. 이 경계는 API/Worker 집중 suite에서 각각 검증한다.
Visible GPU와 training event도 fixture이므로 실제 NVIDIA/RVC 정확도 증거는 아니다.

## Dataset data plane

```bash
.venv/bin/pytest -q \
  apps/api/tests/test_dataset_upload_api.py \
  apps/api/tests/test_dataset_ingestion.py && (
  cd apps/web
  npm test -- --run \
    tests/dataset-bff.test.ts \
    tests/api-projections.test.ts
)
```

Dataset API 통합 test가 owner JWT `init → bounded PUT → finalize` Local data plane,
원본/`prepared_flat.zip`/manifest/report canonical 게시와 URI 비노출을 검증한다. Local PUT의
generation/write-token heartbeat, 절대 expiry 408, 취소 join과 replacement staging 격리,
finalize heartbeat, upload-session별 canonical no-replace, stale finalizer 격리, request 취소와
DB commit 전 실패/commit 후 오류의 durable outcome recovery도 포함한다. S3
presign/stream/publish 계약은 동일 storage adapter를 사용하는 artifact suite가 검증한다. 같은
payload 멱등성, 충돌, owner quota, 만료 generation, 타 owner 404, upload token, 악성 ZIP,
MIME/content signature/size/SHA 불일치, non-WAV `decoder_pending`, partial publish cleanup과
재시도, stale finalize token/heartbeat 회수, 참조/삭제 race와 Experiment/Job/claim readiness
gate도 포함한다. 같은 `local` backend의 다른 root에서는 init replay/PUT/finalize/delete와
Worker claim/GET이 fail-closed하고 원장/원래 object를 보존하는지, historical `UNBOUND`의
active adoption 거부와 completed Dataset 전체 object dry-run/apply/idempotent audit도 포함한다.
`e2f8b4c6a930` migration은 구 pending/finalizing row를 expired/retryable로 닫아 quota trap 없이
generation+1을 발급하고 completed legacy row는 보존하는 SQLite data upgrade/downgrade와
PostgreSQL offline SQL로 검증한다.
Dataset ingestion 회귀는 8/16/24/32-bit와 mono/stereo에서 실제 interleaved sample 수로
clipping/silence를 가중하고 `sqrt(Σ normalized_square / total_samples)` RMS를 검증한다. API 회귀는
`f9c4a7d2b610`의 aggregate all-null/all-present·bounds, historical null 보존, raw
`quality_report_json`/member path 비노출을 확인한다. Web BFF는 exact nested key/algorithm/count/finite
range를 검증하고 malformed upstream aggregate를 502로 닫으며 목록·상세의 historical-null 문구를
단위 검증한다.
LUFS 회귀는 BS.1770-4의 48 kHz K-weighting coefficient, 3초 1 kHz 기준 tone, 파일 경계를 넘지
않는 400 ms/75% overlap complete block과 Dataset-global `>-70 LUFS`/`-10 LU` 2단계 gate를
검증한다. 서로 다른 loudness 파일을 평균하지 않고 block energy로 집계하는지, 짧음·절대 gate
미만·지원 밖 channel layout/sample rate가 typed null인지 확인한다. `d8f2a6c4b901` migration은
기존 PCM row의 loudness all-null을 보존하고 새 complete/available/unavailable 상태의 count·finite
범위를 SQLite constraint와 PostgreSQL offline SQL에서 검증한다. BFF는 exact algorithm/scope/gate
metadata만 공개하고 malformed/NaN 상태를 fail-closed하며 목록·상세 UI가 값/사유/기존 행을
구분하는지 확인한다.
archive core suite는 traversal, symlink/special file, 암호화,
CRC, entry/byte/압축률 폭탄과 결정적 flat manifest를 검증한다.

Worker 수신 경계는 별도 suite가 Manager bearer/lease/attempt header, Local binary stream,
S3 307에서의 Authorization/lease/attempt 비전달을 검증한다. Manager가 설정한 domain cookie와
environment proxy credential도 external object host로 넘어가지 않도록 fresh client를 쓰며,
HTTPS downgrade/userinfo/fragment/상대 URL/redirect-chain 차단, Content-Length/size/SHA-256,
취소 partial 정리와 `O_NOFOLLOW`/0600 원자 게시를 검증한다.
canonical ZIP도 신뢰하지 않고 traversal, symlink, duplicate, CRC corruption과 bomb를 다시
검증한 뒤 flat workspace에 게시한다.

```bash
.venv/bin/pytest -q apps/worker/tests/test_dataset_transfer.py
```

## Experiment 안전 CRUD

```bash
.venv/bin/pytest -q \
  apps/api/tests/test_experiment_crud.py \
  apps/api/tests/test_experiment_migration.py
```

이 suite는 owner/admin 은닉, 이름 정규화·중복 race, immutable name/Dataset, description-only
`expected_row_version` CAS, Job·MLflow 참조 삭제 차단, stable pagination과 16 KiB declared/chunked
JSON 상한을 검증한다. migration은 historical duplicate name과 owner 없는 row를 합치거나
삭제하지 않고 conflict key `NULL`로 보존하며, 이미 유일한 owner/name만 unique key로 backfill한다.
SQLite upgrade/downgrade와 PostgreSQL offline SQL, Job→Experiment `RESTRICT`도 확인한다.

## Model registry와 explicit champion

```bash
.venv/bin/pytest -q \
  apps/api/tests/test_model_registry.py \
  apps/api/tests/test_migrations.py
.venv/bin/pytest -q \
  apps/api/tests/test_api.py \
  apps/api/tests/test_experiment_comparison.py
(
  cd apps/web
  npm test -- --run \
    tests/model-registry-bff.test.ts \
    tests/model-registry.test.ts \
    tests/model-registry-panel.test.ts \
    tests/experiment-run-comparison.test.ts
)
```

Manager suite는 exact current completed real attempt, `worker-claim-v1`, reviewed RVC commit과 승인
runtime image/asset pair가 없는 candidate를 거부해야 한다. Candidate 등록과 promotion 모두
Manager-verified `final_small_model` 및 서버가 infer한 same-attempt `final_index`의 canonical byte를
전체 size/SHA-256으로 재검증한다. Fake/running/failed/stale attempt, duplicate model/index,
`UNBOUND`/다른 namespace, object tamper와 storage timeout은 fail-closed한다. Owner/admin과 타 owner
`404` 은닉, 16 KiB declared/chunked body, actor-scoped idempotency replay/conflict, registry/entry CAS,
2~3개 concurrent promotion winner 한 개, candidate/approved revoke terminal, active revoke 후 no-fallback,
이전 champion approved inactive/rollback과 audit/private-field 부재를 확인한다.

Migration suite는 기존 JobAttempt provenance를 NULL로 보존하고 신규 claim만
`worker-claim-v1`/commit/runtime pair를 snapshot하는지, registry/entry/operation의 FK·unique·state
constraint와 SQLite upgrade/downgrade 및 PostgreSQL offline SQL을 검증한다. 과거 NULL row를 현재
Worker나 환경 설정으로 자동 승인하면 실패다.

Web suite는 cookie-only same-origin 고정 BFF, exact UUID/query/body와 4 KiB browser body, private
upstream field 제거, malformed 2xx `502`, safe `Retry-After`/`Idempotency-Replayed`, complete pagination
version fence를 검사한다. UI는 Fake action 부재, candidate/active champion/inactive approved/revoked
표시, 전체 checksum/runtime provenance, mutation 전 확인과 uncertain response 뒤 blind retry 금지를
검사한다. 실제 browser의 owner/admin/타 owner, response-loss 같은-key 재조정, 두 탭 동시 promotion,
keyboard/screen-reader, 실제 MinIO/S3 tamper·outage는 자동 단위 회귀와 별도 인수 gate다.

2026-07-13 dev.19 source 증적은 maintenance/installer/migration 결합 `124 passed`, 기존
registry+migration `33 passed`(registry 14, migration 19)다. Migration head는
`f5d1c8a9b240`이다. 이 SQLite/fixture 회귀는 실제 PostgreSQL multi-replica promotion 경쟁이나
MinIO/S3 대용량 object 재해시·장애 인수를 대신하지 않는다.

## 관리자 사용자 lifecycle

```bash
.venv/bin/pytest -q \
  apps/api/tests/test_user_lifecycle.py \
  apps/api/tests/test_user_lifecycle_migration.py \
  apps/api/tests/test_auth.py
(
  cd apps/web
  npm test -- --run \
    tests/admin-user-bff.test.ts \
    tests/admin-user-data.test.ts \
    tests/manager-api.test.ts
)
```

Manager suite는 admin-only list/detail/create, normalized email duplicate, 16자 관리 비밀번호 정책,
16 KiB body와 unknown field, row-version CAS, no-op replay, self-demotion/disable, token version에 의한
disable→reactivate와 password-reset 이전 token 영구 401을 검증한다. 두 admin의 동시 cross-demotion은
singleton write fence 뒤 한 요청만 성공하고 active admin 한 명을 보존해야 한다. Idempotency
operation/audit/response에는 password, hash, access-token version과 key 원문이 없어야 한다.
Migration suite는 기존 User byte를 보존한 SQLite upgrade/downgrade, constraint/index/FK/JSONB와
PostgreSQL offline SQL을 검증한다.

Web suite는 cross-origin, 임의 Manager path/field, oversized body와 unsafe/missing idempotency key를
upstream 전 차단하고, HttpOnly server token과 fixed path/body/key만 전달하는지 확인한다. 성공 응답은
public user field만 투영하며 password/auth-version/private field를 제거한다. Email/stale/self/
last-admin/idempotency conflict는 고정 code로 매핑하고 malformed success는 502다. 전체 pagination은
stable total, bounded progress와 unique ID를 요구한다. Next production build의 `/users`와 세 BFF
route 생성도 필수다. 실제 keyboard/screen-reader/반응형/browser session drill은 별도 수동/Playwright
gate다.

## Lease-bound TestSet transfer

```bash
PYTHONPATH=packages/contracts/src:apps/api/src:apps/worker/src .venv/bin/pytest -q \
  packages/contracts/tests/test_contracts.py \
  packages/contracts/tests/test_test_set_transfer_contracts.py \
  apps/api/tests/test_test_sets.py \
  apps/worker/tests/test_test_set_transfer.py \
  apps/worker/tests/test_dataset_transfer.py
```

계약 suite는 no-index sample의 `index_rate=0`,
family/revision/manifest/Job sample-plan/inference-config hash, ordered unique item과 exact
Job/item path, 128 item·2 GiB·총 3,600초 wire cap 및 capability true 조건을 검증한다.
Manager suite는 sample
readiness/F0 method가 맞지 않는 Worker의 미배정,
manifest object exact byte·DB sample-plan·completed upload metadata/key/URI/namespace 재검증,
DB sample-plan hash 단독 변조의 queued/Worker-unassigned 보존, storage-neutral claim, current
Worker/lease/attempt item GET과 mismatch 보존을 검증한다.

Worker suite는 Manager 요청에만 bearer/lease/attempt가 있고 외부 단일 307에는 그 header,
response cookie와 proxy credential이 없는지 확인한다. MIME/Content-Length/encoding/size/SHA,
unsafe redirect와 chain, cancellation partial cleanup, `O_NOFOLLOW`/0600/fsync atomic file 게시를
검사한다. materializer는 count/item/total/duration/rate/channel 상한, bounded retry/cancel,
RIFF/WAVE uncompressed PCM chunk 검사, ordered `inputs/test_set/<item-id>.wav` 전체 디렉터리 원자
게시, replay의 symlink/extra/stale/mode/hash/PCM 재검증과 sample-plan provenance marker를
검증한다. 이 transfer suite만으로 inference output이나 실제 GPU smoke를 인증하지 않는다.

Dataset/TestSet upload 정리는 `test_dataset_upload_api.py`, `test_test_sets.py`와
`test_maintenance.py`가 parent→session 잠금,
generation/write-token heartbeat, 절대 PUT expiry, finalization heartbeat와 no-replace publish,
task-aware exact RQ envelope/reconciler, namespace/generation-scoped first-delete와 confirmation
grace second-delete를 회귀한다. active/finalizing/completed session과 canonical key 보호도 포함한다.
실제 S3에서 기본 7일 grace보다 오래 열린 PUT과 PostgreSQL 다중 replica 경합은 이 fixture가
대체하지 않는다.

## Native Sample publication과 중앙 completion

```bash
.venv/bin/pytest -q \
  packages/contracts/tests/test_sample_contracts.py \
  apps/worker/tests/test_native_inference.py \
  apps/worker/tests/test_sample_publication.py \
  apps/api/tests/test_sample_registration.py \
  apps/api/tests/test_artifact_storage.py
```

이 범위는 PM/Harvest/CREPE/RMVPE driver request, FD/SHA asset 검증, shell-free subprocess,
timeout/cancel/output 상한, deterministic native manifest와 registration request를 검사한다.
Manager 쪽은 approved runtime attempt snapshot, Artifact 역할/manifest/request provenance,
동일 PCM 다중 Sample, Manager-computed PCM v2 지표, replay/conflict, completion 최종 fence,
canonical byte 재해시, same-origin credential 비전달, rate/body/concurrency/deadline을 검증한다.
취소된 PCM thread를 join하기 전 spool/semaphore를 해제하지 않는지와 object stream 취소 뒤 열린
handle/partial spool이 남지 않는지도 회귀한다. 이는 fixture 기반이며 CREPE나 실제
Torch/CUDA/GPU/no-network smoke가 아니다.

## MLflow projection

```bash
.venv/bin/pytest -q apps/api/tests/test_mlflow_integration.py
```

8개 test가 Experiment/Job/metric outbox hook, path/storage URI 제거, disabled/fail-open/
fail-closed readiness, commit 뒤 장애와 재동기화, 다른 projector가 claim한 fail-closed
event와 stalled probe timeout, 안전한 tracking URI/token file, 환경 proxy 무시와 redirect 차단 REST client,
parameter/metric/artifact-link/terminal projection 및 replay marker를 검증한다. REST server는
stateful `httpx.MockTransport`이므로 실제 MLflow container/PostgreSQL/MinIO 통합 smoke와
다중 API replica 최초 Run 생성 경쟁은 별도 배포 검증 항목이다.

## Docker image

```bash
docker build -f apps/api/Dockerfile -t rvc-orchestrator-api:test .
docker build -f apps/web/Dockerfile -t rvc-orchestrator-web:test .
docker build -f apps/worker/Dockerfile -t rvc-orchestrator-worker:test .
docker build -f infra/mlflow/Dockerfile -t rvc-orchestrator-mlflow:test .

for image in \
  rvc-orchestrator-api:test \
  rvc-orchestrator-web:test \
  rvc-orchestrator-worker:test \
  rvc-orchestrator-mlflow:test; do
  user=$(docker image inspect --format '{{.Config.User}}' "$image")
  case "$user" in
    ''|0|0:*|root|root:*) echo "root/default user: $image" >&2; exit 1 ;;
  esac
  printf '%s user=%s\n' "$image" "$user"
done

docker run --rm --network none \
  --tmpfs /var/lib/rvc-worker:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700 \
  -e MANAGER_URL=http://127.0.0.1:8000 \
  -e WORKER_NAME=image-check \
  -e WORKER_TOKEN=CONFIG_ONLY_DO_NOT_USE \
  -e DATA_ROOT=/var/lib/rvc-worker \
  -e MIN_FREE_DISK_BYTES=1 \
  rvc-orchestrator-worker:test --check | python3 -m json.tool
```

API/Web/Worker/MLflow image inspect는 비어 있거나 root인 user를 거부해야 한다. 위 Worker token은
network-none `--check`에만 쓰는 합성 문자열이며 실제 credential로 재사용하지 않는다. GPU와
`nvidia-smi`가 없는 일반 build host의 JSON은 `ok=true`, `gpu_available=false`를 보여야 한다.
이 명령은 실제 RVC/GPU test가 아니며 일반 CI에서 native 학습을 자동 실행하지 않는다.

MLflow의 실제 UID/GID, read-only rootfs, capability drop과 health 기동은 Docker daemon이 있는
환경에서 별도로 실행한다.

아래 Docker Make target과 runtime/release builder는 내부에서 plain `docker`를 호출하므로 같은
system daemon에 직접 접근하도록 승인된 CI/release 계정에서 실행한다. `sudo docker`만 허용되는
설치 계정에서 `sudo make` 또는 즉석 Docker group 변경으로 우회하지 않는다.

```bash
make test-mlflow-docker
```

이 smoke는 image user가 `10002:10002`인지 확인하고 network-none container를 read-only rootfs,
`cap-drop ALL`, no-new-privileges, PID 128과 UID-owned `/tmp` tmpfs로 실행한다. 소유자가 쓸 수 있는
home 쓰기가 실제로 실패하고 `/tmp` 쓰기, boto3/psycopg2/MLflow import와 `/health=OK`가 모두
성공해야 한다. 로컬 host architecture의 증거이므로 최종 `linux/amd64` release image 검증을
대체하지 않는다.

실제 installer 형태의 root 소유 source secret과 역할별 projection은 별도 격리 smoke로 확인한다.

```bash
make test-manager-secret-projection-docker
```

이 smoke는 root:root mode `0600` source를 initializer만 읽고 API, maintenance RQ, MLflow와
database-authz의 서로 다른 volume에 exact 파일·UID/GID·mode로 투영하는지 확인한다. A→B rotation,
API/maintenance credential collision, 빈 파일과
symlink 입력 거부 후 기존 B generation 보존, 실제 API/RQ/MLflow entrypoint의 non-root secret
read도 실행한다. PostgreSQL/Redis/MinIO server 연결을 가짜 성공으로 표현하지 않으며 MLflow의
최종 command만 DB 없이 secret read 경계를 확인하도록 대체한다.

MinIO identity의 bucket 권한은 다음 실제 server smoke로 확인한다.

```bash
make test-minio-policy-docker
make test-redis-acl-docker
```

격리 MinIO에서 Manager app은 Manager bucket에만, MLflow는 artifact bucket에만 write할 수 있어야
하고 maintenance identity는 두 staging prefix 삭제만 성공해야 한다. Init을 두 번 실행하고
의도적으로 붙인 built-in `readwrite`를 재실행이 제거하는지, sole expected policy와 bucket
versioning fail-closed도 확인한다. Redis smoke는 pinned Redis 7.4에서 maintenance ACL의 exact RQ
lifecycle을 실행하고 다른 key와 `FLUSHDB|CONFIG|ACL|MODULE|KEYS|SCAN|EVAL|PUBLISH` 거부를 확인한다.
세 smoke 모두 로컬 daemon architecture의 범위이며 clean Ubuntu amd64 전체 Compose health를
대신하지 않는다.

`apps/worker/Dockerfile` build는 Agent/Fake 기준선만 검증한다. 별도 real-runtime
foundation의 source tar 안전성, wheel METADATA/lock/hash, asset license/source/hash,
network-closed Dockerfile과 CPU stub preflight는 일반 Python infra test에 포함된다.
검증 뒤 원본 입력을 바꾸는 경우 private snapshot 재검증이 실패하는지, HTTPX transport
wheel closure와 Agent wheel import가 고정되는지도 함께 검사한다. verified runtime 없는
bundle의 native 설치 거부, 기존 worker.env mode 불일치 거부와 unverified GPU runtime
acknowledgement entrypoint도 이 suite에서 확인한다. 실제 두 Worker bundle을 1.0→2.0으로
설치해 release-owned env 전환과 custom setting/token/profile/data 보존을 검증하고,
duplicate/invalid image manifest와 symlink env도 거부한다.

```bash
.venv/bin/pytest -q tests/infra/test_worker_runtime_packaging.py
```

`infra/worker/runtime/build-runtime-image.sh --verify-only`도 Docker/GPU 없이 동일한 operator
input을 검증한다. 실제 `Dockerfile.rvc` build에는 사전 load한 amd64 base digest와 전체
offline wheel/asset cache가 필요하다. GPU가 없는 test 결과를 실제 학습 smoke로 기록하지
않는다. guarded native PM/Harvest/CREPE/RMVPE 안전 경계 및 중앙 Sample completion은 fixture로
검증했다. Production runner factory/capability는 builder-generated qualification activation과
실제 asset binding이 있을 때만 동적으로 열리지만 실제 GPU/no-network matrix 증적은 아직 없다.
따라서 현재 disabled activation의 Agent는 `supported_inference_f0_methods=[]`,
`fixed_test_set_inference_ready=false`를 유지하고
`AUTO_SAMPLE_JOBS_ENABLED=false`, `PROFILE_STAGE_SET_VERIFIED=false`를 바꾸지 않는다.

Qualification parser, exact 49-case/evidence archive, disabled/qualified projection,
factory/capability와 installer/image 결박은 다음으로 검증한다.

```bash
.venv/bin/pytest -q tests/infra/test_runtime_qualification.py \
  tests/infra/test_worker_release_readiness.py \
  apps/worker/tests/test_runtime_activation.py \
  tests/infra/test_worker_runtime_packaging.py \
  tests/infra/test_image_bundle_closure.py
```

여기서 qualified fixture는 합성 report다. 실제 NVIDIA GPU에서 case를 실행했다는 증거가 아니며,
운영 activation은 `RUNTIME_QUALIFICATION.md`의 외부 review 절차를 별도로 통과해야 한다.

`infra/worker/runtime/release_readiness.py`는 source/wheel/asset/build/runtime/49-case와
SBOM·vulnerability/container/secret/SAST/license/clean-host review evidence를 읽기 전용으로
열거한다. Exit `0`은 입력 byte와 identity binding 검증, `1`은 missing/invalid/blocked dependency,
`2`는 CLI/output 오류다. 어떤 경우에도 Docker/network/scan을 실행하거나 activation을 쓰지 않고
`activation_permitted=false`를 유지하므로 이 report를 release 승인으로 사용하지 않는다.

Committed clean source에서 exact 8-role Manager self-contained build orchestration이 source/build backend,
Buildx dependency materialization, linux-amd64, OCI image ID와 Docker-save config digest 분리,
optional dependency user/application user/release-label과 no-default-attestation exact archive gate를
적용하는지는 다음 fixture로 검증한다.

```bash
.venv/bin/pytest -q tests/infra/test_manager_self_contained_release.py
```

이 test는 Docker command를 대역으로 검증한다. 실제 image build/archive, upstream digest, scan과 clean
Ubuntu load/start evidence를 만들지 않는다.

중앙 TestSet/Preset/Sample 원장, bounded WAV upload, canonical 재검증, immutable manifest,
Job sample-plan snapshot과 migration은 다음으로도 검증한다.

```bash
.venv/bin/pytest -q apps/api/tests/test_test_sets.py packages/contracts/tests/test_contracts.py
.venv/bin/pytest -q apps/api/tests/test_migrations.py
.venv/bin/pytest -q apps/api/tests/test_storage_adoption_cli.py \
  apps/api/tests/test_artifact_storage.py \
  apps/api/tests/test_dataset_upload_api.py \
  apps/api/tests/test_maintenance.py
```

마지막 suite는 Dataset/Artifact session의 exact namespace mismatch, S3 credential 회전 시
fingerprint 유지와 bucket 변경 시 분리, Worker Dataset 전송/Artifact download/maintenance
fail-closed, SQLite historical UNBOUND/default 제거/downgrade와 PostgreSQL offline migration,
completed byte 재검증 adoption, preview audit와 CLI secret-safe 오류 경계를 검증한다.
`test_maintenance.py`는 추가로 Worker와 enqueue adapter가 공유하는 exact no-resolve RQ
envelope, arbitrary callback/dependent 비실행, inactive poison/terminal quarantine 재생성,
started poison terminal 처리, deterministic enqueue lock, lost queued/retrying/enqueue_failed와
stale running reconciliation, final-attempt started 보존, attempt/completion fence, Redis 장애,
동시 local leader, periodic readiness와 prompt shutdown을 검증한다.

API fixture에서는 sample Job gate/capability를 직접 구성하고 Worker fixture에서는 합성 qualified
activation을 사용해 snapshot, lease-bound transfer, inference publication과 completion을 확인한다.
현재 실제 증적 없는 Agent는 capability false를 유지한다. CREPE와 실제 GPU/no-network matrix가
통과하기 전에는 이를 release
end-to-end sample 검증으로 기록하지 않는다. TestSet 전용 RQ exact-envelope/reconciler,
generation/write-token heartbeat, 절대 PUT expiry, finalize heartbeat와 confirmation grace 이중
삭제는 자동 회귀로 검증했다. 실제 S3에서 전역 grace보다 오래 열린 PUT과 PostgreSQL 다중
replica 경합은 별도 장애 주입 gate다.

검토 commit의 private projection과 typed stage adapter는 무거운 RVC dependency 없이
다음 fixture로 검증한다.

```bash
.venv/bin/pytest -q apps/worker/tests/test_native_runner.py
```

이 suite는 v1/no-F0, v2 다중 F0/feature shard, 공유 checkout 비기록, G/D pair 오류,
deterministic index CLI, 공식 small-model fallback argv, stdout/train.log metric metadata,
asset manifest 재검증, claim commit/GPU mismatch, sample-disabled 전체 stage plan,
build-generated code/config/asset projection inventory, claim 뒤 source TOCTOU와 게시 뒤
private projection 변조 거부,
native inference publication 연결, release capability fail-closed, peer 실패 취소, timeout,
외부 취소와 workspace escape를
검증한다. subprocess가 만드는 산출물은 fixture이므로 실제 Torch/FAISS/RVC/GPU smoke를
대체하지 않는다.

## Live telemetry와 terminal watermark

```bash
.venv/bin/pytest -q \
  packages/contracts/tests/test_contracts.py \
  apps/api/tests/test_api.py \
  apps/api/tests/test_job_observability.py \
  apps/api/tests/test_mlflow_integration.py \
  apps/api/tests/test_telemetry_migration.py \
  apps/worker/tests/test_telemetry_spool.py \
  apps/worker/tests/test_gpu_process.py \
  apps/worker/tests/test_native_runner.py \
  apps/worker/tests/test_vertical_flow.py && (
  cd apps/web
  npm test -- --run \
    tests/bff-observability.test.ts \
    tests/non-overlapping-poller.test.ts \
    tests/metric-presentation.test.ts
)
```

Worker 회귀는 subprocess stdout/stderr callback, 증가분 `train.log`, TensorBoard scalar polling을
한 attempt의 단조 log/metric sequence에 합치고 source/semantic duplicate를 제거하는지 검사한다.
Manager I/O 전에 spool enqueue가 끝나는지, 느린 flush가 writer를 막지 않는지, enqueue/flush/
dead-letter file race가 record를 잃지 않는지와 callback/spool 실패가 process group을 종료하는지도
포함한다. Bearer·quoted secret·JWT/API/access/private key·URL query·절대 경로/control redaction과
정제 log 16 KiB 상한, `current_epoch` durable-first status projection도 확인한다.

같은 suite는 Job 시작 직후 fresh observation과 heartbeat와 독립된 기본 60초 cadence가 GPU가 없는
경우에도 count/disk/availability를, GPU fixture에서는 index별 utilization/VRAM/temperature를
spool하는지 검증한다. 성공한 empty query와 query failure를 availability 1/0으로 구분하고 동일 값의
두 표본도 보존한다. Semantic-invalid nvidia-smi는 heartbeat를 죽이지 않고 unavailable로 닫는다.
Periodic spool 실패는 typed `telemetry_persistence_failed`이고, system enqueue와 terminal watermark가
경합하면 시작한 표본까지 포함해 producer를 봉인한 뒤 final flush 또는 pending late replay를
유지한다. GPU inventory max 64, index `0..1023`, unique index/UUID와 finite 값 계약도 포함한다.

Manager 회귀는 status/log/metric의 declared/chunked 2 MiB raw body, strict UTF-8 JSON과
NaN/Infinity 거부, attempt/sequence conflict와 idempotency key의 canonical payload fingerprint를
검증한다. Terminal status의 두 count가 함께만 존재하는 exclusive upper watermark인지, 이미 저장된
최대 sequence보다 작을 수 없는지, exact Worker/lease/Job/attempt의 watermark 미만 batch만 늦게
수용하는지 검사한다. Legacy terminal, cross-worker/old attempt, 상한 이상 sequence는 거부되고,
late metric은 current Job epoch를 바꾸지 않으면서 MLflow outbox와 원자 commit돼야 한다. Active
ingest와 cancel/terminal race에서 loser는 `503`/`Retry-After` 뒤 terminal watermark로 재평가하며,
같은 payload의 concurrent replay만 duplicate로 수렴해야 한다.

이 suite의 terminal status는 Manager에 커밋된다. Manager 전체 장애 중 status가 커밋되기 전에
lease가 회수되는 경우에는 server watermark가 없고 old spool을 자동 수용하지 않으므로 이 자동
회귀를 모든 outage의 무손실 복구 증거로 사용하지 않는다. Subprocess와 TensorBoard 자료도 fixture라
실제 NVIDIA GPU/RVC 장시간 학습 증거가 아니다.

Manager Job observability와 Web 회귀는 metric `tail=true`가 최신 limit개를 attempt/sequence
정방향으로 반환하고 `tail+offset`을 거부하는지, BFF가 boolean만 전달하는지 검증한다. Dashboard는
15초 poller를 사용하며 initial/background 요청 구분, active 요청 중 tick 생략과 cleanup abort를
`tests/non-overlapping-poller.test.ts`에서 확인한다.

Stage error taxonomy와 no-replay policy는 별도 fixture로 검증한다.

```bash
.venv/bin/pytest -q apps/worker/tests/test_stage_errors.py \
  apps/worker/tests/test_vertical_flow.py \
  apps/worker/tests/test_dataset_transfer.py \
  apps/worker/tests/test_artifact_upload.py \
  apps/worker/tests/test_telemetry_spool.py
```

전체 stage의 timeout mapping, training/checkpoint/index nonzero 단일 호출, unknown fallback,
cancel/lease 우선순위, claim configuration과 runtime-unready 분리, Dataset/TestSet/Artifact
transient bounded exhaustion, Dataset/TestSet integrity 무재시도, telemetry deferred replay와 spool failure를
검증한다. terminal payload와 Agent log에 fixture token, argv, local path 및 URL/query sentinel이
없고 error code/message가 typed taxonomy에서 결정되는지도 확인한다.

## 설치 bundle과 installed release closure

압축 해제한 Manager/Worker bundle에서 일반 checksum과 exact inventory를 모두 확인한다.

```bash
sha256sum -c SHA256SUMS
python3 common/image_bundle.py verify-ledger --root . --ledger-name SHA256SUMS
GIT_COMMIT=$(awk -F= '$1 == "GIT_COMMIT" {print $2; exit}' manifest.env)
COMPONENT=manager  # Worker bundle이면 worker
python3 common/image_bundle.py verify-bundle \
  --root . \
  --component "$COMPONENT" \
  --version 0.1.0-dev.19 \
  --source-commit "$GIT_COMMIT"
```

`verify-ledger`는 추가·누락·중복·unsafe path·symlink/비정규 파일까지 거부한다. 설치기는 release
tree에 mode `0444` `RELEASE_SHA256SUMS`를 원자 생성하며 재설치, Compose
`up|start|restart|run|create`와 Manager rollback은 이를 다시 검증한다. Partial bundle도 빈 image
inventory만 확인하고 끝내지 않고 version, 모든 image reference, pull policy, Worker runtime/build/
asset/qualification provenance와 gate를 manifest에 다시 결박한다. Archive의 `README.md`와
`TESTING.md`는 해당 component/version으로 렌더링돼 있어야 한다.

`tests/infra/test_source_closure.py`는 self-contained build source가 broad ignore에 가려지지 않는지,
`test_image_bundle_closure.py`는 extracted bundle ledger 제거와 Docker config content/user 변조를,
`test_installer_activation.py`는 target Compose prevalidation·forward-only version transition과 기존
env/current 보존을 검사한다. `test_deployment_config.py`의 uninstall 회귀는 systemd와 Compose 중
어느 하나가 실패해도 다른 stop을 시도하고 최종 성공으로 오판하지 않는지 확인한다.

dev.19에도 포함된 Worker custom CA 집중 회귀는 다음과 같다.

```bash
.venv/bin/pytest -q \
  apps/worker/tests/test_tls.py \
  tests/infra/test_installer_activation.py \
  tests/infra/test_deployment_config.py
```

`test_tls.py`는 root-equivalent owner, mode `0444|0644`, regular/non-symlink, 1..1 MiB,
certificate-only ASCII PEM, NUL/private-key 거부와 strict `CERT_REQUIRED`/hostname/TLS 1.2 context를
검증한다. In-memory handshake는 custom CA 성공, CA 누락과 hostname mismatch 실패를 구분한다.
Client 회귀는 Manager의 동기 `urllib`/비동기 `httpx`와 external Dataset/TestSet/Artifact transport가
같은 SSL context를 사용하고 environment proxy를 끄는지 확인한다. Installer 회귀는 fixed read-only
mount/path, atomic publish, option 생략 upgrade의 보존, replacement prevalidation/activation 실패의
이전 byte 보존과 wrapper `up|start|restart|run|create` 재검증을 확인한다. Immutable dev.16 archive에는
이 기능이 없으며, 위 source fixture를 dev.16 설치 증거로 해석하지 않는다. dev.19 archive를 시험할
때는 bundle-local `TESTING.md`의 native negative runbook도 함께 실행한다.

## Manager 전체 Compose smoke

```bash
make test-manager-full-stack-docker
```

이 opt-in harness는 고유 project와 임시 secret/env/loopback port를 만들고 API/Web/MLflow image를
build한 뒤 PostgreSQL, Redis, MinIO, MLflow, migration, API, RQ, Web과 proxy를 함께 기동한다.
macOS에서는 Docker VM이 공유하는 repository-local `.rvc-stack-smoke/`를 사용하고 Linux에서는
`TMPDIR`을 사용하며, 필요하면 `RVC_STACK_SMOKE_WORK_PARENT`로 공유 가능한 부모를 명시한다.
readiness/UI, 실행 UID, 역할별 runtime secret allowlist와 mode/owner, exact MinIO bucket policy와
cross-bucket 거부, migration→maintenance DB authz→RQ 순서, DB runtime self-verify, Redis ACL과
application image label을 확인하고 고유 resource만 정리한다. Runtime secret은
root로 exact inventory·directory/file mode와 owner를 확인하고, 실제 API/RQ/MLflow non-root user로는
directory enumeration이 거부되면서 allowlist의 알려진 파일만 읽을 수 있는지도 별도로 확인한다.

dev.14부터 `internal: true` storage network를 유지하면서 host publish가 필요한 MinIO·MLflow만
`host-access` bridge에 연결하고, bundled proxy에 명시적 `nginx -g 'daemon off;'` command와
zero-argument entrypoint fallback을 둔다. 2026-07-12 local `linux/arm64` Docker에서 이 수정이 포함된
dev.16 전체 harness가 두 packaged artifact BFF route까지 확인하고
`Manager full Compose stack smoke: PASS (docker_architecture=arm64)`로 통과했다.
이는 local architecture의 runtime 통합 증거이며 clean Ubuntu `linux/amd64`, 외부 TLS/browser 또는
GPU/RVC release 증거가 아니다.

## Manager Docker volume 복구 drill

```bash
make test-manager-recovery-docker
```

기본 `make check`와 분리된 opt-in test다. 실제 PostgreSQL database 두 개, MinIO bucket
두 개, Redis와 artifact/Dataset 임시 작업 volume을 격리된 Compose project에 만들고
backup→변조/삭제→restore를 수행한다. 후발 DB table 제거, object byte/metadata/tag,
Redis와 임시 작업 상태 reset, archive의 config/secret 제외를 검증한다. 고유한 test
project 외의 container/network/volume은 정리하지 않는다. 자세한 fixture 범위와 image
prerequisite는 `docs/DEPLOYMENT.md`를 본다.

## dev.19 자동화·설치 증적과 dev.18/dev.17 역사 증거 — 2026-07-13

- dev.19 current source의 `make check`는 Ruff, strict mypy `88 source files`, Python non-E2E
  `749 passed, 4 deselected`, Web Vitest `24 files/211 tests`, ESLint와 Next.js production build를
  통과했다. Maintenance/installer/migration 결합 회귀는 `124 passed`였다.
- dev.19 localhost Manager↔Fake Worker E2E는 제한 sandbox의 loopback bind 거부 뒤 승인된
  환경에서 같은 `make test-e2e`를 재실행해 `4 passed`를 확인했다.
- Alembic head와 Manager schema marker는 `f5d1c8a9b240`이다. dev.19 partial archive의 외부
  SHA-256은 Manager `6c76684c640b92e3cc6aa9ee74f1514a81409d6d20ae71bb46183d32eb899393`,
  Worker `fd63d579dcc8199463a9d0f1d70b2b18ba7f1e7b78a21b6e86f8e8629c2a8f99`다. 외부 sidecar,
  내부 exact `SHA256SUMS`, `verify-ledger`와 strict `verify-bundle`이 PASS했고 symlink/host cache는
  없으며 version-rendered `README.md`/`TESTING.md`/`TEST_RESULT_TEMPLATE.md`를 포함한다. 두 manifest는
  `GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`, empty image inventory이며 Worker activation은
  mode `0444`, 모든 runtime/GPU/profile/Sample gate는 false다.
- 실제 PostgreSQL 16 migration/apply/reapply/self-verify와 임의 function grant 제거, forbidden
  SELECT/DELETE/parent UPDATE/canonical read, 허용된 Dataset cleanup dry-run이 PASS했다. Redis 7.4
  ACL, MinIO delete-only policy, secret projection과 최신 전체 Compose도 PASS했다. Full Compose는
  `docker_architecture=arm64` 증거다.
- 현재 Git tracked file은 0개다. 따라서 `git diff --check`는 검사할 tracked 변경이 없어
  whitespace와 committed source provenance 판정이 `BLOCKED`이며, 다른 lint/test PASS로 대체할 수 없다.

- 이전 dev.17 source의 `make check`는 Ruff, strict mypy `85 source files`, Python non-E2E
  `720 passed, 4 deselected`, Web Vitest `21 files/181 tests`, ESLint, Next.js production build와
  shell syntax를 통과했다. Git tracked file은 0개이므로 내부 `git diff --check` exit 0은
  whitespace/source provenance 증거가 아니다.
- dev.17 localhost Manager↔Fake Worker E2E는 제한 sandbox에서 loopback bind
  `PermissionError` 4건으로 첫 실행이 실패했고, local socket 권한으로 같은 명령을
  재실행해 `4 passed in 5.80s`를 확인했다.
- Worker custom CA와 installer/deployment/runtime packaging 전체 집중 회귀 `327 passed`,
  custom CA/lifecycle/handshake 집중 `42 passed`와 최종 핵심 `8 passed`, Worker 대상 mypy
  `30 source files`, Ruff, `bash -n`을 통과했다. In-memory TLS handshake는 승인 CA+정확한
  hostname만 성공하고 CA 누락과 hostname mismatch를 거부했다.
- Experiment 비교 BFF/UI를 포함한 Web 전체 181 회귀, ESLint와 production build가
  통과했다. 실제 browser/API 비교 E2E와 model registry promotion은 별도 gate다.

- 이전 dev.16 current source의 `make check`와 executable 검사: PASS. 다만 현재 checkout은 Git tracked
  file이 0개라 내부 `git diff --check`가 검사할 대상이 없었으므로 whitespace/source provenance는
  `NOT EVIDENCED`이며 T0 전체 합격으로 확대하지 않는다.
- Python non-E2E unit/integration: `712 passed, 4 deselected`
- Ruff 전체 PASS, strict mypy `84 source files` PASS
- Frontend: Vitest `19 files/162 tests`, ESLint와 Next.js production build PASS
- image bundle closure `33`, release source closure `4`, installer activation `3`, Manager
  self-contained orchestrator `10`, Worker runtime packaging `13`, Worker readiness `6`, runtime
  qualification `20`, supply chain `4`, Manager recovery `13`, deployment config `31`개가 함께
  PASS했다. 여기에는
  누락 checksum ledger, broad-ignore source 누락, Config.User/config digest 변조, upgrade
  prevalidation/역행과 uninstall false-success 회귀가 포함된다.
- localhost Manager↔Fake Worker protocol E2E의 dev.16 current-source 역사 실행: **4 passed**
- 새 SQLite DB `alembic upgrade head`와 `alembic check`, LUFS migration 집중 suite PASS;
  dev.16 schema compatibility marker `d8f2a6c4b901`
- PostgreSQL offline upgrade SQL 생성 PASS
- Manager/Worker `docker-compose ... config --quiet` PASS
- dev.16 Manager/Worker archive의 최종 외부 SHA-256은 Manager
  `9a520623010a4e640e9975bc87835640de8f7ac127830ec9d9106ce7d2939f26`, Worker
  `105971694bed766ea3ae4d7c58ec27db49aa4246e3db0a83988f598e2064d612`다. 이 값과 외부 sidecar,
  내부 exact `SHA256SUMS`, image manifest v2 strict 검증, bundle-local
  `README.md`/`TESTING.md`/`TEST_RESULT_TEMPLATE.md`, host cache 제외와 partial SBOM/license 경로가
  모두 일치해야 archive 배포 증거로 사용할 수 있다. 둘 다
  `GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`, 빈 image/archive inventory이고 Worker
  runtime/GPU/native gate는 false다.
- Local `linux/arm64` Docker에서 MLflow non-root/read-only health, Manager runtime secret
  projection(actual entrypoint 포함), MinIO exact bucket policy의 격리 smoke와 수정된 전체
  application Compose harness가 PASS했다. 전체 smoke는 Web standalone image 안의 artifact download와
  Job artifact BFF route도 실제 file로 확인했다. PostgreSQL/Redis/MinIO recovery drill, clean
  `linux/amd64` host, NVIDIA GPU/native RVC와 실제 외부 TLS/browser는 이 기준선에서 확인하지
  못했으므로 arm64 결과를 최종 설치·출시 증거로 확대 해석하지 않는다.
- 이전 설치·시험 기준선 dev.17 partial archive의 Manager 외부 SHA-256은
  `b131698fbdeb51887d808f1396323b9a0e37ef6495445e60eadbedc024b95b96`, Worker는
  `a4b2951b7f210501e73f2d9ab1b6fb9d78c6ce8f93aed26b59b83d898a4883e7`이며 schema marker는
  `d8f2a6c4b901`이다. 두 manifest 모두 `GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`, 빈
  image/archive inventory이고 Worker runtime/native/GPU/profile/Sample gate는 false다. Bundle 무결성
  PASS를 self-contained/runtime/GPU 출시 PASS로 재표기하지 않는다. 현재 dev.19 기준은 위의 새
  archive/hash와 `f5d1c8a9b240` marker이며 동일한 partial 제한을 유지한다.

## 아직 필요한 검증

- 실제 PostgreSQL에 대한 race/constraint test suite
- Model registry의 실제 PostgreSQL multi-replica promotion 경쟁, MinIO/S3 대용량 canonical
  재해시·tamper/outage와 browser/API response-loss·동시 promotion E2E
- MinIO binary upload/finalize/checksum 통합 test
- 실제 Redis/RQ process 장애와 realtime stream 장애 test. 외부 Redis 없이 수행하는 기본
  회귀는 dequeue/perform execution allowlist, `os.getenv`/`os.path` callable과 callback
  비실행, pickle 거부, generic terminal policy failure, process role/secret 분리, bounded
  delayed retry scheduler 활성화와 queue별 단일 Redis lock/Worker heartbeat key 분리,
  scheduler-promoted 임의 envelope 재차단, exact existing envelope/quarantine, deterministic
  enqueue lock, admin 권한, stale heartbeat/reconciler readiness fail-closed, lost DB run bounded
  reconciliation, cleanup row claim 경쟁, active/canonical 보호, dry-run·멱등·bounded retry를
  검증한다. clean Compose에서는 실제 Redis와 RQ Worker heartbeat/재시작, PostgreSQL advisory
  lock을 쓰는 다중 API replica의 queue 유실 복구, Redis Lua quarantine과 in-flight
  upload/cleanup 경합을 추가 검증한다.
- Frontend component/Playwright E2E
- 실제 NVIDIA GPU에서 고정 RVC commit의 v1/v2 smoke
- Ubuntu 22.04/24.04 clean Manager VM installer/reboot/upgrade/rollback/restore
- Ubuntu NVIDIA clean Worker VM installer/reboot/upgrade/uninstall. Worker 자동 rollback은 제공하지 않음
- dev.17 custom CA의 clean Ubuntu Worker install/reboot/upgrade, 실제 사설 CA
  Manager/Object hostname handshake와 Dataset/TestSet/Artifact 전송, invalid replacement 보존 시험

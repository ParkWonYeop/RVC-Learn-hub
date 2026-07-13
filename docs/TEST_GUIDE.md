# 테스트 가이드

이 문서는 사용자가 RVC Training Orchestrator를 직접 검증할 때의 실행 순서, 합격 기준과 증적
형식을 정의한다. 자동 테스트의 세부 설계는 `docs/TESTING.md`, 설치 명령은
`docs/INSTALLATION_GUIDE.md`를 따른다.

## 권장 실행 순서

처음 시험한다면 아래 순서를 지킨다. 실패한 단계의 상위 단계는 실행해도 PASS로 판정하지 않는다.

1. `T2`에서 전달받은 archive의 외부/내부 checksum과 manifest를 확인한다.
2. source checkout이 있으면 `T0`의 `make check`를 실행한다.
3. 같은 source에서 `T1`의 `make test-e2e`로 localhost Fake protocol을 확인한다.
4. Docker daemon이 있는 source host에서만 `T2`의 MLflow/secret projection/MinIO policy/Redis ACL
   smoke를 각각 실행하고, 가능하면 별도 full Compose smoke도 실행한다. 결과들은 서로 대체하지
   않는다.
5. 별도 Ubuntu Manager 호스트와 image/TLS가 준비됐을 때만 `T3`를 실행한다.
6. dev.20 partial 후보 Worker는 `T4`의 fake/no-start 구성과 native fail-closed만 확인한다.
7. 실제 runtime 포함 self-contained Worker가 생기기 전 `T5`는 `BLOCKED`로 남긴다.
8. `T6`는 production이 아닌 격리 Docker project 또는 복제 VM에서만 수행한다.

### 사용자가 제출할 최소 인수 결과

처음 시험하는 사용자는 모든 T0~T6를 한 번에 수행할 필요가 없다. 준비된 환경에 따라 다음
묶음까지만 실행하고, 실행하지 못한 상위 단계는 실패로 바꾸지 말고 정확한 사유와 함께
`BLOCKED`로 남긴다.

| 준비된 환경 | 최소 실행 묶음 | 제출할 판정 |
|---|---|---|
| Manager/Worker archive만 있음 | T2의 archive 검증 + 설치 가이드 4.1/5절 | `BUNDLE-INTEGRITY`, `CONFIG-ONLY` |
| source checkout과 개발 의존성도 있음 | 위 항목 + T0 + T1 | `EXECUTABLE-SOURCE`, `FAKE-PROTOCOL` |
| 별도 Manager image와 외부 TLS/DNS가 있음 | 위 항목 + T3 | `MANAGER-SMOKE`, `TLS-PRODUCTION` |
| self-contained Worker runtime과 NVIDIA GPU가 있음 | 위 항목 + T5 | `WORKER-NATIVE`; qualification이 있을 때만 `PRODUCTION-SAMPLE` |

저장소를 받은 경우 결과 양식을 먼저 복사한다.

```bash
cp docs/TEST_RESULT_TEMPLATE.md \
  "rvc-test-result-$(date -u +%Y%m%dT%H%M%SZ).md"
```

Archive만 받은 경우에는 압축 해제한 bundle root의 `TEST_RESULT_TEMPLATE.md`를 복사한다.
결과 문서에는 성공한 명령뿐 아니라 예상한 negative test의 nonzero exit,
실패와 `BLOCKED` 사유도 남긴다. Secret을 제거한 log와 screenshot 경로만 기록하고 원본 credential은
첨부하지 않는다.

현재 사용자 인수 기준선은 `0.1.0-dev.20`이다. Manager archive는 committed source
`298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a`, schema `f5d1c8a9b240`에 결박된
`SELF_CONTAINED=true` 후보이며, API/Web/MLflow/PostgreSQL/Redis/MinIO/MinIO Client/Nginx의 정확히
8개 `linux/amd64` image를 포함한다. 외부 SHA-256은
`c6488dad47c7f38c082ed6fa68f1fe3691c069110aef0bbf68a9d7ba5e6f5b70`이다. Worker archive도 같은
source commit을 기록하지만 외부 SHA-256
`7f36cbf27100bf70425c2780142d4fa3f6e6e76d0acf410d3e3fb698aa50558b`,
`SELF_CONTAINED=false`, image 0개, runtime/native/GPU/profile/Sample gate false인 CONFIG-ONLY
partial이다. 따라서 Manager archive 무결성과 release-image Compose PASS를 Worker native 또는
production 전체 PASS로 확대하지 않는다.

역사 기록: dev.19 partial archive의 SHA-256은 Manager
`6c76684c640b92e3cc6aa9ee74f1514a81409d6d20ae71bb46183d32eb899393`, Worker
`fd63d579dcc8199463a9d0f1d70b2b18ba7f1e7b78a21b6e86f8e8629c2a8f99`였다. 두 archive는
`GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`였으므로 현재 dev.20 검증에 재사용하지 않는다.
dev.13, dev.15, dev.16, dev.17의 더 오래된 hash와 runbook도 폐기된 역사 기준선이며 새 설치·upgrade
시험의 신뢰값으로 사용하지 않는다.

## 1. 현재 기준선과 시험 범위

현재 문서의 배포 후보 기준선은 Manager self-contained와 Worker partial이 함께 있는
`0.1.0-dev.20`이다. Manager bundle 무결성과 exact release-image 실행 PASS도 clean Ubuntu 설치,
외부 TLS/browser 또는 Worker native GPU PASS를 뜻하지 않는다. 이 경계에 따라 시험 결과를
다음처럼 구분한다.

| 단계 | 시험 대상 | dev.20 후보에서의 판정 |
|---|---|---|
| T0 | source lint/type/unit/frontend/build | 기준 PASS — Python 752/4 deselected, mypy 88, Web 24/211 |
| T1 | localhost Manager↔Fake Worker HTTP protocol | 기준 PASS — `4 passed in 6.68s`; production/native 증거는 아님 |
| T2 | migration/Compose, Docker 보안 smoke와 bundle 무결성 | Manager 8-image archive/loaded identity/amd64 Compose 기준 PASS; 사용자 재검증 필요 |
| T2-MAINT | maintenance DB/Redis/S3 최소권한·heartbeat | exact release-image Compose 안에서 PASS; 사용자 환경 재검증 필요 |
| T3 | self-contained Manager clean Ubuntu 설치·외부 TLS/browser smoke | `BLOCKED` — clean Ubuntu/외부 TLS 실증 대기 |
| T4 | Worker preflight와 `--no-start` 구성/보호 gate | 사용자 실행 가능 — CONFIG-ONLY/native 거부가 기대 결과 |
| T5 | 실제 native GPU 학습/Sample | `BLOCKED` — dev.20 Worker에 runtime image 없음 |
| T6 | Manager backup/restore/rollback 장애 drill | 격리 Docker/복제 VM에서만 실행 |

`RVC_RUNTIME_INCLUDED=false`인 Worker가 native 설치를 거부하는 것은 실패가 아니라 보호 장치의
합격이다. 반대로 Fake Worker가 production Manager에 등록되지 않는 것도 정상이다. 이 두 gate를
환경 값을 바꿔 우회한 결과는 인정하지 않는다.

## 2. 결과 기록 형식

시험 시작 전에 결과 디렉터리를 만들고 다음 정보를 기록한다. secret, token, password, presigned
URL query, 원본 음성/모델 byte는 증적에 넣지 않는다.

```text
Test ID:
실행 시각(UTC/현지 timezone):
시험자:
source revision 또는 bundle version:
bundle SHA-256:
source working tree clean/dirty와 image ID:
OS / kernel / architecture:
Docker / Compose version:
Docker smoke target, image architecture와 최종 PASS 문자열:
GPU / driver / NVIDIA Container Toolkit:
Manager/Object TLS 종단 위치와 DNS:
실행한 명령:
기대 결과:
실제 결과:
PASS / FAIL / BLOCKED / CONFIG-ONLY / NATIVE-CANDIDATE-UNVERIFIED:
SOURCE-MIXED 여부:
redacted log·screenshot·artifact 경로:
임시 container/network/volume 정리 확인과 남긴 test image:
우회 옵션 또는 환경 차이:
```

아래 값은 반드시 원문 대신 `[REDACTED]`로 바꾼다.

- 관리자 비밀번호와 JWT/session cookie
- Worker bootstrap/issued token
- PostgreSQL/Redis/MinIO secret
- `X-Amz-*` 등 query가 포함된 object URL
- 사용자 Dataset/TestSet의 식별 가능한 파일명과 로컬 절대 경로

## 3. T0 — source 전체 검증

### 3.1 개발 의존성 설치

권장 환경은 Python 3.11과 Node.js 20.9 이상이다. 첫 bootstrap은 Python/NPM registry 접근 또는
사전 준비한 cache가 필요하다.

```bash
(
  set -Eeuo pipefail
  python3 --version
  node --version
  make bootstrap
)
```

이미 `.venv`와 `apps/web/node_modules`가 정확한 lock 기준으로 준비되어 있다면 bootstrap을 다시
할 필요는 없다.

### 3.2 전체 기본 검사

저장소 루트에서 실행한다.

```bash
TRACKED_COUNT=$(git ls-files | wc -l | tr -d '[:space:]')
if [ "$TRACKED_COUNT" -eq 0 ]; then
  echo "Git provenance/whitespace: BLOCKED (tracked source inventory is empty)"
else
  echo "Git tracked source inventory: $TRACKED_COUNT files"
  git diff --check
fi
make check
```

위 명령은 두 판정을 분리한다.

- tracked inventory가 있으면 `git diff --check` exit code 0을 **Git provenance/whitespace PASS**로
  기록한다.
- tracked inventory가 0개이면 **Git provenance/whitespace BLOCKED**로 기록하지만, shell을
  종료하지 않고 `make check`를 계속 실행한다.
- `make check` exit code 0과 아래 실행 단계의 무실패는 **executable source quality
  PASS**로 별도 기록한다.

- Ruff
- strict mypy
- `e2e` marker를 제외한 Python unit/integration
- Web Vitest, ESLint, Next.js production build
- installer/infra/recovery shell 문법
- tracked inventory가 있을 때의 whitespace 오류 검사

`make check`에는 HTTP E2E, Docker image build, Compose container 기동/health, 실제 installer 실행,
Docker volume 복구 drill과 NVIDIA/RVC 학습이 포함되지 않는다. 이 항목들은 각각 T1, T2~T6에서
따로 실행하고 판정한다.

테스트 개수는 기능 추가에 따라 늘 수 있으므로 과거 숫자와 정확히 같아야 하는 것은 아니다.
수집 오류, skip 사유 변경, warning 증가와 실패가 있으면 원문 log를 보존하고 임의로 제외하지
않는다.

dev.20 archive를 만든 committed source에는 Git tracked file 410개가 있었고, source revision은
`298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a`다. 이 archive의 provenance 판정에는 다른 checkout의
현재 HEAD나 dirty 상태를 대신 쓰지 않는다. 사용자는 자신이 시험하는 checkout에서 위 명령으로
tracked inventory와 `git diff --check`를 다시 기록한다.

2026-07-13 dev.20 후보 source의 최신 `make check` 증적은 다음과 같다.

- Ruff PASS
- strict mypy `88 source files` PASS
- Python non-E2E `752 passed, 4 deselected`
- Web Vitest `24 files/211 tests` PASS
- ESLint와 Next.js production build PASS

Localhost HTTP E2E는 기본 sandbox의 socket bind 금지로 실행환경상 차단됐고, local socket
권한으로 동일 명령을 재실행해 `4 passed in 6.68s`를 확인했다. 두 결과와 재실행 이유를 같이
기록한다.

위 숫자와 함께 `make check` 전체 exit code 0을 기록한다. Warning은 삭제하지 말고
종류와 발생 suite를 보존한다. 일부 하위 명령만 성공하거나 마지막 출력만 복사한 결과는
executable source quality PASS가 아니다. 이 기준선은 사용자의 최종 checkout에서 새로
실행한 명령을 대체하지 않으며, 코드가 더 변경되면 현재 실제 개수와 exit code를 기록한다.

### 3.3 보안·runtime 집중 회귀

설치/runtime 경계를 별도로 확인하려면 다음을 실행한다.

```bash
(
  set -Eeuo pipefail
  .venv/bin/pytest -q \
    tests/infra/test_source_closure.py \
    tests/infra/test_installer_activation.py \
    tests/infra/test_image_bundle_closure.py \
    tests/infra/test_manager_self_contained_release.py \
    tests/infra/test_worker_release_readiness.py \
    tests/infra/test_worker_runtime_packaging.py \
    tests/infra/test_deployment_config.py \
    tests/infra/test_manager_recovery.py
)
```

합격 시 ignored source 아래의 필수 BFF route 누락, archive `SHA256SUMS` 제거, application image의
root user와 Docker config content digest 변조가 모두 거부돼야 한다. Manager/Worker target Compose
prevalidation 실패는 기존 env/current byte를 보존하고, 높은 strict SemVer로의 forward 전환만
허용하며 동일·역방향 전환은 거부해야 한다. Uninstall systemd/Compose 중 하나가 실패하면 다른 stop
경로도 시도하되 최종 exit는 nonzero이고 성공 문구가 없어야 한다.

관리자 사용자 lifecycle과 token 무효화/migration 경계는 다음으로 확인한다.

```bash
(
  set -Eeuo pipefail
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
)
```

Sample 관련 fixture 경계는 다음으로 확인한다.

```bash
(
  set -Eeuo pipefail
  .venv/bin/pytest -q \
    packages/contracts/tests/test_sample_contracts.py \
    apps/worker/tests/test_native_inference.py \
    apps/worker/tests/test_sample_publication.py \
    apps/api/tests/test_sample_registration.py \
    apps/api/tests/test_artifact_storage.py
)
```

바로 위 Sample 회귀 명령이 통과해도 실제 Torch/CUDA/GPU, CREPE asset byte, production Sample Job이 검증된
것은 아니다. production Agent는 현재
`supported_inference_f0_methods=[]`, `fixed_test_set_inference_ready=false`를 유지한다.

Model registry와 migration/BFF/UI 경계는 다음으로 집중 확인한다.

```bash
(
  set -Eeuo pipefail
  .venv/bin/pytest -q \
    apps/api/tests/test_model_registry.py \
    apps/api/tests/test_migrations.py
  (
    cd apps/web
    npm test -- --run \
      tests/model-registry-bff.test.ts \
      tests/model-registry.test.ts \
      tests/model-registry-panel.test.ts
  )
)
```

역사적 dev.19 기준 maintenance/installer/migration 결합 회귀는 `124 passed`였고, 당시
registry+migration 집중 회귀 `33 passed`도 전체 source suite에 포함됐다. 현재 판정은 dev.20의
전체 `make check` 결과를 우선하며, 이 과거 숫자를 현재 집중 회귀 재실행으로 대신하지 않는다.
합격하려면 exact
current completed real attempt와 `worker-claim-v1`, reviewed RVC commit, 승인 runtime pair,
유일한 final small model/동일 attempt index만 후보가 되고 Fake·historical NULL·stale current·중복
model·미승인 runtime·wrong commit은 거부되어야 한다. Candidate 생성과 promotion의 canonical
byte tamper/storage outage/verification slot timeout은 fail-closed해야 하며, registry/entry CAS,
동시 promotion 1승자, active champion 0/1, 이전 approved rollback, terminal revoke와 idempotency replay,
owner/admin concealment·private no-store가 모두 검증돼야 한다. Web 회귀는 cookie-only same-origin BFF,
4 KiB exact body, complete pagination/version fence, Fake 후보 action 차단, response-loss/stale 잠금과
full reload 후 상태 확인을 검증한다. 이 자동 회귀를 실제 browser/API response-loss, PostgreSQL
다중 replica 경쟁 또는 실제 S3 대용량 전체 재해시 합격으로 확대 해석하지 않는다.

Live telemetry와 terminal watermark 경계는 다음으로 집중 확인한다.

```bash
(
  set -Eeuo pipefail
  .venv/bin/pytest -q \
    packages/contracts/tests/test_contracts.py \
    apps/api/tests/test_api.py \
    apps/api/tests/test_job_observability.py \
    apps/api/tests/test_mlflow_integration.py \
    apps/api/tests/test_telemetry_migration.py \
    apps/worker/tests/test_telemetry_spool.py \
    apps/worker/tests/test_gpu_process.py \
    apps/worker/tests/test_native_runner.py \
    apps/worker/tests/test_vertical_flow.py
  (
    cd apps/web
    npm test -- --run \
      tests/bff-observability.test.ts \
      tests/non-overlapping-poller.test.ts \
      tests/metric-presentation.test.ts
  )
)
```

합격 조건은 stdout/stderr·증가분 train.log·TensorBoard scalar가 Manager I/O 전에 spool에
durable 저장되고, 같은 attempt의 sequence/current epoch가 단조 증가하는 것이다. Secret/query/path
redaction, 16 KiB sanitized log, 2 MiB status/log/metric raw JSON과 NaN/Infinity 거부, 같은
idempotency key의 다른 payload `409`, terminal count 미만 late replay와 상한/legacy/cross-worker
거부가 모두 통과해야 한다. Job 시작 직후와 기본 60초 cadence의 GPU 수·사용률·VRAM·온도와 disk
snapshot이 같은 spool/sequence/watermark에 들어가고, 동일한 system 값의 연속 표본이 dedupe되지
않는지도 확인한다. 성공한 0-GPU query는 `system.gpu.telemetry_available=1`, query/semantic
검증 실패는 `0`이어야 한다. Periodic spool 실패는 `cancelled`가 아니라
`failed/telemetry_persistence_failed`이고 terminal producer seal 뒤 final flush 또는 bounded pending
late replay가 유지돼야 한다.
이 fixture는 terminal status가 원장에 커밋된 뒤의 전송 지연을
검증한다. Manager 전체 장애 중 status 미커밋 후 lease 회수까지 자동 복구한다는 증거는 아니다.

Dataset integrated loudness와 API/BFF 표시 경계는 다음으로 집중 확인한다.

```bash
(
  set -Eeuo pipefail
  .venv/bin/pytest -q \
    apps/api/tests/test_dataset_ingestion.py \
    apps/api/tests/test_dataset_upload_api.py \
    apps/api/tests/test_migrations.py
  (
    cd apps/web
    npm test -- --run \
      tests/dataset-bff.test.ts \
      tests/api-projections.test.ts
  )
)
```

합격 시 mono/stereo BS.1770-4 reference tone, 파일 경계를 넘지 않는 dataset-global gate,
400 ms/75% overlap, absolute/relative gate, finite range와 typed `null` 사유, migration/API/BFF/UI의
exact metadata 보존을 검증한다. 이 fixture는 실제 현장 음원 calibration이나 non-WAV decoder를
검증하지 않는다.

## 4. T1 — localhost HTTP protocol E2E

```bash
make test-e2e
```

합격 증적은 명령 exit code 0과 마지막 `4 passed`다. `pytest` collection error, warning을 error로
승격한 실패 또는 localhost bind 실패를 skip하거나 제외한 결과는 인정하지 않는다.

이 suite는 임시 SQLite와 `127.0.0.1` Uvicorn, 명시적으로 허용한 Fake Worker를 사용한다. Docker,
MinIO, NVIDIA GPU나 native RVC runtime을 사용하지 않는다. 합격 시 다음 protocol을 검증한다.

- Worker bootstrap 등록과 mode `0600` credential
- Dataset upload/finalize와 Experiment/Job 생성
- atomic claim, attempt/lease/heartbeat와 상태 전이
- claim 전 Job의 `current_attempt_engine_mode`는 `null`, Fake claim 뒤 training/completed 응답은
  `fake`이며 설정의 `rvc_webui` 희망값으로 바뀌지 않음
- log/metric/artifact 수집과 completion gate
- Job이 `training`인 동안 sanitized live log, `current_epoch`와 `loss_g_total`이 HTTP 조회되고 terminal 뒤
  attempt watermark가 실제 저장 count와 일치
- 같은 training 중 `system.disk_free_bytes`, GPU index 0의 utilization과
  `system.gpu.telemetry_available=1`이 HTTP metric tail API에 보이고 system 표본도 terminal
  watermark count에 포함
- 정상 Manager 경로에서 terminal final flush 뒤 Worker pending/dead-letter가 비어 있음
- 세 실제 `WorkerAgent`가 같은 Dataset의 PM/Harvest/RMVPE 조건 Job을 동시에 독립 claim·완료

이 네 E2E는 Worker token prepare/activate·관리자 revoke·재등록, 같은 batch replay의 멱등성과
실제로 발생한 heartbeat/terminal CAS conflict를 직접 증명하지 않는다. 이 경계는 3.3절의 API/
Worker 집중 회귀에서 검증한다. Visible GPU 값도 fixture이므로 실제 NVIDIA 정확도 증거가 아니다.

이 결과는 “Manager↔Fake Worker protocol E2E PASS”로 기록한다. “설치형 Manager↔실제 Worker”나
“RVC 학습 E2E PASS”로 기록하면 안 된다.

2026-07-13 dev.20 source에서 `make test-e2e`는 exit code 0과
`4 passed in 6.68s`로 완료됐다. 이는 현재 source의 Fake protocol 기준선이며 사용자 환경의 시험을
대체하지 않는다. 사용자도 localhost socket을 허용한 환경에서 같은 명령을 새로 실행해 exit code
0과 `4 passed`를 직접 확인한 경우에만 자기 T1을 PASS로 기록한다.

## 5. T2 — migration, Compose와 bundle 검증

### 5.1 fresh SQLite migration

임시 database에서 모든 revision을 올리고 drift를 검사한다.

```bash
DB_PATH=$(mktemp /tmp/rvc-alembic.XXXXXX)
(
  set -Eeuo pipefail
  ENVIRONMENT=development DATABASE_URL="sqlite+aiosqlite:///$DB_PATH" \
    .venv/bin/alembic -c apps/api/alembic.ini upgrade head
  ENVIRONMENT=development DATABASE_URL="sqlite+aiosqlite:///$DB_PATH" \
    .venv/bin/alembic -c apps/api/alembic.ini current
  ENVIRONMENT=development DATABASE_URL="sqlite+aiosqlite:///$DB_PATH" \
    .venv/bin/alembic -c apps/api/alembic.ini check
)
```

합격 기준은 현재 source 단일 head와 dev.20 Manager archive marker가 모두
`f5d1c8a9b240`이고
`No new upgrade operations detected`인 것이다. 임시 DB 경로만
정리하고 운영 DB에는 이 명령을 시험 삼아 실행하지 않는다.

```bash
case "$DB_PATH" in
  /tmp/rvc-alembic.*) rm -f -- "$DB_PATH" ;;
  *) echo "unexpected migration test path; refusing cleanup" >&2; exit 1 ;;
esac
unset DB_PATH
```

PostgreSQL dialect SQL 생성은 database에 연결하지 않고 확인할 수 있다.

```bash
ENVIRONMENT=development \
DATABASE_URL='postgresql+asyncpg://rvc:placeholder@localhost/rvc' \
  .venv/bin/alembic -c apps/api/alembic.ini upgrade head --sql \
  > /tmp/rvc-postgresql-upgrade.sql
```

exit code 0과 단일 revision chain을 확인한다. 이 결과는 실제 PostgreSQL constraint/race 시험을
대체하지 않는다.

### 5.2 Compose 렌더링

```bash
(
  set -Eeuo pipefail
  docker compose --env-file .env.example \
    -f infra/compose/manager.compose.yml config --quiet
  docker compose --env-file .env.example \
    -f infra/compose/worker.compose.yml config --quiet
)
```

두 명령이 모두 exit code 0이어야 한다. `config --quiet`는 image pull, container 기동이나 health를
검증하지 않는다.

### 5.3 Docker 보안 smoke 세 가지

다음 시험은 source checkout과 정상 Docker daemon이 있는 **개발/검증 host**에서 각각 실행한다.
Image build에 registry/package network 또는 미리 준비한 cache가 필요할 수 있다. 세 명령은
`make check`에 포함되지 않으며 하나가 다른 둘을 대신하지 않는다.

이 Make target과 runtime/release builder는 내부에서 plain `docker`를 호출한다. 실행 계정은
같은 system daemon에 직접 접근할 수 있는 승인된 CI/release 계정이어야 한다. 설치 host에서
`sudo docker`만 허용되는 상황을 `sudo make`나 즉석 Docker group 추가로 우회하지 말고 별도 build
host에서 실행한다. 아래 `docker info`의 daemon ID를 증적에 남기고 다른 daemon/context의 image와
결과를 섞지 않는다.

```bash
(
  set -Eeuo pipefail
  docker info
  make test-mlflow-docker
  make test-manager-secret-projection-docker
  make test-minio-policy-docker
  make test-redis-acl-docker
)
```

각 명령의 exit code 0과 다음 마지막 줄을 그대로 증적에 남긴다.

```text
MLflow non-root/read-only health smoke: PASS
Manager runtime secret projection smoke: PASS
MinIO exact bucket policy smoke: PASS
Redis maintenance ACL scope smoke: PASS
```

`test-mlflow-docker`는 build한 image의 user가 `10002:10002`인지 검사하고, network-none container를
read-only rootfs, capability 전체 drop, no-new-privileges, PID 128과 UID-owned mode `0700` `/tmp`
tmpfs로 실행한다. 실제 UID/GID와 zero effective capability, home write 거부, `/tmp` write,
boto3/psycopg2/MLflow import와 SQLite/local-artifact `/health=OK`가 모두 통과해야 한다. 실행 host의
다음 allowlist inspect 결과를 함께 보존한다. 전체 `docker inspect`는 runtime 환경 변수까지 노출할
수 있으므로 증적용으로 사용하지 않는다.

```bash
docker image inspect --format \
  'id={{.Id}} architecture={{.Architecture}} user={{.Config.User}}' \
  rvc-orchestrator-mlflow:nonroot-smoke
```

`test-manager-secret-projection-docker`는 실제 운영 secret이 아닌 합성 값으로 다음을 검증한다.

- root-owned mode `0600` source에서 API `10001:10001`, maintenance `10001:10001`, MLflow
  `10002:10002`, database-authz `10001:10001` 전용 mode `0400` inventory만 원자 projection
- generation A→B 교체 뒤 API/RQ/MLflow 실제 entrypoint가 허용된 secret만 읽음
- maintenance DB/Redis/S3 값이 대응 API credential과 같으면 거부하고 마지막 정상 generation 보존
- 빈 source와 symlink source 거부 뒤 마지막 정상 generation B 보존
- network-none/read-only/capability/PID 제한과 종료 trap의 임시 volume 정리

이 smoke 중 아래 두 줄이 보이는 것은 의도한 negative case다. **최종 PASS 문자열과 exit code 0이
함께 있을 때만** 합격이다.

```text
Manager runtime secret projection failed: source secret size is invalid: jwt_secret
Manager runtime secret projection failed: required source secret is unreadable: jwt_secret
```

`test-minio-policy-docker`는 임시 MinIO, 임시 network/bucket과 합성 credential을 사용한다. Manager
identity는 Manager bucket만, MLflow identity는 MLflow bucket만 읽고 쓸 수 있어야 하며 서로의
bucket list가 거부돼야 한다. Maintenance identity는 두 staging prefix 삭제만 성공하고 list/read/
write/canonical delete/MLflow 접근은 실패해야 한다. Broad policy를 붙인 뒤 init을 다시 실행해 exact
`rvc-manager-app`/`rvc-mlflow-artifacts`/`rvc-maintenance-staging-cleanup` 하나씩으로 복구하는 것도
확인한다. Bucket versioning 활성 상태는 exact cleanup 의미가 아니므로 실패해야 한다.
시작 직후 한두 번의 `connection refused`는 bounded readiness retry 중 나타날 수 있다. 최종 PASS가
없거나 exit code가 0이 아니면 실패다.

`test-redis-acl-docker`는 임시 Redis 7.4와 합성 operator/maintenance password를 사용한다.
Maintenance user의 실제 enqueue/dequeue/execution/result/scheduler lifecycle이 성공하고, 다른 queue/
rate-limit key와 Redis 관리·enumeration·script/pubsub 명령이 ACL에서 거부돼야 한다.

네 smoke는 local Docker 권한 경계를 검증하지만 다음을 증명하지 않는다.

- 현재 host가 arm64라면 최종 `linux/amd64` image
- 설치형 Manager의 PostgreSQL/Redis/외부 TLS/실제 MinIO data plane
- production secret rotation 또는 외부 S3 IAM
- vulnerability/container/secret scan과 법적 license 검토

역사적 dev.16 source의 2026-07-12 재실행에서는 위 독립 target들이 PASS했지만 실행 image는
`linux/arm64`였다. 이 과거 기록을 dev.20 `linux/amd64` archive identity 증거로 사용하지 않는다.

정상 종료와 일반 실패에서는 trap이 test container/network/volume을 정리한다. Build한 아래 test
image와 Docker build cache/base image는 의도적으로 남는다. 다른 시험이 사용하지 않는 것을 확인한
뒤 필요할 때만 정확한 tag를 지운다.

```bash
docker image rm \
  rvc-orchestrator-mlflow:nonroot-smoke \
  rvc-orchestrator-api:secret-projection-smoke \
  rvc-orchestrator-mlflow:secret-projection-smoke
```

강제 종료나 Docker daemon 장애 뒤에는 먼저 이름만 조회한다. `rvc-orchestrator-manager` production
project/container/volume을 삭제하면 안 된다. 조회 결과의 `rvc-secret-projection-<PID>-*` 또는
`rvc-minio-policy-<PID>` test 자원임을 사람이 확인한 뒤 그 정확한 이름만 정리한다.

```bash
docker ps -a --format '{{.Names}}' | awk '/^rvc-(secret-projection|minio-policy)-/'
docker volume ls --format '{{.Name}}' | awk '/^rvc-(secret-projection|minio-policy)-/'
docker network ls --format '{{.Name}}' | awk '/^rvc-(secret-projection|minio-policy)-/'
```

### 5.4 통합 Manager Compose smoke

세 보안 smoke와 별도로 API/Web/MLflow를 build하고 PostgreSQL·Redis·MinIO·RQ·proxy까지 고유
Compose project에서 함께 기동하려면 다음을 실행한다.

```bash
make test-manager-full-stack-docker
```

합격 조건은 exit code 0과 다음 형식의 마지막 줄이다.

```text
Manager full Compose stack smoke: PASS (docker_architecture=...)
```

이 target은 development/HTTP/Fake 허용 설정과 `RVC_IMAGE_PULL_POLICY=missing`을 사용하며 기본적으로
API/Web/MLflow image를 build하고 필요한 base/dependency image를 pull할 수 있다. 네 개의 dynamic
loopback port를 사용하고 readiness는 최대 약 240초를 기다린다. HTTP health/UI, API·RQ UID
`10001`, MLflow UID `10002`, Web UID `1001`,
역할별 exact runtime secret inventory, MinIO exact policy와 cross-bucket deny, release label을
확인하고 migration→maintenance DB authz→RQ 시작 순서, runtime DB self-verify와 Redis ACL도
검증한 뒤 임시 project/volume을 정리한다. 외부 TLS/browser, clean Ubuntu 설치나 linux/amd64 여부를
대체하지 않는다.

`RVC_STACK_SMOKE_SKIP_BUILD=1`은 exact test image를 이미 준비한 경우에만 사용한다. dev.20 archive를
load하고 5.5절의 `verify-loaded`를 통과한 뒤 exact release image를 재시험하는 명령은 다음과 같다.

```bash
RVC_STACK_SMOKE_SKIP_BUILD=1 \
RVC_STACK_SMOKE_API_IMAGE=rvc-orchestrator-api:0.1.0-dev.20 \
RVC_STACK_SMOKE_WEB_IMAGE=rvc-orchestrator-web:0.1.0-dev.20 \
RVC_STACK_SMOKE_MLFLOW_IMAGE=rvc-orchestrator-mlflow:0.1.0-dev.20 \
RVC_STACK_SMOKE_VERSION=0.1.0-dev.20 \
RVC_STACK_SMOKE_REVISION=298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a \
make test-manager-full-stack-docker
```

`RVC_STACK_SMOKE_KEEP=1`은 실패 조사용 project와 합성 secret temp tree를 남기므로 공유/운영
호스트에서는 사용하지 말고, 사용했다면 출력된 정확한 test path/project만 수동 정리한다.
기본 macOS 실행은 Colima/Docker Desktop이 공유할 수 있도록 저장소의 `.rvc-stack-smoke/` 아래에
임시 secret을 만들고 정상 종료 시 제거한다. 별도 Docker 공유 경로가 필요할 때만
`RVC_STACK_SMOKE_WORK_PARENT`를 regular local directory로 지정한다.

dev.20 archive와 identity가 일치하는 8개 `linux/amd64` release image를 arm64 Colima에서
에뮬레이션하고 위 exact-image 명령을 실행한 결과는 exit code 0과 다음 최종 문자열로 PASS했다.

```text
Manager full Compose stack smoke: PASS (docker_architecture=amd64)
```

이 결과는 dev.20 archive image의 amd64 runtime 통합 기준선이다. Host가 arm64이고 emulation을
사용했으므로 clean Ubuntu amd64 installer, systemd/reboot, 외부 TLS/browser 또는 production 승인
증거는 아니다. 사용자는 위 명령을 직접 실행해 자신의 exit code, 최종 PASS 문자열과
`docker_architecture`를 별도 증적으로 남긴다.

### 5.5 dev.20 archive 외부/내부 checksum과 image identity

Archive와 같은 이름의 `.sha256` sidecar가 모두 있어야 한다. 하나라도 없거나 아래 고정 hash와
다르면 설치하지 않고 `FAIL — dev.20 bundle integrity`로 기록한다.

아래는 Ubuntu/GNU coreutils 명령이다. macOS 검증 host에서는 `sha256sum -c FILE`을
`shasum -a 256 -c FILE`로, 마지막 `stat -c '%a %n' PATH`를
`stat -f '%Lp %N' PATH`로 바꾼다. 실제 설치 host의 T3/T4 명령은 지원 대상 Ubuntu에서 원문대로
실행한다.

```bash
(
  set -Eeuo pipefail
  cd dist/installers

  verify_external_archive() {
    local archive=$1 expected=$2 sidecar sidecar_hash sidecar_name sidecar_extra actual
    sidecar="$archive.sha256"
    test -f "$archive" && test ! -L "$archive"
    test -f "$sidecar" && test ! -L "$sidecar"
    test "$(wc -l < "$sidecar" | tr -d '[:space:]')" = 1
    read -r sidecar_hash sidecar_name sidecar_extra < "$sidecar"
    sidecar_name=${sidecar_name#\*}
    test -z "${sidecar_extra:-}"
    test "$sidecar_hash" = "$expected"
    test "$sidecar_name" = "$archive"
    actual=$(sha256sum "$archive" | awk '{print $1}')
    test "$actual" = "$expected"
    sha256sum -c "$sidecar"
  }

  verify_external_archive \
    rvc-manager-0.1.0-dev.20-linux-amd64.tar.gz \
    c6488dad47c7f38c082ed6fa68f1fe3691c069110aef0bbf68a9d7ba5e6f5b70
  verify_external_archive \
    rvc-worker-0.1.0-dev.20-linux-amd64.tar.gz \
    7f36cbf27100bf70425c2780142d4fa3f6e6e76d0acf410d3e3fb698aa50558b
)
```

기대 SHA-256은 다음과 같다.

```text
Manager  c6488dad47c7f38c082ed6fa68f1fe3691c069110aef0bbf68a9d7ba5e6f5b70
Worker   7f36cbf27100bf70425c2780142d4fa3f6e6e76d0acf410d3e3fb698aa50558b
```

이 값은 bundle builder가 만든 외부 sidecar, 개발 이력과 동일해야 한다. 위 함수는 승인된 고정
값과 sidecar의 hash/파일명, archive에서 직접 계산한 hash를 각각 대조한다. 고정 값의 신뢰 출처는
archive/sidecar와 독립된 승인 배포 공지·서명 metadata·조직 전달 위치여야 하며 결과 양식에 적는다.
dev.19 이하의 역사적 hash를 복사하거나 checksum 파일을 다시 만들어 실패를 우회하지 않는다.

별도 임시 디렉터리에 압축을 풀고 내부 checksum도 검사한다.

```bash
BUNDLE_TEST_ROOT=$(mktemp -d /tmp/rvc-dev20-bundle-test.XXXXXX)
(
  set -Eeuo pipefail
  bundle_dir="$PWD/dist/installers"
  test -d "$BUNDLE_TEST_ROOT" && test ! -L "$BUNDLE_TEST_ROOT"
  test -z "$(find "$BUNDLE_TEST_ROOT" -mindepth 1 -print -quit)"

  tar -xzf "$bundle_dir/rvc-manager-0.1.0-dev.20-linux-amd64.tar.gz" \
    -C "$BUNDLE_TEST_ROOT"
  tar -xzf "$bundle_dir/rvc-worker-0.1.0-dev.20-linux-amd64.tar.gz" \
    -C "$BUNDLE_TEST_ROOT"

  cd "$BUNDLE_TEST_ROOT/rvc-manager-0.1.0-dev.20-linux-amd64"
  sha256sum -c SHA256SUMS
  python3 common/image_bundle.py verify-ledger \
    --root . \
    --ledger-name SHA256SUMS
  python3 common/image_bundle.py verify-bundle \
    --root . \
    --component manager \
    --version 0.1.0-dev.20 \
    --source-commit 298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a
  grep -Fx 'VERSION=0.1.0-dev.20' manifest.env
  grep -Fx 'SCHEMA_COMPATIBILITY=f5d1c8a9b240' manifest.env
  grep -Fx 'GIT_COMMIT=298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a' manifest.env
  grep -Fx 'SELF_CONTAINED=true' manifest.env
  python3 - <<'PY'
import json

with open("images-manifest.json", encoding="utf-8") as stream:
    manifest = json.load(stream)
assert manifest["format_version"] == 2
assert manifest["component"] == "manager"
assert manifest["version"] == "0.1.0-dev.20"
assert manifest["platform"] == "linux/amd64"
assert manifest["self_contained"] is True
assert {image["role"] for image in manifest["images"]} == {
    "api", "web", "mlflow", "postgres", "redis", "minio", "minio-client", "nginx"
}
assert len(manifest["images"]) == 8
assert all(image["os"] == "linux" and image["architecture"] == "amd64"
           for image in manifest["images"])
assert len(manifest["archives"]) == 1
PY
  test -s README.md
  test -s TESTING.md
  test -s TEST_RESULT_TEMPLATE.md
  grep -En '0\.1\.0-dev\.20|verify-ledger' README.md TESTING.md

  cd "$BUNDLE_TEST_ROOT/rvc-worker-0.1.0-dev.20-linux-amd64"
  sha256sum -c SHA256SUMS
  python3 common/image_bundle.py verify-ledger \
    --root . \
    --ledger-name SHA256SUMS
  python3 common/image_bundle.py verify-bundle \
    --root . \
    --component worker \
    --version 0.1.0-dev.20 \
    --source-commit 298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a
  grep -Fx 'VERSION=0.1.0-dev.20' manifest.env
  grep -Fx 'GIT_COMMIT=298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a' manifest.env
  grep -Fx 'SELF_CONTAINED=false' manifest.env
  grep -Fx 'RVC_RUNTIME_INCLUDED=false' manifest.env
  grep -Fx 'RVC_NATIVE_RUNNER_AVAILABLE=false' manifest.env
  grep -Fx 'RVC_GPU_SMOKE_VERIFIED=false' manifest.env
  grep -Fx 'RVC_PROFILE_STAGE_SET_VERIFIED=false' manifest.env
  grep -Fx 'RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false' manifest.env
  python3 - <<'PY'
import json

with open("images-manifest.json", encoding="utf-8") as stream:
    manifest = json.load(stream)
assert manifest == {
    "archives": [],
    "component": "worker",
    "format_version": 2,
    "images": [],
    "platform": "linux/amd64",
    "self_contained": False,
    "version": "0.1.0-dev.20",
}
PY
  stat -c '%a %n' infra/worker/runtime/runtime-activation.json
  python3 -m json.tool infra/worker/runtime/runtime-activation.json
  test -s README.md
  test -s TESTING.md
  test -s TEST_RESULT_TEMPLATE.md
  grep -En '0\.1\.0-dev\.20|verify-ledger' README.md TESTING.md
)
```

Manager image를 load할 권한이 있는 격리된 검증 Docker daemon에서는 static 검증 뒤 archive와 실제
daemon identity를 결박한다. `docker load`와 `verify-loaded`를 모두 exit code 0으로 통과해야 하며,
다른 tag의 기존 image inspect로 대신하지 않는다.

```bash
(
  set -Eeuo pipefail
  cd "$BUNDLE_TEST_ROOT/rvc-manager-0.1.0-dev.20-linux-amd64"
  docker load --input images/manager-images.tar.gz
  python3 common/image_bundle.py verify-loaded \
    --root . \
    --component manager \
    --version 0.1.0-dev.20 \
    --source-commit 298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a
)
```

두 bundle의 합격 경계는 다음과 같다.

- format version 2, component/version/platform 일치
- Manager `SELF_CONTAINED=true`, exact archive 1개와 위 8개 `linux/amd64` image
- Worker `SELF_CONTAINED=false`, image/archive inventory 비어 있음
- Manager schema marker `f5d1c8a9b240`
- 두 bundle `GIT_COMMIT=298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a`
- Manager env/Compose에 `PUBLIC_SCHEME`, Worker env/Compose에
  `SYSTEM_TELEMETRY_INTERVAL_SECONDS=60` 포함
- Worker runtime/native/GPU/profile/sample gate 모두 false
- 각 bundle의 checksum, exact ledger, manifest verifier가 모두 오류 없이 exit code 0으로 종료
- 각 bundle의 `README.md`와 `TESTING.md`에 해당 version과 bundle-local 검증 명령,
  `TEST_RESULT_TEMPLATE.md`에 PASS/FAIL/BLOCKED 기록 양식 포함
- Worker `infra/worker/runtime/runtime-activation.json` stat mode `444`, digest `null`, F0 목록 `[]`

Manager는 **self-contained archive/image closure 합격**, Worker는 **CONFIG-ONLY partial 합격**이다.
Manager의 `SBOM_STATUS=partial-release-gates-open`, clean Ubuntu 설치·외부 TLS/browser 미실행과 Worker
runtime 부재 때문에 전체 air-gapped/production 합격은 아니다.

2026-07-13 기준 산출물 검증에서는 Manager 외부 sidecar/checksum, 54개 내부 ledger 항목, exact
8-image archive descriptor/config/layer closure와 `verify-loaded`가 모두 PASS했다. Worker는 외부
sidecar/checksum, 내부 ledger와 empty image/archive closure가 PASS했다. 이 기준 증적은 사용자가
전달받은 byte와 Docker daemon에서 위 명령을 다시 실행하는 것을 대체하지 않는다.

검증이 끝나면 현재 디렉터리를 임시 tree 밖으로 옮긴 뒤, `mktemp`가 만든 정확한 경로만 정리한다.

```bash
cd /tmp
case "$BUNDLE_TEST_ROOT" in
  /tmp/rvc-dev20-bundle-test.*) rm -rf -- "$BUNDLE_TEST_ROOT" ;;
  *) echo "unexpected bundle test path; refusing cleanup" >&2; exit 1 ;;
esac
unset BUNDLE_TEST_ROOT
```

## 6. T3 — Manager clean-host 설치 smoke

`docs/INSTALLATION_GUIDE.md`의 Manager image 준비, `--no-start`, TLS/DNS/env 설정 순서를 완료한
clean Ubuntu VM에서 수행한다.

dev.20 Manager 후보는 8개 image를 포함하므로 별도 source build 없이 설치할 수 있지만, clean Ubuntu
설치 증적은 아직 없다. `PUBLIC_SCHEME=https`를 operator-owned 기준으로 사용하고 client가 보낸
scheme을 무시한다. 아래 기능 smoke에서 systemd/reboot, Secure cookie, 단일 edge HSTS와 실제 외부
인증서/Host 전달까지 확인하기 전 T3는 `BLOCKED`다. Archive image를 다른 build로 교체했다면 결과
전체에 `SOURCE-MIXED`를 표시하고 dev.20 exact-image 인수로 인정하지 않는다.

### 6.1 기동과 readiness

```bash
(
  set -Eeuo pipefail
  systemctl is-enabled rvc-orchestrator-manager.service
  systemctl is-active rvc-orchestrator-manager.service
  MANAGER_RELEASE=$(sudo readlink -f /opt/rvc-orchestrator/manager/current)
  case "$MANAGER_RELEASE" in
    /opt/rvc-orchestrator/manager/releases/*) ;;
    *) echo "Manager current resolves outside releases" >&2; exit 1 ;;
  esac
  sudo stat -c '%U:%G %a %n' \
    "$MANAGER_RELEASE/RELEASE_SHA256SUMS"
  sudo python3 /opt/rvc-orchestrator/manager/lib/image_bundle.py \
    verify-ledger \
    --root "$MANAGER_RELEASE" \
    --ledger-name RELEASE_SHA256SUMS
  sudo /opt/rvc-orchestrator/manager/bin/manager-compose ps -a
  curl --fail --silent --show-error https://manager.example.com/healthz
  curl --fail --silent --show-error https://manager.example.com/readyz \
    | python3 -m json.tool
)
```

합격 기준:

- systemd enabled/active
- healthcheck가 있는 장기 service는 `healthy`
- `rq-worker`는 `running`; `/readyz`의 `rq_worker=ok`
- `manager-secrets-init`, `minio-init`, spool/dataset init과 migration service는 성공 종료(0)
- `/readyz.status=ready`
- `database`, `redis`, `rq_worker`, `maintenance_reconciler`, 정상 baseline의 `mlflow`가 `ok`

실패 시 component별 log를 보존한다.

```bash
sudo /opt/rvc-orchestrator/manager/bin/manager-compose logs --tail=300 \
  postgres redis minio minio-init manager-secrets-init api-migrate api rq-worker mlflow web proxy
```

### 6.2 실행 사용자·secret projection·MinIO policy 인수

서비스가 ready인 실제 설치 host에서 secret **내용을 출력하지 않고** 실행 경계를 확인한다.
`docker inspect` 전체 JSON, `manager.env`, `/run/secrets/current/*`의 `cat` 결과는 증적에 넣지 않는다.

```bash
(
  set -Eeuo pipefail
  MANAGER_COMPOSE=/opt/rvc-orchestrator/manager/bin/manager-compose

  sudo "$MANAGER_COMPOSE" exec -T api sh -eu -c '
    test "$(id -u):$(id -g)" = "10001:10001"
    test "$(stat -Lc "%u:%g %a" /run/secrets/current/jwt_secret)" = "10001:10001 400"
    test ! -e /run/secrets/current/mlflow_postgres_password
    echo "API runtime identity/secret scope: PASS"
  '

  sudo "$MANAGER_COMPOSE" exec -T rq-worker sh -eu -c '
    test "$(id -u):$(id -g)" = "10001:10001"
    test "$(stat -Lc "%u:%g %a" /run/secrets/current/postgres_password)" = "10001:10001 400"
    test ! -e /run/secrets/current/jwt_secret
    test ! -e /run/secrets/current/worker_bootstrap_token
    test ! -e /run/secrets/current/worker_token_pepper
    echo "Maintenance runtime identity/secret scope: PASS"
  '

  sudo "$MANAGER_COMPOSE" exec -T mlflow sh -eu -c '
    test "$(id -u):$(id -g)" = "10002:10002"
    test "$(stat -Lc "%u:%g %a" /run/secrets/current/mlflow_s3_secret_key)" = "10002:10002 400"
    test ! -e /run/secrets/current/jwt_secret
    test ! -e /run/secrets/current/minio_app_secret_key
    test "$(awk "/^CapEff:/ {print \$2}" /proc/self/status)" = "0000000000000000"
    echo "MLflow runtime identity/capability/secret scope: PASS"
  '
)
```

각 명령이 exit code 0과 정확한 PASS 줄을 내야 한다. UID/mode만 맞고 금지 secret이 보이거나,
금지 secret만 없고 서비스 user가 root이면 FAIL이다. 다음은 image/container의 허용된 field만 확인한다.

```bash
(
  set -Eeuo pipefail
  MANAGER_COMPOSE=/opt/rvc-orchestrator/manager/bin/manager-compose
  for service in api web mlflow; do
    cid=$(sudo "$MANAGER_COMPOSE" ps -q "$service")
    test -n "$cid" || { echo "missing container: $service" >&2; exit 1; }
    image_id=$(sudo docker inspect --format '{{.Image}}' "$cid")
    sudo docker image inspect --format \
      "$service id={{.Id}} architecture={{.Architecture}} user={{.Config.User}} version={{index .Config.Labels \"org.opencontainers.image.version\"}} revision={{index .Config.Labels \"org.opencontainers.image.revision\"}}" \
      "$image_id"
  done

  MLFLOW_CID=$(sudo "$MANAGER_COMPOSE" ps -q mlflow)
  sudo docker inspect --format \
    'user={{.Config.User}} read_only={{.HostConfig.ReadonlyRootfs}} pids={{.HostConfig.PidsLimit}} cap_drop={{json .HostConfig.CapDrop}}' \
    "$MLFLOW_CID"
)
```

API/Web/MLflow image user는 비어 있거나 `0`/`root`이면 안 된다. MLflow의 마지막 줄은
`user=10002:10002`, `read_only=true`, `pids=128`, `cap_drop=["ALL"]`이어야 한다. Image
architecture가 `amd64`가 아닌 결과는 최종 Ubuntu amd64 합격 증거가 아니다. Dev.20
self-contained 후보의 application image version은 `0.1.0-dev.20`, revision은
`298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a`이고 installed manifest와 정확히 같아야 한다. 하나라도
다르거나 archive 밖 image로 대체했다면 `SOURCE-MIXED`로 기록한다.

실제 설치 MinIO의 policy mapping과 cross-bucket deny를 **읽기 전용**으로 확인한다. 아래 명령은
root/service credential을 변수로만 읽고 값이나 object key를 출력하지 않는다. `set -x`를 붙이면 안
된다. `manager-compose run`은 시작 전 runtime secret projection을 한 번 새 generation으로
갱신하지만 MinIO object나 policy를 변경하지 않는다.

```bash
MANAGER_COMPOSE=/opt/rvc-orchestrator/manager/bin/manager-compose
sudo "$MANAGER_COMPOSE" run --rm --no-deps --entrypoint /bin/sh minio-init -eu -c '
  root_user=$(tr -d "\r\n" < /run/secrets/minio_root_user)
  root_password=$(tr -d "\r\n" < /run/secrets/minio_root_password)
  app_user=$(tr -d "\r\n" < /run/secrets/minio_app_access_key)
  app_password=$(tr -d "\r\n" < /run/secrets/minio_app_secret_key)
  mlflow_user=$(tr -d "\r\n" < /run/secrets/mlflow_s3_access_key)
  mlflow_password=$(tr -d "\r\n" < /run/secrets/mlflow_s3_secret_key)
  mc alias set local http://minio:9000 "$root_user" "$root_password" >/dev/null
  app_policy=$(mc admin policy entities local --user "$app_user" --json)
  mlflow_policy=$(mc admin policy entities local --user "$mlflow_user" --json)
  case "$app_policy" in *\"policies\":\[\"rvc-manager-app\"\]*) ;; *) exit 51 ;; esac
  case "$mlflow_policy" in *\"policies\":\[\"rvc-mlflow-artifacts\"\]*) ;; *) exit 52 ;; esac
  mc alias set app http://minio:9000 "$app_user" "$app_password" >/dev/null
  mc alias set flow http://minio:9000 "$mlflow_user" "$mlflow_password" >/dev/null
  mc ls "app/$S3_BUCKET" >/dev/null
  if mc ls "app/$MLFLOW_S3_BUCKET" >/dev/null 2>&1; then
    echo "Manager identity can access the MLflow bucket" >&2
    exit 53
  fi
  mc ls "flow/$MLFLOW_S3_BUCKET" >/dev/null
  if mc ls "flow/$S3_BUCKET" >/dev/null 2>&1; then
    echo "MLflow identity can access the Manager bucket" >&2
    exit 54
  fi
  echo "Installed MinIO exact policy/cross-bucket deny: PASS"
'
```

이 결과는 설치 host의 현재 policy를 확인하지만 browser presigned PUT/TLS/서명 보존을 대신하지
않는다. 그 data plane은 다음 브라우저 smoke에서 별도로 확인한다.

### 6.3 브라우저 기능 smoke

사용자 브라우저에서 다음을 순서대로 확인한다.

1. `https://manager.example.com` 인증서가 신뢰되고 관리자 login이 성공한다. Browser storage에서
   session cookie 값은 복사하지 말고 `HttpOnly`, `SameSite`, `Secure` flag만 확인한다. `Secure`가
   없거나 HSTS가 없거나 중복이면 `TLS-PRODUCTION`을 FAIL로 기록한다.
2. `사용자` 화면에서 일반 사용자 하나를 생성하고, 중복 이메일이 거부되는지 확인한다.
3. 별도 private browser profile에서 그 사용자의 초기 비밀번호로 로그인한다. `사용자`와
   `학습 서버` 메뉴가 보이지 않고 직접 API도 403인지 확인한다.
4. 관리자 profile에서 대상 비밀번호를 재설정한 뒤, 기존 사용자 profile의 다음 요청이 401과
   로그인 화면으로 끝나는지 확인한다. 새 비밀번호로만 재로그인이 성공해야 한다.
5. 관리자 profile에서 대상의 역할/활성 상태를 변경하고, 자기 관리자 계정의 강등/비활성화는
   거부되는지 확인한다. 비활성화 전 token은 재활성화 뒤에도 살아나지 않아야 한다.
6. 재활성화된 일반 사용자로 로그인해 작은 정상 PCM WAV 또는 안전한 ZIP Dataset을 업로드한다.
7. 브라우저 개발자 도구에서 object PUT의 CORS, TLS, S3 서명 오류가 없는지 확인한다.
8. Dataset이 ready/usable로 바뀌고 파일/길이/sample rate/품질 지표가 표시되는지 확인한다.
9. Experiment를 만들고 v1/v2·40k/48k·F0 조건 Job을 생성한다.
10. 실제 native Worker가 없으므로 Job이 `queued`에 남고 engine badge가 `실행 전`인지 확인한다.
    dev.20 partial Worker 상태에서는 이것이 기대 결과다. 과거 Fake fixture 결과를 조회할 때는
    `FAKE · 운영 결과 아님` badge와 접근 가능한 경고가 보여야 하며 `RVC WebUI`로 표시되면 FAIL이다.
11. 권한 없는 사용자에게 다른 사용자의 Dataset/Experiment가 노출되지 않는지 확인한다.

#### T3-REGISTRY — Model Registry 사용자 검증

dev.20 migration과 UI가 정상이라면 신규 Experiment 상세의 `Model Registry`는 version `0`, 현재
Champion 없음, 빈 후보/승인/폐기 목록으로 시작한다. dev.20 partial Worker만 있는 환경에는 eligible
real attempt가 없으므로 비교표의 Fake 또는 실행 전 Job에 `후보 등록` 동작이 보이지 않아야 한다.
여기까지는 `MODEL-REGISTRY-EMPTY/Fake-GATE PASS`로 기록할 수 있지만 후보·promotion 전체 인수는
`BLOCKED — qualified real attempt absent`로 남긴다.

검증된 self-contained Worker와 승인 runtime pair를 별도로 준비한 경우에만 폐기 가능한 Experiment와
시험 object namespace에서 다음을 수행한다.

1. 같은 Experiment에 current attempt가 `completed`인 real `rvc_webui` Job 두 개를 준비한다. 각
   attempt의 `worker-claim-v1`, reviewed commit, runtime image digest, asset manifest SHA-256과
   `final_small_model`/선택적 `final_index`가 Manager에서 검증 완료인지 확인한다.
2. 첫 Job 비교 행에서 후보 등록을 열고 Job/attempt, model/index 전체 SHA-256·크기, commit과 runtime
   provenance를 확인한 뒤 명시적으로 등록한다. Browser가 index artifact를 따로 선택하게 하면 FAIL이다.
3. Candidate를 promotion해 현재 Champion 1개가 되는지 확인한다. 두 번째 후보를 등록·promotion하면
   첫 entry는 `approved`인 비활성 rollback 후보로 남고 revoked로 바뀌거나 사라지면 안 된다.
4. 첫 approved entry를 다시 promotion해 rollback하고 active champion이 정확히 하나인지 확인한다.
   Candidate 하나와 active champion 하나를 각각 `quality_rejected|security_issue|operator_request` 중
   실제 사유에 맞는 reason으로 revoke해 champion 없음과 terminal
   `revoked` 이력을 확인한다. Revoked entry에 promotion 동작이 보이면 FAIL이다.
5. 다른 일반 사용자는 Experiment/registry를 404 경계로 보지 못하고 owner/admin만 관리할 수 있는지,
   response와 screenshot에 storage URI, object key, upload session, actor ID가 없는지 확인한다.
6. Stale row-version 또는 의도적으로 유발한 응답 유실 뒤 새 idempotency key로 같은 mutation을 즉시
   반복하지 않는다. UI가 잠기고 전체 원장을 다시 읽어 실제 상태를 확인한 뒤에만 다음 작업을
   허용해야 한다. 이 장애 주입은 production이 아닌 격리 proxy/test 환경에서만 수행한다.

실제 S3/MinIO 대용량 object 전체 재해시·tamper/outage, 실제 browser/API response-loss와 PostgreSQL
다중 replica 동시 promotion을 수행하지 못했다면 각각 `BLOCKED`로 남긴다. 자동 fixture `33 passed`를
이 항목의 대체 증거로 쓰지 않으며, production canonical object를 시험 목적으로 변조하지 않는다.

Screenshot에는 사용자 파일명, 이메일, ID와 query/token을 가린다. 실제 browser↔object upload가
성공해야 T3의 data-plane 항목을 PASS로 기록할 수 있다.

### 6.4 backup smoke

```bash
sudo /opt/rvc-orchestrator/manager/bin/backup
```

`BACKUP_PATH=...`가 출력되고 해당 디렉터리에 `.tar.gz`와 `.sha256`이 있어야 한다. backup 전후
`/readyz`가 다시 ready인지 확인한다. 이 backup은 config/secret을 포함하지 않으므로 별도 보관
증거도 남긴다.

### 6.5 reboot/upgrade smoke

일회용 VM에서만 reboot 후 동일 readiness를 다시 검사한다. Upgrade 시험은 old/new exact image와
두 bundle을 모두 보존하고 다음을 확인한다.

- 기존 config/secret/Docker volume 보존
- `current` symlink가 새 release를 가리킴
- active oneshot unit도 실제로 restart되어 새 image tag로 실행
- 새 bundle의 pending env/target Compose 검증 실패 시 기존 env byte와 `current`가 바뀌지 않음
- 같은 version과 낮은 version upgrade가 거부되고 낮은 Manager version은 guarded rollback만 사용
- migration, readiness, login, 기존 Dataset 조회 성공
- target start 실패는 nonzero와 일관된 target pointer/down 상태로 기록하고 임의 DB 역행을 하지 않음
- uninstall stop/down 일부 실패가 nonzero이며 성공으로 오인되지 않음

## 7. T4 — Worker dev.20 partial 구성과 보호 gate

이 절의 `fake --no-start` 결과는 installer/Compose/권한만 보는 `CONFIG-ONLY`다. Fake engine은 합성
log/artifact를 만드는 protocol fixture이며 RVC source, Torch, CUDA, FAISS나 GPU 학습을 실행하지
않는다. 다음 표현을 서로 바꿔 기록하지 않는다.

| 실제 결과 | 허용 판정 | 금지 표현 |
|---|---|---|
| `make test-e2e` | Fake protocol PASS | 실제 Worker/RVC E2E PASS |
| dev.20 partial `fake --no-start` | Worker CONFIG-ONLY PASS | 학습 서버 설치 완료 |
| dev.20 partial native 거부 | fail-closed gate PASS | native 학습 실패 |
| 별도 unverified runtime + acknowledgement | `NATIVE-CANDIDATE-UNVERIFIED` | production/GPU 검증 완료 |

### 7.1 preflight

실제 Worker 후보 호스트에서는 우회 옵션 없이 실행한다.

```bash
(
  set -Eeuo pipefail
  sudo ./preflight.sh
  nvidia-smi
  sudo docker info
)
```

GPU 없는 일회용 VM의 installer 구성 시험에서만 `--skip-gpu-check`를 쓸 수 있으며 결과를
`CONFIG-ONLY`로 표시한다.

### 7.2 `--no-start` 설치 결과

`docs/INSTALLATION_GUIDE.md` 5절의 fake/no-start 명령을 실행한 뒤 확인한다.
이 CONFIG-ONLY 시험에는 실제 Manager bootstrap token 대신 폐기 가능한 합성 token 파일을 쓰고,
service를 시작하지 않는다.

```bash
(
  set -Eeuo pipefail
  sudo /opt/rvc-orchestrator/worker/bin/worker-compose config --quiet
  WORKER_RELEASE=$(sudo readlink -f /opt/rvc-orchestrator/worker/current)
  case "$WORKER_RELEASE" in
    /opt/rvc-orchestrator/worker/releases/*) ;;
    *) echo "Worker current resolves outside releases" >&2; exit 1 ;;
  esac
  sudo stat -c '%U:%G %a %n' \
    "$WORKER_RELEASE/RELEASE_SHA256SUMS"
  sudo python3 /opt/rvc-orchestrator/worker/lib/image_bundle.py \
    verify-ledger \
    --root "$WORKER_RELEASE" \
    --ledger-name RELEASE_SHA256SUMS
  sudo stat -c '%u:%g %a %n' \
    /var/lib/rvc-orchestrator/worker \
    /etc/rvc-orchestrator/worker/secrets/worker_token \
    /etc/rvc-orchestrator/worker/rvc-profile.yaml
  sudo awk -F= '
    $1 == "ORCHESTRATOR_VERSION" ||
    $1 == "WORKER_IMAGE" ||
    $1 == "RVC_RUNNER_MODE" ||
    $1 == "RVC_IMAGE_PULL_POLICY" ||
    $1 == "RVC_GPU_SMOKE_VERIFIED" ||
    $1 == "RVC_PROFILE_STAGE_SET_VERIFIED" ||
    $1 == "SYSTEM_TELEMETRY_INTERVAL_SECONDS" {print}
  ' /etc/rvc-orchestrator/worker/worker.env
)
```

합격 기준은 release가 dev.20을 가리키고 `RELEASE_SHA256SUMS`가 `root:root 444`이며 exact ledger
검증을 통과하고, data directory가 `10001:10001 700`, token/profile이
`10001:10001 600`이며 Compose config가 유효한 것이다. Allowlist env 출력은 dev.20 image,
`RVC_RUNNER_MODE=fake`, pull policy `missing`, 두 검증 gate `false`, system telemetry `60`을 보여야
한다. `worker.env` 전체나 token 내용은 출력하지 않는다.

이 Worker service는 시작하지 않는다. production Manager가 Fake Worker를 거부하는 정책을
바꾸지 않는다.

### 7.3 fail-closed native gate

이 절은 7.2의 fake/no-start 구성 시험을 완료한 같은 일회용 clean VM과 압축을
해제한 dev.20 partial bundle 디렉터리를 전제로 한다. 먼저 manifest gate를 exact line으로
확인한다.

```bash
set -eu
grep -Fx 'SELF_CONTAINED=false' manifest.env
grep -Fx 'RVC_RUNTIME_INCLUDED=false' manifest.env
grep -Fx 'RVC_NATIVE_RUNNER_AVAILABLE=false' manifest.env
grep -Fx 'RVC_GPU_SMOKE_VERIFIED=false' manifest.env
grep -Fx 'RVC_PROFILE_STAGE_SET_VERIFIED=false' manifest.env
grep -Fx 'RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false' manifest.env
```

7.2에서 이미 저장한 Manager URL, Worker 이름과 폐기 가능한 token을 그대로 사용하므로
negative test에 token을 다시 전달하지 않는다. `--allow-unverified-gpu-runtime`까지 주어도
runtime 누락은 절대 우회되지 않아야 한다.

```bash
set -eu
NATIVE_GATE_LOG=$(mktemp /tmp/rvc-worker-native-gate.XXXXXX)
chmod 0600 "$NATIVE_GATE_LOG"
if sudo ./install.sh \
  --runner-mode native \
  --allow-unverified-gpu-runtime \
  --skip-gpu-check \
  --no-start >"$NATIVE_GATE_LOG" 2>&1; then
  echo "partial Worker unexpectedly accepted native mode" >&2
  exit 1
fi
grep -Fx \
  '[rvc-installer] error: native mode requires a Worker bundle with a verified offline RVC runtime' \
  "$NATIVE_GATE_LOG"
```

이 명령의 nonzero exit는 예정한 negative 결과다. 위 exact 오류 한 줄이 없거나 다른 선행 오류로
중단된 경우는 fail-closed gate PASS가 아니다. 이어서 기존 fake 구성이 변조되지 않았고
service/container가 시작되지 않았음을 확인한다.

```bash
set -eu
sudo grep -Fx 'RVC_RUNNER_MODE=fake' \
  /etc/rvc-orchestrator/worker/worker.env
sudo grep -Fx 'RVC_GPU_SMOKE_VERIFIED=false' \
  /etc/rvc-orchestrator/worker/worker.env
sudo grep -Fx 'RVC_PROFILE_STAGE_SET_VERIFIED=false' \
  /etc/rvc-orchestrator/worker/worker.env
sudo grep -Fx 'RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED=false' \
  /etc/rvc-orchestrator/worker/worker.env
if sudo systemctl is-active --quiet rvc-orchestrator-worker.service; then
  echo "Worker service unexpectedly active" >&2
  exit 1
fi
if sudo systemctl is-enabled --quiet rvc-orchestrator-worker.service; then
  echo "Worker service unexpectedly enabled" >&2
  exit 1
fi
WORKER_CID=$(sudo /opt/rvc-orchestrator/worker/bin/worker-compose ps -q worker)
test -z "$WORKER_CID"
unset WORKER_CID
```

Redaction한 `NATIVE_GATE_LOG`를 증적 디렉터리에 복사한 뒤 임시 파일을 정리한다.

```bash
set -eu
case "$NATIVE_GATE_LOG" in
  /tmp/rvc-worker-native-gate.*) rm -f -- "$NATIVE_GATE_LOG" ;;
  *) echo "unexpected native gate log path; refusing cleanup" >&2; exit 1 ;;
esac
unset NATIVE_GATE_LOG
```

위 요약 문구만 수기로 기록하지 말고, exact prefixed log와 사후 검사 모두를 함께
보존해야 fail-closed gate PASS다.

## 8. T5 — 실제 native GPU 학습 시험

이 단계는 다음이 모두 준비된 뒤에만 시작한다.

- runtime image 하나를 포함한 self-contained Worker bundle
- exact base/source/wheel/asset/projection manifest와 image ID/digest
- Manager와 object endpoint TLS 연결
- production Manager에서 발급 가능한 bootstrap 경로
- 시험용 비식별 Dataset/TestSet과 충분한 disk/VRAM
- Worker runtime image가 기본으로 신뢰하는 public CA chain, 또는 dev.20에서 아래 계약으로
  설치한 custom CA의 Manager/Object HTTPS chain

현재 dev.20 partial 계약만 있고 self-contained runtime bundle이 없다면 T5는 `FAIL`이
아니라 사유를 적은 `BLOCKED`로 기록한다.
`RVC_RUNNER_MODE=fake` 또는 generic Agent image에서 나온 결과는 어떤 경우에도 T5에 제출하지 않는다.

dev.20은 custom CA를 지원하지만 아래 시험을 통과해야 하며, host trust store만 수정하거나 TLS
검증 비활성화/image CA store 수동 변경 결과는
인정하지 않는다.

### 8.A — dev.20 custom CA 설치·negative 시험

Clean Ubuntu Worker에서 조직 CA certificate chain을 root-owned regular non-symlink mode `0644`
`/root/rvc-worker-custom-ca.pem`으로 준비하고 설치 가이드 5.3절 validator를 통과시킨 뒤
`--ca-bundle-file /root/rvc-worker-custom-ca.pem --no-start`로 설치한다. 설치 전 source의 SHA-256을
기록하되 certificate PEM 본문은 결과 문서에 붙이지 않는다.

```bash
(
  set -Eeuo pipefail
  sudo stat -c '%u:%g %a %s %n' /root/rvc-worker-custom-ca.pem
  sudo python3 common/worker_ca.py validate \
    --path /root/rvc-worker-custom-ca.pem --required-uid 0
  sudo stat -c '%u:%g %a %n' \
    /etc/rvc-orchestrator/worker/ca \
    /etc/rvc-orchestrator/worker/ca/custom-ca.pem
  sudo python3 /opt/rvc-orchestrator/worker/lib/worker_ca.py validate \
    --path /etc/rvc-orchestrator/worker/ca/custom-ca.pem --required-uid 0
  sudo awk -F= '
    $1 == "WORKER_CA_BUNDLE_HOST_DIR" ||
    $1 == "WORKER_CA_BUNDLE_PATH" {print}
  ' /etc/rvc-orchestrator/worker/worker.env
  sudo /opt/rvc-orchestrator/worker/bin/worker-compose config --quiet
)
```

Directory `0:0 755`, file `0:0 444`, host path와 fixed container path
`/etc/rvc-worker/ca/custom-ca.pem`이 정확히 한 번 보여야 한다. Wrapper의
`start|restart|run|create`는 매번 이 계약을 재검증해야 한다.

폐기 가능한 복제 VM/bundle에서 다음 negative를 각각 독립 실행한다. Invalid mode `0600`, source
symlink, `PRIVATE KEY`를 포함한 PEM은 validator/install이 nonzero여야 하며 기존 installed CA SHA-256,
`worker.env`, `current`가 바뀌면 실패다. 기대 실패를 만들기 위해 production CA 자체를 수정하지
않는다. Hostname mismatch는 CA가 맞더라도 certificate SAN에 없는 별칭을 Manager URL로 사용한
one-shot/register가 certificate verification error로 실패해야 한다. `verify=false`, `curl -k`,
`SSL_CERT_FILE` 우회 결과는 증거가 아니다.

Source 자동 회귀는 file mode/symlink/private key/NUL/size/PEM, atomic replacement 보존, system
default+custom trust, hostname mismatch, Manager/object 공통 strict context와 proxy 비사용을 검증한다.
그러나 이 fixture는 clean Ubuntu container에서 실제 Manager/Object HTTPS와 Dataset/TestSet/Artifact
전송을 수행한 증거가 아니다. 그 실연결과 install→reboot→upgrade→replacement 실패 보존은
`PENDING — CLEAN-UBUNTU-CUSTOM-CA-ENDPOINT`로 따로 기록한다.

### 8.0 Worker release-readiness report

실제 GPU 시험 전에 설치 가이드 6절의 `release_readiness.py`로 evidence report를 만든다. 아무 입력
없이 실행하면 exit `1`과 함께 모든 필수 항목이 `missing`으로 열거돼야 하며, 이는 현재 partial
상태의 정상적인 fail-closed 진단이다.

```bash
set +e
python3 infra/worker/runtime/release_readiness.py \
  --output /tmp/worker-release-readiness.json
READINESS_EXIT=$?
set -e
python3 -m json.tool /tmp/worker-release-readiness.json
test "$READINESS_EXIT" -eq 1
unset READINESS_EXIT
```

실제 후보에서는 6절의 전체 source/wheel/asset/build/runtime/qualification/review 인자를 사용한다.
Exit code 의미는 `0=열거된 evidence input 검증`, `1=missing/invalid/blocked dependency`,
`2=CLI/output 오류`다. Exit `0`도 출시 승인이 아니며 JSON은 항상
`activation_permitted=false`, `activation_projection_written=false`여야 한다. Report와 입력 manifest
hash를 증적에 남기되 evidence 안의 민감 경로/내용은 별도 redaction한다.

### 8.1 증적 수집

다음 정보를 시험 전에 보존한다.

```bash
(
  set -Eeuo pipefail
  uname -a
  cat /etc/os-release
  sudo docker version
  sudo docker compose version
  nvidia-smi
  WORKER_RELEASE_VERSION=0.1.0-rc.1
  WORKER_IMAGE="rvc-orchestrator-worker:$WORKER_RELEASE_VERSION"
  sudo docker image inspect --format \
    'id={{.Id}} architecture={{.Architecture}} user={{.Config.User}} version={{index .Config.Labels "org.opencontainers.image.version"}} revision={{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$WORKER_IMAGE"
)
```

추가로 bundle 외부/내부 checksum, `manifest.env`, `images-manifest.json`, runtime build manifest,
base digest, RVC source/wheelhouse/assets/projection manifest SHA-256을 기록한다. secret이 포함된
`worker.env` 전체를 복사하지 않는다. `WORKER_RELEASE_VERSION`은 실제 후보로 바꾼다. 허용된
format inspect만 남기고 전체 image/container inspect JSON은 증적에 복사하지 않는다.

### 8.2 Worker service·Manager 등록 smoke

학습 Job을 만들기 전에 Worker 자체 연결을 먼저 확인한다.

설치 가이드 6절의 native `--no-start` 설치를 마친 직후에는 service를 enable하기 전에 다음
one-shot을 먼저 통과시킨다. Installed wrapper가 ledger/environment/loaded image/activation을
재검증하고 실제 GPU/mount 구성으로 health check를 실행한다.

```bash
set -Eeuo pipefail
sudo /opt/rvc-orchestrator/worker/bin/worker-compose \
  run --rm --no-deps worker --check | python3 -m json.tool
```

Exit code 0과 아래 `--check` JSON 기준을 확인한 뒤에만 systemd service를 시작한다. 실패한
one-shot을 우회해 이미 실행 중인 container의 `exec` 결과만 제출하지 않는다.

```bash
(
  set -Eeuo pipefail
  WORKER_COMPOSE=/opt/rvc-orchestrator/worker/bin/worker-compose
  sudo systemctl is-active rvc-orchestrator-worker.service
  sudo "$WORKER_COMPOSE" ps
  sudo "$WORKER_COMPOSE" logs --tail=200 worker

  WORKER_CID=$(sudo "$WORKER_COMPOSE" ps -q worker)
  test -n "$WORKER_CID"
  WORKER_IMAGE_ID=$(sudo docker inspect --format '{{.Image}}' "$WORKER_CID")
  sudo docker image inspect --format \
    'id={{.Id}} architecture={{.Architecture}} user={{.Config.User}} version={{index .Config.Labels "org.opencontainers.image.version"}} revision={{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$WORKER_IMAGE_ID"

  sudo "$WORKER_COMPOSE" exec -T worker \
    python -m rvc_worker --check | python3 -m json.tool
)
```

Manager `학습 서버` 화면에서 예상한 이름의 Worker가 `online`, 최근 heartbeat, 정확한 GPU 수·index·
VRAM으로 보이고 current Job이 비어 있어야 한다. Worker 재부팅 뒤에도 같은 Worker ID로 재접속하고
credential file mode `0600`이 유지돼야 한다. 새 ID가 생기거나 token이 log에 보이면 학습을 시작하지
않고 FAIL로 기록한다.

실제 native 후보의 image는 `linux/amd64`, user `10001:10001`, 후보 version과 committed revision을
보여야 한다. `--check` JSON은 최소 `ok=true`, `settings.runner_mode=native`, `gpu_available=true`,
`gpu_telemetry_available=true`, 예상 `gpu_count`, reviewed `rvc_native_revision`,
`rvc_native_assets_ready=true`를 보여야 한다. 이 값은 native runtime을 읽을 수 있다는 선행 조건일
뿐 GPU 학습 성공 증거가 아니다. `fixed_test_set_inference_ready=true`와 네 inference F0 목록은
exact 49-case qualification이 결박된 후보에서만 허용한다. Core-only/unverified 후보에서 이 값이
false/빈 목록인 것은 정상이며 Sample PASS로 승격하지 않는다.

### 8.3 최소 core 학습 matrix

각 행은 독립 Job/attempt로 한 epoch만 실행하고 결과를 섞지 않는다.

| 축 | 필수 값 |
|---|---|
| RVC version | v1, v2 |
| sample rate | 40k, 48k |
| F0 사용 | false, true |
| F0 방식 보충 | pm, harvest, dio, rmvpe, rmvpe_gpu |
| index | off, on |
| GPU routing | single GPU, 지원할 경우 multi-GPU |

기본 교차 행은 `v1/v2 × 40k/48k × F0 false/true` 8개다. F0=true 기본 행 외에 모든 training
F0 방식과 RMVPE GPU ID routing을 추가 확인한다. Job마다 다음을 검증한다.

- Worker가 online이고 GPU/VRAM/heartbeat가 대시보드에 보임
- immutable Job config와 claim config가 일치
- preprocess/F0/feature/train/checkpoint/index/export stage가 한 번씩만 실행
- lease renew, training terminal 전 stdout/train.log/TensorBoard 기반 log/loss/epoch와 GPU metric
  지속 수신. `current_epoch`가 현재 attempt에서만 단조 증가해야 함. GPU별
  `system.gpu.<index>.utilization_percent|vram_used_mb|vram_total_mb|temperature_c`와
  `system.gpu.telemetry_available`, `system.disk_free_bytes`의 시작 직후/설정 cadence와 단위를 함께
  확인. Dashboard 최신 200개 tail이 15초 polling으로 갱신되고 요청이 겹치지 않아야 함
- v1은 `3_feature256`, v2는 `3_feature768`
- `G_*.pth`와 `D_*.pth` pair가 checkpoint 역할로 등록
- `final_small_model.pth`가 checkpoint 단순 복사가 아닌 deployable model 역할로 등록
- index-on에서 `final.index`, 원래 `added_*.index` 이름/checksum, `total_fea.npy` 보존
- `environment.json`, `config.json`, `artifact_manifest.json`과 source/runtime provenance 일치
- terminal `completed` 뒤 lease 해제, 실패 시 typed error code와 secret-safe message

취소 시험은 학습 중 cancel하여 process group이 종료되고 Job이 `cancelled`가 되는지 본다. Worker
재시작/네트워크 단절 시험은 lease 상한, telemetry spool replay, 중복 stage 미실행과 새 attempt
분리를 확인한다. Manager 연결만 짧게 끊었다가 active lease 또는 커밋된 terminal watermark 안에서
복구하면 pending sequence가 중복 없이 전달돼야 한다. Terminal status가 커밋되기 전 Manager 전체
장애가 lease 회수까지 이어진 경우에는 watermark가 없어 old attempt telemetry 자동 replay를
합격 조건으로 두지 않는다. 해당 pending/dead-letter와 status/lease event를 보존해 운영 잔여 위험으로
기록하고 새 attempt에 수동으로 합치지 않는다.

### 8.4 Sample matrix — 현재 production 경로는 차단 상태

향후 production Agent가 실제로 capability를 광고하고 approved runtime pair가 Manager에 등록된
뒤에만 다음을 실행한다.

| inference F0 | index off | index on |
|---|---:|---:|
| PM | 필수 | 필수 |
| Harvest | 필수 | 필수 |
| CREPE | 필수 | 필수 |
| RMVPE | 필수 | 필수 |

Factory/capability 연결은 구현됐지만 exact 49-case qualification과 현재 asset/image binding을 모두
통과한 mode `0444` activation에서만 열린다. 현재 실제 증적이 없으므로 disabled projection과
capability false 상태에서 이 표를 수동 flag 변경으로 실행하지 않는다. 현재 판정은
`BLOCKED — real runtime qualification evidence absent`다. 증적 schema와 projection 검증은
[Worker runtime qualification](RUNTIME_QUALIFICATION.md)을 따른다.

### 8.5 no-network 시험

시험 네트워크에서 Manager/object endpoint만 allowlist하고 public registry, package index, Git,
model host와 일반 DNS/egress를 차단한다. 구현 환경에 맞는 외부 firewall/namespace 정책을 사용하고
다음을 증명한다.

- image pull 없이 `RVC_IMAGE_PULL_POLICY=never`로 시작
- 빈 외부 cache에서도 source/wheel/model download 시도 없음
- proxy/token 환경을 Worker subprocess가 상속하지 않음
- 허용된 Manager/object 통신 외 DNS/connection 없음
- cache miss가 fail-closed하며 네트워크 fallback하지 않음

방화벽 정책, flow/DNS log, container log와 image/cache inventory를 함께 보존한다. 단순히 인터넷을
사용하지 않았다는 진술만으로 no-network PASS로 판정하지 않는다.

## 9. T6 — 복구와 파괴적 시험

### 9.1 격리 Docker volume drill

```bash
make test-manager-recovery-docker
```

이 명령은 고유 Compose project에 PostgreSQL/MinIO/Redis/작업 volume을 만들고
backup→변조→restore를 수행한 뒤 해당 격리 자원을 정리한다. Docker daemon과 필요한 base image가
필요하다. production host/project/volume을 대상으로 실행하지 않는다.

합격 증적은 exit code 0과 다음 두 PASS 줄이다.

```text
PASS: databases, object metadata, Redis, artifact spool, and Dataset work state were restored/reset
PASS: archive excluded Manager config paths, secret paths, and their values
```

정상 종료와 일반 실패에서는 고유 `rvc-recovery-drill-*` project와 다섯 named volume을 trap이
정리한다. Docker cleanup 자체가 실패하면 script도 실패하고 보호된 workspace 경로를 출력한다.
원인을 확인하기 전에 production volume이나 이름이 다른 project를 삭제하지 않는다. Local
`rvc-orchestrator-api:recovery-drill` image와 base image/build cache는 남을 수 있다.

### 9.2 복제 VM restore/rollback

실제 설치 restore는 production snapshot을 복제한 폐기 가능한 VM에서만 수행한다.

합격 조건:

- restore 전 자동 pre-restore backup 생성 또는 명시적 승인 기록
- 외부/내부 backup checksum 검증
- Manager/MLflow DB와 두 bucket의 byte/metadata 복구
- Redis와 임시 spool 상태는 의도대로 reset
- 복구 후 migration head/readiness/login/Dataset download 확인
- Manager rollback은 같은 compatibility marker와 실제 Alembic set에서만 수행
- rollback이 DB downgrade를 하지 않는다는 점 확인

Worker에는 자동 rollback script가 없다. Worker는 upgrade/reboot/uninstall 보존 시험을 별도
수행하고 Manager rollback 결과와 합치지 않는다.

## 10. 정리와 문제 해결

| 증상 | 판정과 조치 |
|---|---|
| `make check` 일부만 PASS | 전체 target의 최종 exit code가 0이 아니면 T0 FAIL. 실패 suite만 고쳐 다시 전체 실행 |
| `git ls-files` 결과가 비어 있음 | `git diff --check`가 실질적으로 아무것도 검사하지 않음. Git provenance/whitespace만 `BLOCKED`/`NOT EVIDENCED`로 기록하고 `make check`는 계속 실행해 executable source quality를 별도 판정 |
| E2E `permission denied`/`operation not permitted` on `127.0.0.1` | 제품 PASS가 아니라 실행환경 `BLOCKED`. localhost bind가 허용된 환경에서 같은 명령 재실행 |
| E2E가 4개보다 적게 collect | marker/수집 오류를 숨기지 말고 FAIL. `--ignore`나 임의 skip으로 맞추지 않음 |
| Docker daemon/permission 오류 | `docker info`부터 복구. `sudo`와 non-sudo 결과를 섞어 서로 다른 daemon/context를 시험하지 않음 |
| Docker의 legacy builder 경고 | 경고만으로 실패는 아님. 최종 PASS와 exit 0을 확인하고 builder/architecture를 증적에 기록 |
| MLflow smoke의 user가 빈 값/root | 잘못된 또는 오래된 image. 정확한 source/tag로 rebuild하고 PASS 전 진행 중단 |
| Secret smoke에 두 projection failure 줄 | 빈/symlink negative case의 기대 출력. 마지막 projection PASS가 없거나 exit nonzero면 실제 실패 |
| MinIO smoke 초반 `connection refused` | bounded readiness retry 중 가능. 최종 policy PASS가 없으면 실패이며 root credential을 출력하지 말고 Docker log를 조사 |
| Docker smoke 뒤 test 자원 잔여 | 5.3절 조회 명령으로 test prefix를 확인하고 정확한 test 이름만 정리. production project/volume 금지 |
| archive checksum/strict verifier 불일치 | 즉시 중단하고 원본 archive와 전달 경로를 다시 확보. `SHA256SUMS` 재생성 금지 |
| Manager `manager-secrets-init` 실패 | source secret의 regular-file/mode/비어 있지 않음과 projection volume을 조사. secret 원문을 log에 출력하지 않음 |
| `/readyz`는 실패하지만 `/healthz`는 성공 | liveness만 정상. PostgreSQL/Redis/RQ/MLflow component를 복구하기 전 Manager PASS 금지 |
| installed MinIO policy audit 실패 | exact service user mapping/cross-bucket deny가 깨진 상태. 광역 `readwrite`로 임시 우회하지 않음 |
| Fake Worker 등록 거부 | production의 기대 보호 동작. `ALLOW_FAKE_WORKERS`를 바꾸지 않고 T1 localhost fixture만 사용 |
| dev.20 partial native 설치 거부 | partial bundle의 기대 fail-closed. 7.3절 exact 오류와 사후 inactive/no-container/fake-env 검사를 모두 확인. `--allow-unverified-gpu-runtime`도 runtime 부재를 우회할 수 없음 |
| `--allow-unverified-gpu-runtime`으로 native 시작 | 검증 완료가 아니라 위험 확인. T5 matrix 전에는 `NATIVE-CANDIDATE-UNVERIFIED` |
| GPU count 0 | `gpu_telemetry_available=true`면 성공한 0-GPU 관측, false면 query/semantic 실패. 실제 GPU host에서는 둘 다 원인 조사 |
| Host `curl`은 성공하지만 Worker TLS 실패 | dev.20 installed CA owner/mode/PEM/fixed path와 certificate SAN을 확인하고 clean endpoint 시험 전 PASS 금지; 검증 비활성화 금지 |

시험 log를 제출하기 전 token, password, session cookie, access key, presigned query, 사용자 파일명과
절대 경로를 다시 redaction한다. Container/image의 전체 inspect와 전체 env dump는 수집하지 않는다.
`SOURCE-MIXED`, `CONFIG-ONLY`, `BLOCKED`, `NATIVE-CANDIDATE-UNVERIFIED` 표시는 log를 정리해도
제거하지 않는다.

## 11. 최종 판정표

| 항목 | PASS 조건 | 현재 dev.20 후보 판정 |
|---|---|---|
| Executable source quality | `make check` 전체 exit 0, 수집/경고 보존 | PASS 기준선: Ruff, mypy 88, Python 752+4 deselected, Web 24/211 |
| Git provenance/whitespace | non-empty tracked inventory와 `git diff --check` exit 0 | archive source `298ee1e…`, tracked 410개 기준 PASS; 사용자 checkout 재검증 필요 |
| Fake protocol | T1 exit 0, 범위 정확히 기록 | PASS 기준선: `4 passed in 6.68s` |
| Docker MLflow hardening | UID 10002, read-only/cap-drop/PID/tmpfs와 health PASS | source host에서 가능; host architecture 한정 |
| Docker secret projection | synthetic generation 교체/최소 inventory/negative 보존 PASS | source host에서 가능; production rotation 증거 아님 |
| Docker MinIO policy | 두 exact policy, cross-bucket deny와 broad-policy 제거 PASS | source host에서 가능; 외부 S3/TLS 증거 아님 |
| Manager full Compose | 고유 project에서 전 service ready, 역할별 secret/policy 경계와 정리 PASS | exact dev.20 `linux/amd64` release images PASS under arm64 emulation; clean-host 증거는 아님 |
| Manager self-contained bundle | 외부/내부 checksum, exact 8-image closure와 loaded identity 일치 | 기준 archive PASS; 사용자 daemon에서 재검증 필요 |
| Worker partial bundle | 외부/내부 checksum, empty image inventory와 모든 native gate false | CONFIG-ONLY PASS; runtime 없음 |
| Manager functional smoke | clean Ubuntu, ready/runtime identity/secret scope/policy/browser upload/backup | `BLOCKED` — clean Ubuntu/systemd/외부 TLS/browser 증거 대기 |
| Model registry 자동 회귀 | backend registry+migration과 Web BFF/UI 집중 명령 exit 0 | backend `33 passed`; 실제 browser/API·S3·multi-replica는 별도 |
| Model registry production 인수 | qualified real attempt, canonical 전체 재해시, Champion/rollback/revoke와 장애·경쟁 주입 | dev.20 partial Worker만으로 BLOCKED |
| Production TLS | operator scheme, Secure cookie/HSTS와 외부 TLS 검증 | 코드 준비, clean browser 증거 대기 |
| Worker custom CA TLS | strict projection/context와 실제 Manager/Object hostname·전송 | dev.20 source 회귀 준비, clean Ubuntu endpoint 증거 대기 |
| Worker config | preflight/config/권한, native 누락 거부 | 가능 |
| Native core | self-contained runtime + 실제 GPU matrix | dev.20 partial Worker 계약만으로 BLOCKED |
| Production Sample | 실제 capability + 4 F0/index matrix | BLOCKED |
| Air-gapped release | closed image closure, no-pull/no-network 증거 | BLOCKED |
| v1.0 release | 모든 보안·license·취약점·clean-host gate 완료 | BLOCKED |

하나의 단계가 PASS여도 더 높은 단계를 자동으로 PASS 처리하지 않는다. 특히 자동 fixture, Compose
렌더링, partial archive와 Fake Worker 결과를 실제 RVC/GPU 또는 production 설치 인증으로 확대
해석하지 않는다.

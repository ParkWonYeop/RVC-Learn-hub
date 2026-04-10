# Manager API

FastAPI와 SQLAlchemy 2 기반 중앙 관리 API다. PostgreSQL을 운영 원장으로 사용하며 테스트에서는 SQLite async driver를 지원한다. 외부 API는 `/api/v1`로 versioning하고 liveness/readiness는 `/health`, `/ready`에서 제공한다.

## 개발 실행

저장소 루트에서 다음을 실행한다.

```bash
python3 -m venv .venv
.venv/bin/pip install -r apps/api/requirements-dev.lock
.venv/bin/pip install --no-deps -e packages/contracts -e apps/api

DATABASE_URL=sqlite+aiosqlite:///./manager.db \
  .venv/bin/alembic -c apps/api/alembic.ini upgrade head
DATABASE_URL=sqlite+aiosqlite:///./manager.db \
  WORKER_BOOTSTRAP_TOKEN=local-bootstrap \
  WORKER_TOKEN_PEPPER=local-pepper \
  JWT_SECRET=local-jwt-secret-with-at-least-thirty-two-characters \
  .venv/bin/uvicorn rvc_manager_api.main:app --reload --port 8000
```

실제 운영 URL은 `postgresql+asyncpg://user:password@host/database` 형식을 사용한다. 운영 프로세스 시작 전에 별도 one-shot 작업으로 `alembic upgrade head`를 실행한다.

`JobRead.current_attempt_engine_mode`는 현재 Job의 exact `JobAttempt.engine_mode`를 나타낸다.
아직 attempt가 없으면 `null`, Fake Worker가 실행했으면 `fake`, 검증된 RVC WebUI adapter 실행이면
`rvc_webui`다. 이는 JobConfig의 희망 backend나 현재 Worker capability에서 추정하지 않으며
생성·목록·상세·취소·재시도 응답이 같은 batch projection을 사용한다. Fake 값은 운영 학습 결과의
증거가 아니다.

`9d2f4b7c8e10`으로 처음 upgrade하면 기존 Dataset/Artifact upload session은 현재 storage로
추측 귀속되지 않고 `UNBOUND`가 된다. upgrade 전 `pending|finalizing`을 모두 drain하고 backup을
만든다. terminal row는 `rvc-manager-adopt-storage-sessions --kind dataset|artifact` preview로
전체 object byte를 검증한 뒤 explicit `--session-id ... --apply`로만 결박한다. preview도 audit
event를 기록하며 자세한 중단/rollback/quota 주의사항은 `docs/OPERATIONS_GUIDE.md`를 따른다.

## 핵심 환경 변수

| 변수 | 의미 |
|---|---|
| `ENVIRONMENT` | `development`, `test`, `production` |
| `PUBLIC_SCHEME` | 외부 사용자가 접속하는 신뢰된 공개 scheme인 `http` 또는 `https`; production은 `https`만 허용하며 요청의 forwarding header로 추론하지 않음 |
| `PROCESS_ROLE` | `api` 또는 `maintenance`; 설치 Compose는 API/migration과 RQ Worker에 각각 고정하며 외부 override를 허용하지 않음 |
| `LOG_LEVEL` | 구조화 API log level; `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `DATABASE_URL` | SQLAlchemy async DB URL |
| `REDIS_URL` | Redis URL |
| `READINESS_CHECK_REDIS` | `/ready`에서 Redis ping을 필수화 |
| `RQ_ENABLED`, `RQ_QUEUE_NAME` | 중앙 maintenance RQ와 고정 queue 이름; 설치 기본 `true`, `rvc-maintenance` |
| `RQ_READINESS_TIMEOUT_SECONDS` | Redis/RQ Worker readiness probe 상한; 기본 2초 |
| `RQ_WORKER_HEARTBEAT_MAX_AGE_SECONDS`, `RQ_WORKER_TTL_SECONDS` | 최근 Worker heartbeat 허용 나이와 RQ idle heartbeat 주기 경계; 기본 75/45초 |
| `MAINTENANCE_CLEANUP_GRACE_SECONDS` | 만료/실패 Dataset staging 정리 전 유예; 기본 604800초(7일), `DATASET_UPLOAD_TTL_SECONDS` 이상이어야 함 |
| `MAINTENANCE_CLEANUP_BATCH_SIZE` | 한 run에서 검사할 session 상한; 기본 250, 최대 1000 |
| `MAINTENANCE_TASK_TIMEOUT_SECONDS` | RQ job과 내부 loop 시간 상한; 기본 300초 |
| `MAINTENANCE_TASK_MAX_ATTEMPTS` | storage 실패 시 총 attempt 상한; 기본 3 |
| `MAINTENANCE_RETRY_BACKOFF_SECONDS`, `MAINTENANCE_RETRY_BACKOFF_MAX_SECONDS` | 지수 retry 시작/상한; 기본 30/300초 |
| `MAINTENANCE_CLEANUP_CLAIM_STALE_SECONDS` | 중단된 DB cleanup claim 회수 기준; task timeout보다 길어야 하며 기본 900초 |
| `MAINTENANCE_RECONCILE_ENABLED` | API replica의 PostgreSQL 원장→RQ 유실 job reconciler; 운영 기본 `true` |
| `MAINTENANCE_RECONCILE_INTERVAL_SECONDS`, `MAINTENANCE_RECONCILE_STALE_SECONDS` | reconcile 주기와 readiness heartbeat 상한; 기본 15/120초 |
| `MAINTENANCE_RECONCILE_BATCH_SIZE` | 한 leader cycle에서 잠그고 확인할 run 상한; 기본 100 |
| `JWT_SECRET` 또는 `JWT_SECRET_FILE` | 사용자 access JWT HMAC key; production 필수 |
| `JWT_ISSUER`, `JWT_AUDIENCE` | 검증할 JWT issuer/audience |
| `JWT_ACCESS_TTL_SECONDS` | access JWT 수명; 기본 900초 |
| `WORKER_BOOTSTRAP_TOKEN` | 최초 Worker 등록용 공유 bootstrap secret |
| `WORKER_TOKEN_PEPPER` | DB에 저장할 Worker token HMAC용 비밀값 |
| `LEASE_SECONDS` | Job lease 갱신 간격의 상한 |
| `WORKER_OFFLINE_SECONDS` | lease 회수 전에 Worker가 offline이어야 하는 최소 시간 |
| `LEASE_RECOVERY_MAX_ATTEMPTS` | lease 유실 Job의 자동 재배정 최대 attempt 수(0이면 자동 재배정 없음) |
| `ALLOW_FAKE_WORKERS` | 개발/test fake engine 배정 허용; production에서는 설정 자체가 거부됨 |
| `STORAGE_BACKEND` | `auto`, `local`, `s3`; production은 최종적으로 `s3`여야 함 |
| `LOCAL_STORAGE_ROOT` | test/development filesystem adapter root |
| `PUBLIC_API_BASE_URL` | local adapter가 Worker에 반환할 Manager base URL |
| `ARTIFACT_UPLOAD_TTL_SECONDS` | 크기 기반 PUT session 수명의 상한; 기본 3600초 |
| `ARTIFACT_DOWNLOAD_TTL_SECONDS` | S3 GET URL 수명; 기본 60초 |
| `ARTIFACT_MAX_BYTES` | 단일 PUT artifact 최대 byte 수; 기본·상한 5 GiB |
| `ARTIFACT_STREAM_CHUNK_BYTES` | 검증·local download chunk 크기; 기본 1 MiB |
| `ARTIFACT_VERIFICATION_SPOOL_DIR` | checksum 검증 중 사용할 bounded 임시 파일 경로 |
| `ARTIFACT_FINALIZING_STALE_SECONDS` | 중단된 finalizing session 회수 기준; 기본 7200초 |
| `ARTIFACT_RETRY_AFTER_SECONDS` | retryable upload 상태의 권장 재조회 간격; 기본 5초 |
| `ARTIFACT_ATTEMPT_MAX_SESSIONS` | attempt별 유효 upload session 상한; 기본 256개 |
| `ARTIFACT_ATTEMPT_MAX_BYTES` | attempt별 유효 upload 선언 용량 상한; 기본 100 GiB |
| `DATASET_UPLOAD_TTL_SECONDS` | 크기 기반 Dataset PUT session 수명의 상한; 기본 3600초 |
| `DATASET_DOWNLOAD_TTL_SECONDS` | Worker용 S3 GET URL 수명; 기본 60초 |
| `DATASET_UPLOAD_MAX_BYTES` | 단일 원본 Dataset 최대 byte 수; 기본·상한 5 GiB |
| `DATASET_OWNER_MAX_SESSIONS` | owner별 동시 pending/finalizing Dataset session 상한; 기본 8개 |
| `DATASET_OWNER_MAX_BYTES` | owner별 동시 upload 선언 용량 상한; 기본 20 GiB |
| `DATASET_UPLOAD_WRITE_STALE_SECONDS`, `DATASET_UPLOAD_WRITE_HEARTBEAT_SECONDS` | local PUT writer stale fence/heartbeat; 기본 300/15초 |
| `DATASET_FINALIZING_STALE_SECONDS` | heartbeat가 끊긴 finalize 회수 기준; 기본 1800초 |
| `DATASET_FINALIZING_HEARTBEAT_SECONDS` | finalize 세션 heartbeat 간격; 기본 30초 |
| `DATASET_CLEANUP_LATE_WRITER_GRACE_SECONDS`, `DATASET_CLEANUP_CONFIRMATION_GRACE_SECONDS` | Dataset staging late PUT 유예/두 번째 삭제 확인 간격; 기본 7200/60초, 전역 7일 grace가 더 크면 전역값 적용 |
| `DATASET_RETRY_AFTER_SECONDS` | retryable Dataset 상태의 권장 재조회 간격; 기본 5초 |
| `DATASET_INGESTION_ROOT` | Manager 전용 private snapshot 경로; 운영 Compose는 mode `0700` volume 사용 |
| `DATASET_MAX_ENTRIES` | ZIP entry 수 상한; 기본 10,000개 |
| `DATASET_MAX_FILE_UNCOMPRESSED_BYTES` | ZIP entry별 비압축 byte 상한; 기본 2 GiB |
| `DATASET_MAX_TOTAL_UNCOMPRESSED_BYTES` | ZIP 전체 비압축 byte 상한; 기본 20 GiB |
| `DATASET_MAX_COMPRESSION_RATIO` | ZIP entry 압축률 상한; 기본 200 |
| `TEST_SET_UPLOAD_TTL_SECONDS` | TestSet WAV PUT session 수명; 기본 1800초 |
| `TEST_SET_ITEM_MAX_BYTES` | 단일 고정 WAV 상한; 기본 256 MiB |
| `TEST_SET_OWNER_MAX_SESSIONS`, `TEST_SET_OWNER_MAX_BYTES` | owner 전체 pending/finalizing session/byte 상한; 기본 64개/2 GiB |
| `TEST_SET_MAX_ITEMS` | revision별 verified item 상한; 기본·상한 128개 |
| `TEST_SET_MAX_TOTAL_BYTES`, `TEST_SET_MAX_TOTAL_DURATION_SECONDS` | revision 전체 상한; 기본·상한 2 GiB/3,600초 |
| `TEST_SET_MAX_DURATION_SECONDS` | 단일 PCM WAV duration 상한; 기본 600초 |
| `TEST_SET_MIN_SAMPLE_RATE_HZ`, `TEST_SET_MAX_SAMPLE_RATE_HZ` | PCM WAV sample-rate 허용 범위; 기본 8,000~192,000 Hz |
| `TEST_SET_MAX_CHANNELS` | PCM WAV channel 상한; 기본 2 |
| `SAMPLE_MAX_BYTES`, `SAMPLE_MAX_DURATION_SECONDS`, `SAMPLE_MAX_CHANNELS` | 단일 canonical Sample PCM 상한; 기본·상한 256 MiB/600초/2 channel |
| `SAMPLE_REGISTRATION_JSON_MAX_BYTES` | Sample 등록 raw JSON 상한; 기본·상한 64 KiB |
| `EXPERIMENT_JSON_MAX_BYTES` | Experiment 생성/설명 수정 raw JSON 상한; 기본 16 KiB, 허용 범위 8~64 KiB |
| `SAMPLE_VERIFICATION_TIMEOUT_SECONDS`, `SAMPLE_VERIFICATION_MAX_CONCURRENCY` | canonical byte/PCM 검증 전체 deadline과 process 동시성; 기본 120초/2개 |
| `SAMPLE_APPROVED_RUNTIME_BUNDLES` | sample Job에 허용할 `sha256:<image>@<asset-manifest-sha256>` 쉼표 목록; 활성화 시 필수 |
| `AUTO_SAMPLE_JOBS_ENABLED` | sample-enabled Job 생성 운영 gate; Worker runtime 완성 전 기본 `false` |
| `LOG_STREAM_POLL_INTERVAL_SECONDS` | SSE DB polling 간격; 기본 1초 |
| `LOG_STREAM_HEARTBEAT_SECONDS` | SSE heartbeat 간격; 기본 15초 |
| `LOG_STREAM_MAX_CONNECTION_SECONDS` | SSE 연결 수명 상한; 기본 300초 |
| `LOG_STREAM_BATCH_LIMIT` | SSE poll 한 번의 log 상한; 기본 100개 |
| `S3_ENDPOINT_URL` | MinIO/S3 endpoint |
| `S3_PRESIGN_ENDPOINT_URL` | 원격 Worker/browser가 접근 가능한 PUT/GET URL endpoint |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | object storage credential |
| `S3_BUCKET`, `S3_REGION` | object storage 대상 |
| `S3_ADDRESSING_STYLE` | MinIO 기본 `path` 또는 AWS `virtual` |
| `S3_VERIFY_TLS` | S3 endpoint TLS 인증서 검증 여부 |
| `S3_PRESIGN_BIND_CHECKSUM` | 지원 서버에서 `x-amz-checksum-sha256`도 서명할지 여부 |
| `MLFLOW_ENABLED` | MLflow projection 활성화; 기본 `false`, 설치 stack 기본 `true` |
| `MLFLOW_FAIL_CLOSED` | MLflow 장애를 readiness/쓰기 응답 `503`으로 승격; 설치 기본 `false` |
| `MLFLOW_TRACKING_URI` | credential/query/fragment 없는 MLflow HTTP(S) endpoint |
| `MLFLOW_TRACKING_TOKEN`, `MLFLOW_TRACKING_TOKEN_FILE` | 선택적 bearer token; 운영은 file 사용 권장 |
| `MLFLOW_REQUEST_TIMEOUT_SECONDS` | 개별 MLflow REST 요청 timeout; 기본 5초 |
| `MLFLOW_READINESS_TIMEOUT_SECONDS` | fail-open probe가 healthcheck를 막지 않는 짧은 timeout; 기본 1초 |
| `MLFLOW_SYNC_INTERVAL_SECONDS` | outbox background 재처리 주기; 기본 5초 |
| `MLFLOW_SYNC_BATCH_SIZE` | 주기당 projection event 상한; 기본 20개 |
| `MLFLOW_PROCESSING_STALE_SECONDS` | 중단된 processing claim 회수 기준; 기본 120초 |
| `MLFLOW_RETRY_MAX_SECONDS` | 지수 backoff 상한; 기본 300초 |
| `CORS_ORIGINS` | 쉼표로 구분한 허용 origin |
| `RATE_LIMIT_ENABLED` | Redis 기반 API rate limit 활성화; 설치 기본 `true` |
| `RATE_LIMIT_FAIL_CLOSED` | Redis 장애 시 API 요청을 503으로 차단할지 여부; 설치 기본 `true` |
| `RATE_LIMIT_DEFAULT_REQUESTS_PER_MINUTE` | credential/IP별 일반 API 분당 상한; 기본 600 |
| `RATE_LIMIT_LOGIN_REQUESTS_PER_MINUTE` | IP별 로그인 분당 상한; 기본 10 |
| `RATE_LIMIT_REGISTER_REQUESTS_PER_MINUTE` | IP별 Worker 등록 분당 상한; 기본 10 |
| `RATE_LIMIT_WORKER_TOKEN_ROTATION_REQUESTS_PER_MINUTE` | Worker/admin별 token rotate/revoke 분당 상한; 기본 6 |
| `RATE_LIMIT_UPLOAD_REQUESTS_PER_MINUTE` | credential별 upload init 분당 상한; 기본 120 |
| `RATE_LIMIT_FINALIZE_REQUESTS_PER_MINUTE` | credential별 finalize 분당 상한; 기본 30 |
| `RATE_LIMIT_SAMPLE_REQUESTS_PER_MINUTE` | Worker별 Sample 등록 분당 상한; 기본 30 |
| `RATE_LIMIT_SAMPLE_DOWNLOAD_REQUESTS_PER_MINUTE` | 사용자별 Sample WAV download 분당 상한; 기본 60 |
| `WORKER_TOKEN_ROTATION_TTL_SECONDS` | prepare된 1회성 Worker token 활성화 유효시간; 기본 600초 |

`auto` storage는 endpoint와 두 S3 credential이 모두 있으면 S3를 선택하고, 그렇지
않으면 local을 선택한다. `production`은 PostgreSQL URL, bootstrap token, 개발
기본값이 아닌 Worker pepper, 32자 이상의 JWT secret과 S3/MinIO storage를 요구한다.
custom `S3_ENDPOINT_URL`을 쓰는 production은 Compose 내부 주소와 분리된
`S3_PRESIGN_ENDPOINT_URL`도 명시해야 한다. 후자는 원격 Worker와 사용자 browser가
실제로 접근 가능한 credential/query/fragment 없는 절대 HTTPS 주소여야 하며 서명 URL
생성에만 사용한다. development/test에서는 같은 형태의 절대 HTTP 주소도 허용한다.

운영 entrypoint는 Uvicorn raw access log를 끄고 Manager middleware가 query 없는 path,
검증된 request ID, method, status와 latency만 JSON으로 기록한다. Authorization, token,
password와 presigned query는 formatter에서도 다시 redaction한다. API 응답에는
`nosniff`, frame 차단, no-referrer, 제한된 Permissions Policy와 API 전용 CSP를 붙이고,
운영자가 `PUBLIC_SCHEME=https`로 고정한 production 응답에는 HSTS를 추가한다. 임의의
`X-Forwarded-Proto`를 신뢰해 HSTS 여부를 바꾸지 않으며 설치 Nginx도 외부 forwarding
header를 폐기하고 같은 공개 scheme을 upstream에 다시 기록한다.

설치 stack은 Redis Lua의 `INCR`/`EXPIRE` 원자 연산으로 여러 API replica가 공유하는
요청 window를 적용한다. Redis key에는 IP나 bearer 원문 대신 서버 secret으로 HMAC한
digest만 저장한다. 제한 응답은 `429`와 `Retry-After`, `RateLimit-*` header를 반환한다.
Redis 장애 시 production 기본은 fail-closed `503`이며 liveness `/health`는 rate limit
대상이 아니다.

## RQ maintenance plane

설치 stack의 별도 non-root `rq-worker` service는 내부 `storage` network에서만 Redis,
PostgreSQL과 object storage에 접근한다. Docker socket, GPU, host port는 없고 read-only
filesystem, 전체 capability drop, bounded PID를 사용한다. 전용 entrypoint는 PostgreSQL,
Redis, S3 credential만 읽으며 JWT secret, Worker bootstrap/token pepper와 MLflow token은
mount하지 않는다. `PROCESS_ROLE=maintenance`가 아니면 Worker와 task가 시작되지 않고,
maintenance role로 HTTP API를 시작하려 해도 거부된다.

API가 enqueue할 수 있는 callable은 Dataset staging cleanup과 TestSet staging cleanup 두
종류로 고정되고 RQ job에는 서버가 생성한 `maintenance_task_runs.id`만 JSON serialization으로
들어간다. client는
callable, object key, batch 크기, timeout 또는 retry 인자를 지정할 수 없다. 실행 Worker는
Redis를 신뢰하지 않고 dequeue 직후와 fork된 process의 perform 직전에 queue/origin,
JSON serializer, exact callable, canonical UUID 단일 인자, 빈 kwargs/meta/dependency/callback/
repeat와 bounded timeout/TTL/retry를 검증한다. 위반 job은 callable/callback을 import하지 않고
generic policy failure로 종료하며, success/failure handler도 dependent/repeat를 실행하지
않는다. 허용된 task도 PostgreSQL run과 upload session을 다시 잠그고 status, grace, claim,
storage backend와 task type에 맞는 exact server-owned staging key를 확인해야 삭제한다.
두 task 모두 upload generation/write token, namespace와 cleanup claim generation을 다시
대조하고 confirmation grace를 둔 두 번째 삭제까지 성공한 뒤에만 완료한다.

- `POST /api/v1/admin/maintenance/dataset-staging-cleanup`: admin JWT와
  `Idempotency-Key`로 dry-run 또는 실제 정리를 enqueue한다. 동일 actor/key/dry-run 조합은
  결정적인 RQ job ID와 기존 DB run을 반환한다. active writer/finalizing/completed session과
  canonical key를 제외하고 first-delete 뒤 기본 60초 후 second-delete한다.
- `POST /api/v1/admin/maintenance/test-set-staging-cleanup`: 같은 exact-envelope 정책으로
  TestSet staging만 정리한다. active/finalizing/completed session과 canonical key는 항상
  제외하며 첫 삭제 뒤 기본 60초 confirmation grace를 거쳐 다시 확인·삭제한다.
- `GET /api/v1/admin/maintenance/{run_id}`: DB 원장의 queued/running/retrying/terminal 상태,
  attempt와 typed 집계를 조회한다. Redis result를 원장으로 신뢰하지 않는다.
- `/ready`: RQ가 켜졌으면 Redis ping과 별도로 queue에 등록된 최근 RQ Worker heartbeat가
  없거나 stale일 때 `rq_worker=no_worker|stale|unavailable`로 fail-closed한다.

RQ scheduler는 storage 실패의 bounded delayed retry를 due 시점에 같은 queue로 돌려보내는
용도로만 Worker 내부에서 켠다. scheduler가 새 cleanup run이나 주기 task를 생성하지는 않는다.
현재 운영자는 외부 cron/systemd timer 또는 운영 자동화가 짧은 수명의 admin JWT와 새
idempotency key로 위 enqueue API를 호출해야 한다. 기본 순서는 dry-run 검토 후 같은 보존
정책의 실제 run이며, 주기는 운영 retention 정책이 정한다. client가 Redis에 직접 enqueue하거나
별도 범용 RQ scheduler/worker를 붙이는 것은 지원하지 않는다. due retry도 dequeue/perform
execution allowlist를 다시 통과한다. RQ의 queue별 Redis `NX` lock으로 여러 Worker 중 한
scheduler만 활성화되며 scheduler lock heartbeat는 Worker registry heartbeat와 별도 key다.
따라서 `/ready`는 계속 실제 allowlist Worker의 최근 heartbeat만 보고 fail-closed한다.
Dataset finalize/검증은 여전히 요청 process의 bounded thread에서 inline
실행되므로 `SYS-005`는 Partial이다. Redis 유실/FLUSH 뒤 queued DB run은 PostgreSQL 원장의
task type을 보존하는 reconciler가 재조정한다. maintenance 전용 PostgreSQL role,
staging-prefix delete-only S3 IAM·Redis ACL과 실제 장애 주입 시험은
아직 release gate다. execution allowlist는 Redis
credential 탈취 시 임의 Python 실행을 막지만 queue 삭제, heartbeat 위조와 서비스 거부까지
막는 가용성 경계는 아니다.

## MLflow projection과 장애 정책

PostgreSQL은 유일한 학습 원장이고 MLflow는 비교·검색을 위한 파생 projection이다.
`MLFLOW_ENABLED=true`이면 Experiment/Job 생성, 새 metric batch, Manager가 checksum을
검증한 Artifact metadata와 terminal 상태를 `mlflow_sync_events` outbox에 원장 변경과
같은 transaction으로 기록한다. API는 commit 뒤 즉시 한 번 투영하고, 실패한 event는
background projector가 지수 backoff로 재처리한다. Manager Experiment/Job ID를 MLflow
tag로 검색한 뒤 create하므로 응답 유실이나 여러 API replica의 재처리도 같은 Run을
순차적으로 재사용한다. Metric/param/tag batch는 MLflow 한도에 맞게 나누고 각 조각에 event-key
digest marker를 함께 남긴다.

MLflow에는 안전한 JobConfig scalar, attempt 번호가 붙은 metric, Artifact ID/type/크기/
SHA-256/표시명과 권한 검사를 거치는 Manager 상대 download path만 보낸다. pretrained
path, storage URI, presigned query, token, credential 및 임의 Artifact metadata는 보내지
않는다. tracking URI는 userinfo/query/fragment를 거부하고 redirect를 따라가지 않아
bearer token이 다른 origin으로 전달되지 않는다. HTTP client는 `trust_env=false`라서
process의 `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` 또는 `.netrc`도 tracking credential
전달 경로로 사용하지 않는다.

- `MLFLOW_ENABLED=false`: outbox를 만들지 않고 `/ready`는 `mlflow=disabled`다.
- 기본 fail-open: MLflow 장애에도 원장 API는 성공하고 outbox가 pending으로 남는다.
  `/ready`는 `mlflow=unavailable`을 표시하지만 전체 status는 ready다.
- `MLFLOW_FAIL_CLOSED=true`: `/ready`가 `503`이고, commit 뒤 즉시 projection이 실패한
  write는 `503`과 `ledger_committed=true`, 원장 resource ID를 반환한다. DB/outbox는 이미
  안전하게 commit됐으므로 client는 같은 생성 요청으로 새 resource를 만들지 말고 반환된
  ID를 조회해야 한다. Worker metric/status/artifact retry는 기존 idempotency key/event를
  재동기화한다.

MLflow REST의 부분 성공 가능성 때문에 process가 metric write와 marker 사이에서 정확히
죽으면 동일 timestamp/step/value가 history에 한 번 더 보일 수 있다. PostgreSQL Metric
원장은 중복되지 않으며 MLflow 최신 값의 의미도 같지만, MLflow history의 물리적
exactly-once는 보장하지 않는 at-least-once projection이다.
한 API process 안에서는 projection lock으로 request/background 경쟁을 직렬화하지만,
여러 API replica가 서로 다른 첫 event에서 동일 Job Run을 동시에 최초 생성하는 극단적
경쟁은 MLflow REST가 client 지정 run ID나 tag unique constraint를 제공하지 않아 중복
Run 가능성이 남는다. 배포 기본은 API 1 replica이며 다중 replica 출시는 PostgreSQL
advisory lock 또는 별도 단일 projector service 검증이 필요하다.

## 최초 관리자 bootstrap과 사용자 인증

DB migration 직후 아래 one-shot CLI를 한 번 실행한다. 비밀번호 CLI 인자는 의도적으로 제공하지 않으며, 운영에서는 권한 `0600`인 일반 파일을 사용한다.

```bash
printf '%s' 'replace-with-a-long-password' > /secure/admin-password
chmod 0600 /secure/admin-password

DATABASE_URL=postgresql+asyncpg://manager:password@postgres/manager \
JWT_SECRET_FILE=/run/secrets/jwt_secret \
ADMIN_BOOTSTRAP_EMAIL=admin@example.com \
ADMIN_BOOTSTRAP_PASSWORD_FILE=/secure/admin-password \
rvc-manager-bootstrap-admin
```

email은 `ADMIN_BOOTSTRAP_EMAIL_FILE`로도 전달할 수 있다. 비밀번호 원문 환경 변수와
CLI 인자는 금지되며 `ADMIN_BOOTSTRAP_PASSWORD_FILE` 또는 `--password-file`만
허용한다. bootstrap은 DB lock을 획득하며 최초 관리자 한 명만 생성한다. 같은 활성
관리자로 재실행하면 no-op이고 다른 계정 생성·승격·재활성화는 거부한다.

- `POST /api/v1/auth/login`: email/password로 15분 access JWT 발급
- `GET /api/v1/auth/me`: 현재 활성 사용자 조회
- `POST /api/v1/auth/logout`: JWT `jti`를 DB에 폐기하고 이후 재사용 거부

사용자 password는 Argon2id로 저장한다. 일반 사용자는 자신이 만든 Dataset, Experiment, Job만 생성·조회·취소·재시도할 수 있고 타 사용자 ID는 `404`로 숨긴다. admin은 모든 사용자 리소스와 Worker 목록/상세를 조회할 수 있다. Worker bearer 인증은 사용자 JWT와 별도 흐름이다.

## Worker 인증과 재시작

1. 최초 설치 시 bootstrap token으로 `POST /api/v1/workers/register`를 호출한다.
2. 응답의 Worker bearer token은 이때 한 번만 표시된다. DB에는 HMAC-SHA256 결과만 저장한다.
3. Worker는 token을 로컬 secret store에 보관한다.
4. 재시작 시 저장 token으로 `GET /api/v1/workers/me`를 호출해 identity를 복구한다.
5. idle/no-active-lease에서 `/workers/token-rotation/prepare`의 1회 token을 mode 0600 파일에
   먼저 저장하고 old+pending 동시 증명으로 `/activate`한다. pending 중 Job claim은 409다.
6. admin emergency revoke는 `/workers/{id}/token/revoke`에서 exact name과 reason을 요구한다.
   active assignment는 기본 거부하며 명시적 force에서만 Job/attempt/lease를 cancelled/released로
   닫는다. 폐기된 row는 bootstrap+exact ID/name의 `/workers/re-enroll`로만 재활성화한다.

register/prepare/re-enroll의 평문 token 응답은 모두 `private, no-store`와 `Pragma: no-cache`이며,
DB와 audit에는 token hash와 비민감 rotation metadata만 남는다. 상세 host 절차는
`docs/OPERATIONS_GUIDE.md`를 따른다.

Job claim은 queued 상태를 조건으로 한 compare-and-swap 갱신을 사용한다. 승자만 attempt와 lease를 생성하므로 PostgreSQL과 SQLite 모두에서 동일 Job을 두 Worker가 동시에 가져가지 않는다. Worker가 `completed`를 보고하기 전 현재 attempt에 필요한 final model/index metadata를 먼저 등록해야 한다. 만료 lease는 Worker offline grace도 지난 뒤 unfinished attempt를 실패로 닫고, cancel이 아니며 자동 회수 상한 미만일 때만 새 attempt로 재큐잉한다.

## Experiment 안전 CRUD

`POST /api/v1/experiments`는 현재 사용자 소유의 `ready`, `is_usable=true` Dataset만 받으며
owner별 정규화된 이름 중복을 `409`로 거부한다. `GET /experiments`는 생성 시각과 ID의 안정
정렬, bounded `offset`/`limit` pagination을 제공하고 타 사용자 row는 숨긴다.

Experiment의 name과 Dataset은 생성 뒤 immutable이다. `PATCH /experiments/{id}`는
`expected_row_version`과 명시적인 description만 받아 compare-and-swap으로 갱신하며 stale
version은 `409`다. `DELETE /experiments/{id}`도 query의 `expected_row_version`을 요구하고,
Job 참조나 MLflow projection/outbox가 있거나 MLflow가 활성화된 경우 삭제를 거부한다. 생성,
변경, 삭제는 audit event에 기록된다. POST/PATCH raw JSON은 선언된 길이와 chunked body 모두
기본 16 KiB 상한을 적용하며 unknown field, name/Dataset 변경 시도는 `422`다.

historical duplicate name이나 owner가 없는 row는 migration에서 ID와 Job 연결을 보존하기 위해
conflict key를 `NULL`로 격리한다. 새 생성은 API lookup과 DB unique constraint를 함께 사용하고,
Job→Experiment FK는 `RESTRICT`이므로 API 검사 뒤 생긴 참조 race도 삭제를 막는다.

## Dataset upload, 검증과 삭제

사용자 JWT로 다음 server-owned data plane을 사용한다. client는 object key나 storage URI를
보내지 않으며 API 응답에도 내부 URI가 없다.

1. `POST /api/v1/datasets/uploads/init`
   - 표시 이름, 안전한 단일 filename, 허용 MIME, 정확한 byte 크기와 SHA-256,
     idempotency key를 보낸다.
   - 지원 입력은 ZIP 또는 WAV/FLAC/MP3/M4A/OGG/AAC다. 확장자와 MIME 조합, owner별
     동시 session/byte quota와 단일 5 GiB 상한을 먼저 검증한다.
   - Manager가 Dataset/UploadSession과 UUID 기반 staging/canonical key를 만들고 Local
     bounded PUT endpoint 또는 S3/MinIO presigned PUT을 반환한다. 같은 key와 payload는
     같은 세션을 반환하고 다른 payload는 `409`다. 만료 세션은 staging을 정리한 뒤 같은
     Dataset에 새 session ID/generation과 서로 겹치지 않는 staging/canonical namespace를 발급한다.
2. 응답의 `upload_url`과 `upload_headers`를 그대로 사용해 raw byte를 `PUT`한다.
   Local adapter는 `Content-Length`, `Content-Type`, session HMAC을 확인하고 Dataset→session
   잠금으로 generation/write token을 CAS claim한다. 전송 중 heartbeat를 갱신하고 `expires_at`을
   절대 deadline으로 지킨 뒤 제한된 임시 파일을 원자 게시한다. stale writer는 replacement
   session의 key를 알거나 삭제할 수 없다.
3. `POST /api/v1/datasets/uploads/{session_id}/finalize`
   - Manager가 staging object 전체를 bounded stream으로 다시 읽어 size/SHA-256과 실제
     file signature를 검증한다.
   - mode `0700` Manager snapshot 안에서 archive를 안전하게 풀고 재귀 수집한 뒤
     `prepared_flat.zip`, `manifest.json`, `quality_report.json`을 결정적으로 만든다.
   - 원본과 세 산출물을 모두 canonical storage에 게시한 뒤에만 Dataset을 `ready`로
     바꾼다. 게시 중 실패하면 부분 object를 정리하고 typed `failure_code`와 retry 상태를
     남긴다. finalize token과 heartbeat가 장시간 준비 작업을 stale session으로 잘못
     회수하는 것을 막는다. request 취소/commit 오류는 fresh DB session으로 completed outcome을
     먼저 확인하고 미커밋이면 그 upload session이 게시한 canonical key만 shield cleanup한다.

PCM WAV만 현재 duration/sample rate/channel, clipping/silence/RMS 검증을 완료한다.
다른 codec은 원본과 flat/report를 보존하되 `decoder_pending`, `is_usable=false`로 남기며
Experiment/Job 생성과 Worker claim을 거부한다. `GET /datasets`와
`GET /datasets/{id}`는 checksum, 크기, typed 품질 집계와 상태만 반환한다. canonical
`quality_report.json` 및 DB `quality_report_json`에는 member path와 세부 사유가 포함될 수 있어
공개 응답에 넣지 않는다. 대신 source/skipped/rejected/duplicate count와
`pcm-sample-weighted-v1` aggregate를 반환한다. clipping/silence는 interleaved PCM sample count로
가중하고 RMS는 raw normalized square sum에서 계산한다. decoder 대기 파일은 제외하며 migration
`f9c4a7d2b610` 이전 행은 exact sample count가 없으므로 aggregate/count를 `null`로 보존한다.
Mono/stereo PCM의 nested `loudness`는 `itu-r-bs1770-4-mono-stereo-v1` K-weighting과 파일별
complete 400 ms/75% overlap block을 사용한다. 전체 block에 `>-70 LUFS` 절대 gate와
`-10 LU` 상대 gate를 적용하며 파일별 LUFS를 평균하지 않는다. 짧은 입력, gate 미만, 지원하지
않는 layout/rate는 finite 숫자를 만들지 않고 typed reason과 `integrated_lufs=null`을 반환한다.
`d8f2a6c4b901` 이전 PCM aggregate는 nested loudness를 raw JSON에서 재구성하지 않고 null로 보존한다.
완료된 Dataset에 대한
`validate`와 `prepare-flat`은 기존 결과를 멱등 반환하고 진행 중 상태는 `409`다.

`DELETE /datasets/{id}`는 owner/admin만 호출할 수 있다. 참조 Experiment/Job 또는
활성 upload/finalize 또는 아직 이중 staging cleanup이 끝나지 않은 expired/failed session이
있으면 `409`다. 삭제는 Dataset을 먼저 `deleting`으로
commit해 새 Experiment/Job race를 차단하고, 모든 세대의 staging/canonical object를
정리한 뒤 DB row를 삭제한다. object 정리가 실패하면 `delete_failed`와 retry 가능한
failure code를 보존한다. client URI를 받는 legacy `POST /datasets`는 test에서만 일반
사용자에게 열리고 development는 admin 전용, production은 항상 `403`이다.

현재 ingestion은 API event loop 밖의 worker thread에서 동기 실행되지만 같은 HTTP
finalize 요청 수명 안에 있다. entry/byte/압축률은 강제 제한하지만 안전하게 중단할 수
없는 Python thread에 거짓 wall-clock timeout을 적용하지 않는다. 대형 처리의 durable
timeout/cancel/restart는 Redis/RQ 또는 별도 subprocess 작업 경계로 옮기는 후속 gate다.

`ready`, `is_usable=true`이며 완료된 server-owned upload session이 있는 Dataset만 실제
Worker claim에 `dataset_transfer`로 포함된다. 계약은 `prepared_flat.zip`의 정확한 크기와
SHA-256, 고정 filename/MIME, query 없는 Manager 상대 경로만 제공하고 내부 storage URI나
presigned query를 claim JSON에 넣지 않는다. Worker는 현재 bearer와 lease/attempt header로
`GET /api/v1/workers/jobs/{job_id}/dataset`을 호출한다. Local storage는 bounded stream,
S3/MinIO는 60초 기본의 307 presigned GET이며 요청 사실은 URI 없이 audit한다. test 전용
legacy URI fixture는 transfer 없이만 claim될 수 있어 실제 runner에서는 fail-closed다.

## TestSet/Preset 원장과 sample-plan gate

`POST /api/v1/test-sets`는 owner-scoped draft revision을 만든다. 각 고정 PCM WAV는
`POST /test-sets/{id}/item-uploads/init`으로 안전한 `item_key`, 유일한 `sort_order`, WAV
basename/MIME, 정확한 size/SHA-256과 license/provenance reference를 선언한 뒤 응답 target에
raw PUT하고 `POST /test-sets/item-uploads/{session_id}/finalize`한다. Manager는 user와
TestSet 행을 잠가 owner quota 및 예약 충돌을 직렬화하고, upload token은 hash만 저장한다.
backend와 local root 또는 S3 endpoint/bucket/region의 credential 없는 namespace fingerprint도
session에 고정해 설정 변경 뒤 잘못된 adapter cleanup을 거부한다. license/provenance는 URL,
query나 storage scheme이 아닌 allowlist namespace의 opaque record ID만 받는다. 전체
byte/SHA-256과 RIFF/WAVE uncompressed PCM decode, duration/sample-rate/channel 상한을 통과한
byte만 서버 생성 canonical key에 게시한다. failure/stale 전이는 fresh TestSet→upload row lock과
finalization token CAS가 성공한 경우만 canonical을 정리한다.

`POST /test-sets/{id}/finalize`는 unresolved pending/finalizing/failed session이 없고 한 개
이상의 item이 있을 때만 실행된다. 각 item에 completed session이 정확히 하나인지, 현재
storage backend/server key/URI/size/SHA가 일치하는지와 실제 canonical object 전체 hash를
다시 검증한다. manifest는 storage URI, presigned URL, 내부 item UUID를 제외한 결정적 JSON이고
ready revision은 수정·삭제할 수 없다. list summary는 `items_included=false`, detail/finalize는
`items_included=true`를 명시한다. Preset 변경은 새 immutable revision을 만들며 최신 revision
번호 재사용을 막기 위해 다른 revision이 남은 family의 latest hard delete는 거부한다.

sample-enabled Job은 ready TestSet manifest를 재계산하고 ordered item ID/metadata와 inline
inference config를 storage-neutral `sample_plan_json`/SHA-256으로 snapshot한다. claim과 item GET은
current Worker/lease/attempt, 게시된 manifest byte, DB plan과 canonical namespace를 다시 검증한다.
Worker의 ordered PCM materializer와 pinned PM/Harvest/RMVPE inference는 model/index/output을
canonical Artifact로 게시한 뒤 `POST /api/v1/workers/jobs/{job_id}/samples`로 논리 Sample을
등록한다. 동일 PCM hash는 한 Artifact를 여러 item이 공유하지만 Sample row는 item/input identity를
각각 보존한다.

등록 API는 64 KiB raw JSON, 분당 30회, PCM 검증 concurrency/deadline과 단일 출력
256 MiB/600초/2 channel, attempt 논리 출력 합계 2 GiB/3,600초 상한을 적용한다. current
claim과 approved runtime image/asset 쌍,
native manifest/request SHA-256, `sample_model|sample_index|sample_output` 역할, model/index/output
SHA·size·type, TestSet item/config를 잠근 원장과 교차검증한다. Worker 값이 아니라 Manager가
canonical WAV를 다시 읽어 계산한 `pcm-normalized-v2` 지표를 authoritative evidence로 저장한다.
동일 등록 replay만 200이며 충돌 payload는 409다. sample-enabled completion은 모든 item이 정확히
하나의 Sample을 갖고 현재 canonical model/index/output byte가 원장 hash와 일치해야 통과한다.
Sample download도 재해시 후 streaming하며 same-origin redirect에 bearer/cookie를 노출하지 않는다.

CREPE의 manifest-pinned offline asset, Torch `>=2.6` release runtime과 실제 GPU/no-network matrix가
아직 없으므로 배포 기본 `AUTO_SAMPLE_JOBS_ENABLED=false`와 Worker capability false는 유지한다.
TestSet local PUT은 generation/write-token CAS heartbeat와 session expiry 절대 deadline을 사용하고,
finalize의 verify/PCM/no-replace publish 전 구간도 generation/finalization-token heartbeat로
보호한다. `POST /api/v1/admin/maintenance/test-set-staging-cleanup`은 exact namespace와 staging
key만 first-delete한 뒤 confirmation grace 후 다시 삭제해야 완료된다. 기본 유효 grace는
`max(MAINTENANCE_CLEANUP_GRACE_SECONDS, TEST_SET_CLEANUP_LATE_WRITER_GRACE_SECONDS)`이므로 현재
7일이고 confirmation은 60초다. active/finalizing/completed session과 canonical key는 삭제하지
않는다. 실제 S3의 7일보다 긴 in-flight PUT 및 다중 replica 경합 검증은 release gate다.

## Artifact upload, 검증과 다운로드

실제 Worker는 storage URI나 object key를 정하지 않는다. 현재 lease로 다음 흐름을
사용한다.

1. `POST /api/v1/workers/jobs/{job_id}/artifact-uploads/init`
   - `lease_id`, `attempt_id`, idempotency key, logical artifact type, 표시 filename,
     MIME, 정확한 byte 크기와 SHA-256을 보낸다.
   - Manager는 UUID 기반 staging/canonical key와 짧은 PUT target을 생성한다.
   - PUT 만료는 5분의 연결 여유와 2 MiB/s 전송 시간을 합산하고 설정 상한(기본
     3600초)을 적용한다. lease는 upload 중 heartbeat로 갱신될 수 있으므로 URL 만료를
     현재 lease 시각으로 줄이지 않지만, finalize는 최신 active lease를 다시 검증한다.
2. 응답 `upload_url`에 `upload_headers`를 그대로 사용해 raw bytes를 `PUT`한다.
   - local adapter는 짧은 HMAC upload header를 사용한다.
   - S3/MinIO는 content length/type와 SHA-256 metadata header가 서명된 URL을 사용한다.
3. `POST /api/v1/workers/jobs/{job_id}/artifact-uploads/{session_id}/finalize`
   - Manager가 staging object를 제한된 chunk로 끝까지 읽어 정확한 size와 SHA-256을
     다시 계산한다. HEAD와 Worker metadata만으로 성공 처리하지 않는다.
   - 일치한 byte만 별도 canonical key에 게시하고 `Artifact` 원장을 만든다.
   - 불일치하면 canonical row를 만들지 않고 staging object를 정리한다.

동일 idempotency payload 또는 동일 attempt/type/SHA 재요청은 같은 session/artifact를
반환하고 다른 payload 재사용은 `409`다. pending session이 만료된 뒤 같은 payload를
재요청하면 이전 세대를 expired로 보존하고 새 generation의 session을 발급한다. 완료 artifact는 owner/admin만
`GET /api/v1/jobs/{job_id}/artifacts`로 조회하고
`GET /api/v1/artifacts/{artifact_id}/download`로 받을 수 있다. 응답 metadata에는
내부 storage URI를 노출하지 않는다. S3는 짧은 presigned GET으로 redirect하고 local
adapter는 인증된 streaming response를 제공한다.
metadata-only `/workers/jobs/{job_id}/artifacts`는 기존 GPU 없는 E2E를 위해
test/development의 명시적 Fake Worker와 `file://`, `fake=true` 조합에서만 허용된다.

init 응답은 모든 상태에서 `failure_code`, `retryable`, `retry_after_seconds`를 제공한다.
진행 중인 finalizing은 retryable이며, 기본 7200초 동안 갱신되지 않은 finalizing은
compare-and-swap으로 pending에 회수한 뒤 다시 검증할 수 있다. spool 생성·write·flush·fsync·cleanup
I/O 오류는 raw 500으로 빠지지 않고 명시적 failure code와 retryable pending으로 복구한다.
attempt quota는 `pending`, `finalizing`, `completed`인 최신 유효 session만 개수와 선언 byte에
합산하며 `failed`, `expired` 세대는 object 정리 후 quota에서 제외한다.

## Job 관측 데이터 조회

사용자 JWT로 다음 Manager read API를 사용한다. 일반 사용자는 자신이 소유한 Experiment의
Job만 볼 수 있고 다른 사용자의 Job은 `404`로 숨긴다. admin은 전체 Job을 조회할 수 있다.
모든 응답은 `private, no-store`이며 DB에 저장된 값만 반환한다.

- `GET /api/v1/jobs/{job_id}/logs`
  - attempt 번호, sequence, row ID 순으로 안정 정렬한다.
  - `attempt_id`, `sequence_gte`, `sequence_lte`, timezone-aware
    `occurred_at_gte`, `occurred_at_lte` filter를 지원한다.
  - `limit` 상한은 500이다. `after`에는 응답의 opaque `next_cursor`를 전달한다.
    `tail=true`는 마지막 `limit`개를 같은 정방향 순서로 반환하며 `after`와 함께 쓸 수 없다.
  - message와 structured fields의 bearer/JWT/RVC token, password, secret key,
    authorization, URL query는 Worker ingest에서 DB에 쓰기 전에 redaction한다. 조회와
    SSE에서도 같은 redaction을 다시 적용해 기존 row에 대한 defense-in-depth를 유지한다.
- `GET /api/v1/jobs/{job_id}/metrics`
  - attempt 번호, sequence, row ID 순으로 안정 정렬하며 `key`, `epoch`, `step`,
    `attempt_id`, `offset`, `limit`을 지원한다. `limit` 상한은 500이다.
  - `tail=true`는 전체 결과 중 최신 `limit`개를 같은 정방향 순서로 반환한다.
    `offset`이 0이 아닐 때는 함께 사용할 수 없으며 `offset`은 반환 구간의 실제 시작 위치다.
- `GET /api/v1/jobs/{job_id}/artifacts`
  - 생성 시각 내림차순과 row ID로 안정 정렬하며 `artifact_type`, `offset`, `limit`을
    지원한다. `limit` 상한은 200이고 내부 `storage_uri`는 응답 schema에 없다.

`GET /api/v1/jobs/{job_id}/logs/stream`은 동일한 사용자 인증과 redaction을 적용하는 SSE다.
`after` 또는 표준 `Last-Event-ID`에 log cursor를 전달해 재개할 수 있다. 짧은 DB session으로
polling하며 heartbeat를 보내고 설정된 연결 수명과 access JWT 만료 중 더 이른 시점에
종료한다. 매 poll마다 token 폐기·사용자 활성 상태·Job 소유권을 다시 확인하며 proxy
buffering과 cache를 금지한다. Authorization query parameter는 지원하지 않는다.

## 검증

저장소 루트에서 실행한다.

```bash
.venv/bin/pytest packages/contracts/tests apps/api/tests
.venv/bin/ruff check packages/contracts apps/api
.venv/bin/mypy packages/contracts/src apps/api/src
```

## 현재 제한

- refresh token/session rotation은 아직 없다. logout은 현재 access JWT 한 개의 `jti`를 만료 시점까지 폐기한다.
- Redis login rate limit은 제공하지만 계정 잠금 정책은 후속 단계다.
- Dataset finalize는 현재 요청 내 동기 작업이다. hard timeout/cancel과 multipart/resume,
  finalize 자체의 durable RQ 전환은 후속 단계다. 만료/실패 staging orphan은 별도 RQ
  cleanup과 admin dry-run/status API로 정리한다. Dataset/TestSet 모두 전역 grace와 late-writer
  grace 중 큰 값(기본 7일) 뒤 first-delete, 기본 60초 뒤 second-delete를 요구하지만 주기 호출
  scheduler는 외부 운영 경계다. 실제 원격 S3의 7일보다 긴 PUT은 아직 장애 주입하지 않았다.
- non-WAV는 격리 decoder/ffmpeg 검증 전까지 `decoder_pending`이며 LUFS 분석과 학습에
  사용할 수 없다.
- 실제 MinIO를 통한 Worker Dataset 307 GET은 adapter/redirect 단위 검증만 완료했고,
  배포 MinIO·TLS endpoint를 포함한 통합 smoke는 아직 남아 있다.
- 현재 artifact PUT은 단일 object 방식이다. 대형 checkpoint의 multipart/resume와
  S3 multipart abort와 canonical delete tombstone 주기 작업은 후속 단계다.
- S3 finalize는 검증한 byte를 server spool에 제한적으로 저장한 뒤 canonical object로
  다시 게시하므로 무결성을 우선하는 대신 object storage read/write 비용이 추가된다.
- Redis는 rate limit과 allowlisted Dataset/TestSet staging cleanup RQ/readiness에 연결됐다. DB polling
  wake-up, realtime fan-out과 Dataset finalize RQ 전환은 아직 연결되지 않았다.
- lease 회수는 현재 heartbeat/claim 요청 시 실행된다. Worker가 전혀 없는 환경을 위한 주기적 scheduler는 아직 없다.
- Worker 한 대당 동시 활성 Job 하나만 지원한다.

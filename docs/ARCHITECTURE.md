# 아키텍처 기준선

## 제품 경계

RVC Training Orchestrator는 중앙 Manager와 여러 GPU Worker로 구성한다. Manager는 신뢰 가능한 메타데이터와 작업 상태의 원장이고, Worker는 외부 RVC WebUI 저장소를 호출하는 실행기다. RVC upstream 자체를 이 저장소의 도메인 코드와 결합하지 않는다.

```text
Browser -> Reverse Proxy -> Next.js / FastAPI
                               |-- PostgreSQL (원장)
                               |-- Redis/RQ (알림·비동기 작업)
                               |-- MinIO/S3 (대형 파일)
                               `-- MLflow (실험 추적)

GPU Worker -- authenticated HTTPS --> FastAPI
     `-- isolated subprocess --> pinned RVC WebUI checkout
```

Browser-facing scheme은 client forwarding header가 아니라 operator-owned `PUBLIC_SCHEME` 하나로
결정한다. Production은 `https`만 허용하고 bundled proxy가 API/Web upstream의
`X-Forwarded-Proto`를 이 값으로 재작성한다. Secure cookie와 HSTS도 같은 설정을 사용해 proxy hop
사이의 내부 HTTP와 외부 HTTPS를 혼동하지 않는다.

Worker의 outbound TLS는 system default trust를 기본으로 하며, 조직 사설 CA가 필요하면 설치기가
검증해 release 밖 `/etc/rvc-orchestrator/worker/ca/custom-ca.pem`에 원자 게시한 단일 bundle을
추가 trust로 사용한다. Container에는 host directory를 read-only로 mount하고 fixed path
`/etc/rvc-worker/ca/custom-ca.pem`만 허용한다. Manager API의 동기 `urllib`/비동기 `httpx` 요청과
Dataset/TestSet/Artifact external object client는 hostname 검증, `CERT_REQUIRED`, TLS 1.2 이상을
강제하는 같은 `SSLContext`를 사용한다. Fresh object client의 credential 분리와 environment proxy
비사용도 그대로 유지한다. Custom CA는 신뢰 root를 추가할 뿐 HTTP endpoint를 암호화하지 않으므로
production Manager와 object endpoint는 여전히 HTTPS여야 한다. Immutable dev.16 bundle에는
이 projection이 없고 dev.17에서 installer·Compose·SSL context 경계가 도입됐다. 현재 dev.19
source와 Worker partial bundle은 이 경계를 그대로 포함하지만, 실제 clean Ubuntu endpoint 연결
증거 전에는 사설 CA production 인수를 완료로 판정하지 않는다.

## 서비스 책임

### Manager API

- 사용자와 Worker 인증
- 관리자 사용자 목록·생성, 역할/활성 CAS와 비밀번호 재설정
- 데이터셋 업로드, 검증 상태, 저장 URI 관리
- Experiment와 Job config 검증
- 검증 모델 candidate/approval/revoke 원장과 active champion 관리
- Worker capability에 맞는 작업 배정과 lease 관리
- 상태·로그·metric·artifact metadata 수집
- 다운로드 권한 검사, 실시간 로그 전달, 감사 기록

### Maintenance execution plane

- API는 maintenance run을 PostgreSQL에 먼저 기록하고 exact JSON RQ envelope만 Redis에 넣는다.
- RQ process는 API application identity를 재사용하지 않는다. 별도 PostgreSQL login
  `rvc_maintenance`, Redis ACL user `rvc_maintenance`, MinIO maintenance identity를 사용한다.
- PostgreSQL login은 maintenance run과 Dataset/TestSet upload session의 필요한 column만
  읽고 갱신한다. Dataset/TestSet parent row는 직접 읽거나 갱신하지 않고, migration이 만든
  `SECURITY DEFINER` 함수가 upload ID에서 parent를 유도하고 binding을 재확인한 뒤 row lock만
  제공한다. 함수 owner는 `NOLOGIN`이고 `PUBLIC` 및 다른 role의 실행 권한은 제거한다.
- MinIO identity는 Manager bucket의 `datasets/staging/*`와 `test-sets/staging/*`에 대한
  `DeleteObject`만 가진다. List/Get/Put, canonical key와 MLflow bucket 접근은 거부하며 bucket
  versioning이 활성화돼 exact 삭제 의미를 보장할 수 없으면 init이 실패한다.
- Redis ACL은 고정 maintenance queue/job/worker/scheduler/result key와 실제 RQ lifecycle command만
  허용한다. Generic registry cleanup, pub/sub control, callback/dependent/repeat 실행과 Redis 관리
  명령은 허용하지 않는다. Long DB lock/object delete/confirmation wait 중에는 exact
  run ID·attempt CAS heartbeat를 별도 DB session으로 갱신해 stale reconciler와 중첩 실행을 막는다.

### Dashboard

- 관리자 전용 사용자 계정/권한/비밀번호 관리
- Worker와 작업 상태 시각화
- Dataset 품질 보고서와 Experiment/Job 생성
- 로그, loss, GPU metric, artifact, sample 비교
- 비교한 real Run의 모델 후보 등록, 명시적 승인과 폐기 이력 확인
- 권한이 있는 cancel/retry/download 작업

### Worker Agent

- 등록, heartbeat, GPU/disk capability 보고
- 원자적으로 배정된 작업의 dataset과 고정 test set 수신
- job별 workspace에서 RVC 단계를 실행
- 로그와 metric을 batch 전송하고 네트워크 실패 시 임시 spool
- 모델, 인덱스, checkpoint, sample, 환경 manifest를 checksum과 함께 업로드

## 작업 상태 머신

정상 경로는 아래 순서를 따른다. `use_f0=false`, index/sample 비활성화 같은 config에 따라 일부 상태는 건너뛸 수 있지만 서버가 허용한 전이만 사용한다.

```text
queued -> assigned -> downloading_dataset -> validating_dataset
 -> preparing_flat_dataset -> preprocessing -> extracting_f0
 -> extracting_features -> training -> saving_checkpoint
 -> building_index -> collecting_small_model -> generating_samples
 -> evaluating -> uploading_artifacts -> completed
```

모든 실행 상태는 `failed` 또는 cooperative cancel을 거쳐 `cancelled`로 끝날 수 있다. 재시도는 기존 attempt를 보존한 채 `retrying -> queued`로 새 attempt를 만든다. Worker heartbeat가 사라졌다는 이유만으로 즉시 중복 배정하지 않고 lease 만료와 프로세스 생존 가능성을 함께 고려한다.

## Live telemetry와 terminal 경계

- Native runner는 subprocess stdout/stderr callback, 증가분 `train.log` tail과 TensorBoard scalar
  polling을 하나의 attempt-scoped telemetry session에 연결한다. 같은 의미의 source event/metric은
  중복 제거하되 알려지지 않은 line도 sanitized log로 남긴다. Log와 metric은 각각 0부터 시작하는
  attempt-wide 단조 sequence를 공유하고, Manager I/O 전에 mode `0600` local spool에 원자 저장한다.
  느린 Manager 전송은 별도 delivery task가 맡으므로 subprocess pipe callback이 network I/O를
  기다리지 않는다. Spool 저장 실패는 학습을 계속하며 자료를 버리는 대신 process group을 종료하는
  typed failure다.
- 같은 session은 Job 시작 뒤 fresh observation과 heartbeat와 독립된 기본 60초 cadence로
  `system.gpu.count`, `system.gpu.telemetry_available`, 남은 disk byte와 GPU index별 사용률·사용/전체
  VRAM·온도를 먼저 spool한다. Training parser와 달리 동일한 system 값도 다른 관측 시각이면
  보존한다. 성공한 empty GPU query는 availability=1/count=0, query/semantic 실패는
  availability=0/count=0으로 구분한다. 모든 system 표본은 stage/loss와 같은 sequence/watermark를
  사용하고 spool 실패는 typed `telemetry_persistence_failed`로 Job을 중단한다.
- `current_epoch` metric은 durable batch로 먼저 저장·전송되고 실행 중인 current attempt에 한해 Job
  projection과 같은 transaction에서 단조 증가한다. Terminal/old attempt 뒤 도착한 metric은 attempt
  원장과 MLflow outbox에는 남을 수 있지만 현재 Job epoch를 되돌리거나 올리지 않는다.
- Worker는 모든 producer와 delivery boundary를 봉인한 뒤 terminal status에
  `telemetry_log_count`/`telemetry_metric_count`를 함께 싣는다. 두 값은 durable하게 발급된 다음
  sequence, 즉 exclusive upper watermark다. Manager는 이미 저장된 최대 sequence보다 작은
  watermark를 거부하고, terminal 뒤에는 exact Worker/lease/Job/attempt와
  `sequence < watermark`를 모두 만족하는 pending batch만 late ingest한다. Watermark 없는 legacy
  terminal, 다른 Worker/attempt, 상한 이상 sequence는 fail-closed한다.
  Producer 봉인 뒤 final best-effort flush를 실행하므로 healthy Manager에서는 pending이 비고,
  retryable outage에서는 watermark 미만 durable record가 late replay용으로 남는다.
- Active log/metric ingest는 Job의 조건부 no-op update를 write fence로 사용한다. Cancel 또는 terminal
  commit이 먼저 이기면 ingest는 `503`과 `Retry-After`를 반환해 Worker가 durable record를 보존한 채
  terminal watermark 기준으로 재평가하게 한다. 같은 idempotency key는 canonical payload
  fingerprint에도 결박되며, 다른 payload나 같은 sequence의 다른 값은 `409`다. Worker status/log/
  metric raw JSON은 기본 `WORKER_TELEMETRY_JSON_MAX_BYTES=2097152` 상한과 strict JSON finite-number
  검사를 인증·schema parsing 전에 통과해야 한다.
- Metric read API의 `tail=true`는 필터 결과의 최신 limit개를 가져오되 item은 attempt/sequence
  정방향으로 반환하고 offset과 결합하지 않는다. Dashboard는 최신 200개를 15초마다 조회하며
  active 요청 중 tick을 생략하고 unmount/filter 변경 시 request를 abort한다.
- 이 경계는 terminal status가 Manager에 커밋된 경우의 전송 지연을 복구한다. Manager가 완전히
  불가한 동안 Worker가 terminal에 도달하고 그 status가 커밋되기 전에 lease가 만료·회수되면,
  server-side watermark가 없으므로 old local spool을 새 attempt에 자동 합치지 않는다. 해당 자료는
  운영자 조사 대상으로 남으며 이 경우를 정상 자동 복구 또는 무손실 보장으로 표현하지 않는다.

## 데이터와 저장소

- PostgreSQL: 사용자, Worker, Dataset, Experiment, Job/Attempt/Lease, 로그 색인, metric, artifact, sample, preset, audit event
- PostgreSQL의 User row version/access-token version과 secret-free 관리자 mutation 멱등 원장은
  사용자 lifecycle의 권한·재전송·동시성 경계를 보존한다. 모든 관리자 mutation은 singleton
  bootstrap-state write fence 뒤 actor 권한과 target을 다시 읽어 cross-demotion을 직렬화한다.
  비밀번호·역할·활성 변경은 token version을 증가시켜 이미 발급된 JWT를 즉시 무효화한다.
- Redis/RQ: 중앙 내부 `rvc-maintenance` queue가 Dataset/TestSet staging orphan 정리를 API
  process와 분리해 실행한다. RQ payload는 JSON의 고정 callable과 PostgreSQL run UUID
  하나뿐이다. Redis는 untrusted envelope로 취급해 Worker가 dequeue/perform 두 경계에서
  callable·인자·callback/dependency/repeat를 allowlist 검증하며, 실제 대상·상태·결과는
  PostgreSQL 원장에서 다시 잠가 결정한다. maintenance process는 전용 role/entrypoint로
  API/Worker 인증 secret을 받지 않는다. Redis와 최근 RQ Worker heartbeat는 production
  readiness의 fail-closed 대상이다. enqueue adapter도 deterministic ID가 이미 있다는 사실만
  믿지 않고 Worker와 같은 exact envelope 및 queued/scheduled/started 위치를 검사한다.
  inactive poison/terminal/ghost는 callback·dependent를 resolve하지 않는 Redis Lua quarantine
  뒤 exact job으로 교체하고, started poison은 DB run을 typed terminal로 닫는다.
  API replica의 periodic reconciler는 PostgreSQL transaction advisory lock과 row
  `FOR UPDATE SKIP LOCKED`로 leader-safe하게 기존 `queued|retrying|enqueue_failed`와 timeout을
  넘긴 `running`만 bounded 재전달한다. `completed|failed`를 고르거나 새 run을 만들지 않는다.
  Redis와 RQ Worker뿐 아니라 reconciler cycle freshness도 production readiness의 fail-closed
  대상이다. Redis는 외부 Worker나 browser에 공개하지 않고 PostgreSQL 원장을 대체하지 않는다.
- MinIO/S3: Dataset 원본, `prepared_flat.zip`, manifest/quality report, model, index, checkpoint, log bundle, TensorBoard, sample wav
- MLflow: Job config, attempt별 scalar metric, 검증된 artifact의 Manager download 링크와
  terminal 상태를 파생 projection한다. PostgreSQL 변경과 같은 transaction의 durable
  `mlflow_sync_events` outbox를 commit한 뒤 REST로 투영하며, 실패는 지수 backoff로
  재처리한다. fail-open/fail-closed는 readiness와 API 응답 정책만 바꾸고 원장을
  rollback하거나 MLflow를 원장으로 승격하지 않는다.

Object key는 사용자 입력을 직접 포함하지 않고 서버가 생성한 UUID 기반 prefix를 사용한다. DB에는 저장 URI, 크기, SHA-256, 원본 표시명, MIME, artifact type을 기록한다.

## Model registry와 champion 선택

Model registry는 MLflow와 분리된 PostgreSQL 승인 원장이다. Experiment마다 row-version을 가진
singleton registry가 있고, entry는 `candidate`, `approved`, `revoked` 중 하나다. `revoked`는
terminal이다. Active slot은 Experiment마다 최대 한 entry만 가리키며 새 promotion은 이전 champion을
삭제하거나 폐기하지 않고 `approved` inactive 이력으로 남긴다. 따라서 운영자는 이전 승인 entry를
명시적으로 다시 promotion할 수 있지만, 현재 champion이 자동으로 과거 모델로 fallback되지는 않는다.

Candidate 생성은 exact current `completed` Job/attempt만 받는다. Attempt는 real `rvc_webui`,
`execution_provenance_version=worker-claim-v1`, reviewed RVC commit과 승인된 runtime image/asset digest
쌍을 가져야 한다. Manager는 completed upload, exact storage namespace와 manager verification을 가진
유일한 `final_small_model`을 사용하고 같은 attempt의 `final_index`가 있으면 browser가 선택한 값이
아니라 서버 원장에서 결박한다. Historical NULL provenance, Fake engine, 미완료·교체된 attempt,
중복/다른 유형 Artifact는 후보가 될 수 없다.

Candidate 생성과 promotion은 frozen model/index canonical object 전체를 다시 읽어 size와 SHA-256을
확인한다. 긴 object read는 bounded semaphore/deadline을 사용하며, storage/spool 장애는 `503`과
`Retry-After`, byte 또는 ledger 불일치는 `409`로 fail-closed한다. 이후 fresh transaction의
Experiment write fence 뒤 active User와 owner/admin 권한, registry/entry CAS, Job/attempt/Artifact/upload
fingerprint를 다시 확인한다. 모든 mutation은 actor-scoped SHA-256 key hash와 JWT-secret keyed canonical
request fingerprint를 가진 durable idempotency operation 및 같은 transaction의 audit event를 남긴다.
원문 key, storage URI/object key/upload session과 raw metadata는 public response, audit 또는 operation
snapshot에 포함하지 않는다. MLflow에는 이 원장을 위임하지 않으며 MLflow 장애가 committed registry
mutation을 되돌리지 않는다.

Browser mutation intent는 server-render 시점의 current actor UUID, 최초 key, byte-identical body와
mutation 전 전체 public Registry 지문에 결박한다. Initial POST, 응답 유실 뒤 GET reconciliation과
동일 요청 재확인 직전에 cookie-only same-origin `/bff/session/identity`의 exact `{actor_id}` projection을
확인한다. Mutation BFF는 canonical `X-RVC-Expected-Actor-ID`를 필수로 받아 Manager에 고정 전달하고,
Manager는 같은 요청의 인증 actor와 다르면 operation 생성 전에 `409`로 거부한다. Experiment 또는
actor가 바뀌면 client component key도 바뀌어 보존 intent를 폐기한다. Transport 오류, invalid success
projection과 모든 `5xx`는 commit 여부가 불명확하므로 새 key로 재전송하지 않는다.

현재 dev.19 partial bundle은 승인된 production runtime digest pair와 실제 GPU qualification이
없으므로 production candidate가 없는 것이 정상이다. 원장/API/BFF/UI 자동 회귀 통과와 실제 운영
활성화를 구분하며, 환경 변수나 SQL로 runtime gate를 열어 후보를 만들지 않는다. 실제 S3 대용량
canonical 재해시·tamper/outage, PostgreSQL 다중 replica promotion 경쟁과 browser/API response-loss
인수 전에는 model registry를 production 승인 근거로 사용하지 않는다.

모든 Dataset/Artifact/TestSet upload session은 `storage_backend`와 함께 credential을 제외한
object namespace fingerprint SHA-256을 생성 시점에 고정한다. local은 resolve된 root,
S3는 endpoint/bucket/region/addressing style을 포함하므로 access key/secret 회전은 허용하지만
같은 `s3` backend의 다른 bucket 또는 endpoint, 같은 `local` backend의 다른 root는 같다고
보지 않는다. init replay, PUT, expiry/retry/finalize/delete, maintenance, Worker Dataset
claim/GET와 Artifact download는 현재 adapter의 exact fingerprint가 다르거나 migration
sentinel `UNBOUND`이면 객체를 읽거나 지우지 않고 fail-closed한다.

`9d2f4b7c8e10` 이전의 Dataset/Artifact session은 어느 namespace의 byte인지 DB만으로 증명할
수 없으므로 migration이 현재 설정을 자동 backfill하지 않고 `UNBOUND`로 표시한다. 별도
operator adoption은 `pending|finalizing`을 거부하며 failed/expired staging 또는 completed
canonical object 전체의 size/SHA-256, DB metadata와 canonical URI를 다시 대조한 뒤에만
현재 namespace를 기록한다. preview는 binding/object를 바꾸지 않지만 audit event를 남긴다.

Dataset은 `init → bounded raw PUT/presigned PUT → finalize` 세션으로 수신한다. Manager는
staging byte 전체의 크기와 SHA-256을 다시 검증하고 mode `0700` 전용 snapshot에서
archive를 안전하게 재귀 수집한다. 원본, 결정적 `prepared_flat.zip`, `manifest.json`,
`quality_report.json` 네 object가 모두 canonical storage에 게시된 뒤에만 `ready`다.
품질 분석은 PCM frame을 bounded chunk로 읽으면서 interleaved sample count, clipped/silent sample
count와 normalized square sum을 누산한다. Dataset row에는 `pcm-sample-weighted-v1`과 이 누산값에서
계산한 clipping/silence/RMS aggregate를 all-null/all-present constraint로 저장한다. raw
PCM mono/stereo integrated loudness는 K-weighting filter 상태를 파일마다 초기화하고 각 파일의
complete 400 ms block을 75% overlap으로 수집한다. `>-70 LUFS` 절대 gate를 통과한 전체 block
energy로 상대 gate `-10 LU`를 계산한 뒤 다시 전체 파일 block에 적용하므로, file LUFS 평균이나
파일 경계 synthetic block이 아니다. Algorithm/scope는 각각
`itu-r-bs1770-4-mono-stereo-v1`, `global-gate-over-per-file-complete-blocks-v1`로 고정한다. 짧은
입력, 절대 gate 미만, layout/rate 지원 밖은 typed unavailable reason과 null LUFS를 저장하고
기존 PCM aggregate는 migration에서 loudness를 재구성하지 않는다.
`quality_report.json`은 source/member path를 포함하는 내부 canonical/audit 자료이고, 사용자 API와
Dashboard BFF는 typed count/aggregate만 투영한다. `f9c4a7d2b610` 이전 historical row는 exact sample
count를 증명할 수 없어 backfill하지 않는다.
non-WAV는 격리 decoder가 추가되기 전 `decoder_pending`이고 LUFS를 계산하거나 Job에 사용할 수 없다.
finalize는 현재 API process의 worker thread에서 동기 실행되며 token/heartbeat로 stale
중복 실행을 막는다. 만료 또는 실패 뒤 기본 7일 grace가 지난 staging object는 별도 RQ
Worker가 row lock/CAS claim 후 staging key만 멱등 삭제한다. `pending` 활성 세션,
`finalizing`, `completed`와 canonical key는 정리 대상이 아니다. Dataset local PUT은
generation/write-token heartbeat와 절대 deadline을 사용하고, canonical key는 immutable upload
session ID 아래 격리한다. finalize의 검증·prepare·no-replace publish·commit은 finalization token
heartbeat에 결박하며 cancel/commit 오류는 durable completed outcome을 fresh session에서 확인한
뒤 미커밋 session key만 정리한다. RQ cleanup도 exact generation first-delete 뒤 confirmation
second-delete까지 성공해야 완료한다. finalize 자체의 durable timeout/cancel/restart RQ 전환은
아직 구현되지 않았다. Redis job 유실은 중앙 reconciler가 복구하지만 실제 Redis/PostgreSQL
다중 replica와 전역 grace보다 긴 원격 S3 PUT 장애 주입은 열린 release gate다.

`e2f8b4c6a930` upgrade는 구 dataset-wide canonical key를 가진 active
`pending|finalizing` session을 `expired`로 fence하고 Dataset을 retryable `upload_pending`으로
되돌린다. completed legacy row는 기존 URI와 함께 보존하며 동일 idempotency payload replay는
새 session ID/generation-scoped key를 만든다. migration 전 구 API replica와 client drain은
운영 전제다.

Artifact upload도 attempt/lease에 결박된 session의 exact namespace에서만 staging 검증과
canonical 승격을 수행한다. completed Artifact download는 해당 Artifact를 만든 completed
session의 fingerprint와 현재 adapter가 일치해야 하며, 같은 key가 다른 namespace에 있더라도
그 byte를 대신 반환하지 않는다.

고정 TestSet은 revision별 immutable 원장이다. draft item은 사용자 행과 TestSet 행을 같은
순서로 잠근 뒤 `item_key`/`sort_order`를 예약하고, 서버 생성 staging/canonical key로만
raw PUT 또는 presigned PUT을 받는다. Manager는 전체 byte/SHA-256과 RIFF/WAVE PCM decode,
duration/sample-rate/channel 상한, allowlist namespace의 opaque license/provenance record ID를
검증한다. upload session은 backend뿐 아니라 local root 또는 S3 endpoint/bucket/region의
credential 없는 namespace fingerprint SHA-256도 고정하며 cleanup/retry/finalize/delete 때
현재 adapter와 다르면 원장을 보존하고 fail-closed한다. ready 전환은 각 item과
정확히 하나인 completed session, 현재 storage backend, canonical key/URI 및 실제 object
전체 hash를 다시 대조한다. manifest는 storage URI, presigned URL과 DB item UUID 없이
결정적으로 직렬화하며 해시와 내부 storage URI만 원장에 둔다. 목록은 item을 생략할 때
`items_included=false`를 명시한다.

sample-enabled Job은 ready TestSet 행을 다시 잠그고 manifest를 DB item으로 재계산한 뒤
ordered item ID/metadata와 inline inference config의 storage-neutral `sample_plan_json` 및
SHA-256을 immutable snapshot으로 저장한다. `index.build_index=false`이면 inference
`index_rate=0`만 허용해 없는 retrieval index의 묵시적 fallback을 snapshot으로 보존하지 않는다.
claim은 `fixed_test_set_inference_ready=true`이고
요청한 inference F0 method를 광고한 real RVC Worker만 후보로 삼는다. 배정 transaction 안에서
published manifest object exact byte, DB manifest/item, Job sample-plan hash와 item마다 정확히
하나인 completed upload의 canonical key/URI·namespace를 다시 대조한다. claim의
`TestSetTransfer`는 내부 URI/presigned query 없이 TestSet/family/revision, manifest/sample-plan/
inference-config hash와 ordered item ID/key/order/size/SHA/PCM metadata, current Job의 Manager
상대 path만 전달한다. 증명이 실패하면 Job과 Worker를 미배정 상태로 보존한다.

`GET /api/v1/workers/jobs/{job}/test-set/items/{item}`은 current Worker bearer, lease와 attempt,
Job↔TestSet↔item identity, namespace와 같은 transfer snapshot을 다시 검증한 뒤 Local bounded
stream 또는 짧은 단일 307을 반환한다. Worker는 Manager 요청과 외부 object 요청을 서로 다른
client로 열어 외부 요청에 Authorization/lease/attempt, Manager response cookie와 environment
proxy credential을 넣지 않는다. exact Content-Length/MIME/size/SHA와 RIFF/WAVE uncompressed
PCM metadata를 확인하고 ordered item을 mode `0700` partial directory의
`<item-id>.wav` mode `0600`으로 받은 뒤 inventory 전체를 재검증해 `inputs/test_set`을 원자
게시한다. replay도 symlink/extra/stale/mode/hash/PCM을 다시 검증하며 Manager가 검증한
sample-plan hash와 inference/item provenance를 output marker에 남긴다.

Worker는 materialized TestSet과 same-attempt final small model/index를 pinned RVC Pipeline에
투입해 PM/Harvest/CREPE/RMVPE output을 만든다. 별도 subprocess는 shell 없이 실행하고
input/model/index/operator asset을 FD+SHA-256으로 재검증하며 timeout/cancel과 출력 상한을
적용한다. CREPE는 strict asset manifest와 private projection의 고정
`runtime/crepe/full.pth`만 허용한다. 같은 FD byte를 전·중·후에 검증하고
`torchcrepe.Crepe("full")`에 명시적 `weights_only=True` strict state dict를 pre-bind한다.
같은-attempt small model도 `weights_only=True`, manifest-verified HuBERT/RMVPE operator byte는
명시적 `weights_only=False`로 분리한다. attempt-private `TORCH_HOME`과 offline environment는
cache/network fallback을 차단한다. publication manifest/request hash와 runtime image/asset
digest는 model/index/output Artifact metadata에 역할과 함께 고정된다.

Sample table은 Job↔attempt, Job↔TestSet snapshot, item↔TestSet, Artifact↔Job/attempt를 composite
FK로 결박한다. 등록 API는 current claim, approved runtime bundle, Job sample-plan과
model/index/output SHA·size·type·역할·native manifest/request를 verified upload session에
교차검증한다. canonical WAV의 PCM과 `pcm-normalized-v2` 지표는 Manager가 다시 계산한다.
같은 PCM을 낸 여러 item은 한 content-addressed Artifact를 공유할 수 있지만 논리 Sample row는
각각 유지한다. 단일 출력 256 MiB/600초와 attempt 논리 출력 합계 2 GiB/3,600초를
driver/publication/registration/completion에서 중복 검증한다. completion은 모든 item의 Sample과
현재 canonical model/index/output byte를 단일
deadline 안에서 재검증하고, download도 현재 hash 검증 뒤에만 응답한다.

TestSet local PUT은 TestSet→session lock 아래 generation/write-token CAS heartbeat와 절대 expiry
deadline을 사용한다. finalize도 verify·PCM 검사·no-replace canonical publish 전 구간에서
finalization token heartbeat를 갱신한다. 전용 RQ task는 exact namespace/generation/key를 다시
검사하고 전역 maintenance grace와 TestSet late-writer grace 중 큰 값(기본 7일) 뒤 first-delete,
기본 60초 confirmation grace 뒤 second-delete가 모두 끝난 경우에만 완료한다. active/finalizing/
completed session과 canonical object는 절대 대상이 아니다. 실제 S3의 7일보다 긴 in-flight PUT과
다중 replica 경합은 별도 장애 주입 gate다.

구현과 운영 활성화는 분리한다. upstream source verifier의 Torch 2.4 marker와 별도로
Torch `2.6.0+cu124`, Torchvision `0.21.0+cu124`, Torchaudio `2.6.0+cu124`, CUDA runtime
12.4 후보 lock 및 CREPE safe-loader는 구현됐다. Production factory는 builder-generated
qualification activation이 있을 때만 고정 TestSet dependency를 만들고 실제 binding evidence가
있을 때 네 inference F0 capability를 광고한다. Activation은 exact 49-case report archive와
runtime image/build/asset identity에 결박되고 고정 read-only 경로로만 전달된다. 그러나 현재 실제
amd64 base digest, GPU/no-network matrix, 취약점·container·라이선스 검토가 없으므로 현재 bundle의
Agent는 inference F0 목록을 비우고 `fixed_test_set_inference_ready=false`를 광고하며
`AUTO_SAMPLE_JOBS_ENABLED=false`,
`RVC_GPU_SMOKE_VERIFIED=false`, `PROFILE_STAGE_SET_VERIFIED=false`,
`RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false`를 유지한다.

## RVC 호환 규칙

| 구분 | v1 | v2 |
|---|---|---|
| feature directory | `3_feature256` | `3_feature768` |
| feature dimension | 256 | 768 |
| pretrained root | `assets/pretrained` | `assets/pretrained_v2` |

- 지원 sample rate는 config schema가 허용한 pretrained 조합에 한정한다.
- training F0: `pm`, `harvest`, `dio`, `rmvpe`, `rmvpe_gpu`
- inference F0: `pm`, `harvest`, `crepe`, `rmvpe`
- `weights/<experiment>.pth`가 있으면 이를 small model로 수집한다. 없으면 지원하는 upstream 추출 도구를 호출한다.
- `G_*.pth`, `D_*.pth`는 resume/reproducibility checkpoint로만 분류한다.
- 최종 FAISS index는 `final.index`로 제공하되 원본 `added_*.index` 정보도 manifest에 남긴다.
- real Worker의 profile/native mode는 lease-bound `DatasetStageRunner`를
  `TestSetStageRunner`로 감싼다. sample capability gate가 닫혀 있어 운영 claim에는 도달하지
  않지만, wrapper를 재귀적으로 풀어 아래 native
  commit/asset/claim 검증을 우회하지 않는다.
  `native`는 reviewed commit, strict asset manifest와 build-generated source/config/asset
  inventory를 image label·bundle provenance에 고정한다. startup/claim에서 현재 source를
  재검증하고 `O_NOFOLLOW` FD로 확인한 동일 byte만 read-only private projection에 원자
  복제한 뒤, claim의 현재 GPU index와 stage별 private inventory가 일치할 때만 core stage를
  실행한다.
- lease-bound TestSet transfer, pinned PM/Harvest/CREPE/RMVPE inference, canonical Sample
  publication/registration/completion은 구현됐지만 release GPU/no-network matrix가 없으므로
  현재 native sample-enabled Job은 계속 fail-closed한다. 환경변수나 CLI로 capability를 직접 열 수
  없고 qualification projection, actual asset SHA와 dependency binding이 모두 일치해야 한다.
  Manager 생성 gate와 Worker disabled template 기본은 false이며 자세한 release 계약은
  `RUNTIME_QUALIFICATION.md`에 있다.
- Worker `StageExecutor`의 모든 per-stage policy는 executor attempt가 1이다. 같은 attempt의
  stage 전체 retry는 partial training/checkpoint/index 산출물을 섞을 수 있어 금지한다.
  Dataset/TestSet download와 Artifact upload만 원자·멱등 transport 내부 bounded retry를 사용하고,
  telemetry transient는 durable spool에 남긴다. 최종 실패는 stage/code/category/retryable/
  sanitized cause를 가진 `StageExecutionError`로 정규화한다. cancel·lease loss가 항상
  우선하며 terminal message와 Agent log에는 내부 exception, argv, path, URL/query를 넣지 않는다.

## API 기준선

외부 경로는 `/api/v1`로 versioning한다. 원문 설계의 `/api/...` 의미를 유지하되 초기 구현부터 호환 가능한 version prefix를 둔다.

- Auth: login/logout/me; refresh session rotation은 후속 단계
- Workers: register/re-enroll, two-phase token rotate, admin revoke, heartbeat, next-job,
  job status/logs/metrics/artifacts/samples

Worker bearer 회전은 `prepare -> local durable stage -> activate`의 두 단계다. Manager에는 current와
pending token의 HMAC-SHA256만 있고 pending 평문은 1회 응답 뒤 Worker의 mode 0600 파일에만 있다.
pending 동안 claim은 닫힌다. activate는 old bearer와 pending 전용 header를 함께 요구하고 old를
즉시 폐기한다. emergency admin revoke는 active ledger를 기본 거부하며 명시적 force에서만
lease/Job/attempt를 cancelled로 terminal 처리한다. Job/Worker row version CAS가 stale status 또는
claim commit의 terminal overwrite를 차단한다. inactive Worker는 shared bootstrap과 exact ID/name을
다시 증명해야 같은 원장 identity로 re-enroll할 수 있다.
- Datasets: upload/list/detail/delete/validate/prepare-flat
- Experiments: create/list/detail, description row-version patch, reference-safe delete와 compare
- Model registry: Experiment별 list, candidate create, entry promote/revoke와 active champion 조회
- Jobs: create/list/detail/cancel/retry
- Artifacts/Samples: list와 권한 검사된 download

Worker write 요청은 batch와 idempotency key를 지원한다. 목록 API는 pagination을 사용한다. 오류는 안정적인 machine code, 사람이 읽을 메시지, correlation ID로 응답한다.

Model registry mutation body는 strict exact-key schema와 bounded raw JSON을 통과하고 모든 요청에
`Idempotency-Key`가 필요하다. Browser는 same-origin BFF의 고정 endpoint만 호출하며 HttpOnly JWT,
Manager path, Artifact index 선택 또는 runtime provenance 값을 직접 다루지 않는다. BFF는 public
allowlist만 투영하고 네트워크 단절·invalid 2xx·모든 `5xx`처럼 commit 여부가 불명확한 mutation을 새
key로 자동 재전송하지 않는다. 최초 actor와 현재 cookie session을 exact identity BFF로 확인하고,
browser mutation의 expected actor header를 Manager 인증 actor와 다시 결박한다. Registry GET으로
version/state를 확인한 뒤 완전히 unchanged인 경우에만 보존한 같은 key와 같은 body의 명시적
재확인을 허용한다.

Dataset 내부 URI는 사용자 응답에서 숨기며 owner/admin 경계를 적용한다. 삭제는 Dataset
행을 잠그고 `deleting`을 먼저 commit한 뒤 object를 정리한다. 참조 Experiment/Job과
활성 upload/finalize가 있으면 거부한다. Experiment/Job 생성과 Worker claim도 같은
Dataset readiness를 다시 검사한다. 원격 Worker용 Dataset GET과 archive
checksum 전달은 `JobClaim.dataset_transfer`로 구현한다. claim은 내부 URI 대신
`prepared_flat.zip`의 정확한 size/SHA-256과 인증된 Manager 상대 경로만 제공한다.
현재 Worker bearer/lease/attempt가 모두 일치해야 Local bounded stream 또는 짧은 S3
presigned 307을 받고, Worker는 archive와 각 member를 재검증한 뒤 job workspace의
`inputs/prepared_flat`에 원자적으로 materialize한다. Manager 요청과 external object 요청은
fresh client로 분리해 Authorization/lease/attempt, response cookie와 proxy credential을
external 307에 전달하지 않으며 HTTPS Manager의 HTTP downgrade, userinfo/fragment, 상대 URL과
redirect chain을 거부한다.

sample-enabled claim의 TestSet item도 같은 Worker/lease/attempt와 fresh-client 경계를 사용한다.
각 item endpoint는 Job snapshot에 포함된 ID만 반환하고, Worker는 전체 snapshot을 한 디렉터리로
원자 게시하므로 개별 파일 download 성공만으로 TestSet 수신 완료를 선언하지 않는다.

## 설치 및 배포 결정

초기 지원 플랫폼은 Ubuntu 22.04/24.04 x86_64다.

- Manager bundle: 애플리케이션 image/compose/config template, install/upgrade/uninstall/backup 도구, manifest와 checksum
- Worker bundle: Worker image/config/system service, NVIDIA 환경 사전 검사, pinned RVC revision/asset manifest, checksum
- 개발 중에는 image를 로컬 build할 수 있게 하고, release package는 registry pull 방식과 air-gapped image archive 방식을 구분한다.
- 설치 파일은 데이터 디렉터리를 패키지와 분리하고 재실행·업그레이드에도 보존한다.
- Worker custom CA는 versioned release에 넣지 않고 root-controlled config에 보존한다. 설치/upgrade에서
  새 CA option을 생략하면 기존 byte를 유지하고, replacement의 staging·prevalidation 또는 activation이
  실패하면 이전 CA와 environment를 함께 복구한다. 일반 uninstall도 config와 CA를 삭제하지 않는다.

installer image closure v2는 self-contained bundle을 closed world로 취급한다. Manager는
`api`, `web`, `mlflow`, `postgres`, `redis`, `minio`, `minio-client`, `nginx` 정확히 8개,
Worker는 `runtime` 정확히 1개의 linux/amd64 image만 허용한다. strict
`images-manifest.json`은 archive hash/size와 Docker-save inventory, source/runtime reference,
image/config digest, OS/architecture, application release version/revision label을 고정한다.
Manager dependency는 upstream source reference와 별도로 version-scoped
`rvc-orchestrator-<role>:<version>` alias를 실행해 upgrade가 rollback 대상 tag를 덮지 않게 한다.
self-contained 환경은 모든 Compose image service에 `RVC_IMAGE_PULL_POLICY=never`를 사용한다.
설치기는 load 전에 archive/manifest, load 뒤 현재 image identity를 검증하고, 설치된
start/restart/run/create 및 rollback도 release manifest·env·loaded identity를 다시 확인한다.
partial bundle은 빈 v2 image inventory와 `SELF_CONTAINED=false`를 명시하며 air-gapped
release가 아니다.

## 아직 확정이 필요한 운영 정책

- 조직/사용자별 격리 수준과 quota
- dataset 및 checkpoint 기본 보존 기간
- Worker lease와 offline 판정 기본 시간
- 공식 배포 도메인/TLS 발급 방식
- RVC upstream commit과 pretrained asset 배포 라이선스
- GPU/CUDA/PyTorch 지원 조합

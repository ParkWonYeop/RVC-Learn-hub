# AGENTS.md

이 문서는 RVC Training Orchestrator 저장소에서 작업하는 사람과 자동화 에이전트가 반드시 따라야 할 공통 지침이다. 하위 디렉터리에 별도 `AGENTS.md`가 생기면 그 디렉터리에서는 하위 문서가 추가 규칙을 제공하지만, 이 문서의 안전·추적 규칙을 완화할 수 없다.

## 작업 시작 전 필독

1. `README.md`로 제품 경계를 확인한다.
2. `CHECKLIST.md`에서 현재 단계와 미완료 항목을 확인한다.
3. `docs/DEVELOPMENT_HISTORY.md`의 최신 항목을 읽어 직전 결정과 검증 결과를 확인한다.
4. `docs/ARCHITECTURE.md`의 서비스 경계와 불변 조건을 확인한다.
5. `docs/REQUIREMENTS_TRACEABILITY.md`에서 관련 요구사항 ID와 완료 조건을 확인한다.
6. 실제 GPU/RVC image 또는 installer를 변경하면 `docs/RVC_RUNTIME_MATRIX.md`의
   호환 조합과 출시 gate를 확인한다.
7. installer나 설치 흐름을 변경하면 `docs/INSTALLATION_GUIDE.md`와
   `docs/DEPLOYMENT.md`를 함께 갱신한다.
8. 설치·운영·사용 흐름을 변경하면 `docs/OPERATIONS_GUIDE.md`의 역할별 runbook과
   liveness/readiness 구분을 함께 갱신한다.
9. 테스트 범위나 출시 판정을 변경하면 사용자 실행·증적 기준인 `docs/TEST_GUIDE.md`와
   자동화 상세인 `docs/TESTING.md`를 함께 갱신한다. 사용자 인수 결과를 받을 때는
   `docs/TEST_RESULT_TEMPLATE.md`의 상태와 redaction 기준을 사용한다.
10. 작업 전 `git status --short`를 확인하고 다른 작업자의 변경을 보존한다.

## 작업 종료 규칙

- 실제로 완료하고 검증한 항목만 `CHECKLIST.md`에서 `[x]`로 바꾼다.
- 코드·구성·운영 방식에 영향을 준 모든 작업은 `docs/DEVELOPMENT_HISTORY.md` 맨 위의 최신 날짜 아래에 기록한다.
- 이력에는 목적, 변경 파일, 핵심 결정, 실행한 검증 명령과 결과, 남은 위험을 구체적으로 남긴다.
- 공개 API, 상태, 환경 변수 또는 설치 절차가 바뀌면 관련 문서와 예제도 같은 변경에서 갱신한다.
- 구현과 테스트를 요구사항 ID에 연결하고 추적표 상태를 함께 갱신한다.
- 임시 우회나 미구현 동작은 숨기지 말고 체크리스트와 이력에 명시한다.

## 아키텍처 불변 조건

- **Manager와 Worker 분리**: Manager는 학습 명령을 실행하지 않고, Worker만 RVC 엔진을 실행한다.
- **Container 실행 사용자 증거**: API/Web/Worker image는 명시적 non-root user를 유지하고 image
  inspect와 실제 preflight로 검증한다. MLflow는 image와 Compose 모두 UID/GID `10002:10002`로
  고정하고 read-only rootfs, capability drop, PID 상한과 UID-owned `/tmp` tmpfs를 유지한다.
  Dockerfile 문자열이나 Compose render만으로 실행 증거를 대신하지 말고 network-none health smoke도
  통과시킨다.
- **Manager runtime secret projection**: installer가 만든 root 소유 mode `0600` source secret을
  API/RQ/MLflow non-root container에 직접 bind-mount하지 않는다. Root, network-none initializer만
  source를 읽고 API UID/GID `10001:10001`, maintenance `10001:10001`, MLflow `10002:10002`의 서로
  다른 named volume에 exact allowlist를 투영한다. API에만 JWT/bootstrap/pepper를 제공하고 RQ에는
  PostgreSQL·Redis·Manager S3, MLflow에는 PostgreSQL·전용 S3만 제공한다. Generation directory와
  mode `0400` 파일을 모두 완성·fsync한 뒤 `current` symlink를 원자 교체하며 `fchmod` 뒤 `fchown`
  순서, `O_NOFOLLOW`, regular/non-empty/size/NUL 검사를 유지한다. 실패 시 이전 generation을 보존하고
  start/restart/create 전에 installed Compose wrapper가 projection을 새로 고쳐야 한다.
- **MinIO 최소 권한**: Manager app identity와 MLflow identity에는 각각 exact Manager bucket 또는
  MLflow artifact bucket 정책 하나만 연결한다. Built-in `readwrite` 같은 전역 정책을 남기거나
  두 identity가 상대 bucket을 읽고 쓰게 해서는 안 된다. Init 재실행은 broad policy를 제거하고
  exact policy attachment를 다시 검증해야 한다.
- **인증 분리**: 사용자 JWT와 Worker별 토큰은 별도 인증 흐름과 권한을 사용한다. 원문 토큰이나 비밀번호를 DB/로그에 저장하지 않는다.
- **JobConfig immutable snapshot**: 신규 Job은 기본값까지 채운 `JobConfig.model_dump(mode="json")`의
  canonical UTF-8 JSON과 SHA-256을 같은 transaction에 저장한다. Claim 전에는 저장 raw JSON 해시,
  정규화 모델 해시, Job 핵심 컬럼과 저장 해시가 모두 같아야 하며 새 attempt와 Worker claim에는 같은
  해시를 복제한다. Canonical number는 JSONB 왕복에서 부호가 사라지는 `-0.0`을 `0.0`으로 접는다.
  Worker는 wire validation 뒤 workspace 생성 전에 다시 해시한다. Active lease의 heartbeat/renew/input
  download/status/telemetry/artifact/sample과 긴 검증의 최종 write fence도 exact Job/attempt hash를
  다시 확인한다. Artifact local PUT은 UUID writer token·heartbeat·절대 만료와 단일 seal CAS를
  유지한다. S3 PUT은 exact `If-None-Match: *`만 허용하고 Worker는 transport 오류, sealed local `409`,
  conditional S3 `412` 뒤 새 PUT을 만들지 않고 같은 session finalize로 Manager의 전체 byte 검증을
  받아야 한다. Artifact finalize는 별도 UUID 소유권 token과 DB heartbeat를 유지하며 token CAS를
  먼저 terminal로 commit한 소유자만 원장을 확정한다. API-owned cleanup reconciler는 RQ maintenance
  identity를 재사용하지 않고 별도 cleanup token을 claim한다. 삭제 전 Job/attempt/type/session ID로
  staging/canonical key를 재구성해 exact match를 확인하고, local staging은 terminal 뒤 단일 pass,
  S3 staging과 실패 canonical은 각각 grace 뒤 first delete와 confirmation grace 뒤 second delete가
  모두 끝나야 완료한다. Historical NULL cleanup marker도 같은 reconciler가 처리하고 production에서
  reconciler를 끄거나 실패 cleanup을 quota 해제로 간주하지 않는다. Sample 최종
  등록은 PostgreSQL row lock뿐 아니라 SQLite에서도 유효한 Job no-op write fence 뒤 전체 claim을
  다시 읽는다. Historical
  `NULL` Job/attempt 해시는 현재 설정으로 추정 backfill하지 않고 새 claim/retry/model candidate에서
  fail-closed하며 NULL queued row는 claim 후보 limit 전에 제외한다. Non-NULL corrupt queued Job은
  정의된 `queued -> failed` 전이와 audit로 격리하고, corrupt attempt의 lease 회수는 자동 재큐잉하지
  않는다. Attempt가 생긴 뒤 Job 해시를 바꾸거나 stale hash의 유효 JSON을 비교/MLflow/실행 증거로
  사용하지 않는다.
- **TLS 전달 신뢰 경계**: 외부 TLS proxy 뒤에서 browser HTTPS를 판정할 때 bundled proxy가
  `X-Forwarded-Proto`를 내부 HTTP 값으로 덮거나, 임의 client가 보낸 forwarding header를 신뢰하게
  해서는 안 된다. Dev.11의 operator-owned `PUBLIC_SCHEME` 단일 값, production https start gate,
  upstream header 재작성, Secure session cookie와 edge-owned HSTS 결박을 유지한다. 실제 외부 TLS/
  browser 검증 전 production 합격으로 기록하지 않는다.
- **Worker TLS custom CA 경계**: 사설 CA는 installer의 `--ca-bundle-file`로만 받고
  root 소유 regular non-symlink, mode `0444|0644`, 1 MiB 이하의 certificate-only PEM을
  검증한 뒤 release 밖 설정에 mode `0444`로 원자 게시한다. Container에서는
  `/etc/rvc-worker/ca/custom-ca.pem` 고정 read-only path만 허용하고 start/restart/run/create 전
  owner·mode·PEM을 다시 검증한다. System trust에 custom CA를 추가한
  `CERT_REQUIRED`, hostname check, TLS 1.2+ SSL context 하나를 Manager control/stream과
  Dataset·TestSet·Artifact object 전송에 공통 사용하며 `verify=false`, `curl -k`, 환경 proxy
  credential을 허용하지 않는다. Custom CA는 HTTP를 암호화하지 않으므로 production
  Manager/Object endpoint는 계속 HTTPS여야 한다.
- **Worker CA 신뢰 경계**: Host trust store에 사설 CA를 추가한 것을 Worker container의
  Manager/Object TLS 검증 증거로 취급하지 않는다. Custom CA의 read-only mount와 명시적 SSL
  context가 구현·검증되기 전 설치 인수는 runtime image가 기본 신뢰하는 public CA만 허용한다.
  `verify=false`, `curl -k` 또는 image CA store 수동 변경으로 우회하지 않는다.
- **관리자 사용자 lifecycle**: 사용자 목록·생성·역할/활성 변경·비밀번호 재설정은 admin-only
  `/api/v1/admin/users` 경계에서만 수행한다. Mutation은 16 KiB raw body, strict schema,
  actor-scoped hash idempotency key와 keyed fingerprint, target row version을 유지한다. 비밀번호,
  hash, idempotency key 원문을 response/audit/operation ledger에 넣지 않는다. Singleton DB write
  fence와 fence 뒤 actor 재검증을 제거하지 말고 자기 강등/비활성화 및 마지막 active admin
  보호를 유지한다.
- **사용자 token 영구 fencing**: 역할·활성 상태나 비밀번호가 바뀌면
  `access_token_version`을 증가시키고 모든 기존 JWT를 즉시 401로 닫는다. 재활성화가 이전 token을
  되살리거나 version claim 없는 legacy JWT를 허용하면 안 된다. Browser는 HttpOnly cookie를
  same-origin 고정 BFF 밖으로 전달하지 않고 사용자 mutation 응답 유실을 blind retry하지 않는다.
- **원자적 작업 배정**: 하나의 작업은 한 번에 한 Worker에만 임대된다. 임대 만료·heartbeat·재시도는 중복 학습을 막는 방향으로 구현한다.
- **terminal/heartbeat 경쟁**: terminal transition이 versioned Worker release와 heartbeat telemetry
  commit에 경합해 `StaleDataError`가 나면 actor ID를 request session에 보존하고 active lease,
  current Job/attempt/Worker, expected status와 cancel/artifact gate 전체를 정확히 한 번 다시 읽는다.
  Lease 교체·만료·재배정이나 취소를 통과시키는 부분 재시도 또는 무제한 재귀로 바꾸지 않는다.
- **terminal telemetry watermark와 ingest fence**: Worker는 attempt 전체의 log/metric sequence를
  각각 0부터 단조 증가시키고 Manager 전송 전에 local spool에 원자 저장한다. terminal status의
  `telemetry_log_count`와 `telemetry_metric_count`는 함께만 보내는 exclusive upper bound이며,
  durable enqueue가 끝난 producer를 봉인한 뒤 계산한다. Manager는 exact Worker/lease/Job/attempt가
  같은 terminal attempt에 대해서만 `sequence < count`인 late batch를 허용하고, watermark가 없는
  legacy/system-recovery terminal, 다른 Worker/attempt와 상한 이상 sequence는 거부한다. late metric은
  현재 Job의 epoch를 바꾸지 않는다. Active ingest와 terminal/cancel 경쟁은 Job write fence로
  직렬화하고 fence가 졌으면 `503`/`Retry-After`로 terminal watermark 재평가를 유도한다. log/metric
  idempotency key는 canonical payload fingerprint와 결박해 같은 key의 다른 payload를 거부한다.
  Worker telemetry status/log/metric raw JSON은 기본 2 MiB 상한과 strict finite-number 검사를 유지한다.
  Active Job의 session-start fresh observation과 기본 60초 system snapshot은 heartbeat와 독립된
  deadline에서 같은 metric sequence와 spool을 사용한다. 설정 범위 10~3,600초를 유지한다. Canonical
  key는 `system.gpu.count`, `system.gpu.telemetry_available`, `system.disk_free_bytes`,
  `system.gpu.<index>.utilization_percent|vram_used_mb|vram_total_mb|temperature_c`다. 같은 값이 여러
  cadence에서 관측돼도 서로 다른 시간 표본이므로 semantic dedupe하지 않는다. 성공한 empty query는
  availability=1/count=0, query/semantic 실패는 0/0이어야 한다. GPU inventory의 64개/index 범위·
  고유 index/UUID, VRAM과 finite 값 검증을 완화하지 않는다. Spool 실패는
  `failed/telemetry_persistence_failed`이며 terminal producer seal 뒤 healthy final flush와 retryable
  outage pending 보존을 유지한다.
  Manager 전체 장애 중 terminal status가 커밋되기 전에 lease가 회수된 경우에는 watermark가 없으므로
  old local spool을 자동 채택하지 않는다. 이 잔여 자료는 운영자 조사 대상으로 명시하고 검증 없이
  새 attempt에 합치거나 정상 복구로 기록하지 않는다.
- **Metric 최신 구간**: `GET /jobs/{id}/metrics?tail=true`는 최신 limit개를 읽되 응답을
  attempt/sequence 정방향으로 반환하고 nonzero offset과 결합하지 않는다. Browser BFF는 boolean
  tail만 전달하며 Dashboard 15초 poller는 요청을 겹치지 않고 cleanup 시 active request를 abort한다.
- **Worker token 회전/폐기**: 평문은 1회 응답과 mode `0600` Worker credential에만 둔다.
  self rotation은 idle/no-active-lease의 prepare→durable local stage→old+pending activate로 수행하고
  pending 중 claim을 막는다. admin revoke는 RBAC와 exact identity 확인을 거치며 active assignment를
  force할 때 lease/Job/attempt를 먼저 terminal cancelled/released로 닫는다. stale status/claim은
  row version CAS로 거부하고 inactive-only bootstrap re-enroll만 같은 Worker ID를 재활성화한다.
- **Experiment 불변성과 변경 경계**: Experiment의 정규화한 이름과 Dataset binding은 Job/MLflow
  provenance이므로 생성 뒤 바꾸지 않는다. 설명만 `expected_row_version` CAS로 수정하고, Job 또는
  MLflow projection/outbox가 있는 Experiment는 삭제하지 않는다. 신규 owner/name 충돌은 DB unique
  key와 API 선검사로 모두 막되 migration 전 중복 history의 ID·이름·Job 관계를 임의로 합치거나
  삭제하지 않는다.
- **Model registry 승인 원장**: 모델 후보는 exact current `completed` Job/attempt의 real
  `rvc_webui` 실행에서만 만들고, `worker-claim-v1` attempt snapshot의 reviewed RVC commit과 승인된
  runtime image/asset digest 쌍을 요구한다. Manager가 검증 완료한 `final_small_model`과 같은 attempt의
  유일한 `final_index`를 원장에 결박하며 candidate 생성과 promotion에서 canonical object 전체를
  size/SHA-256으로 다시 검증한다. 상태는 `candidate -> approved -> revoked`이고 `revoked`는 terminal이다.
  Experiment별 active champion은 0개 또는 1개이며 새 promotion이 이전 승인 row를 폐기하지 않는다.
  Registry와 entry row version CAS, Experiment write fence 뒤 actor 권한 재검증, actor-scoped hashed
  idempotency와 keyed fingerprint를 유지한다. 원문 idempotency key, storage URI/object key/upload
  session/raw metadata는 response·audit·operation ledger에 넣지 않는다. Fake, historical NULL,
  미승인 runtime과 canonical byte 불일치는 fail-closed하며 MLflow를 registry 원장으로 사용하지 않는다.
  Browser의 응답 유실 intent는 최초 actor ID·key·byte-identical body·전체 원장 지문에 함께 결박한다.
  Initial mutation, GET reconciliation과 같은 요청 재확인 전에 same-origin session identity를 다시
  확인하고 BFF가 요구하는 `X-RVC-Expected-Actor-ID`를 Manager가 현재 인증 actor와 같은 요청 안에서
  대조한다. Actor/Experiment가 바뀌면 intent를 폐기하고 page를 remount하며, transport·invalid success와
  모든 `5xx`는 불명확 결과로 취급해 새 key로 blind retry하지 않는다.
- **유실 lease 회수**: lease 만료와 Worker offline grace를 모두 확인한 뒤 unfinished
  attempt를 먼저 종료하고 새 attempt를 배정한다. cancel 요청을 자동 재큐잉보다 우선하고,
  자동 회수 상한을 우회하지 않는다.
- **상태 전이 검증**: 임의 문자열로 상태를 덮어쓰지 않고 정의된 상태와 전이만 허용한다. 종료 상태는 `completed`, `failed`, `cancelled`다.
- **경로 안전성**: 업로드 파일명, 압축 해제 경로, 실험 이름을 신뢰하지 않는다. path traversal, symlink escape, 압축 폭탄과 허용되지 않은 확장자를 차단한다.
- **RVC 버전 인식**: v1 feature는 `3_feature256`, v2 feature는 `3_feature768`이다. v3는 현재 지원 범위가 아니다.
- **F0 옵션 분리**: 학습은 `pm|harvest|dio|rmvpe|rmvpe_gpu`, 샘플 inference는 `pm|harvest|crepe|rmvpe`만 허용한다.
- **모델 의미 보존**: `logs/<exp>/G_*.pth`와 `D_*.pth`는 체크포인트다. 배포 모델은 `weights/<exp>.pth` 또는 공식 추출 절차의 결과다.
- **인덱스 의미 보존**: `added_*.index`를 최종 `index/final.index`로 정규화하되 원본 이름과 checksum을 metadata에 남긴다. `total_fea.npy`는 별도 artifact다.
- **모델/인덱스 역직렬화 신뢰 경계**: 사용자나 외부가 업로드한 `.pth`, `.pt`,
  `.index`는 native runner 입력으로 받지 않는다. 같은 attempt가 생성한 model/index와
  source/size/SHA-256이 manifest에 고정된 operator asset만 허용한다. upstream
  `pyproject.toml`의 Torch 2.4 marker는 reviewed source 검증 기준으로만 유지하고 출시
  runtime으로 사용하지 않는다. 현재 release 후보는 Torch `2.6.0+cu124`, Torchvision
  `0.21.0+cu124`, Torchaudio `2.6.0+cu124`, CUDA runtime `12.4`의 exact lock이다.
  sample inference의 같은-attempt small model과 `runtime/crepe/full.pth`는 명시적
  `weights_only=True`, HuBERT/RMVPE는 manifest로 검증한 operator-reviewed byte에 한해
  명시적 `weights_only=False`로 읽는다. 전역 loader override로 이 호출별 trust mode를
  바꾸지 않는다. 실제 amd64 base digest, GPU/no-network matrix, 취약점 및 라이선스 검토를
  통과하기 전에는 runtime release gate를 열지 않는다.
- **CREPE offline asset 경계**: `runtime/crepe/full.pth`는 strict asset manifest와
  build-generated private projection의 exact inventory에 반드시 포함한다. inference driver는
  고정 path를 `O_NOFOLLOW` FD/size/SHA-256으로 전·중·후 재검증하고, `torchcrepe.Crepe("full")`에
  `weights_only=True` strict state dict를 미리 bind한 뒤에만 upstream pipeline을 호출한다.
  `TORCH_HOME`은 attempt-private이어야 하고 Hugging Face/Transformers offline mode를 유지하며
  cache miss를 network download나 process-global torchcrepe cache로 대체하지 않는다.
- **외부 프로세스 격리**: RVC 명령은 인자 배열로 실행하고 shell 문자열 결합을 금지한다. job별 작업 디렉터리 밖을 쓰지 못하게 한다.
- **단계 재실행 금지**: `StageExecutor`는 어떤 stage도 자동 재실행하지 않는다. 특히
  training/checkpoint/index의 partial output을 같은 attempt에서 섞지 않는다. 자동 retry는
  원자·멱등인 Dataset/TestSet download와 Artifact upload 내부의 명시적 bounded backoff만 허용한다.
  telemetry transport 실패는 durable spool에 보류하고 stage를 재실행하지 않는다.
- **typed Worker 실패**: terminal `error_code`는 `StageExecutionError`의 고정 taxonomy에서만
  선택하고 `error_message`와 로그에는 exception 원문, argv, local path, URL query, token을
  넣지 않는다. cancellation/lease loss는 다른 오류보다 우선해 `cancelled`로 종료한다.
- **공유 RVC source 불변**: image/host의 pinned RVC checkout을 cwd로 직접 실행하거나
  그 `logs`, `assets/weights`, `weights`에 쓰지 않는다. 실제 stage는 검증된 allowlist를
  build-generated path/size/SHA-256/mode manifest와 image/bundle provenance에 결박한다.
  Runtime image build는 clean 40-hex committed orchestrator source와 release source closure를
  요구하고 Worker/contracts/runtime helper를 exact Git archive에서만 가져온다. Host working tree
  `cp -R`, ignored cache, non-amd64 daemon 또는 target platform을 생략한 build를 허용하지 않는다.
  attempt의 `work/rvc`에는 expected regular file을 `O_NOFOLLOW` FD로 열어 검증한 동일 byte만
  원자 복제하고, stage마다 read-only private projection의 전체 inventory를 재검증한 뒤에만
  실행한다.
- **Dataset 수신 검증**: claim에는 내부 storage URI를 넣지 않고 server-verified
  `prepared_flat.zip`의 Manager 상대 경로, size와 SHA-256만 제공한다. Worker 인증은
  Manager endpoint에만 보낸다. 외부 307은 fresh client와 `trust_env=false`로 요청해
  Authorization/lease/attempt, response cookie와 proxy credential을 전달하지 않으며,
  archive를 workspace 안에서 다시 경로·symlink·중복·폭탄 검사한 뒤 평탄화한다.
- **Dataset upload/cleanup fence**: local raw PUT은 Dataset→session 잠금 뒤 immutable session
  generation/write-token CAS heartbeat와 `expires_at` 절대 deadline을 사용한다. finalize의 spool,
  preparation, session-scoped no-replace canonical publish와 최종 commit도 generation/finalization
  token heartbeat에 결박한다. request 취소나 commit 결과 모호성은 fresh session에서 durable
  `completed`를 먼저 확인하고, 미커밋일 때만 해당 upload session의 canonical key를 shield cleanup한다.
  새 generation은 새 session ID와 staging/canonical namespace를 사용하며 old writer/finalizer가
  replacement key를 지우지 못한다. maintenance는 active writer/finalizer와 completed session을
  제외하고 유효 grace 뒤 exact staging key를 first-delete, confirmation grace 뒤 second-delete한
  경우에만 완료한다. canonical key는 maintenance에서 삭제하지 않고 Dataset hard delete도 모든
  failed/expired staging cleanup이 끝날 때까지 거부한다.
- **Dataset 품질 집계 공개 경계**: canonical `quality_report.json`과 DB의
  `quality_report_json`은 source/member 경로와 세부 거부 사유가 들어갈 수 있는 내부 원장이다.
  browser와 일반 Dataset API에는 이를 그대로 반환하지 않고 typed count와 `pcm_quality`만
  allowlist 투영한다. PCM aggregate는 `pcm-sample-weighted-v1`만 허용하고 validated file count,
  interleaved sample count, clipping/silence 비율, RMS 비율과 silence threshold가 모두 null이거나
  모두 존재해야 한다. clipping/silence는 파일 평균이 아니라 실제 sample count로 가중하고,
  RMS는 per-file 반올림값이 아니라 `sqrt(Σ(sample/full_scale)² / total_samples)`로 계산한다.
  Integrated loudness는 `itu-r-bs1770-4-mono-stereo-v1`과
  `global-gate-over-per-file-complete-blocks-v1`만 허용한다. 파일마다 K-weighting 상태를 초기화하고
  400 ms/75% overlap의 complete block만 모아 strict `>-70 LUFS` 절대 gate 뒤 전체 energy의
  `-10 LU` 상대 gate를 적용한다. File LUFS를 평균하거나 서로 다른 파일 끝/시작을 합쳐 synthetic
  block을 만들지 않는다. 짧은 입력, 절대 gate 미만, speaker mask를 확정할 수 없는 3채널 이상과
  8~384 kHz 밖 sample rate는 숫자를 추정하지 말고 typed reason과 `integrated_lufs=null`로 남긴다.
  decoder 대기 파일과 exact sample count가 없는 historical row를 0으로 추정하거나 raw report에서
  재구성하지 않는다. LUFS migration 전 PCM row의 nested loudness도 명시적 `null`로 보존한다.
- **TestSet 수신 검증**: sample Job claim은 storage URI/presigned query 대신 TestSet/family/
  revision, manifest/sample-plan/inference-config hash와 ordered item ID/metadata, current Job의
  Manager 상대 item path만 제공한다. Manager는 claim과 item GET 모두에서 current Worker/
  lease/attempt, ready manifest object, DB sample-plan과 completed upload의 metadata/canonical
  key·URI·namespace를 다시 검증한다. Worker는 Dataset과 같은 fresh-client 307 경계를 적용하고 Content-Length/MIME/
  size/SHA-256, RIFF/WAVE uncompressed PCM과 sample rate/channel/duration을 확인한다. 모든
  item은 `inputs/test_set/<item-id>.wav` mode `0600`으로 전용 partial directory에 받은 뒤
  exact inventory를 재검증해 디렉터리 전체를 원자 게시한다. replay도 symlink/extra/stale/
  permission/hash/PCM을 다시 검증하고 Manager sample-plan 재검증 hash를 provenance marker에
  남긴다. count/item/total byte와 retry/cancel 상한을 임의로 완화하지 않는다.
- **TestSet upload/cleanup fence**: local PUT은 TestSet→session 잠금 뒤 generation/write-token
  CAS와 heartbeat를 사용하고 session `expires_at`을 절대 deadline으로 지킨다. finalize의 spool,
  PCM 검사와 no-replace canonical publish 전 구간도 generation/finalization-token heartbeat로
  보호한다. cleanup은 exact namespace와 generation-scoped staging key만 소유하며 active writer,
  finalizing, completed session과 canonical key를 건드리지 않는다. 유효 grace 뒤 first delete와
  confirmation grace 뒤 second delete가 모두 끝난 뒤에만 cleanup 완료로 기록한다.
- **Sample 원장과 canonical byte 결박**: Worker는 같은 native inference run의 model/index/output에
  reviewed RVC commit, approved runtime image/asset digest 쌍, native manifest/request SHA-256과
  `sample_model|sample_index|sample_output` 역할을 기록한다. Manager는 current lease/attempt,
  Job sample-plan, TestSet item, verified upload session과 이 metadata를 등록·완료 시 다시
  교차검증하고 canonical object 전체를 재해시한다. Sample download도 현재 byte 검증 뒤에만
  제공한다. 동일 PCM 출력은 하나의 content-addressed Artifact를 여러 논리 Sample이 공유할 수
  있지만 item/input identity는 Sample row마다 따로 보존한다. 단일/attempt 총 byte·duration,
  raw JSON, PCM channel/rate와 검증 concurrency/deadline 상한을 우회하지 않는다.
- **Sample browser 전송**: 브라우저는 HttpOnly session을 same-origin BFF 밖으로 보내지 않는다.
  Sample JSON은 storage/artifact 내부 식별자를 제거한 allowlist projection만 제공하고 WAV BFF는
  외부 redirect를 거부한다. 단일 유효 `Range`/강한 `If-Range`만 전달해 200/206/416과 bounded
  Content-Length/Content-Range/ETag만 투영한다. Manager는 매 요청 canonical byte를 재해시하고
  content SHA-256을 안정 ETag로 사용하며, 검증 slot과 spool은 전송 완료·disconnect까지 보유한다.
- **sample index 의미 보존**: `auto_inference_samples.enabled=true`인데
  `index.build_index=false`이면 `index_rate`는 반드시 `0`이어야 한다. 없는 index를 조용히
  fallback하거나 nonzero retrieval 설정을 snapshot에 남기지 않는다. Manager claim은 DB의
  `sample_plan_json`과 SHA-256을 다시 계산해 둘 중 하나라도 변조되면 Job/Worker를 미배정
  상태로 보존한다.
- **typed runner 활성화 gate**: `native`는 검증된 offline runtime bundle에서만 명시적으로
  선택하는 guarded mode다. 시작 전 reviewed commit/asset manifest를 다시 검증하고 claim의
  training/RMVPE GPU ID를 현재 visible capability와 대조한다. lease-bound TestSet 전송이
  구현됐고 PM/Harvest/CREPE/RMVPE inference와 Sample publication/completion 코드 및 Torch
  `2.6.0+cu124` 후보 lock이 있더라도 실제 amd64 base digest, GPU/no-network matrix,
  취약점·라이선스 검토를 통과하기 전에는 `fixed_test_set_inference_ready=false`, 빈 inference
  F0 capability와 `AUTO_SAMPLE_JOBS_ENABLED=false`를 유지하고
  `auto_inference_samples.enabled=true`는 fail-closed한다. 실제 release matrix를 통과하기 전에는
  installer의 명시적 unverified-GPU 확인과 `RVC_GPU_SMOKE_VERIFIED=false`,
  `PROFILE_STAGE_SET_VERIFIED=false`, `RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false`를 유지한다.
- **Sample release qualification**: capability를 env/YAML/CLI boolean이나 digest로 열지 않는다.
  raw qualification은 core 8, training F0 5, Sample 32, 운영 4의 exact 49-case report archive와
  runtime image/build/asset provenance를 모두 검증해야 한다. builder만 mode `0444`
  `runtime-activation.json`을 만들며 Compose는 이를 고정 경로에 read-only mount한다. Worker는
  exact disabled 또는 fully-qualified 두 상태만 받고 실제 asset manifest와 dependency binding을
  다시 대조한다. 현재 실제 증적이 없으므로 disabled 상태를 유지한다. 자세한 계약은
  `docs/RUNTIME_QUALIFICATION.md`를 따른다.
- **멱등 업로드**: artifact에는 유형, 크기, SHA-256, 저장 URI를 기록하고 재전송 시 중복 레코드를 만들지 않는다.
- **업로드 storage namespace 결박**: Dataset, Artifact와 TestSet upload session은 backend
  이름만이 아니라 credential을 제외한 exact local root 또는 S3 endpoint/bucket/region/
  addressing-style fingerprint SHA-256도 저장한다. credential 회전은 같은 namespace로
  취급하지만 root/bucket/endpoint 변경과 `UNBOUND` 과거 행은 init replay, PUT, expire,
  finalize, delete, maintenance, Worker claim/GET와 Artifact download에서 fail-closed한다.
  migration에서 과거 행을 현재 설정으로 자동 backfill하지 않는다. 운영자 adoption은
  pending/finalizing을 거부하고 staging 또는 모든 canonical object의 전체 size/SHA-256과
  DB URI를 다시 검증한 뒤에만 명시적으로 결박하며 dry-run도 audit event를 기록한다.
- **검증 후 완료**: Worker local URI나 선언만으로 Artifact를 신뢰하지 않는다. 임시
  object 전체의 크기/SHA-256을 Manager가 재검증하고 canonical 게시한 뒤에만 Job을
  완료한다. production presign endpoint는 remote Worker가 접근 가능한 HTTPS여야 한다.
- **전송 자원 상한**: 단일 object, attempt 총 byte/session 수와 checkpoint retention을
  Worker/Manager 양쪽에서 제한한다. 초과분을 조용히 누락하지 않는다.
- **로그 최소화**: HTTP query와 header를 access log에 복사하지 않는다. 검증된 request
  ID, method, query 없는 path, status와 latency만 구조화하고 formatter redaction을 유지한다.
- **분산 요청 제한**: 운영 rate limit은 process memory가 아니라 Redis 원자 연산을
  사용하고 key에는 credential/IP 원문을 넣지 않는다. fail-closed 기본과 health 예외를
  임의로 완화하지 않는다.
- **중앙 maintenance queue 신뢰 경계**: RQ callable과 인자는 client가 정하지 않는다.
  queue에는 allowlist task와 PostgreSQL run UUID만 넣는다. 실행 Worker도 Redis를 신뢰하지
  않고 dequeue 직후와 perform 직전에 JSON serializer, 고정 queue/origin/callable, canonical
  UUID 단일 인자, 빈 kwargs/meta/dependency/callback/repeat를 다시 검증한다. 기본 RQ Worker로
  되돌리거나 callback/dependent/repeat 실행을 허용하지 않는다. maintenance process에는
  JWT, Worker bootstrap/pepper, MLflow token을 mount하지 않고 `PROCESS_ROLE=maintenance`를
  고정한다. API PostgreSQL/Redis/S3 credential도 재사용하지 않는다. PostgreSQL은 maintenance
  login의 exact column ACL과 NOLOGIN owner의 upload-id 기반 `SECURITY DEFINER` parent-lock 함수만,
  MinIO는 `datasets/staging/*|test-sets/staging/*` `DeleteObject`만, Redis는 exact RQ lifecycle
  key/command ACL만 허용한다. 대응 API credential과 같은 maintenance source secret, broad policy,
  bucket versioning, PUBLIC/다른 role의 function EXECUTE는 fail-closed한다. Long parent/session lock,
  S3 delete와 confirmation wait 중 exact run/attempt CAS heartbeat를 유지하고 ownership 유실 뒤
  결과를 commit하지 않는다. 실제 삭제 대상은 session row를 다시 잠가 grace/status/claim/exact staging key를
  확인한다. `pending` 활성 upload, `finalizing`, `completed`와 canonical object를 maintenance
  task에서 삭제하지 않는다. Redis는 외부에 공개하지 않고 RQ Worker heartbeat fail-closed
  readiness를 유지한다. deterministic job ID가 이미 있어도 worker execution policy와 같은
  queue/origin/JSON serializer/callable/UUID args/empty kwargs·meta·dependency·callback·repeat/
  description/timeout/TTL/retry envelope와 실제 queued/scheduled/started 위치가 모두 맞을 때만
  existing으로 인정한다. poisoned inactive job은 callback/dependent를 resolve하지 않는 원자
  quarantine 뒤 exact job으로 교체하고, started poison은 DB run을 typed terminal로 닫는다.
  API replica reconciler는 PostgreSQL advisory transaction lock과 row `SKIP LOCKED`를 사용해
  기존 `queued|retrying|enqueue_failed`와 stale `running`만 bounded 재전달한다. 새 run을 만들거나
  `completed|failed`를 재실행하지 않으며 final-attempt exact started job을 중복 생성하지 않는다.
- **브라우저 Dataset 전송**: browser는 HttpOnly JWT를 읽거나 presigned upload target에
  전달하지 않는다. BFF가 Manager의 method/origin/header를 allowlist로 검증한 descriptor만
  일회성 메모리에서 사용하고 URL query/token을 화면·로그·Dataset 응답에 보존하지 않는다.
  같은 origin target도 반드시 `credentials: omit` 등으로 session cookie를 제외한다.
- **브라우저 Experiment/Job 생성**: browser는 Manager path/header를 선택하지 않고
  same-origin BFF의 고정 endpoint만 호출한다. BFF exact-key/byte 상한과 Manager 검증을
  모두 유지하며, 다중 Job은 제출 직전 기존 이름 전체를 조회한 뒤 단건 결과를 보존한다.
  확정한 success/conflict/error를 후속 전송 오류로 덮어쓰지 않고, commit 확인 뒤 form을
  다시 활성화하지 않는다. 응답 유실로 commit 여부가 불명확하면 목록 확인 전 재제출을
  허용하지 않는다. 목록은 bounded pagination을 끝까지 검증하며 상한 초과 시 partial을
  전체 또는 빈 결과처럼 표시하지 않는다.
  CREPE safe-loader 구현을 포함한 전체 GPU/no-network release matrix가 검증되기 전에는
  auto sample을 항상 disabled/null로 보내고 완료 기능처럼 표시하지 않는다.
  Job A/B는 동일 TestSet item ID끼리만 정렬하고 current-attempt ledger 중복이나 stale fetch를
  조용히 덮어쓰지 않는다. Experiment PATCH/DELETE UI는 exact same-origin BFF만 사용하고
  row version과 immutable name/Dataset, Job/MLflow delete conflict를 그대로 노출한다. dirty
  PATCH 성공은 정확한 version 증가와 public projection을 확인하고, stale/응답 유실 뒤 재제출은
  최신 page를 확인할 때까지 잠근다. DELETE는 exact Experiment name 확인과 빈 body를 요구한다.
- **Job engine 표시의 원장**: Job 응답의 실행 엔진은 구성의 희망 backend가 아니라 exact current
  `JobAttempt.engine_mode`에서만 계산한다. Attempt가 없으면 `null`/`실행 전`, 실제 실행이면
  `rvc_webui`, Fake 실행이면 `fake`를 목록·상세·Overview에서 일관되게 표시한다. 구성값으로
  fallback하거나 Worker 광고 capability를 Job 실행 결과로 추측하지 않는다. Fake에는
  `FAKE · 운영 결과 아님` badge와 접근 가능한 경고를 유지하고 운영 학습 증거로 표현하지 않는다.
- **MLflow는 파생 projection**: PostgreSQL 원장 변경과 MLflow outbox를 같은 transaction에
  기록한 뒤 외부 REST 호출을 수행한다. MLflow 장애 때문에 commit된 Job/Metric/Artifact를
  되돌리거나 잃지 않으며, storage URI·presigned query·path·token을 projection payload나
  오류 로그에 넣지 않는다. fail-open/fail-closed와 readiness 의미를 명시적으로 보존한다.
- **installer image closure v2**: self-contained Manager bundle은
  `api|web|mlflow|postgres|redis|minio|minio-client|nginx` 정확히 8개, Worker bundle은
  `runtime` 정확히 1개의 linux/amd64 image만 허용한다. Docker-save archive와 strict
  `images-manifest.json`의 archive hash/size, source/runtime reference, image/config digest,
  OS/architecture와 application release label을 load 전후에 검증한다. Manager dependency
  image의 `Config.User` key가 없으면 빈 값으로 정규화하되 application user 검증은 완화하지 않는다.
  Containerd image store의 OCI index `.Id`와 Docker-save config byte digest는 정상적으로 다를 수
  있으므로 둘을 각각 기록·검증하고 단순 equality를 강제하지 않는다.
  Single-platform installer image는 Buildx default provenance attestation을 image 안에 섞지 않고
  `--provenance=false`로 export하며, Docker-save에는 exact runtime identity descriptor/config/layer
  closure만 허용한다. 외부 SBOM·scan·source provenance gate를 이 설정으로 대체하지 않는다.
  Self-contained Worker bundle의 installer/infra/verifier/document/SBOM 입력은 clean 40-hex commit의
  exact Git archive에서만 가져온다. Runtime build manifest는 qualification 유무와 관계없이 exact
  schema, release version, orchestrator commit과 image identity를 검증하고 disabled activation도
  archive 안에서 mode `0444`를 유지한다.
  Worker release factory는 반드시 두 단계다. Core factory만 새 runtime image를 만들고 disabled
  activation 후보를 별도 output에 게시한다. 49-case는 그 exact image ID로 실행하며, qualified
  factory는 같은 existing image ID와 core build manifest, asset, qualification/evidence를 요구하고
  image를 build/pull/retag/remove하지 않는다. Basename이 같은 core/qualified archive는 서로 다른
  output directory에 보존하고 어느 쪽도 덮어쓰지 않는다. 두 factory 모두 final output에 직접 bundle을
  쓰지 않는다. Publisher는 verifier/archive/checksum을 private stable snapshot으로 먼저 고정하고,
  외부 checksum, safe tar root/type, 내부 exact ledger/image closure, runtime image ID와 activation mode를
  다시 검증한 뒤 sidecar 먼저, archive 마지막 순서의 fsync/no-clobber hard-link 게시를 사용한다.
  Runtime builder는 Docker build 직후 post-build label/manifest 검증보다 먼저 private ownership record에
  exact image ID를 원자 게시한다. Core 실패 cleanup은 이 실행의 record가 있고 tag가 그 image ID를
  계속 가리킬 때만 제거하고,
  교체되거나 기존에 있던 tag와 qualified factory의 existing image를 삭제하지 않는다. Qualification이
  있어도 scan/license/reviewer/clean-host gate까지 자동 승인하거나 결과를 production release라고 부르지
  않는다.
  Cross-architecture Buildx release는 dependency source tag도 target platform의 zero-layer image로
  materialize한 뒤 실제 architecture를 검사한다. `docker pull --platform` 출력만으로 target tag의
  local platform을 증명하지 않는다. Manager dependency image는 rollback tag overwrite를 막는
  version-scoped `rvc-orchestrator-<role>:<version>`
  alias로만 실행하고 self-contained Compose는 `RVC_IMAGE_PULL_POLICY=never`를 사용한다.
  install/upgrade/start/restart/rollback은 installed manifest·env·현재 loaded identity를 다시
  검증하기 전 release를 활성화하지 않는다. partial bundle은 `SELF_CONTAINED=false`와 빈 v2
  image inventory를 명시하며 air-gapped release로 표현하지 않는다.
- **installer release file/environment closure**: 배포 archive의 `SHA256SUMS`는 단순 checksum
  목록이 아니라 누락·추가·symlink·비정규 파일과 안전하지 않은 경로를 모두 거부하는 exact
  inventory여야 한다. 설치 시 release 전체에서 mode `0444` `RELEASE_SHA256SUMS`를 새로 만들고,
  Manager/Worker Compose wrapper와 Manager rollback은 start/restart/run/create/전환 전에 이를
  다시 검증한다. Installed `manifest.env`, image manifest, runtime activation과 release-owned env의
  version/image/pull-policy/provenance가 서로 다르면 활성화하지 않는다. 사용자 소유 설정을 보존하는
  것과 release-owned key를 과거 값으로 남기는 것을 혼동하지 않는다. Bundle-local `README.md`와
  `TESTING.md`도 선택한 version과 exact ledger 명령을 포함해야 한다.

## 코드와 테스트 지침

- Python은 명시적 타입, 작은 서비스 경계, UTC timezone-aware datetime을 사용한다.
- API 입력은 스키마로 검증하고 DB 모델을 그대로 응답하지 않는다.
- 중앙 서버 테스트는 실제 GPU, RVC 저장소, 외부 S3에 의존하지 않는다. 저장소·큐·RVC runner를 대역으로 주입한다.
- Worker의 RVC 명령 생성과 산출물 탐색은 순수 함수 중심으로 작성하고 v1/v2 모두 테스트한다.
- 실제 학습 smoke test는 별도 표식과 명시적 환경 변수 없이는 실행되지 않아야 한다.
- 프론트엔드는 서버 응답 타입을 중앙 정의하고 loading, empty, error, unauthorized 상태를 모두 처리한다.
- 비밀값, 모델, 데이터셋, 대형 checkpoint, 빌드 산출물은 Git에 커밋하지 않는다.
- 의존성 버전은 lockfile 또는 고정 범위로 재현 가능하게 유지하고, 라이선스를 확인한다.
- `git diff --check`는 관련 파일이 Git에 tracked된 경우에만 whitespace 증거로 인정한다.
  `git ls-files`가 비어 있거나 변경이 전부 untracked이면 exit code 0이어도 `BLOCKED`로 기록하고
  다른 lint/test 결과를 Git provenance나 whitespace 검증으로 확대 해석하지 않는다.
- runtime dependency, base image 또는 RVC asset을 바꾸면 `docs/SUPPLY_CHAIN.md`와 license
  catalog/SBOM 입력을 함께 갱신한다. `partial-release-gates-open`을 scan·digest·법적 검토
  없이 완전한 SBOM이나 release attestation으로 바꾸지 않는다.

## 기본 검증 순서

구체 명령은 구현이 추가될 때 이 섹션과 각 패키지 README에 갱신한다.

1. Python format/lint/type 검사
2. Manager 단위·통합 테스트
3. Worker 단위 테스트와 command snapshot 테스트
4. Frontend lint/type/build 검사
5. Compose 구성 렌더링과 container health check
6. 설치 패키지 clean-VM 설치/제거/업그레이드 smoke test

현재 실행 가능한 Frontend 검증:

```bash
cd apps/web
npm test
npm run lint
npm run build
```

전체 기본 검증과 localhost Fake Worker HTTP protocol E2E:

```bash
make check
make test-e2e
```

`make test-e2e`는 localhost socket을 열기 때문에 제한된 실행 환경에서는 별도 권한이 필요할 수
있다. 사용자 실행 순서와 증적·합격 기준은 `docs/TEST_GUIDE.md`, 자동화의 상세 범위는
`docs/TESTING.md`를 본다. Fake fixture 결과를 production/native GPU 검증으로 확대 해석하지 않는다.

### 현재 dev.20 기준선

- Manager schema head는 `f5d1c8a9b240`이다. Manager archive SHA-256은
  `c6488dad47c7f38c082ed6fa68f1fe3691c069110aef0bbf68a9d7ba5e6f5b70`, 크기는
  `667617422` byte다. Worker archive SHA-256은
  `7f36cbf27100bf70425c2780142d4fa3f6e6e76d0acf410d3e3fb698aa50558b`, 크기는
  `108488` byte다. 두 manifest 모두 tracked source commit
  `298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a`에 결박돼 있다.
- Manager는 `SELF_CONTAINED=true`이고 정확히 8개의 linux/amd64 image를 포함한다. 외부 checksum,
  내부 exact ledger, image/archive closure, load 뒤 identity와 release image를 사용한 전체 Compose
  smoke가 모두 PASS했다. 이 runtime 증거는 arm64 Colima host의 amd64 emulation에서 얻었으므로
  clean Ubuntu 22.04/24.04 x86_64 native 설치 증거로 확대 해석하지 않는다.
- Worker는 같은 dev.20의 별도 archive지만 `SELF_CONTAINED=false`, 빈 image inventory,
  `RVC_RUNTIME_INCLUDED=false`이고 GPU/profile/native Sample gate가 모두 false인 partial이다.
  Manager 후보가 self-contained라는 사실로 Worker runtime 또는 최종 두 설치 파일 gate를 열지 않는다.
- dev.20 packaging/image 집중 회귀와 committed source의 clean-tree/source-closure 및
  `git diff --check`가 통과했다. 저장소 전체의 dev.20 `make check` 기준선은 Python non-E2E
  `752 passed, 4 deselected`, strict mypy `88 source files`, Web `24 files/211 tests`, lint/build이며
  localhost HTTP E2E는 `4 passed in 6.68s`였다.
- 현재 installer는 archive의 `SHA256SUMS` 누락을 실제 Git source root가 아닌 곳에서 거부하고,
  strict SemVer forward upgrade만 허용한다. Target Compose를 pending env로 검증하기 전에는
  `current`/환경을 바꾸지 않으며 uninstall stop/down 오류를 성공으로 보고하지 않는다. dev.15
  bundle-local 문서는 verifier에 `current` symlink를 직접 넘기는 오류가 있으므로 새 설치·시험에는
  사용하지 않는다. dev.17과 dev.18은 각각 custom CA/Experiment 비교와 model registry의 immutable
  역사 증거이며, dev.19은 maintenance 최소권한 source를 포함하지만 image 없는 과거 partial이다.
  dev.14 이하의 과거 root-level installer/upgrade script는 guard 자체가 없다.
- Self-contained image record는 Docker-save config byte의 실제 SHA-256과 `Config.User`를 함께
  검증한다. Manager API `10001:10001`, Web `nextjs`, MLflow `10002:10002`, Worker runtime
  `10001:10001`을 바꾸려면 Dockerfile, image closure, 실제 non-root smoke와 문서를 같이 검증한다.
- dev.20 Manager release-image 전체 Compose smoke는 proxy, loopback MinIO/MLflow,
  API/RQ/MLflow runtime secret 권한과 exact bucket policy를 실제 8-image stack에서 확인했다.
  다만 arm64 host emulation이므로 clean linux/amd64 lifecycle, reboot/rollback과 장기 부하는 별도다.
- Clean Ubuntu amd64, 실제 외부 TLS/browser, NVIDIA GPU/native Worker runtime과 Worker
  self-contained image closure는 계속 출시 gate로 남긴다. Model registry도 실제 S3 대용량 canonical 재해시와
  tamper/outage, PostgreSQL 다중 replica promotion 경쟁, browser/API response-loss E2E 전에는
  production 승인 원장 인수를 완료로 판정하지 않는다.

## 설치 패키지 기준

- 1차 지원 대상은 Ubuntu 22.04/24.04 x86_64다.
- Manager 설치물은 Docker Engine과 Compose plugin을 전제로 하며 서비스 구성, `.env` 생성, health check, upgrade/backup 절차를 제공한다.
- Worker 설치물은 NVIDIA driver, NVIDIA Container Toolkit, 호환 CUDA/GPU 검사를 수행하고 RVC 저장소/weight의 출처와 버전을 기록한다.
- 설치 스크립트는 재실행 가능해야 하며, 기존 데이터 삭제나 덮어쓰기는 명시적 확인 없이는 수행하지 않는다.
- 최종 배포 파일은 Manager와 Worker를 분리하고 SHA-256 checksum과 버전 manifest를 함께 생성한다.
- 설치 명령과 현재 partial/self-contained 제한은 `docs/INSTALLATION_GUIDE.md`를 단일 사용자
  진입점으로 유지한다.

## 금지 사항

- 테스트 통과를 위해 실제 인증·권한·경로 검사를 비활성화하지 않는다.
- 사용자 변경을 무단으로 되돌리거나 destructive Git 명령을 사용하지 않는다.
- RVC upstream 코드를 무분별하게 복사해 fork하지 않는다. commit hash를 고정한 adapter 방식으로 통합한다.
- 로그에 토큰, Authorization header, presigned URL query, 사용자 음성 내용 또는 전체 환경 변수를 출력하지 않는다.

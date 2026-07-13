# 보안과 데이터 보존 기준선

## 보호 대상

- 사용자 계정과 Worker credential
- 업로드된 음성 Dataset과 고정 테스트 음원
- 학습 config, 모델, index, checkpoint와 sample
- Worker GPU 자원과 RVC 실행 환경
- Job 상태 원장, audit event, 로그와 metric
- Model registry candidate/approval/revoke 이력과 active champion
- object storage와 database backup

음성, sample과 학습 모델은 모두 민감한 사용자 데이터로 취급한다. 공개 모델이라는 표시가 없는 한 공개 URL이나 익명 접근을 허용하지 않는다.

## 신뢰 경계와 위협 행위자

1. Browser에서 들어오는 사용자 입력과 파일은 신뢰하지 않는다.
2. Manager API와 PostgreSQL/Redis/MinIO/MLflow 사이에는 service credential과 내부 network 경계가 있다.
3. 원격 Worker는 인증된 경우에도 제한된 주체다. 손상되거나 오래된 Worker가 다른 Job 데이터를 읽거나 쓸 수 있다고 가정한다.
4. RVC upstream source, Python package, container image와 model asset은 공급망 입력이다.
5. presigned URL을 본 제3자와 TLS를 공격하는 network 행위자를 고려한다.

주요 공격은 credential 탈취, IDOR, archive/path traversal, command injection, 악성 audio decoder 입력, zip bomb, 과도한 log/metric으로 인한 자원 고갈, stale lease write, artifact 바꿔치기, dependency/image 변조다.

## 필수 통제

### 인증과 권한

- v1.0 보안 기준에서는 사용자 access token을 짧게 유지하고 refresh rotation/revocation을 지원해야 한다.
- Worker token은 최초 한 번만 표시하고 DB에는 keyed hash 또는 강한 digest만 저장한다.
- Worker token은 Worker 자체와 현재 소유한 lease 범위에만 권한이 있다.
- Worker token 회전은 idle/no-active-lease에서만 prepare하고 pending hash만 DB에 저장한다.
  pending secret은 표준 Worker API에 사용할 수 없고 old+pending 동시 증명 후 activate할 때 old
  token을 즉시 폐기한다. pending 중 claim을 막고 응답 유실은 0600 로컬 credential의 old/pending
  쌍으로 복구한다. 관리자 emergency revoke는 RBAC·exact Worker name 확인·audit를 요구하며,
  active assignment를 force할 때 Job/attempt/lease를 명시적으로 cancelled/released한 뒤에만
  Worker를 비활성화한다. 재등록은 inactive/unassigned Worker ID와 bootstrap을 함께 증명한다.
- 모든 Dataset/Experiment/Job/Artifact 접근에서 소유권 또는 역할을 서버가 검사한다.
- Model registry read/mutation도 Experiment owner 또는 admin만 허용하고 타 소유자, 다른
  Experiment의 Job/attempt/Artifact와 entry ID를 모두 같은 `404`로 숨긴다. Mutation은
  Experiment write fence 뒤 active actor와 token version을 다시 확인하고 registry/entry
  row-version CAS를 적용한다. Browser BFF는 server-render 시점 actor와 현재 cookie session을
  private no-store `/bff/session/identity`의 exact actor ID로 대조하고 canonical
  `X-RVC-Expected-Actor-ID`를 Manager에 전달한다. Manager는 인증 actor가 달라졌으면 operation을
  만들기 전에 `409`로 닫는다. Identity endpoint는 email·role·token을 투영하거나 browser가 넣은
  Authorization을 받지 않는다.
- 관리자 사용자 생성·역할/활성 변경·비밀번호 재설정은 admin 전용 고정 API와 16 KiB body
  상한을 사용한다. Mutation은 actor별 hash된 idempotency key와 keyed request fingerprint,
  target row version으로 보호하고 응답/audit/멱등 원장에 비밀번호·hash를 저장하지 않는다.
  Singleton DB fence가 cross-demotion을 직렬화하며 자기 강등/비활성화와 마지막 활성 관리자
  제거를 막는다. 권한·활성·비밀번호 변경은 user의 access-token version을 증가시켜 기존 JWT를
  즉시 영구 무효화하고, 재활성화가 과거 token을 되살리지 못하게 한다.
- 관리자 생성·재설정 비밀번호는 최소 16자, 제어문자 없음, 최소 8개 서로 다른 문자와 이메일
  local-part 비포함 정책을 적용하고 Argon2id로 hash한다. Browser BFF는 같은-origin 고정 경로와
  public response projection만 허용하며 응답 유실 때 blind retry를 하지 않는다.
- 관리자 bootstrap, token 발급/회전/폐기, delete와 download를 audit한다.
- login, Worker 등록, upload/finalize와 일반 API는 원문 credential/IP를 저장하지 않는
  Redis HMAC key로 분산 rate limit하고, 운영 Redis 장애에는 fail-closed한다.
- TLS 종단과 application 사이 forwarding header는 명시한 trusted proxy hop에서만 정규화한다.
  Browser가 실제 HTTPS를 사용해도 내부 Nginx가 `X-Forwarded-Proto`를 HTTP로 덮으면 Secure session
  cookie와 HSTS가 빠질 수 있고, 반대로 임의 client header를 신뢰하면 scheme spoofing이 된다.
  Dev.11은 client scheme을 폐기하고 operator-owned `PUBLIC_SCHEME=https`를 Nginx/API/Web에 고정해
  production start, Secure cookie와 단일 edge HSTS를 같은 값에 결박한다. 실제 외부 TLS/browser
  검증 전에는 production 배포를 승인하지 않는다.
- PostgreSQL·Redis·API backend는 `internal: true` network에만 두고 host port로 공개하지 않는다.
  외부 TLS/object proxy와 운영자 진단에 loopback port가 필요한 MinIO·MLflow만 별도
  `host-access` bridge를 함께 사용한다. 해당 published address는 `127.0.0.1` 기본값을 유지하고
  공인 interface bind, Redis/PostgreSQL의 host-access 연결이나 direct internet exposure를 허용하지
  않는다.
- RQ Redis는 internal network/service credential 뒤에 두고 host port로 공개하지 않는다.
  Redis write credential 자체가 탈취될 수 있다고 보고 job은 JSON serializer, 고정 callable,
  DB run UUID 한 개만 사용한다. 실행 Worker는 dequeue 직후와 perform 직전에 queue/origin,
  callable, canonical UUID, kwargs/meta/dependency/callback/repeat와 timeout/TTL/retry를
  allowlist로 검증하고 위반 job을 callable/callback import 없이 generic failure로 닫는다.
  custom success/failure handler도 Redis가 뒤늦게 삽입한 dependent/repeat를 실행하지 않는다.
  enqueue/reconcile 경계도 같은 validator로 exact queue/origin/JSON serializer/callable/
  canonical run UUID/empty kwargs·meta·dependency·callback·repeat/group/allow-dependency/
  enqueue-front/description/timeout/TTL/retry를 확인한다. queued/scheduled/started 위치가 없는
  ghost나 inactive poison은 원자 quarantine에서 list/registry/hash만 제거해 callback/dependent를
  실행하지 않고 exact job을 다시 만든다. started poison은 실행 중 삭제·중복 생성하지 않고
  PostgreSQL run을 `failed`와 typed code로 닫아 운영 조사를 요구한다.
  API schema에는 callable/module/args/kwargs/object key가 없다. 별도 RQ Worker는 non-root,
  read-only/capability-drop이며 Docker socket/GPU 권한이 없다. 내부 scheduler는 bounded
  delayed retry를 queue로 되돌릴 뿐 새 주기 task를 생성하지 않고, due job도 execution
  allowlist를 다시 통과한다. scheduler는 queue별 Redis `NX` lease로 하나만 활성화되고 그
  lock heartbeat는 readiness가 확인하는 실제 Worker registry heartbeat와 분리된다. 전용 maintenance
  role/entrypoint에는 PostgreSQL·Redis·S3 이외 JWT, Worker bootstrap/pepper, MLflow token을
  전달하지 않고 그 role로 HTTP API를 시작할 수 없다.
- MLflow는 upstream image의 빈/root 기본 user를 상속하지 않는다. Image와 Compose에서 숫자
  UID/GID `10002:10002`를 이중 고정하고 read-only rootfs, `cap_drop: ALL`, no-new-privileges,
  PID 128과 mode `0700` `/tmp` tmpfs만 제공한다. Release 증거는 image inspect뿐 아니라 이 권한으로
  실제 server health가 뜨고 소유 home 쓰기가 실패하는 network-none smoke를 포함한다.
- Installer가 보관하는 root 소유 mode `0600` source secret은 non-root API/RQ/MLflow에 직접
  mount하지 않는다. Root, network-none initializer만 이를 읽고 API, maintenance, MLflow와
  database-authz별
  독립 named volume에 exact allowlist를 mode `0400`과 고정 UID/GID로 투영한다. 전체 source를
  `O_NOFOLLOW`/regular/non-empty/size/NUL 검증하고 새 generation을 file·directory fsync한 뒤
  `current` symlink를 원자 교체한다. Partial generation이나 네 profile 중 하나의 실패는 이전
  generation을 훼손하지 않아야 한다. API secret을 maintenance/MLflow volume에 합치거나 RQ에
  JWT/bootstrap/pepper를 노출하지 않는다. Maintenance PostgreSQL/Redis/S3 credential이 대응하는
  API credential과 같으면 새 generation을 게시하지 않는다.
- MinIO의 Manager app과 MLflow identity는 built-in `readwrite`를 사용하지 않는다. 각각 exact
  Manager bucket 또는 MLflow artifact bucket의 필요한 list/location/multipart/object
  get/put/delete action만 가진 sole policy를 연결한다. Init은 재실행 때 broad policy를 떼고
  expected attachment를 확인하며 상대 bucket 접근 실패를 실제 server smoke로 검증한다.
- Maintenance identity에는 Manager bucket의 `datasets/staging/*`와 `test-sets/staging/*`
  `DeleteObject`만 허용한다. PostgreSQL은 column ACL과 upload-id 기반 parent-lock 함수,
  Redis는 exact RQ lifecycle key/command ACL을 사용한다. RQ가 API application DB/S3 credential,
  Redis 관리 명령, generic callback/dependent/repeat cleanup 경로를 얻으면 배포를 중단한다.

### 파일과 subprocess

- 업로드를 streaming 처리하고 전체/파일별 크기, 파일 수와 압축 비율을 제한한다. 강제
  wall-clock timeout은 안전하게 종료 가능한 RQ/subprocess 작업 경계 안에서만 적용한다.
- archive entry의 절대 경로, `..`, drive prefix, symlink/hardlink와 device file을 거부한다.
- 확장자와 MIME만 믿지 않고 격리된 decoder로 실제 audio를 확인한다.
- object key와 job workspace path는 서버가 생성한 UUID로 구성한다.
- 사용자 입력을 shell, repository URL, Python module path나 임의 environment key로 전달하지 않는다.
- subprocess는 고정 interpreter와 allowlisted entrypoint, 검증된 argv, 최소 environment, `shell=False`를 사용한다.
- Worker container에 Docker socket을 mount하거나 불필요한 `privileged` 권한을 주지 않는다.

### model 직렬화와 sample inference 신뢰 경계

- PyTorch model/checkpoint는 단순 데이터가 아니라 code 실행 가능성이 있는 공급망 입력으로
  취급한다. [PyTorch security policy](https://github.com/pytorch/pytorch/security/policy)와
  [`torch.load` 경고](https://docs.pytorch.org/docs/stable/generated/torch.load.html)에 따라
  신뢰하지 않는 `.pth`/`.pt`를 Worker process에서 역직렬화하지 않는다.
- reviewed upstream `pyproject.toml`의 Torch 2.4 marker는 source archive 변경 감지에만
  사용하고 runtime으로 출시하지 않는다. 2.4.0은 `weights_only=True`에서도 code execution이
  가능했던 [CVE-2025-32434](https://github.com/advisories/GHSA-53q9-r3pm-6pq6)의 영향
  범위다. 별도 후보 lock은 Torch `2.6.0+cu124`, Torchvision `0.21.0+cu124`, Torchaudio
  `2.6.0+cu124`, CUDA runtime 12.4지만 실제 amd64 base digest와 GPU/no-network,
  vulnerability/container/license 검토가 끝나지 않아 release runtime이 아니다.
  `weights_only=True`, checksum 또는 `ProcessSpec` process-group 종료만으로 악성 model을
  sandbox했다고 간주하지 않는다. 현재 subprocess는 같은 container filesystem과 network를
  사용할 수 있으므로 untrusted serialization 격리 경계가 아니다.
- 학습·sample inference는 pinned source가 같은 attempt에서 생성한 checkpoint/small model/index와
  그 stage가 기록한 exact size/SHA-256만 읽는다. 외부 업로드 model/index, 다른 attempt의
  산출물, 경로만 일치하고 provenance가 없는 파일은 거부한다.
- HuBERT, RMVPE, pretrained 및 torchcrepe model byte는 operator가 source, license, size,
  SHA-256을 고정한 asset/wheel manifest에서만 공급한다. CREPE는 고정
  `runtime/crepe/full.pth`가 strict asset manifest와 private projection exact inventory에
  있어야 한다. checksum은 provenance 확인 수단이지 출처가 불명인 model을 신뢰 가능하게
  바꾸는 수단이 아니다.
- sample inference loader policy는 호출별로 분리한다. 같은-attempt small model과 CREPE
  `full.pth`는 명시적 `weights_only=True`이고, CREPE state dict는
  `torchcrepe.Crepe("full")`에 strict load해 process-global infer model/capacity를 pre-bind한다.
  HuBERT/RMVPE는 manifest-verified operator byte에 한해 명시적 `weights_only=False`다.
  전역 Torch loader override로 이 정책을 바꾸지 않는다. CREPE path와 동일 FD byte를
  전·중·후 검증하고 attempt-private `TORCH_HOME`, Hugging Face/Transformers offline mode로
  package cache 또는 network fallback을 막는다.
- sample inference는 lease-bound TestSet transfer와 Sample completion 검증에 더해
  (a) 현재 Torch `2.6.0+cu124` 후보의 전체 호환/GPU/no-network matrix와 명시적 loader
  policy를 통과하거나,
  (b) same-attempt small model을 tensor-only
  [safetensors](https://pytorch.org/projects/safetensors/)와 strict JSON metadata로 변환하고
  남은 pickle asset을 operator-trusted manifest byte로 한정한 GPU/no-network matrix를
  통과한 뒤에만 활성화한다.
- 해당 gate와 v1/v2 40k/48k, F0/non-F0, `pm|harvest|crepe|rmvpe`, index on/off matrix가
  끝날 때까지 빈 inference F0 capability, `fixed_test_set_inference_ready=false`,
  `AUTO_SAMPLE_JOBS_ENABLED=false`, `RVC_GPU_SMOKE_VERIFIED=false`,
  `PROFILE_STAGE_SET_VERIFIED=false`, `RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false`를 유지한다.
- GPU 결과를 운영 기능으로 투영할 때는 사용자 제공 boolean을 받지 않는다. Strict qualification은
  core 8, training F0 5, Sample 32, 운영 4의 exact report archive, runtime image/build/asset
  provenance를 검증한다. Builder가 만든 mode `0444` activation만 고정 Compose mount로 전달하고,
  Worker는 disabled 또는 fully-qualified 상태와 현재 asset byte를 다시 검사한다. installed start는
  qualification와 evidence archive hash/size 및 loaded image ID를 다시 검증한다.

### 무결성과 재현성

- Dataset manifest와 모든 Artifact에 SHA-256, byte size, MIME과 logical type을 기록한다.
- Dataset client는 object key/URI를 정하지 않는다. Manager가 staging byte 전체의
  size/SHA-256과 content signature를 재검증하고 원본·flat·manifest·quality report를
  모두 canonical 게시한 뒤에만 사용할 수 있게 한다.
- Dashboard의 Dataset BFF는 init 응답의 method/origin/header를 allowlist로 재검증하고
  public Dataset field만 browser에 투영한다. presigned query/token은 일회성 upload에만
  사용하고 UI·로그·목록/상세에 보존하지 않는다. 외부 target은 credential 없는 요청만,
  같은 origin target은 `credentials: omit`으로 HttpOnly JWT cookie를 제외한 요청만 허용한다.
- Dataset의 raw `quality_report_json`/canonical report는 archive member 경로와 상세 검사 사유를
  포함할 수 있으므로 공개 모델에서 제외한다. API와 BFF는 bounded typed issue count 및
  all-null/all-present `pcm_quality`만 반환하고, unknown nested field·비정상 algorithm·bool count·
  non-finite/out-of-range metric은 fail-close한다. historical null을 0으로 표시하지 않는다.
- Worker upload는 attempt별 temporary key에 저장한 뒤 중앙이 size/checksum을 검증하고 canonical reference로 승격한다.
- Dataset/Artifact/TestSet upload session은 backend와 credential 없는 exact storage namespace
  fingerprint를 함께 고정한다. S3 credential만 회전한 경우 fingerprint는 유지되지만 local
  root, S3 endpoint/bucket/region/addressing style이 달라지거나 과거 행이 `UNBOUND`이면
  replay/PUT/finalize/delete/maintenance/claim/download가 객체를 건드리지 않고 실패한다.
  backend 이름과 object key가 같다는 이유로 다른 namespace의 byte를 채택하지 않는다.
- 운영 presigned PUT/GET endpoint는 HTTPS만 허용한다. Worker는 HTTPS Manager가 HTTP
  artifact upload URL을 반환하면 거부한다. Dataset GET은 lease-bound Manager endpoint의
  단일 307만 수용하고 외부 요청에서 Worker Authorization/lease header를 제거하며,
  downgrade, userinfo, fragment와 추가 redirect를 거부한다.
- Worker와 Manager 양쪽에서 artifact object 5 GiB, attempt 256 session/100 GiB 기본
  상한을 적용하고 G/D checkpoint는 Worker가 각각 최신 20개만 게시한다.
- Dataset은 단일 원본 5 GiB, owner별 동시 8 session/20 GiB, ZIP 10,000 entry와
  파일별 2 GiB/전체 20 GiB 비압축 상한을 기본으로 적용한다.
- Worker claim에는 canonical Dataset의 내부 URI를 넣지 않고 exact size/SHA-256과
  Manager 상대 path만 제공한다. 수신 파일은 `O_NOFOLLOW` mode `0600` partial에 쓰고
  fsync/원자 게시하며 canonical ZIP도 traversal, symlink, duplicate, CRC와 bomb를 다시
  검사한 뒤 workspace flat directory로 게시한다.
- JobConfig는 기본값 포함 정규화 JSON의 canonical SHA-256을 Job, exact attempt와 Worker claim에 같은
  값으로 기록하며 signed zero를 JSONB-stable `0.0`으로 정규화한다. Manager는 raw/정규화 hash와 Job
  컬럼을 claim/retry/model registry, active lease mutation, terminal/Artifact/Sample 최종 fence,
  comparison/MLflow projection 및 hash가 있는 신규 row 조회에서 fail-closed 대조하고 Worker는 wire
  parse와 workspace 생성 전에 재검증한다. Artifact local writer token/heartbeat와 seal CAS는 동시 PUT을
  하나로 제한하고 S3는 exact `If-None-Match: *`만 허용한다. Worker는 transport 오류, local `409`, S3
  `412` 뒤 같은 session finalize를 사용해 Manager 검증으로 수렴한다. Canonical publish는 별도 UUID
  finalization token과 heartbeat로 소유권을 유지하며 exact token의 terminal CAS 전에는 object를
  정리하지 않는다. 이후 API reconciler는 RQ maintenance credential을 확장하지 않고 별도 cleanup
  token을 사용한다. Job/attempt/type/session에서 재구성한 key와 stored key가 다르면 어떤 object도
  삭제하지 않으며, S3 staging/실패 canonical은 first-delete와 confirmation-delete를 모두 통과해야
  cleanup 완료와 quota 해제가 가능하다. Production API는 이 reconciler를 비활성화할 수 없다.
  Sample 최종 원장은 SQLite에서도 실효적인 Job write fence 뒤
  전체 claim/config를 재검증한다.
  Historical NULL Job은 history-only 조회만 허용하며 claim/retry/registry에는 사용할 수 없다. NULL을 현재
  설정으로 backfill하거나 hash만 바꿔 attempt provenance를 재작성하지 않는다. NULL queued row는 claim
  후보에서 제외하고 non-NULL corrupt row는 sanitized failure/audit로 격리하며 corrupt attempt를 자동
  재큐잉하지 않는다. RVC commit, adapter
  profile, image digest, Python/CUDA/PyTorch와 asset checksum도 함께 기록한다.
- Model registry candidate는 exact current completed real attempt의 `worker-claim-v1` snapshot,
  reviewed commit과 승인된 runtime image/asset pair가 모두 있을 때만 생성한다. 과거 attempt를
  현재 Worker row나 환경 설정으로 추정 backfill하지 않는다. Model은 Manager-verified
  `final_small_model` 하나, index는 browser 입력이 아니라 같은 attempt의 유일한 verified
  `final_index`를 사용한다.
- Candidate 생성과 promotion은 model/index canonical object 전체를 bounded spool로 다시 읽어
  선언 size/SHA-256과 대조한다. Byte 불일치, namespace `UNBOUND`, duplicate artifact와 timeout/
  storage 오류를 성공으로 축소하지 않는다. Entry public projection은 파일명·size·checksum과
  runtime provenance만 포함하고 URI, key, upload session과 raw metadata를 제거한다.
- Registry mutation의 원문 idempotency key는 SHA-256 hash로만 식별하고 request body/path/resource는
  JWT-secret keyed fingerprint에 결박한다. 같은 actor/key의 다른 요청은 `409`, exact replay는 저장한
  public response snapshot을 반환한다. Browser는 actor ID·최초 key·byte-identical body·사전 원장
  지문을 하나의 in-memory intent로 유지하고 actor/Experiment 변경 시 폐기한다. Transport, invalid
  success와 모든 `5xx` 뒤 새 key를 이용한 blind retry로 승인 row를 중복 생성하지 않는다.
- guarded `native` Worker는 source root를 절대 경로로 제한하고 reviewed commit을 설정으로
  바꾸지 못하게 한다. 생성과 claim 직전에 strict asset manifest의 모든 size/SHA-256/mode를
  확인하며 training/RMVPE GPU ID를 새로 수집한 visible capability와 대조한다. mismatch 또는
  `rvc_assets_ready=false`이면 Dataset/RVC subprocess 전에 실패한다.
- Runtime build는 code/config/asset projection 대상의 path/size/SHA-256/source mode manifest를
  생성하고 그 hash를 lock, build manifest와 image label에 결박한다. Worker는 shared path를
  다시 신뢰하지 않고 `O_NOFOLLOW` FD의 `fstat`과 streaming hash가 expected record와 일치한
  바로 그 byte만 private tree에 쓴다. 각 stage는 private projection도 expected inventory로
  다시 검증한다.
- self-contained installer의 image closure v2는 Manager exact 8 roles와 Worker exact 1
  runtime role만 허용한다. archive hash/size와 Docker-save inventory, source/runtime reference,
  image/config digest, linux/amd64, application release label을 load 전후에 확인한다. Manager
  dependency는 version-scoped alias로 격리하고 Compose는 `pull_policy=never`를 사용한다.
  installed start/restart와 rollback도 manifest·env·loaded identity가 다르면 release를
  활성화하지 않는다. partial bundle의 빈 inventory를 self-contained로 승격하지 않는다.
- Bundle `SHA256SUMS`는 누락뿐 아니라 추가·symlink·비정규 파일과 unsafe path를 거부한다.
  설치 release는 mode `0444` `RELEASE_SHA256SUMS` exact inventory를 만들고 Compose wrapper와
  rollback이 활성화 전에 다시 검증한다. Release manifest, image/runtime activation과
  release-owned environment의 version/image/pull-policy/provenance가 다르면 시작하지 않는다.
- stale/만료 lease와 이전 attempt의 상태·log·metric·artifact write를 거부한다.
- Job과 Worker write는 row version CAS도 사용해 SQLite처럼 `FOR UPDATE`가 무효인 환경과
  revoke/status/claim 경합에서도 terminal cancellation을 stale write가 덮지 못하게 한다.
- container image는 release에서 tag뿐 아니라 digest와 SBOM을 제공한다. image closure v2
  identity 검증은 SBOM, 취약점 scan이나 라이선스 검토를 대신하지 않는다.

### 로그와 비밀

- Authorization, cookie, Worker token, password, secret, presigned query와 전체 environment를 기록하지 않는다.
- correlation ID는 credential과 무관한 난수로 생성한다.
- Manager request logger는 query를 수집하지 않고 검증된 request ID, method, path, status,
  latency만 구조화한다. Uvicorn raw access log는 비활성화한다.
- Worker terminal failure는 exception 원문이나 class 이름을 전송하지 않고 고정
  `StageExecutionError.error_code`와 generic message만 사용한다. sanitized cause는 enum이고
  Agent 자체 로그도 Manager/object URL, query, argv, local path와 token 대신 stage/category/
  HTTP status 분류만 기록한다. unknown exception도 `stage_internal_error`로 fail-closed한다.
- RVC stdout/stderr, 증가분 `train.log`와 TensorBoard-derived log에는 경로나 환경이 포함될 수
  있으므로 Worker가 spool 저장 전에 bearer, named/quoted secret, JWT, API/access/private key,
  Worker token, URL query, file URI/absolute path와 control character를 제거한다. 정제된 단일 log도
  16 KiB로 제한하고 Manager가 저장 전에 다시 redaction한다. 원본 subprocess 출력 접근 권한은
  attempt workspace 운영자에게만 제한한다.
- Worker status/log/metric endpoint는 raw body를 기본 2 MiB로 제한하고 Content-Length와 chunked
  실제 byte를 모두 검사한다. UTF-8 strict JSON과 finite number만 schema parsing에 전달하며 NaN/
  Infinity를 허용하지 않는다. 운영 proxy 상한을 이보다 크게 두더라도 API 상한은 유지한다.
- Telemetry idempotency key는 canonical payload SHA-256 fingerprint에 결박한다. 같은 key의 다른
  payload, 같은 sequence의 다른 값, 다른 Worker/lease/attempt를 replay로 인정하지 않는다.
  Terminal attempt의 log/metric count는 함께만 존재하는 exclusive upper watermark이며 exact
  binding과 상한 아래 late batch만 허용한다. Legacy/system-recovery terminal처럼 watermark가 없는
  attempt는 old spool을 fail-closed한다.
- `.env`, Dataset, model, index, checkpoint, sample, local spool과 backup은 Git에서 제외한다.

## 데이터 보존과 삭제 정책

초기 안전 기본값은 **참조되는 사용자 데이터의 자동 삭제 없음**이다. 운영자가 quota/retention을 명시적으로 켜기 전 Dataset, final model/index, sample과 성공 attempt의 config/environment manifest를 보존한다.

- 현재 Dataset 삭제는 권한과 참조/활성 upload를 검사하고 `deleting` 상태를 먼저
  commit한 뒤 모든 upload 세대의 staging/canonical object와 DB row를 정리한다. object
  삭제 실패는 `delete_failed`로 보존한다. 대규모 운영용 비동기 tombstone/재시도 queue는
  후속 단계다.
- 한 Dataset을 여러 Experiment가 참조하면 Dataset 삭제를 거부하거나 명시적 cascade preview/확인이 필요하다.
- Job 삭제 시 원본 Dataset을 삭제하지 않는다.
- final model/index는 checkpoint cleanup과 별개의 보존 class다.
- Model registry entry가 참조하는 Job/attempt/model/index와 registry audit/idempotency 원장은
  retention 대상이다. Candidate 또는 승인 이력을 지우기 위해 Artifact나 Job을 cascade하지 않고,
  `revoked`도 forensic/승인 이력으로 보존한다. Active champion 폐기 시 다른 approved entry로 자동
  fallback하지 않는다.
- 만료되었거나 실패한 Dataset upload staging object와 fenced TestSet staging object만
  allowlist RQ orphan cleanup 대상이다. 기본 전역 유예는 7일이고 admin dry-run/result와
  audit를 제공한다. Dataset task는 upload row를 잠그고
  `pending` expiry, `failed|expired` age, stale claim과 exact server-owned staging key를 다시
  확인한다. `finalizing`, `completed`, 아직 유효한 `pending`과 canonical 원본/flat/manifest/
  quality object는 절대 삭제하지 않는다. storage 실패 claim은 bounded RQ retry 뒤 DB에
  typed failure로 남는다. namespace mismatch/`UNBOUND`도 typed deferred failure이며 객체와
  `cleanup_completed_at`을 보존한다. Dataset과 TestSet task 모두 exact session generation/write
  token과 cleanup claim generation을 다시 검사하고 첫 삭제 뒤 기본 60초 confirmation grace를
  둔 뒤 다시 삭제한다. active writer/finalizing/completed session과 canonical key는 두 task 모두
  대상이 아니다. multipart abort와 canonical delete tombstone은 아직 후속 단계다.
- Worker local workspace는 중앙 upload finalize와 checksum 확인 전 삭제하지 않는다. 성공 후 cleanup 유예 기간은 구성값이며 기본 7일이다.
- audit event는 일반 사용자 delete와 분리하고 최소 1년 보존을 운영 기본값으로 제안한다. 법적/조직 요구가 있으면 더 길게 설정한다.
- backup에도 동일한 접근 통제와 만료 정책을 적용하며, 삭제 요청이 backup rotation에서 사라지는 예상 시점을 사용자에게 표시한다.
- uninstall은 기본적으로 DB, object와 config를 보존한다. 영구 삭제 모드는 별도 명시적 확인과 backup 안내 없이는 실행하지 않는다.

## 제한과 후속 검증

- 현재 구현은 Argon2id password, 15분 access JWT, DB JTI logout revocation,
  admin/user 소유권과 Worker token 분리, query 비노출 구조화 로그와 API 보안 header를
  제공한다. login을 포함한 Redis rate limit은 구현했지만 refresh/session rotation은
  아직 없으므로 access token 만료 후 재로그인이 필요하다.
- Dataset/Experiment/Job owner 격리, 타 owner Dataset 조회/finalize/delete 은닉,
  참조·활성 session 삭제 차단, 현재 Worker lease/attempt Dataset download와 검증된
  Artifact 목록/download는 자동 테스트했다.
- Experiment name과 Dataset binding은 immutable이고 description 변경은 client가 본
  `row_version`의 compare-and-swap만 허용한다. 삭제도 owner/admin과 version을 다시 확인하며
  Job 참조, MLflow 활성화 또는 projection/outbox가 있으면 거부한다. historical duplicate name은
  migration에서 합치거나 삭제하지 않고 conflict key `NULL`로 격리하고 신규 owner/name만 DB
  unique constraint로 보호한다. POST/PATCH JSON은 선언 길이와 chunked body 모두 16 KiB 상한이다.
- Model registry의 candidate/promotion/revoke API와 same-origin BFF는 구현·자동 회귀 뒤에도 실제
  browser/API의 response-loss 재조정, PostgreSQL 다중 replica 동시 promotion, MinIO/S3 대용량
  canonical 전체 재해시·timeout·byte 변조·outage와 keyboard/screen-reader 인수가 별도 release
  gate다. Runtime qualification이 닫힌 현재 dev.20 partial
  bundle에서는 승인 digest pair가 없으므로 실제 production candidate가 0인 것이 기대 상태이며,
  환경변수로 이 gate를 우회해 registry 기능을 시연하지 않는다.
- Dataset finalize는 bounded thread에서 동기 실행한다. entry/byte/압축률 상한은
  강제하지만 Python thread를 안전하게 강제 종료할 수 없어 hard timeout은 제공하지
  않는다. staging cleanup만 별도 RQ Worker로 분리됐으며 finalize를 Redis/RQ 또는 격리
  subprocess로 전환하기 전에는 대형 archive가 요청을 오래 점유할 수 있다.
- Dataset과 TestSet local PUT은 parent→session 잠금, generation/write-token CAS heartbeat와 절대
  expiry deadline을 사용한다. Dataset canonical은 upload-session별 namespace에 no-replace로
  게시하고 finalize cancellation/commit 오류 때 fresh durable outcome을 확인한 뒤 미커밋 key만
  정리한다. 실제 원격 S3에서 전역 7일 grace보다 오래 지속되는 presigned PUT과 다중 replica
  장애 주입은 남아 있다. DB가 완전히 불가해 ambiguous commit을 재조회할 수 없으면 corruption을
  피하려고 canonical을 보존하므로 operator tombstone/reconcile이 후속 gate다.
- `e2f8b4c6a930`은 구 binary의 dataset-wide canonical key를 가진 `pending|finalizing` Dataset
  session을 자동 신뢰하지 않고 `expired`, `upload_fencing_upgrade_required`로 닫아 새 generation을
  요구한다. completed legacy row와 URI는 보존한다. migration 자체가 실행 중인 구 writer process를
  중단시키지는 못하므로 모든 API replica/client를 먼저 drain하지 않은 upgrade는 지원하지 않는다.
- Redis queue/job이 유실되면 API replica reconciler가 PostgreSQL의
  `queued|retrying|enqueue_failed`와 stale `running`을 advisory lock/row lock 아래 bounded
  재전달한다. exact started final attempt는 중복 생성하지 않고, completion은 status+attempt
  fence를 다시 검사해 reconciler terminal 결정을 덮지 않는다. Redis 장애 cycle은 첫 실패에서
  멈추고 readiness를 닫는다. 다만 Redis credential 탈취 시 execution allowlist는 임의 Python
  실행을 차단한다. 현재 maintenance process는 전용 PostgreSQL column/function role,
  staging-prefix delete-only S3 identity와 exact RQ Redis ACL로 API credential에서 분리됐고 실제
  PostgreSQL/Redis/MinIO local container smoke를 통과했다. API/운영자 Redis identity는 아직 별도
  rate-limit/queue/readiness 역할로 세분화되지 않았으며, 다중 replica와 외부 S3/Redis/PostgreSQL
  restart·timeout·partition 장애 주입은 release gate다.
- PCM WAV는 BS.1770-4 mono/stereo integrated LUFS와 Dataset-global gate, typed unavailable
  상태를 검증한다. Non-WAV는 content signature만 확인하고 격리 decode/ffmpeg sandbox가 아직
  없으므로 LUFS를 추정하지 않으며 `decoder_pending`으로 격리해 학습을 거부한다.
- S3 download의 만료값과 owner/admin 권한은 adapter/API 테스트했지만 실제 MinIO에서
  만료·query 변조를 검증하는 통합 테스트는 아직 남아 있다.
- `9d2f4b7c8e10` migration은 historical Dataset/Artifact session을 현재 storage에 자동
  귀속하지 않고 `UNBOUND`로 둔다. operator adoption은 active `pending|finalizing`을 거부하고
  terminal staging 또는 completed canonical object 전체를 다시 검증한다. preview도 감사
  event를 기록하고 결과에는 target backend와 namespace hash만 남기며 object key, URI/query,
  credential은 남기지 않는다. production upgrade 전 active upload를 drain하지 않으면
  UNBOUND active session이 owner/attempt quota를 계속 점유하고 cleanup도 진행되지 않을 수 있다.
- 다중 조직 tenant 모델은 아직 확정되지 않았으므로 초기 schema도 owner/organization 확장 여지를 둔다.
- pretrained/HubERT/RMVPE file별 재배포 라이선스는 미확정이다. 설치 package에 포함하기 전에 provenance를 검토한다.
- 고정 테스트 음원은 직접 사용·배포 권리를 확보한 파일만 등록한다.
- TestSet upload token 원문은 init 응답에만 있고 DB에는 SHA-256만 저장한다. user row/TestSet
  row lock으로 owner quota와 item 예약을 직렬화한다. backend 이름과 credential 없는 storage
  namespace fingerprint를 session에 함께 고정해 root/bucket/endpoint 변경 상태의 cleanup을
  거부한다. finalize failure는 fresh TestSet→upload lock과 token CAS가 성공한 경우만 상태와
  canonical을 정리한다. ready 전에는 completed session과 canonical object 전체 hash를
  재검증한다. license/provenance는 storage/URL scheme이 아닌 권리 원장 namespace allowlist의
  opaque record ID만 허용한다. 공개 응답·manifest·Job plan에는 storage URI와 presigned query를
  넣지 않는다. local PUT은 session을 다시 잠근 generation/write-token CAS heartbeat와 절대
  expiry deadline을 사용하고 finalize도 verify·PCM 검사·no-replace canonical publish 전 구간에서
  finalization token heartbeat를 유지한다. 전용 RQ cleanup은 전역 maintenance grace와 TestSet
  late-writer grace 중 큰 값(기본 7일) 뒤 exact staging key만 first-delete하고 confirmation grace
  뒤 재확인·재삭제한 경우에만 완료한다. 실제 S3에서 7일보다 긴 in-flight PUT과 다중 API replica
  경합 장애 주입은 별도 출시 gate이며 canonical object는 maintenance가 삭제하지 않는다.
- Sample 등록은 교차 Job/attempt/TestSet/item/Artifact ID 조합을 composite FK와 current lease
  fence로 막는다. verified model/index/output session의 SHA·size·type, approved runtime
  image/asset digest, native manifest/request hash와 역할을 Job snapshot에 교차검증한다. canonical
  WAV 전체를 제한된 spool로 다시 읽고 Manager가 계산한 PCM 지표를 원장에 보존한다. 등록과
  completion은 Redis rate limit, raw body, PostgreSQL single-flight/advisory lock, process semaphore와
  전체 deadline을 적용하며 취소된 검사 thread를 join한 뒤에만 spool/slot을 정리한다.
- canonical local publish는 no-replace link, S3는 conditional `If-None-Match: *`로 write-once를
  강제한다. completion과 Sample download가 현재 byte를 다시 해시하므로 저장소에서 같은 크기의
  byte를 바꿔도 완료·다운로드가 거부된다. 같은 PCM을 여러 논리 Sample이 공유할 때도 Artifact
  metadata에는 특정 첫 item의 self-asserted 등록 payload를 넣지 않고 content/runtime 증거만 둔다.
- `native` core stage, TestSet transfer와 PM/Harvest/CREPE/RMVPE inference/Sample completion
  코드와 production qualification→factory 연결은 구현됐지만 실제 digest-pinned 2.6.0/cu124
  image의 GPU/no-network matrix는 미검증이다. 현재 disabled projection은 빈 inference capability와
  `fixed_test_set_inference_ready=false`,
  `AUTO_SAMPLE_JOBS_ENABLED=false`, runtime/bundle의 `RVC_GPU_SMOKE_VERIFIED=false`,
  `PROFILE_STAGE_SET_VERIFIED=false`, `RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false`를 유지한다.
  현재 GPU smoke 미검증 bundle은 installer 확인 옵션과 runtime acknowledgement가 모두 없으면
  시작하지 않는다.
- Manager 전체 장애 중 Worker terminal status가 원장에 커밋되기 전에 lease가 회수되면 terminal
  watermark가 없으므로 해당 old attempt의 local pending telemetry를 중앙이 자동 수용할 수 없다.
  다른 attempt에 합치면 provenance가 깨지므로 fail-closed하며, 운영자는 Worker dead-letter/pending
  보존본과 lease/status event를 별도 조사해야 한다. Terminal status가 이미 커밋된 일시적 전송
  지연만 bounded late replay의 무손실 범위다.
- Job-bound system telemetry는 GPU inventory를 최대 64개와 index `0..1023`으로 제한하고 index와
  non-null UUID의 중복, non-finite 온도/사용률을 계약에서 거부한다. GPU/VRAM/온도/disk 표본은
  heartbeat 요청 전에 credential 없는 기존 metric spool에 저장하며 새 파일 형식이나 별도 token을
  만들지 않는다. 같은 값의 반복은 시간 증적이므로 dedupe하지 않되 terminal 봉인 이후 새 표본은
  발급하지 않는다.
- 실제 공개 전 SAST, dependency/container scan, secret scan, archive fuzzing, IDOR와 lease race 통합 테스트, backup/restore drill을 수행한다.

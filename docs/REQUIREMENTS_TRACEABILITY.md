# 요구사항 추적표

원본은 2026-07-11 업로드된 1,343줄의 「RVC 다중 학습 서버 중앙 관리 시스템 구축 보고서」다. 첨부 파일은 저장소 외부의 임시 경로일 수 있으므로, 구현에 필요한 규범적 요구를 이 문서에서 고유 ID와 인수 조건으로 유지한다.

상태는 `Planned`, `Partial`, `Verified`, `Deferred` 중 하나다. `Verified`는 자동화된 검증 근거가 개발 이력에 기록된 경우에만 사용한다.

## 시스템과 서비스 경계

| ID | 요구사항 | 인수 조건 | 상태 |
|---|---|---|---|
| SYS-001 | 중앙 Manager는 RVC 학습을 직접 실행하지 않는다. | Manager image와 권한에 GPU/RVC 실행 경로가 없고 Worker protocol로만 학습을 요청한다. | Verified |
| SYS-002 | 여러 물리 GPU Worker를 중앙에 연결한다. | 3개 Worker가 동시에 등록·heartbeat·서로 다른 Job claim을 수행하는 E2E가 통과한다. | Verified — 세 실제 `WorkerAgent` 인스턴스의 localhost HTTP 동시 실행 검증; 실제 NVIDIA/native smoke는 별도 release gate |
| SYS-003 | 같은 Dataset을 여러 조건으로 병렬 학습한다. | 한 Experiment 안에 여러 immutable JobConfig를 생성하고 독립 실행한다. | Verified — 동일 Dataset/Experiment의 PM·Harvest·RMVPE 세 Job을 세 Agent가 겹치는 attempt로 독립 완료 |
| SYS-004 | PostgreSQL을 metadata 원장으로 사용한다. | 재시작 후 사용자·Worker·Dataset·Job·Artifact 관계와 상태가 보존된다. | Planned |
| SYS-005 | Redis/RQ를 중앙 내부 비동기 작업에 사용한다. | Dataset 검증/정리 task가 API process와 분리되어 실행되고 장애 상태가 노출된다. | Partial — Dataset/TestSet staging cleanup RQ Worker, task-aware DB ledger/claim, dequeue+perform/enqueue exact JSON policy, execution-material poison quarantine, bounded retry, queue-loss reconciler와 readiness 검증; maintenance 전용 PostgreSQL column/function role, staging delete-only MinIO identity, exact RQ Redis ACL과 long-operation CAS heartbeat를 실제 PostgreSQL 16/Redis 7.4/MinIO/Compose에서 검증. Dataset finalize/검증 RQ 전환과 실제 다중 replica·외부 S3/Redis/PostgreSQL restart/partition 장애 주입은 대기 |
| SYS-006 | MinIO/S3에 대형 Dataset과 Artifact를 저장한다. | upload/finalize/checksum/download와 exact namespace mismatch 통합 테스트가 통과한다. | Verified — backend+credential 없는 namespace snapshot, 다른 root/bucket fail-closed와 credential rotation 동일성, Manager/MLflow identity의 상대 bucket 거부와 broad-policy 재실행 제거 smoke 검증 |
| SYS-007 | MLflow에 parameter, metric, artifact link를 투영한다. | MLflow 장애가 원장 Job을 손상하지 않고 재동기화할 수 있다. | Verified — durable outbox/replay와 fail-open/closed 회귀, UID 10002 read-only health 및 root source→전용 runtime secret actual-entrypoint smoke 포함 |
| SYS-008 | 실시간 로그와 상태를 제공한다. | sequence 기반 SSE/WebSocket 재연결 테스트가 통과한다. | Verified |

## 중앙 API와 데이터 모델

| ID | 요구사항 | 인수 조건 | 상태 |
|---|---|---|---|
| API-001 | API는 versioned `/api/v1` 경로를 제공한다. | OpenAPI에 모든 외부 endpoint가 prefix 아래 표시된다. | Verified |
| API-002 | User, Worker, Dataset, Experiment, Job을 저장한다. | migration과 관계/constraint 통합 테스트가 통과한다. | Verified — Dataset/Artifact historical session은 `UNBOUND` sentinel, 신규 행은 exact namespace가 필수; Dataset PCM aggregate all-null/all-present·bounds와 historical null 보존; Experiment immutable name/Dataset, description row-version CAS, 참조·MLflow 안전 삭제와 historical duplicate 보존; SQLite/PostgreSQL upgrade+downgrade 검증 |
| API-003 | Log, Metric, Artifact, Sample, Preset metadata를 저장한다. | 중복 key/idempotency와 pagination 테스트가 통과한다. | Verified — log/metric sequence와 canonical payload fingerprint 충돌, terminal exclusive watermark의 bounded late replay, Sample 등록/replay/list/download, composite graph와 native/runtime provenance 교차검증 포함 |
| API-004 | 재시도마다 JobAttempt를 분리 보존한다. | retry가 이전 로그·metric·artifact를 덮어쓰지 않는다. | Verified |
| API-005 | 모든 상태 전이를 append-only event로 추적한다. | 잘못된 전이를 거부하고 event sequence가 연속적이다. | Verified |
| API-006 | Worker 배정은 atomic lease다. | 동시 claim에서 한 Worker만 성공하고 stale lease write가 거부된다. | Verified |
| API-007 | 작업 cancel과 retry를 제공한다. | cooperative cancel과 새 attempt retry E2E가 통과한다. | Verified |
| API-008 | Worker capability로 Job을 매칭한다. | VRAM/tag/RVC asset과 sample inference readiness/F0 method가 맞지 않는 Worker는 claim하지 못한다. | Verified |
| API-009 | 고정 TestSet revision을 검증된 WAV와 결정적 manifest로 동결한다. | owner/idempotency/예약 충돌, 전체 size/SHA/PCM decode, canonical 재검증, immutable delete와 claim-time manifest/object 재검증이 통과한다. | Verified |
| API-010 | 관리자가 사용자 계정 lifecycle을 중앙에서 관리한다. | admin-only list/detail/create, role/active CAS, password reset, replay와 migration 회귀가 통과한다. | Verified — secret-free idempotency/audit, normalized email, row/token version과 16 KiB body 포함 |

## Dataset

| ID | 요구사항 | 인수 조건 | 상태 |
|---|---|---|---|
| DATA-001 | WAV/지원 audio와 ZIP Dataset을 streaming 업로드한다. | 크기 제한 내 업로드, 중단 오류와 storage namespace 변경 거부가 검증된다. | Verified — local PUT generation/write-token heartbeat와 절대 deadline, session-scoped canonical, finalize 취소/commit-outcome 복구, 확인형 이중 cleanup과 legacy active-session generation replay 포함 |
| DATA-002 | 중첩 폴더에서 audio를 재귀 수집해 flat Dataset을 만든다. | 입력 tree가 순번 기반 충돌 없는 flat manifest가 된다. | Verified |
| DATA-003 | 파일명과 경로를 안전하게 정규화한다. | 공백·한글·특수문자·중복명 fixture가 안전한 이름으로 매핑된다. | Verified |
| DATA-004 | 깨진 파일과 중복을 검사한다. | 실제 decode 실패 및 checksum 중복이 보고서에 기록된다. | Partial |
| DATA-005 | 길이, 수, sample rate, channel, clipping, silence, RMS/LUFS를 보고한다. | 고정 audio fixture의 허용 오차 내 결과가 검증된다. | Verified — PCM sample-count 가중 clipping/silence/RMS와 BS.1770-4 mono/stereo K-weighting·400 ms/75% overlap·전역 2단계 gate를 독립 FFmpeg ebur128 기준 tone과 검증; 짧음/절대 gate 미만/지원 밖 layout·rate는 typed null, `d8f2a6c4b901` migration과 API/BFF가 historical LUFS null을 보존 |
| DATA-006 | path traversal, symlink, 압축 폭탄을 막는다. | 악성 archive fixture가 작업 디렉터리 밖을 쓰지 못하고 거부된다. | Verified |
| DATA-007 | Manager가 canonical flat Dataset을 만들고 Worker는 checksum을 재검증한다. | 동일 manifest를 재처리해도 결과 hash가 같고 다른 namespace에서는 claim/GET이 닫히며 external 307에 Worker credential/cookie가 전달되지 않는다. | Verified |

## Job 설정과 상태

| ID | 요구사항 | 인수 조건 | 상태 |
|---|---|---|---|
| JOB-001 | JobConfig는 배정 후 immutable snapshot이다. | canonical JSON hash가 저장되고 실행 중 수정 API가 없다. | Partial |
| JOB-002 | 상태 목록은 원문 정의를 지원한다. | enum과 전이표가 queued부터 terminal까지 contract test를 통과한다. | Verified |
| JOB-003 | `use_f0=false`이면 F0 단계를 생략한다. | 상태와 command plan 모두 extracting_f0를 포함하지 않는다. | Verified |
| JOB-004 | optional index/sample 단계는 config에 따라 생략한다. | 각 조합의 전이 테스트가 통과한다. | Verified |
| JOB-005 | `completed`는 필수 Artifact 검증 후에만 허용한다. | model 및 활성화된 index, sample-enabled면 모든 TestSet Sample과 현재 canonical byte가 없으면 완료가 거부된다. | Verified |
| JOB-006 | loss와 epoch, GPU 상태를 지속 수집한다. | 중복 batch가 하나의 logical metric으로 저장된다. | Partial — native stdout/train.log/TensorBoard와 시작 직후/60초 GPU·VRAM·온도·disk/availability를 같은 durable sequence로 전송하고 terminal 전 HTTP tail로 loss/epoch/indexed GPU를 조회; 동일 system 시간 표본 보존과 final flush를 fixture/E2E로 검증, 실제 NVIDIA 장시간 정확도·부하 증적 대기 |
| JOB-007 | sample Job은 게시할 model/index 의미와 일치하는 Artifact 수집을 요구한다. | enabled인데 `collect_samples=false`, `collect_small_model=false`, `index.build_index=false`+nonzero `index_rate`, 또는 nonzero `index_rate`+`collect_index=false` 조합이 계약에서 거부된다. | Verified |

## Worker Agent

| ID | 요구사항 | 인수 조건 | 상태 |
|---|---|---|---|
| WRK-001 | Worker별 token으로 등록·인증한다. | token 원문은 서버 DB/로그에 없고 회전·폐기가 가능하다. | Verified — idle-only two-phase self rotation, response-loss recovery, immediate old-token revoke, admin force revoke와 inactive-only bootstrap re-enroll 회귀 통과 |
| WRK-002 | heartbeat와 GPU/VRAM/온도/disk 상태를 보고한다. | fake 및 `nvidia-smi` fixture parsing test가 통과한다. | Verified — fresh Job observation과 60초 cadence, 성공한 empty query와 실패 availability 구분, 64개/index·UUID/VRAM/finite semantic fail-safe를 durable spool/watermark에 연결 |
| WRK-003 | 장시간 학습과 heartbeat를 동시에 수행한다. | fake 장시간 Job 중 heartbeat 간격이 유지된다. | Verified |
| WRK-004 | Job별 attempt workspace를 격리한다. | 사용자 입력으로 root 밖에 파일을 만들 수 없다. | Verified |
| WRK-005 | 중앙 단절 시 log/metric을 spool하고 재전송한다. | 장애 주입 뒤 순서·idempotency를 유지해 복구한다. | Verified — durable enqueue, typed persistence 실패, producer seal 후 healthy final flush와 503 pending late replay, payload fingerprint/watermark를 검증; terminal status 미커밋 뒤 lease 회수 시 watermark 없는 old spool은 자동 수용하지 않음 |
| WRK-006 | 취소 시 process group을 정상 종료하고 제한 시간 뒤 강제 종료한다. | child process를 남기지 않는 cancel test가 통과한다. | Verified |
| WRK-007 | 모든 실행 환경과 RVC commit/asset checksum을 기록한다. | `environment.json`과 artifact manifest schema가 검증된다. | Partial — attempt의 approved image/asset digest와 native inference manifest/request hash를 Artifact/Sample까지 기록·재검증하고 49-case release 증적 projection 경계도 구현했지만 실제 GPU 증적 대기 |
| WRK-008 | Fake runner로 GPU 없는 E2E를 제공한다. | Dataset→Job→Artifact 전체 fake 흐름이 CI에서 통과한다. | Verified |
| WRK-009 | stage timeout/error와 retry 경계를 명시하고 민감정보 없는 terminal 실패를 보고한다. | 전 stage timeout, no-replay, cancel 우선, Dataset/TestSet/Artifact bounded transfer exhaustion과 redaction test가 통과한다. | Verified |
| WRK-010 | Job snapshot의 고정 TestSet을 lease-bound로 안전하게 수신한다. | capability/method 또는 DB sample-plan hash mismatch는 미배정되고 current Worker/lease/attempt만 item을 받으며, external 307 credential/cookie 비전달, exact response hash/PCM, 전체 디렉터리 atomic/replay 검증이 통과한다. | Verified — 전송/materialization만 해당하며 inference와 Sample completion은 포함하지 않음 |

## RVC Adapter

| ID | 요구사항 | 인수 조건 | 상태 |
|---|---|---|---|
| RVC-001 | RVC v1과 v2만 지원한다. | v3와 임의 값은 API와 Worker 양쪽에서 거부된다. | Verified |
| RVC-002 | v1은 `3_feature256`, v2는 `3_feature768`을 사용한다. | command/artifact parser contract test가 두 버전을 검증한다. | Verified |
| RVC-003 | pretrained를 version/sample rate/use_f0로 resolve한다. | 지원 matrix 전체와 미지원 조합 오류가 테스트된다. | Verified |
| RVC-004 | training F0는 pm/harvest/dio/rmvpe/rmvpe_gpu다. | 다른 값과 GPU 없는 rmvpe_gpu가 거부된다. | Verified |
| RVC-005 | inference F0는 pm/harvest/crepe/rmvpe다. | training-only 방식이 sample config에서 거부된다. | Verified |
| RVC-006 | preprocess/F0/feature/train/index/inference 명령을 안전하게 실행한다. | argv만 사용하고 shell injection fixture가 실행되지 않는다. | Partial — guarded native core와 PM/Harvest/RMVPE 고정 TestSet inference, timeout/cancel/FD 검증은 연결; CREPE asset 및 실제 GPU/no-network matrix 대기 |
| RVC-007 | stdout/log/TensorBoard에서 metric을 파싱한다. | 알려지지 않은 line에도 학습은 유지하고 raw log를 보존한다. | Verified — stdout/stderr callback, 증가분 train.log tail과 TensorBoard scalar polling을 같은 attempt telemetry session에 연결하고 source/semantic dedupe·unknown-line sanitized log 보존을 fixture로 검증; 실제 GPU runtime 자격 증명은 RVC-006/012와 별도 |
| RVC-008 | `weights/<exp>.pth`를 final small model로 수집한다. | 존재 시 checksum manifest와 inference smoke test가 통과한다. | Partial |
| RVC-009 | small model이 없으면 공식 checkpoint 추출을 사용한다. | G checkpoint 단순 복사를 하지 않고 추출 결과를 검증한다. | Verified |
| RVC-010 | G/D checkpoint를 pair와 epoch 의미를 보존해 수집한다. | 파일명 epoch parser가 mtime과 무관하게 올바른 pair를 선택한다. | Verified |
| RVC-011 | `added_*.index`와 `total_fea.npy`를 수집한다. | 최종 index는 `final.index`, 원본 이름은 metadata에 남는다. | Partial |
| RVC-012 | 고정 테스트 음성으로 자동 sample을 생성한다. | 활성화한 각 입력에 model/index/config가 기록된 출력이 생긴다. | Partial — 4종 driver, canonical publication/completion과 qualification→factory/capability 연결은 fixture로 구현; 실제 CREPE asset 승인, Torch `>=2.6` runtime과 49-case GPU/no-network 증적 대기 |
| RVC-013 | sample resample은 미적용 `0` 또는 명시적 `16000..192000` Hz만 허용한다. | 계약, Job API와 Worker claim이 `1..15999` 및 범위 밖 값을 거부한다. | Verified |

## Dashboard와 비교

| ID | 요구사항 | 인수 조건 | 상태 |
|---|---|---|---|
| UI-001 | Worker 목록에 online/GPU/VRAM/current Job/heartbeat를 표시한다. | loading/empty/error/offline 상태의 UI test가 통과한다. | Partial |
| UI-002 | Dataset 업로드와 품질/flat 결과를 표시한다. | 업로드 진행·실패·보고서 흐름 E2E가 통과한다. | Partial — BFF/UI, 10,000개까지 complete bounded pagination, typed issue count와 sample-count 가중 PCM aggregate 목록·상세 투영 및 private raw-report 차단 회귀 구현; 실제 browser↔MinIO 대용량 E2E와 반응형·접근성 시각 QA 대기 |
| UI-003 | 여러 조건의 Job을 한 번에 생성한다. | 조합 preview와 서버 검증 오류가 보인다. | Partial — ready Dataset matrix, 완전한 기존 이름 조회, 확정 행 보존/응답 유실 분리, terminal create 잠금, 409/422/429 UI와 회귀 구현; 실제 browser/API E2E와 create idempotency key 대기 |
| UI-004 | Job 목록/상세/실시간 로그/loss/GPU graph를 제공한다. | SSE 재연결과 terminal 상태 UI test가 통과한다. | Partial — SSE, canonical GPU/availability 단위, 최신 200개 metric tail과 non-overlapping 15초 polling 및 HTTP E2E를 구현; 실제 browser 장시간 graph/재연결·접근성 E2E 대기 |
| UI-005 | model/index/checkpoint/sample 다운로드를 제공한다. | 권한과 만료를 지키는 download E2E가 통과한다. | Partial — Sample player와 single Range/strong If-Range BFF, Manager 200/206/416·stable ETag·disconnect cleanup 회귀 완료; 실제 browser/object-storage E2E 대기 |
| UI-006 | 동일 Experiment의 설정/metric/sample A-B 비교를 제공한다. | 최소 2개 Run 비교 E2E가 통과한다. | Partial — owner/admin 2~16 Job comparison API와 cookie-only same-origin BFF가 immutable config, exact current-attempt engine/timing, key당 latest 200 allowlisted metric, Manager-verified model/index/sample availability를 내부 storage 값 없이 제공. 상세 화면은 선택, Fake 경고, global sequence metric graph·원장, 학습 시간과 결과물을 비교하고 동일 TestSet item A/B player와 description PATCH/delete를 연결함. Model registry BFF/UI도 Fake action 차단, checksum/runtime provenance, candidate/champion/inactive approved/revoked와 uncertain-response 잠금을 제공하며 Web 24 files/211 tests와 production build가 PASS. 실제 browser/API 비교·registry response-loss/동시 promotion E2E는 대기 |
| UI-007 | fake와 real engine mode를 명확히 표시한다. | fake 결과를 운영 모델로 오인할 수 없는 badge와 metadata가 있다. | Verified — exact current JobAttempt의 `current_attempt_engine_mode`만 사용하고 config fallback을 금지; Overview/목록/상세의 Fake badge·접근 가능한 경고와 API/Web/HTTP E2E 검증 |
| UI-008 | 관리자가 사용자 생성·권한·활성·비밀번호를 화면에서 관리한다. | same-origin BFF, stale/응답유실 UX와 관리자 보호 동작이 browser E2E를 통과한다. | Partial — 고정 BFF/public projection, 멱등 키, row-version UI와 production build·143 Web 회귀 통과; 실제 browser E2E 대기 |

## 보안과 운영

| ID | 요구사항 | 인수 조건 | 상태 |
|---|---|---|---|
| SEC-001 | 사용자는 JWT, Worker는 별도 token을 사용한다. | 서로의 credential로 다른 API에 접근할 수 없다. | Verified |
| SEC-002 | password는 Argon2id, token은 강한 one-way hash로 저장한다. | 원문 검색 검사와 인증 test가 통과한다. | Verified |
| SEC-003 | 사용자별 Dataset/Artifact 권한을 분리한다. | 다른 사용자의 ID를 알아도 조회·다운로드·삭제가 거부된다. | Verified |
| SEC-004 | 다운로드는 짧은 만료와 범위 제한을 적용한다. | 만료·변조 URL이 실패한다. | Partial |
| SEC-005 | 생성/삭제/download/token 작업을 audit한다. | actor/action/resource/correlation ID가 남는다. | Partial — Worker prepare/abort/activate/admin revoke/re-enroll과 storage adoption 및 maintenance reconcile/reject/repair typed audit 구현, 전체 correlation/retention 검증 대기 |
| SEC-006 | 비밀과 presigned query를 로그에서 가린다. | redaction test에 token이 나타나지 않는다. | Verified — Worker live stdout/train.log에도 bearer·named/quoted secret·JWT/API/access/private key·URL query·absolute path/control과 16 KiB 상한을 적용하고 Manager에서 재차 redaction |
| SEC-007 | 사용자 권한 변경과 비밀번호 재설정은 기존 세션을 즉시 폐기하고 마지막 관리자를 보존한다. | token version, self/last-admin 보호와 동시 cross-demotion 회귀가 통과한다. | Verified |
| OPS-001 | Worker offline과 lease 만료를 감지한다. | 장애 E2E에서 중복 실행 없이 복구 정책을 따른다. | Partial |
| OPS-002 | backup/restore/upgrade/rollback을 제공한다. | clean VM 복구 drill에서 metadata와 object가 일치한다. | Partial — historical namespace 자동 backfill 금지, byte 검증형 adoption과 e2 이전 active Dataset upload의 fail-closed expire/generation replay 구현; production-like pre-upgrade process drain/adoption/rollback drill 대기 |
| OPS-003 | 사용자/프로젝트 storage quota와 retention을 제공한다. | canonical Dataset/Artifact/Sample 총량 초과와 참조 중 삭제가 거부되고 reclaim이 audit된다. | Partial — upload session count/byte 상한은 구현; 장기 canonical 사용량 quota/retention 대기 |
| OPS-004 | checkpoint resume는 이전 산출물을 섞지 않고 새 attempt로 수행한다. | 검증된 G/D pair만 새 attempt 입력이 되고 원 attempt 원장은 불변이다. | Planned |
| OPS-005 | terminal Job, Worker offline과 quota 알림을 보낸다. | outbox replay에도 사용자당 한 logical notification만 전달된다. | Planned |
| OPS-006 | 승인 가능한 model registry를 제공한다. | exact current real attempt의 reviewed commit과 승인 runtime provenance, canonical model/index 재해시를 통과한 candidate만 등록되고 candidate/approved/revoked, active champion 0/1, rollback promotion과 revoke가 CAS·멱등 원장·audit로 보존된다. | Partial — PostgreSQL 원장, owner/admin CAS·actor-scoped hashed idempotency·audit API와 same-origin BFF/UI를 구현했다. Candidate 생성/promotion의 canonical byte 재해시, Fake/historical NULL/unapproved runtime 차단, active champion 0/1·inactive approved rollback·revoke terminal을 registry 14+migration 19 집중 회귀와 전체 API 271, Web 211 회귀로 검증했다. 실제 PostgreSQL multi-replica 경쟁, MinIO/S3 대용량 재해시·tamper/outage와 browser/API response-loss 인수는 대기. MLflow는 계속 파생 projection이며 registry 원장이 아님 |

## 배포와 설치 파일

| ID | 요구사항 | 인수 조건 | 상태 |
|---|---|---|---|
| DEP-001 | 중앙 stack을 Docker/Compose로 실행한다. | clean Ubuntu에서 health/readiness, trusted TLS scheme, Secure cookie/HSTS가 모두 통과한다. | Partial — operator-owned `PUBLIC_SCHEME`, production HTTPS start gate와 spoof 방지, MLflow UID 10002/read-only/cap-drop/PID 제한, root mode 0600 source secret의 API/RQ/MLflow별 atomic projection과 actual-entrypoint smoke, Manager/MLflow exact MinIO bucket policy·broad-policy 제거를 구현. dev.20의 정확한 8개 linux/amd64 release image로 전체 Compose health/readiness·loopback MinIO/MLflow·secret inventory/policy runtime smoke가 arm64 Colima emulation에서 PASS; clean amd64 전체 lifecycle/browser TLS는 대기 |
| DEP-002 | Worker를 NVIDIA runtime으로 실행한다. | privileged/Docker socket 없이 GPU가 노출된다. | Partial — guarded native Compose/installer gate와 pre-start one-shot 순서를 검증. Worker custom CA는 root-owned bounded PEM을 release 밖 mode `0444`로 원자 게시하고 고정 read-only mount, start 재검증, Manager/Object 공통 `CERT_REQUIRED`/hostname/TLS 1.2+ context와 CA 누락·hostname mismatch handshake 회귀를 통과. Clean GPU VM과 실제 사설 CA endpoint 연결 smoke는 대기 |
| DEP-003 | Manager와 Worker 설치 파일을 분리한다. | 각각 독립 bundle과 checksum이 생성된다. | Verified — dev.20 Manager와 Worker를 source `298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a`의 별도 archive로 생성하고 외부 sidecar와 내부 exact ledger를 검증했다. Manager는 schema `f5d1c8a9b240`, `667617422` byte, SHA-256 `c6488dad47c7f38c082ed6fa68f1fe3691c069110aef0bbf68a9d7ba5e6f5b70`, exact 8-image `SELF_CONTAINED=true` 후보다. Worker는 `108488` byte, SHA-256 `7f36cbf27100bf70425c2780142d4fa3f6e6e76d0acf410d3e3fb698aa50558b`, `SELF_CONTAINED=false`이고 image/runtime과 활성 gate가 없는 partial이다. 설치 파일 분리는 검증됐지만 이 비대칭 bundle 쌍은 production 인수 증거가 아님 |
| DEP-004 | 설치는 재실행 가능하고 기존 데이터를 기본 보존한다. | install→reinstall→upgrade→uninstall preserve test가 통과한다. | Partial — Manager/Worker 1.0→2.0 forward upgrade의 env/current 전환, prevalidation 실패 시 기존 byte 보존, downgrade 거부, Worker token/profile/data/custom CA 보존과 custom CA replacement staging 실패 불변, uninstall 일부 실패 nonzero를 검증; mode `0444` installed `RELEASE_SHA256SUMS` exact inventory와 release-owned environment provenance closure 포함, clean VM 전체 lifecycle 대기 |
| DEP-005 | online과 air-gapped 설치 경로를 구분한다. | image archive가 없는 offline 모드는 명확한 preflight 오류를 낸다. | Partial — dev.20 Manager는 exact 8-image archive, `RVC_IMAGE_PULL_POLICY=never`, load 전 closure와 load 뒤 identity 검증을 통과해 offline 후보 경로가 실행 가능하다. Worker dev.20은 image/runtime 없는 partial로 명시되어 offline/native 실행을 fail-closed한다. Clean Ubuntu의 실제 단절망 설치·upgrade·rollback과 Worker runtime archive는 대기 |
| DEP-006 | version manifest, asset/runtime checksum과 SBOM을 제공한다. | release validation이 누락 파일을 거부한다. | Partial — archive와 installed release의 exact ledger가 누락·추가·symlink·비정규·unsafe path를 거부하고, extracted bundle의 ledger 제거도 exact Git source root가 아니면 거부한다. Repository ignore policy는 local secret/cache/runtime state와 실제 Dataset·audio·model·index·archive를 제외하면서 release source ignore closure를 검증한다. dev.20 Manager는 exact image/archive identity, `Config.User`, 별도 OCI/config digest와 source commit을 strict verify/load 검증하고 bundle-local 문서와 dependency inventory를 제공한다. Worker runtime image와 self-contained bundle byte도 clean committed source/source closure/exact Git archive에 결박하고, runtime build manifest exact schema·release identity와 activation `0444`를 qualification 전후 모두 검증한다. Native runtime/asset archive, vulnerability/container/secret scan과 법적 검토는 남음 |
| DEP-007 | Ubuntu 22.04/24.04 x86_64를 1차 지원한다. | 두 OS clean VM 설치 smoke test가 통과한다. | Planned |

## 결정 기록

- [ADR-0001](adr/0001-remote-worker-job-claim.md): 원격 Worker는 HTTP claim/lease를 사용하고 Redis를 외부에 노출하지 않는다.
- [ADR-0002](adr/0002-canonical-dataset-preparation.md): Manager가 canonical flat Dataset을 만들고 Worker가 무결성을 재검증한다.
- [ADR-0003](adr/0003-installation-platform.md): 1차 설치 지원 플랫폼과 Manager/Worker bundle을 분리한다.
- [ADR-0004](adr/0004-fixed-testset-sample-provenance.md): 고정 TestSet revision, Preset snapshot과 Sample provenance를 분리한다.

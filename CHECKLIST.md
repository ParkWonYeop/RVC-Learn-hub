# 개발 체크리스트

마지막 갱신: 2026-07-13

실제로 구현하고 검증한 항목만 체크한다. 세부 변경과 검증 근거는 `docs/DEVELOPMENT_HISTORY.md`에 남긴다.

## 0. 요구사항과 저장소 기반선

- [x] 업로드된 1,343줄 설계 문서 전체 검토
- [x] Manager/Worker/RVC 엔진의 책임 경계 확정
- [x] RVC v1/v2, F0 방식, 모델·인덱스 산출물 규칙 추출
- [x] 루트 `AGENTS.md` 작성
- [x] 상세 개발 이력 문서 작성
- [x] 단계별 체크리스트 작성
- [x] 초기 아키텍처와 설치 대상 가정 문서화
- [x] 공개 API와 요구사항 간 추적표 완성
- [x] 위협 모델과 데이터 보존 정책 확정

## 1. 중앙 관리 서버 기반

- [x] Python 프로젝트와 재현 가능한 의존성 구성
- [x] FastAPI application factory, 설정, health/readiness와 correlation ID
- [x] 구조화 로깅과 민감정보 redaction
- [x] PostgreSQL 연결 및 migration 체계
- [x] 사용자, Worker, Dataset, Experiment, Job 모델
- [x] JobLog, Metric, Artifact 모델
- [x] Sample 관계 ID graph, immutable Preset revision, 고정 TestSet/Item/upload session 모델과 단일 migration
- [x] TestSet bounded WAV init/PUT/finalize, storage-neutral manifest와 owner/admin API
- [x] ready TestSet 재검증과 Job inline inference/sample-plan hash snapshot
- [x] sample capability/F0 matching과 manifest·sample-plan·canonical item 원장/namespace 재검증형 Job claim
- [x] current Worker/lease/attempt에 결박된 TestSet item stream 또는 단일 presigned 307
- [x] TestSet PUT/finalize generation heartbeat와 first/confirmation 이중삭제 staging maintenance
- [x] audit log와 작업 임대(lease) 모델
- [x] Redis/RQ 연결과 최근 maintenance Worker heartbeat 기반 fail-closed readiness
- [x] RQ dequeue/perform JSON execution allowlist와 maintenance process 인증 secret 분리
- [x] storage 실패 bounded delayed retry를 전달하는 내부 RQ scheduler
- [x] Redis queue 유실 시 PostgreSQL maintenance run reconciler와 poisoned envelope 복구
- [x] maintenance 전용 PostgreSQL column/function role, staging-prefix delete-only S3 IAM과
  exact RQ Redis ACL 및 long-operation CAS heartbeat
- [x] MinIO/S3 storage adapter와 로컬 테스트 adapter
- [x] Dataset/Artifact exact storage namespace snapshot, fail-closed data plane과 검증형 legacy adoption
- [x] MLflow 연동 adapter와 선택적 비활성화 모드

## 2. 인증과 보안

- [x] 초기 관리자 bootstrap 절차
- [x] 비밀번호 hash, JWT login/me/logout 흐름
- [x] 역할 기반 사용자 권한
- [x] 관리자 사용자 목록·생성, 역할/활성 row-version CAS와 비밀번호 재설정 API/UI
- [x] secret-free mutation 멱등 원장, 16자 관리 비밀번호 정책과 access-token version 전체 폐기
- [x] 자기 강등/비활성화 및 동시 cross-demotion에서 마지막 활성 관리자 보존
- [x] Worker 등록, 응답유실 복구형 2단계 token 회전, 관리자 폐기와 inactive-only 재등록
- [x] 다운로드용 만료 URL 또는 인증 streaming
- [x] Dataset/TestSet 외부 307에 Worker 인증·lease·cookie·proxy 인증을 전달하지 않는 fresh-client 경계
- [x] 업로드 MIME/확장자/크기 제한
- [x] ZIP path traversal, symlink, 압축 폭탄 방어
- [x] audit log와 민감정보 redaction
- [x] CORS allowlist와 API 보안 header 구성
- [x] Redis-backed rate limit
- [x] Sample raw body/rate/concurrency/deadline 상한과 취소 시 PCM thread/spool/FD join-cleanup

## 3. 데이터셋

- [x] bounded streaming raw PUT 또는 S3 presigned PUT 업로드 API
- [ ] multipart/resume 업로드 API
- [x] dataset metadata와 object storage 저장
- [x] 중첩 폴더 오디오 탐색과 flat dataset 생성
- [x] 안전한 파일명 정규화와 충돌 처리
- [x] PCM WAV broken file, 길이, sample rate, channel 검사
- [ ] 격리 decoder 기반 non-WAV 검증
- [x] clipping, silence, RMS, duplicate 품질 지표
- [x] BS.1770-4 mono/stereo integrated LUFS와 전역 block gate·typed unavailable 상태
- [x] 품질 보고서와 사용 가능 여부 API
- [x] 참조·활성 업로드 차단과 원본/준비본/보고서 동기 삭제
- [ ] 비동기 tombstone과 orphan staging retention 정리 작업
  - [x] 만료/실패 Dataset upload staging RQ 정리, dry-run, audit와 재시도
  - [x] Dataset PUT/finalize generation heartbeat, session-scoped canonical과 확인형 cleanup 경합 차단
  - [ ] Dataset canonical delete tombstone과 비동기 재시도

## 4. Experiment와 Job 오케스트레이션

- [x] Experiment 안전 CRUD와 동일 Dataset 다중 immutable 조건 생성
  - [x] owner/admin 404 경계, description-only row-version CAS와 audit
  - [x] immutable name/Dataset, Job·MLflow 참조 delete 차단과 legacy duplicate quarantine
  - [x] 16 KiB raw JSON, pagination, 동시 update/name conflict와 migration 회귀
- [x] Job config 스키마와 version-aware 검증
- [x] Job 생성/조회/filter/pagination API
- [x] 우선순위와 Worker tag/VRAM capability matching
- [x] 원자적 next-job 배정 및 lease 갱신
- [x] 전체 상태 머신과 허용 전이 검증
- [x] cancel cooperative protocol
- [x] retry attempt와 이전 실행 이력 보존
- [x] 유실 Worker 감지 및 안전한 재배정 정책

## 5. Worker 통신 API

- [x] Worker register와 capability 보고
- [x] heartbeat와 GPU/VRAM/disk 상태 수집
- [x] atomic claim 기반 next-job polling
- [x] 상태 update와 lease 소유권 검증
- [x] terminal release와 heartbeat Worker row-version 경합의 bounded 전체 fence 재검증
- [x] 로그 batch ingestion
- [x] 실시간 로그 stream
- [x] metric batch ingestion
- [x] Job 시작 직후/60초 GPU·VRAM·온도·디스크·availability 시계열과 terminal final flush 결합
- [x] Worker status/log/metric 2 MiB raw body, strict JSON과 non-finite number 거부
- [x] attempt/sequence와 canonical payload fingerprint에 결박된 log/metric 멱등·충돌 검증
- [x] terminal exclusive telemetry watermark와 exact lease/attempt의 bounded late replay
- [x] active telemetry와 terminal/cancel 경쟁의 DB write fence 및 retryable `503` 재평가
- [x] artifact upload session/complete 흐름
- [x] storage-neutral TestSet claim과 lease/attempt-bound item download 흐름
- [x] sample metadata 등록과 Artifact/model/index/config SHA·size·type·역할 및 runtime/native provenance 교차검증
- [x] 로그/metric/artifact 요청 멱등성 key 처리

## 6. 학습 서버 Agent

- [x] Worker Python package, CLI와 YAML/env 설정
- [x] 등록, 0600 credential 지속, heartbeat, polling, graceful shutdown
- [x] `nvidia-smi` capability/metric collector
- [x] job별 격리 workspace와 disk quota 검사
- [x] dataset download/checksum/검증
- [x] flat dataset 준비
- [x] profile/native 공통 canonical Dataset stage 연결
- [x] ordered TestSet PCM WAV 수신, 전체 디렉터리 원자 게시와 replay exact-inventory 재검증
- [x] 동일 PCM content dedupe를 보존하는 Worker Artifact publication과 논리 Sample별 등록
- [x] native claim 직전 commit/asset manifest/visible GPU 재검증
- [x] 취소 신호와 subprocess process-group 종료
- [x] 단계별 typed retry/timeout/error 분류와 executor-level replay 금지
- [x] 네트워크 단절 시 로그·metric spool과 재전송
- [x] native stdout/stderr, incremental `train.log`, TensorBoard scalar의 실행 중 durable-first 수집
- [x] attempt-wide log/metric sequence, source/semantic dedupe와 `current_epoch` terminal 전 투영
- [x] secret/query/path/control 제거·16 KiB log 상한과 terminal producer 봉인/watermark 보고
- [x] fresh/60초 GPU·VRAM·온도·디스크 snapshot, query availability와 typed spool 실패

## 7. RVC v1/v2 Adapter

- [x] 고정 commit 기반 RVC repository 검증
- [x] allowlist 기반 job-local RVC source/asset projection과 shared output 격리
- [x] build-generated projection input manifest와 FD byte TOCTOU 검증
- [x] preprocess/F0/feature/train/checkpoint/index/model/manifest typed stage adapter
- [x] guarded `native` runner mode와 sample-enabled fail-closed 연결
- [x] pretrained resolver(version/sample rate/use_f0)
- [x] preprocess command builder
- [x] training F0 command builder 5종
- [x] v1/v2 HuBERT feature command builder
- [x] training command builder와 GPU ID 검증
- [x] stdout/train log/TensorBoard metric parser
- [x] v1/v2 deterministic FAISS index builder
- [x] `added_*.index`와 `total_fea.npy` version-aware 탐색
- [x] small model 검색과 checkpoint 단순 복사 방지
- [x] 공식 small model 추출 fallback 실행
- [x] G/D checkpoint 분리 탐색·수집
- [ ] 고정 테스트셋 sample inference 4종 F0
  - [x] inference resample을 `0` 또는 `16000..192000`으로 제한하는 공통 계약/API/Worker 검증
  - [x] index 미생성 sample Job의 `index_rate=0` 교차검증
  - [x] 고정 commit inference source hash와 `torch.load`/CVE 신뢰 경계 문서화
  - [x] lease-bound TestSet 계약/claim/item download와 typed atomic PCM materializer
  - [x] pinned PM/Harvest/RMVPE inference, canonical Artifact 뒤 Sample 등록과 Job completion gate
  - [x] 단일/attempt 총 출력 상한, PCM v2 지표, runtime manifest/request/역할 및 canonical byte 최종 재검증
  - [x] 49-case qualification 증적→builder-generated activation→고정 read-only mount→factory/capability
    연결과 증적 없는 release의 fail-closed 회귀
  - [ ] CREPE manifest-pinned offline asset과 no-network inference
    - [x] 고정 `runtime/crepe/full.pth` exact projection, path-free evidence, strict
      `weights_only=True` prebind와 cache/offline 환경 경계
    - [ ] 실제 weight의 출처·라이선스·SHA 승인과 OS egress 차단 GPU 실행 증명
  - [ ] Torch `>=2.6` 또는 safetensors 경로와 전체 GPU/no-network matrix 검증
    - [x] Torch 2.6.0/torchvision 0.21.0/torchaudio 2.6.0 cu124 후보 lock과 자산별 explicit
      `weights_only` 정책
    - [ ] reviewed amd64 base digest, 실제 RVC/GPU/no-network·취약점 matrix
- [x] artifact manifest/checksum/environment 기록

## 8. 대시보드

- [x] Next.js/React 프로젝트와 반응형 application shell
- [x] JWT 로그인과 인증/권한 shell
- [x] 관리자 사용자 생성·역할/활성 변경·비밀번호 재설정 화면과 same-origin BFF
  - [ ] 실제 browser에서 lifecycle, 기존 session 폐기, 반응형·keyboard·screen-reader E2E
- [x] Worker 목록/상태/GPU/heartbeat 화면
- [ ] Dataset 업로드/품질 보고서 화면
  - [x] HttpOnly BFF 기반 init/finalize/list/detail/delete와 private field projection
  - [x] chunk SHA-256, presigned/local PUT, progress/cancel/retry/429 UX
  - [x] status/file/duration/sample-rate/duplicate/rejected/decoder pending과 삭제 충돌 UX
  - [x] DatasetRead typed PCM sample-count 가중 clipping/silence/RMS 집계와 목록·상세 투영
  - [ ] 실제 browser↔MinIO 대용량 E2E와 반응형·keyboard·screen-reader 시각 QA
- [x] Experiment 생성과 동일 Dataset 다중 조건 Job 생성 화면
  - [x] ready/is_usable Dataset 선택, v1/v2·40k/48k·F0 matrix preview와 16개 상한
  - [x] HttpOnly BFF, exact body/path/query 검증, 기존 Job 이름 전체 조회와 부분 성공/429 UX
  - [x] exact current JobAttempt engine mode 표시와 Fake 결과 운영 오인 방지 경고
  - [x] 확정 행 보존·POST 응답 유실 분리와 Experiment terminal 제출 잠금
  - [x] Dataset/Experiment/Job 전체 bounded pagination과 10,000개 상한 fail-closed 표시
  - [x] GPU/no-network release matrix 미검증에 따른 auto sample 강제 비활성화 표시
- [x] Job 목록/status 화면
- [x] Job status filter와 검색 화면
- [x] Job 상세, 실시간 로그, loss와 GPU별 사용률/VRAM/온도·디스크 graph
  - [x] 최신 200개 metric tail API/BFF와 non-overlapping 15초 polling
- [x] checkpoint/model/index 다운로드
- [x] canonical 재검증형 sample voice player와 single Range/If-Range BFF
- [ ] Experiment 설정/지표/A-B sample 비교
  - [x] owner/admin 2~16 Job 선택, immutable config/current attempt timing·engine,
    allowlisted latest metric과 verified artifact availability를 제공하는 비교 API
  - [x] same-origin BFF와 2~16 Job 선택, 설정·학습 시간·current engine,
    global sequence metric graph·원장, 검증된 model/index/Sample 가용성 비교 화면
  - [x] 동일 TestSet item current-attempt Sample A/B player와 authoritative PCM metric/provenance
  - [x] Experiment description row-version PATCH와 참조 안전 delete 대시보드 UI/BFF
  - [x] real completed Run의 verified model/index candidate 등록과 explicit champion 승인·폐기 원장
  - [x] model registry same-origin BFF, Fake 차단, checksum/runtime provenance와 응답 유실 UX
  - [ ] 실제 browser/API Experiment mutation·Sample 비교·model registry response-loss/동시 promotion E2E
  - [ ] 실제 MinIO/S3 대용량 registry 재해시·tamper/outage와 PostgreSQL multi-replica promotion 경쟁
- [x] cancel/retry와 오류/빈 상태 UX
- [x] semantic 상태·focus style 정적 구현, frontend 단위 회귀와 production build 검증
- [ ] 전체 화면 실제 browser 반응형·keyboard·screen-reader 시각 QA

## 9. 배포와 설치 파일

- [x] Manager API/Frontend/Worker multi-stage Dockerfile
- [x] PostgreSQL, Redis, MinIO, MLflow, proxy 포함 Compose
- [x] service health check와 dependency readiness
- [x] MLflow UID/GID `10002`, read-only rootfs, capability/PID 제한과 health runtime smoke
- [x] root `0600` source secret의 API·maintenance·MLflow별 원자 runtime projection과 실제 entrypoint smoke
- [x] Manager/MLflow identity의 exact MinIO bucket policy와 broad policy 제거 smoke
- [x] TLS/domain 구성 예제
- [x] operator-owned `PUBLIC_SCHEME`, client scheme spoof 차단과 production Secure cookie/HSTS gate
- [ ] 외부 TLS 종단의 실제 인증서/Host/Secure cookie/HSTS clean browser 검증
- [ ] Worker container custom CA의 clean-host Manager·Object TLS 인수
  - [x] root-owned bounded certificate PEM, 고정 read-only mount와 start 전 재검증
  - [x] system trust+custom CA의 `CERT_REQUIRED`/hostname/TLS 1.2+ 공통 SSL context와
    Manager·Object transport, CA 누락/hostname mismatch handshake 회귀
  - [ ] clean Ubuntu Worker container에서 실제 사설 CA Manager/Object endpoint 연결
- [x] backup/restore/upgrade/rollback 스크립트
- [x] Ubuntu Manager 설치 패키지 빌더
- [x] Ubuntu NVIDIA Worker 설치 패키지 빌더
- [x] verified runtime 없는 native 선택과 unverified GPU 무확인 시작 차단
- [x] 편집 가능한 env flag 없이 runtime image/asset/49-case 증적에 결박된 Sample activation 경로
- [x] tar bundle, version manifest, 내부/외부 SHA-256 생성
- [x] image closure v2 exact role/archive/image identity, offline pull 차단과 start/rollback 재검증
- [x] bundle exact `SHA256SUMS`, installed mode `0444` `RELEASE_SHA256SUMS`와 release file/env closure
- [x] 최신 script/Compose/partial SBOM을 포함한 dev.2 Manager/Worker 개발 번들 생성·checksum 검증
- [x] d1e7a9c4f620 schema marker와 최신 infra/설정을 포함한 dev.4 개발 번들 생성·내외부 checksum 검증
- [x] e2f8b4c6a930 Dataset fence 설정을 포함한 dev.5 Manager/Worker 개발 번들 생성·내외부 checksum 검증
- [x] f9c4a7d2b610, image manifest v2, CREPE/Torch 2.6 후보를 포함한 dev.6 partial 개발 번들 검증
- [x] active oneshot 재시작과 Worker UID/GID 권한 보정을 포함한 dev.7 partial 개발 번들 검증
- [x] qualification/read-only activation/factory 경계를 포함한 dev.8 partial 개발 번들 검증
- [x] b4a91d7e2c63 사용자 lifecycle/3-Worker race 보정을 포함한 dev.9 partial 개발 번들 검증
- [x] ca8d3e7f4b10 live telemetry/watermark와 2 MiB ingest gate를 포함한 dev.10 partial 개발 번들 검증
- [x] trusted scheme, system metric hardening/tail polling을 포함한 dev.11 partial 개발 번들 검증
- [x] runtime secret projection, exact MinIO policy, authoritative engine mode를 포함한 dev.12 partial 개발 번들 검증
- [x] `d8f2a6c4b901` LUFS, strict installed release closure와 bundle-local 문서를 포함한 dev.13 partial 번들 검증
- [x] proxy/loopback publish/full-stack smoke와 component별 runbook을 포함한 dev.14 partial 번들 검증
  (Manager `83ae2b7a9ec3d0f99175520ad781223314c7b677bc2ec694b43a2b675a356d70`,
  Worker `792b2bdf4007509ea301a469abfd82683fa029e363023d980dd4122392b18d7b`)
- [x] source ignore/image user closure, checksum fail-closed, forward-only atomic activation과 uninstall
  실패 전파를 포함한 dev.15 partial 번들 검증
  (Manager `a0c18bc938d3ca82c1995f1100dfa7a8d5e094fb5311332e57820ecad3c3e0aa`,
  Worker `4a1d942abadc86f4ef8df89d260f03a85fdac81ff4f6357b1cb27fe9524ae7d5`)
- [x] physical installed-release 검증 runbook, 결과 템플릿, MLflow overlay lock과 release-readiness
  도구를 포함한 dev.16 partial 번들 검증
  (Manager `9a520623010a4e640e9975bc87835640de8f7ac127830ec9d9106ce7d2939f26`,
  Worker `105971694bed766ea3ae4d7c58ec27db49aa4246e3db0a83988f598e2064d612`)
- [x] Experiment 비교 BFF/UI, Worker custom CA, fail-fast/fixed-hash/negative runbook을 포함한
  dev.17 partial 번들의 외부/`SHA256SUMS`/strict manifest 검증
  (Manager `b131698fbdeb51887d808f1396323b9a0e37ef6495445e60eadbedc024b95b96`,
  Worker `a4b2951b7f210501e73f2d9ab1b6fb9d78c6ce8f93aed26b59b83d898a4883e7`)
- [x] `e4c7b9d2f610` model registry 원장/API/BFF/UI를 포함한 dev.18 partial 번들의
  외부 sidecar·내부 `SHA256SUMS`·strict ledger/bundle 검증
  (Manager `83de04e5d8e5fb5a4fecb041fec2e6a6aa08a14aa04622f5a36a1b3ba6e484b7`,
  Worker `6e631c9f49dd62f06d9132f55ee728364eef0d08894059cd67fc3b2f6b63b1a8`)
- [x] `f5d1c8a9b240` maintenance 최소권한 migration/Compose/installer를 포함한 dev.19 partial
  번들의 외부 sidecar·내부 `SHA256SUMS`·strict ledger/bundle 검증
  (Manager `6c76684c640b92e3cc6aa9ee74f1514a81409d6d20ae71bb46183d32eb899393`,
  Worker `fd63d579dcc8199463a9d0f1d70b2b18ba7f1e7b78a21b6e86f8e8629c2a8f99`)
- [x] partial 제약, exact image 준비, TLS와 `--no-start` 순서를 포함한 Manager/Worker 설치 가이드
- [x] 고정 archive hash 신뢰 anchor, fail-fast, `MANAGER-CONFIG`, secret pre-state와
  Worker native negative 후 불변 확인을 포함한 사용자 인수 runbook
- [x] clean committed source와 exact 8-role linux/amd64/user/label을 강제하는 Manager
  self-contained release orchestrator
- [ ] application image와 Torch `>=2.6`/CREPE/GPU 검증 runtime을 포함한 self-contained 최종 번들
- [x] 재설치/업그레이드 시 설정과 데이터 보존
- [x] Worker upgrade release-owned env 원자 갱신과 사용자 설정/ack 보존
- [x] Manager/Worker target Compose prevalidation, strict SemVer 역행 차단과 activation 전 실패 시
  기존 env/current 보존 회귀
- [x] Manager/Worker uninstall systemd/Compose 일부 실패의 nonzero 전파와 false-success 방지
- [ ] clean Ubuntu VM Manager 설치 smoke test
- [ ] clean GPU VM Worker 설치와 sample job smoke test

## 10. 품질과 출시

- [x] backend 단위/SQLite 통합 테스트
- [x] worker 단위/contract 테스트
- [x] frontend unit 테스트와 인증 route 회귀 테스트
- [x] Manager↔Fake Worker 실제 HTTP protocol E2E 테스트
- [x] 동일 Dataset의 세 조건 Job을 세 실제 WorkerAgent가 동시에 claim·완료하는 HTTP E2E
- [x] 고유 project/동적 loopback을 사용하는 Manager 전체 Compose smoke harness 구현
- [x] dev.14 source의 localhost HTTP E2E 재실행 증적(`4 passed`)
- [x] dev.15 source의 전체 검사(`691 passed, 4 deselected`, mypy 82, Web 19/162)와 localhost
  HTTP E2E 재실행 증적(`4 passed`)
- [x] dev.16 source의 전체 검사(`712 passed, 4 deselected`, mypy 84, Web 19/162)와 localhost
  HTTP E2E 재실행 증적(`4 passed`)
- [x] dev.17 source의 전체 검사(`720 passed, 4 deselected`, mypy 85, Web 21/181)와
  sandbox bind 제한 후 localhost HTTP E2E 재실행 증적(`4 passed in 5.80s`)
- [x] dev.18 model registry source의 전체 검사(`735 passed, 4 deselected`, mypy 87,
  Web 24/211)와 sandbox bind 제한 후 localhost HTTP E2E 재실행 증적(`4 passed`)
- [x] dev.19 maintenance 최소권한 source의 전체 검사(`749 passed, 4 deselected`, mypy 88,
  Web 24/211), localhost HTTP E2E(`4 passed`)와 결합 집중 회귀(`124 passed`)
- [x] model registry+migration 집중 회귀(`33 passed`: registry 14, migration 19),
  API+Experiment comparison(`29 passed`)과 전체 API suite(`271 passed`)
- [x] Manager 전체 Compose smoke의 실제 runtime PASS 증적(arm64 개발 host; clean amd64는 별도 gate)
- [ ] PostgreSQL/Redis/MinIO 장애 복구 테스트
- [ ] 운영 PostgreSQL/MinIO upgrade drain과 historical `UNBOUND` storage adoption drill
- [x] partial dependency SBOM과 declared-license 보고서 자동 생성
- [x] Worker source/wheel/asset/runtime/49-case/scan·license evidence를 비활성 상태로 열거하는
  read-only release-readiness report
- [ ] vulnerability/container/secret 검사와 법적 license 검토
- [ ] 성능 기준(대형 업로드, 로그/metric ingestion) 검증
- [x] 운영자/사용자/Worker 설치 문서
- [x] 자동/HTTP/설치/GPU 단계별 사용자 테스트와 redacted 증적 수집 가이드
- [x] 환경별 최소 인수 묶음과 PASS/FAIL/BLOCKED를 기록하는 사용자 테스트 결과 템플릿
- [x] 알려진 제한과 RVC upstream 호환표
- [ ] tracked Git revision에서 whitespace와 release source provenance 재검증(현재 tracked file 0개)
- [ ] v1.0.0 release note와 두 설치 파일 checksum 검증

## 11. 장기 운영 고도화

- [x] 사용자 역할, audit 원장과 upload session 단위 quota
- [ ] 사용자/프로젝트별 canonical Dataset·Artifact·Sample storage quota와 retention 정책
- [ ] 검증된 G/D checkpoint에서 **새 attempt**로 시작하는 명시적 resume protocol
- [x] Worker version/capability 보고, 대시보드 표시와 설치 bundle upgrade 경계
- [ ] Job terminal/Worker offline/storage quota notification 채널과 멱등 outbox
- [ ] 검증 모델을 후보/승인/폐기 상태로 관리하는 model registry와 promotion audit
  - [x] exact current real attempt, reviewed commit/승인 runtime과 canonical model/index 재검증형 candidate
  - [x] candidate→approved→revoked, active champion 0/1, inactive approved rollback과 no-fallback 상태 원장
  - [x] owner/admin fence, registry/entry CAS, actor-scoped 멱등 원장과 secret/storage-free audit
  - [x] same-origin BFF/UI, Fake action 차단, checksum/runtime provenance와 uncertain-response 잠금
  - [ ] 실제 PostgreSQL multi-replica 경쟁, MinIO/S3 대용량 재해시·장애와 browser/API 인수

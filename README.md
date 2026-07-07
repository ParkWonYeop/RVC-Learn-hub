# RVC Training Orchestrator

여러 GPU 학습 서버에서 RVC v1/v2 학습을 수행하고 중앙에서 데이터셋, 작업, 로그, 지표, 모델, 인덱스와 샘플 음성을 관리하는 플랫폼이다.

## 구성

- **Manager**: FastAPI API, Next.js 대시보드, PostgreSQL, Redis, MinIO/S3, MLflow, reverse proxy
- **Worker**: GPU 상태 수집, 작업 수신, RVC 명령 실행, 로그/지표 파싱, 결과물 업로드
- **Deployment**: 중앙 서버와 학습 서버용 Docker 구성 및 Linux 설치 패키지

현재 구현 진행 상황은 [CHECKLIST.md](CHECKLIST.md), 변경 이유와 검증 내역은 [docs/DEVELOPMENT_HISTORY.md](docs/DEVELOPMENT_HISTORY.md)에서 확인한다. 모든 작업자는 먼저 [AGENTS.md](AGENTS.md)를 읽어야 한다.

실행형 설치 절차는 [docs/INSTALLATION_GUIDE.md](docs/INSTALLATION_GUIDE.md), 단계별 합격 기준과
증적 수집은 [docs/TEST_GUIDE.md](docs/TEST_GUIDE.md)를 따른다.
[docs/TEST_RESULT_TEMPLATE.md](docs/TEST_RESULT_TEMPLATE.md)는 사용자가 실행 결과를 기록해
전달할 때 복사해 쓰는 양식이다. 패키징·배포 상세는
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md), 역할별 운영 runbook은
[docs/OPERATIONS_GUIDE.md](docs/OPERATIONS_GUIDE.md), 자동 테스트 범위는
[docs/TESTING.md](docs/TESTING.md)에 유지한다. RVC/CUDA 호환 출시는
[docs/RVC_RUNTIME_MATRIX.md](docs/RVC_RUNTIME_MATRIX.md), 보안 기본값은
[docs/SECURITY.md](docs/SECURITY.md), SBOM과 라이선스 report의 증명 범위는
[docs/SUPPLY_CHAIN.md](docs/SUPPLY_CHAIN.md)를 따른다. 실제 GPU 결과를 capability로 연결하는
증적 schema와 절차는 [docs/RUNTIME_QUALIFICATION.md](docs/RUNTIME_QUALIFICATION.md)에 있다.

## 핵심 원칙

- 중앙 Manager는 RVC 학습을 직접 실행하지 않는다.
- Worker는 RVC WebUI 저장소를 외부 학습 엔진으로 감싸며 v1/v2 산출물 차이를 명시적으로 처리한다.
- 학습용 F0 방식과 샘플 생성용 F0 방식을 분리한다.
- `G_*.pth`/`D_*.pth` 체크포인트와 배포용 small model을 구분한다.
- 업로드된 데이터셋은 검증하고 평탄화한 뒤 안전한 파일명으로 RVC에 전달한다.

## 개발 상태

Manager의 인증/원장, 검증형 Dataset·Artifact data plane, lease 기반 Worker protocol,
실시간 관측 대시보드, MLflow durable projection, canonical Dataset Worker 전송과 pinned RVC
typed adapter의 guarded `native` 실행 경계, immutable TestSet/Preset/Sample 원장, 검증형
고정 WAV data plane, lease-bound TestSet 수신과 canonical Sample 등록/completion gate까지
구현되어 있다. Native에는 pinned RVC Pipeline 기반 PM/Harvest/RMVPE와 고정
`runtime/crepe/full.pth`를 `weights_only=True`로 strict prebind하는 CREPE 고정 TestSet 추론,
결정적 manifest/publication 경계가 연결돼 있다. CREPE byte는 asset/projection manifest의
size·SHA-256과 attempt-private exact inventory에 결박되고 request에는 host path를 싣지 않는다.
Manager는 approved runtime digest 쌍,
model/index/output 역할과 manifest/request SHA-256, 현재 canonical byte/PCM 지표를 완료 직전에
다시 검증한다. TestSet PUT/finalize는 generation-token heartbeat와 절대 deadline을 사용하고,
전용 RQ maintenance가 유효 grace 뒤 staging을 두 번 확인 삭제한다. Worker token은 응답 유실을
복구하는 idle-only 2단계 회전, 관리자 긴급 폐기와 inactive-only 동일 identity 재등록을 제공한다.
Experiment는 immutable name/Dataset 위에서 description row-version CAS와 참조 안전 삭제를
제공하며, 대시보드는 해당 수정/삭제 BFF와 검증된 Sample 재생·동일 TestSet item 기준 A/B 비교를
제공한다. Model registry는 exact current real `rvc_webui` attempt의 reviewed commit·승인 runtime
provenance와 Manager가 재해시한 canonical model/index만 candidate로 받고,
`candidate -> approved -> revoked` 상태, Experiment별 active champion 0/1과 이전 approved 모델의
rollback promotion을 PostgreSQL 원장에 보존한다. Owner/admin CAS·hashed idempotency·audit API와
same-origin BFF/UI는 Fake 결과의 후보 등록을 차단하고 checksum/runtime provenance를 표시한다.
Dataset raw PUT/finalize도 generation heartbeat, upload-session별 canonical namespace와
확인형 이중 staging cleanup으로 늦은 writer/finalizer를 replacement 세대와 격리한다.
Dataset 품질 응답은 내부 raw report를 노출하지 않고 `pcm-sample-weighted-v1`의 exact interleaved
sample count 가중 clipping/silence와 raw square-sum RMS, typed issue count만 반환한다. exact 표본 수가
없는 기존 행은 값을 만들지 않고 `null`로 유지한다. Integrated loudness도
`itu-r-bs1770-4-mono-stereo-v1`과 Dataset-global 2단계 gate로 계산하며, 계산할 수 없는 입력과
migration 전 행은 숫자를 추정하지 않고 typed reason 또는 historical `null`로 구분한다.
관리자는 `/users` 화면에서 계정을 생성하고 역할·활성 상태를 row-version CAS로 변경하거나
비밀번호를 재설정할 수 있다. 마지막 활성 관리자와 자기 계정 강등/비활성화는 차단되고,
권한·활성·비밀번호 변경은 이전 access token을 영구 무효화한다. 실제 HTTP E2E는 세 Worker
Agent가 같은 Dataset의 서로 다른 F0 조건 Job을 동시에 claim·완료하는 경로와 heartbeat/terminal
Worker 행 충돌의 bounded fence 재검증을 포함한다.
Active Job은 시작 직후와 기본 60초 간격의 GPU별 사용률·VRAM·온도 및 남은 disk를 loss/epoch와
같은 durable attempt metric sequence에 저장한다. GPU query 실패와 실제 0-GPU를 availability로
구분하고, Job 상세는 최신 200개를 15초 간격으로 갱신해 terminal watermark까지 연결한다.
Manager/Worker bundle builder, Manager backup/restore/rollback과 partial SBOM도 제공한다. Installer image
closure v2는 self-contained Manager의 정확한 8개 image와 Worker runtime 1개를 archive byte,
image/config digest, linux/amd64, release label에 결박하고 load 전·후와 start/rollback 시 다시
검증한다. 설치 파일은 strict closed-world checksum inventory로 추가·누락 파일도 거부하며,
설치된 release에는 mode `0444` `RELEASE_SHA256SUMS`를 생성해 start/restart/rollback 전에 다시
검증한다. 개발 번들은 이 계약을 사용하되 image가 없는 `self_contained=false`로 명시된다.
Worker Sample activation은 49개 GPU/no-network report archive와 exact runtime image/build/asset
identity를 검증한 builder만 mode `0444` projection으로 생성할 수 있다. Compose는 projection을
고정 read-only 경로에 mount하며 production factory와 Agent capability는 실제 binding evidence가
있을 때만 네 inference F0 방식을 연다. 사용자 env/YAML/CLI flag로 이 경계를 열 수 없다.

아직 v1.0은 아니다. Dataset finalize RQ/tombstone maintenance 확대, non-WAV
sandbox decoder, 실제 CREPE weight의 출처·라이선스·SHA 승인, Torch 2.6.0/cu124 후보 runtime의
reviewed base digest·전체 GPU/no-network matrix와 실제 browser Experiment mutation/Sample 비교 E2E,
관리자 사용자 lifecycle의 session 폐기·반응형·keyboard/screen-reader browser E2E,
model registry의 실제 browser/API response-loss·동시 promotion E2E, 실제 PostgreSQL 다중 replica
promotion 경쟁과 MinIO/S3 대용량 canonical 재해시·tamper/outage 인수, 장기 S3 PUT 및 MinIO 장애 주입,
clean Ubuntu/NVIDIA GPU smoke, 완전한 SBOM/취약점·라이선스 검토가 출시 gate로 남아 있다.
따라서 현재 증적 없는 개발 bundle의 Agent는 `fixed_test_set_inference_ready=false`를 광고하고
운영 auto-sample gate는 닫힌 상태다.
`CHECKLIST.md`에서 검증된 항목만 완료로 취급하며, 개발용 Fake 결과나 GPU를 쓰지 않은
fixture 결과를 실제 RVC 학습 인증으로 해석하지 않는다.

## 현재 검증된 개발 설치 번들

dev.19 maintenance 최소권한 source와 schema head `f5d1c8a9b240`을 포함한 partial archive를 생성하고
외부 sidecar, 내부 exact `SHA256SUMS`, strict ledger/bundle 검증까지 완료했다. 두 archive는 여전히
`GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`, empty image inventory이며 Worker activation의 모든
runtime/GPU/profile/Sample gate가 false이므로 self-contained 또는 production 설치 파일이 아니다.

- `dist/installers/rvc-manager-0.1.0-dev.19-linux-amd64.tar.gz`
  (`6c76684c640b92e3cc6aa9ee74f1514a81409d6d20ae71bb46183d32eb899393`)
- `dist/installers/rvc-worker-0.1.0-dev.19-linux-amd64.tar.gz`
  (`fd63d579dcc8199463a9d0f1d70b2b18ba7f1e7b78a21b6e86f8e8629c2a8f99`)

각 archive 옆의 `.sha256`, archive 내부 `SHA256SUMS`의 exact inventory와 image manifest v2가
검증됐다. 압축을 푼 bundle에는 현재 version에 맞춘 `README.md`, `TESTING.md`와
`TEST_RESULT_TEMPLATE.md`가 있으며
`python3 common/image_bundle.py verify-ledger --root . --ledger-name SHA256SUMS`로 누락뿐 아니라
추가 파일도 확인한다. dev.12는
dev.11의 trusted HTTPS와 system metric 경계에 더해 root 소스 secret을 API·maintenance·MLflow별
비루트 runtime volume으로 원자 투영하는 초기화 단계, Manager/MLflow의 exact MinIO bucket policy,
JobAttempt 기반의 authoritative engine mode와 Fake 결과 경고 UI를 포함한다.
dev.13은 이 dev.12 경계를 보존하면서 Dataset integrated LUFS, strict 설치 release file/environment
closure와 bundle-local 설치·시험 문서를 추가했고, dev.14는 proxy foreground 실행,
MinIO/MLflow loopback publish network와 실제 Manager 전체 Compose smoke를 보강했다. dev.15는
release source ignore closure, Docker config byte/digest와 application `Config.User` 결박,
누락된 bundle checksum ledger 거부, forward-only upgrade와 전환 전 Compose 검증, uninstall 실패
전파를 추가했다. dev.16은 이 경계를 유지하면서 physical installed-release 검증 runbook,
bundle-local 결과 템플릿, exact MLflow overlay lock, Manager self-contained release orchestrator와
Worker read-only release-readiness report를 추가했다. dev.17은 Experiment 비교 BFF/UI source,
Worker custom CA installer와 fixed read-only mount/공통 strict SSL context, bundle-local native
negative runbook과 설치 가이드의 fail-fast·고정 hash·config-only·secret pre-state 보정을 추가했다.
dev.18은 이 경계를 보존하면서 model registry 원장/API/BFF/UI와 `e4c7b9d2f610` migration을
추가했다. dev.19는 전용 maintenance PostgreSQL/Redis/S3 identity, 장기 cleanup heartbeat와
`f5d1c8a9b240` parent-lock 함수 migration을 추가했다.
dev.15의 내장 runbook은 `current` symlink를 verifier root로 넘기는 명령 오류가 있고 dev.16은
custom CA와 이번 runbook audit 보정이 없으므로 새 설치·시험에는 dev.19을 사용한다. dev.14 이하는
code guard 자체도 없다. dev.19 파일은 설치·업그레이드·백업·복구 스크립트, Compose/프록시 구성,
Torch 2.6/cu124 후보 lock과
partial SBOM/license report를 담지만 application image와 검증된 RVC/CUDA runtime image는
포함하지 않는다. 두 manifest는 `SELF_CONTAINED=false`이고 image/archive inventory가 비어 있다.
두 manifest의 `GIT_COMMIT=uncommitted`도 production source provenance를 제공하지 못한다.
따라서 air-gapped 최종 설치 파일이나
production release가 아니며, clean Ubuntu/NVIDIA VM 및 실제 GPU matrix를 통과하기 전에는
`v1.0.0`으로 이름을 바꾸거나 release gate 값을 수정하면 안 된다.
현재 checkout에서 수동 빌드한 image와 dev.19 installer를 조합한 결과도 `SOURCE-MIXED` 기능
시험일 뿐이다. Trusted scheme 구현은 완료됐지만 clean browser/TLS 종단 검증 전 production
TLS 판정은 계속 차단한다.

2026-07-13 dev.19 source의 `make check`는 Python `749 passed, 4 deselected`, strict mypy
88 source files, Web 24 files/211 tests, Ruff, ESLint와 Next.js production build를 통과했다.
Localhost HTTP E2E는 승인된 socket 환경에서 `4 passed`였다. Maintenance/installer/migration
결합 회귀는 `124 passed`, 실제 PostgreSQL 16 role/function/negative/dry-run smoke와 Redis 7.4,
MinIO, secret projection 및 전체 Manager Compose smoke가 PASS했고 Alembic head는
`f5d1c8a9b240`이다. Full Compose 증적은 `docker_architecture=arm64`이므로 최종 amd64 증거가 아니다.
현재 Git tracked inventory가 0개이므로 이 결과는 executable source 증거이며
`git diff --check` 기반 whitespace와 committed source provenance는 계속 `BLOCKED`다.
정확한 SHA-256, 선행 image 준비와 현재 가능한 시험 범위는
[설치 가이드](docs/INSTALLATION_GUIDE.md)에서 먼저 확인한다.

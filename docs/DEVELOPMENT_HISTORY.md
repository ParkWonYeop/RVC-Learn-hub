# 상세 개발 이력

이 문서는 변경 사실뿐 아니라 그 이유, 검증 결과와 남은 위험을 후속 작업자에게 전달한다. 최신 날짜의 항목을 위에 추가하고, 같은 날짜 안에서는 최신 작업을 위에 둔다.

## 2026-07-13

### dev.20 준비 — committed source에서 TypeScript cache를 release 입력과 분리

**목적과 변경 범위**

- 복원된 `reconstructed/main`의 clean commit `a9b3f2bebb07d2d83e19c6928c269a56a66b0a90`과
  409개 tracked file을 기준으로 self-contained Manager release preflight를 다시 실행했다.
  정상적인 Next.js/TypeScript 검사 뒤 생성되는 `apps/web/tsconfig.tsbuildinfo`가 로컬
  `.git/info/exclude`에만 있어 source closure 검증이 중단되는 문제를 재현했다.
- `.gitignore`와 `.dockerignore`에 `*.tsbuildinfo`를 명시하고
  `tools/verify_release_source.py`가 regular non-symlink 여부를 먼저 검사한 뒤 이 생성 cache만
  release source 후보에서 제외하도록 했다. 실제 application source가 ignore rule에 가려지면
  실패하는 기존 closed-world 검사는 유지한다.
- Partial bundle 회귀가 repository에 commit이 생긴 뒤에도 `--source-commit uncommitted`를
  고정해 실패하던 fixture를 bundle의 실제 `manifest.env` provenance로 검증하도록 수정했다.
  이는 product verifier를 완화하지 않고, partial bundle도 이용 가능한 exact Git commit을
  기록하는 현재 builder 계약을 테스트가 따르게 한 변경이다.
- 이 개발 호스트처럼 Buildx plugin이 없는 Docker에서도 표준 builder가 제공하는
  `docker build --platform linux/amd64`를 사용하도록 Manager release orchestrator에 명시적 fallback을
  추가했다. Buildx가 있으면 기존 `buildx build --load`를 그대로 사용하며, 어느 경로든 build 뒤
  8개 image의 실제 OS/architecture, application UID와 release label 검증을 동일하게 통과해야 한다.

**검증과 남은 범위**

- `python3 tools/verify_release_source.py --repo-root .`은 생성된 `tsconfig.tsbuildinfo`가 존재하는
  상태에서 `Release source ignore closure verified (409 files)`로 PASS했다.
- source closure, Manager self-contained orchestrator, image closure와 installer activation 집중
  Pytest를 통과했고 `git diff --check`도 PASS했다. 첫 시도는 존재하지 않는 과거 파일명
  `tests/infra/test_image_bundle.py` 때문에 collection 단계에서 종료됐으며, 실제
  `test_image_bundle_closure.py`로 바로잡은 첫 실행에서는 위 `uncommitted` fixture 불일치를
  발견해 수정 후 재실행했다.
- Buildx/표준 Docker build 두 경로와 deployment contract의 집중 회귀도 PASS했고
  `bash -n installers/manager/build-self-contained-release.sh`를 통과했다. 실제 Docker 29 client에서
  Buildx 명령은 부재하지만 `docker build --help`의 `--platform` 지원을 확인했다.
- 이 변경은 source preflight 한 단계만 복구한다. 실제 linux/amd64 8-image Manager archive 생성,
  dependency image의 비어 있는 `Config.User` inspect 처리, clean Ubuntu 설치와 Worker CUDA/RVC
  runtime qualification은 별도 release gate로 남는다.

### dev.19 maintenance PostgreSQL·Redis·S3 최소권한과 partial 설치 번들

**목적과 변경 범위**

- `CHECKLIST.md`의 열린 항목이던 maintenance 전용 PostgreSQL role, staging-prefix delete-only S3
  IAM과 Redis ACL을 구현했다. API application credential을 RQ process에서 재사용하지 않고,
  installer가 새 `maintenance_postgres_password`, `maintenance_redis_password`,
  `maintenance_s3_access_key`, `maintenance_s3_secret_key`를 root-owned mode `0600` source로 만들게
  했다. `infra/runtime/manager-secrets-init.py`는 API, maintenance, MLflow와 database-authz 네
  profile을 UID/GID·mode `0400` generation으로 원자 투영한다. Maintenance 값이 대응 API 값과
  같으면 새 generation 게시 전 실패하고 이전 generation을 보존한다.
- `apps/api/alembic/versions/f5d1c8a9b240_maintenance_db_least_privilege.py`로 schema head를
  `f5d1c8a9b240`으로 올렸다. 두 `SECURITY DEFINER` 함수는 server-generated upload ID에서
  Dataset/TestSet parent를 유도하고 parent row를 잠근 뒤 child-parent binding을 다시 확인한다.
  `infra/runtime/maintenance-db-authz.py`는 `rvc_maintenance` login과 `NOLOGIN` function owner를
  만들고 role membership, database/schema/sequence/table·column·function ACL을 exact allowlist로
  재적용한다. Restore로 주입된 다른 role의 EXECUTE와 PUBLIC 권한도 제거하며, projected secret의
  UID/GID `10001:10001`, mode `0400`, regular/non-symlink/size/NUL을 확인한다. RQ 시작 전
  `verify-runtime`은 main PostgreSQL password 없이 maintenance login 자체로 같은 경계를 검사한다.
- `services/maintenance.py`는 PostgreSQL에서 parent table을 직접 SELECT/UPDATE하지 않고 위 함수를
  호출하며, maintenance run/session을 `load_only` exact column으로 읽는다. Exact run ID·attempt·
  `running` CAS heartbeat를 기본 15초마다 별도 DB session으로 갱신한다. Parent/session lock, S3
  delete가 오래 걸릴 때도 guard가 heartbeat를 유지하고 ownership을 잃으면 in-flight operation을
  취소한다. Confirmation wait도 같은 bounded pulse를 사용한다.
- `infra/minio/init.sh`는 `rvc-maintenance-staging-cleanup` identity에 Manager bucket의
  `datasets/staging/*`, `test-sets/staging/*` `DeleteObject`만 허용한다. List/Get/Put, canonical과
  MLflow bucket 접근은 거부하고 broad policy를 재실행 때 제거한다. Bucket versioning이 활성화돼
  delete marker만 생길 수 있으면 exact cleanup 의미가 아니므로 fail-closed한다.
- `infra/redis/entrypoint.sh`는 별도 `rvc_maintenance` ACL user를 만들고 고정 RQ queue/job/WIP/
  execution/result/worker/scheduler와 `rvc:maintenance:*` key, 실제 RQ 2.6.1 lifecycle command만
  허용한다. `rq_worker.py`는 Redis pub/sub와 global suspension, callback/dependent/repeat를 실행할
  수 있는 generic registry cleanup을 제거하되 죽은 bounded-retry scheduler 재획득은 유지한다.
  `maintenance_queue.py`는 inactive poison에 WIP/execution/result material이 남아 있으면 deterministic
  job ID를 재사용하지 않고 `maintenance_queue_poisoned_execution`으로 닫는다.
- `infra/compose/manager.compose.yml`, RQ entrypoint, Manager installer/wrapper/recovery fixture와
  bundle-local 문서를 네 secret profile과 migration→`maintenance-db-authz`→RQ 순서에 맞췄다.
  Installed `start|restart`는 service-scoped 인자를 거부하고 full `up --force-recreate`로 모든 access
  initializer를 다시 통과한다. `.env.example`, `README.md`, `CHECKLIST.md`, 아키텍처/보안/배포/
  설치/운영/테스트/runtime matrix/요구사항 추적 문서와 `dist/installers/README.md`를 같은 계약으로
  갱신했다.

**변경 파일과 검증**

- 주요 source는 migration, `config.py`, `services/maintenance.py`, `maintenance_queue.py`,
  `rq_worker.py`, `infra/runtime/maintenance-db-authz.py`, Manager secret projection/Compose/entrypoint,
  MinIO/Redis init, Manager installer와 recovery scripts다. 새/확장 테스트는
  `test_maintenance_db_authz.py`, `test_maintenance_redis_safety.py`, migration/maintenance 회귀,
  deployment/installer/recovery fixture, secret projection, Redis ACL, MinIO policy와 Manager full
  stack smoke다. `Makefile`에 `test-redis-acl-docker`를 추가하고 새 authz script를 Ruff/mypy 전체
  대상에 포함했다.
- `.venv/bin/pytest -q tests/infra/test_deployment_config.py ... apps/api/tests/test_migrations.py`의
  maintenance/installer/migration 결합 124건이 PASS했다. 전체 `make check`는 Ruff, strict mypy
  `88 source files`, Python non-E2E `749 passed, 4 deselected`, Web Vitest `24 files/211 tests`,
  ESLint와 Next.js production build를 통과했다. 첫 전체 실행에는 기존 TestSet cleanup fixture의
  SQLAlchemy connection GC warning 1건이 비결정적으로 나타났고, 최종 전체 재실행은 같은 749건과
  warning 없이 exit 0이었다. 첫 warning도 삭제하거나 숨기지 않고 이력에 남긴다.
- 승인된 loopback 환경의 `make test-e2e`는 `4 passed in 5.47s`였다.
- 실제 PostgreSQL 16에서 full migration→authz apply/self-verify, 임의 restore role function grant
  주입→reapply 제거, maintenance login의 users SELECT/DELETE/parent UPDATE/canonical SELECT 거부와
  허용된 Dataset cleanup dry-run을 확인했다. `make test-redis-acl-docker`,
  `make test-minio-policy-docker`, `make test-manager-secret-projection-docker`도 실제 pinned server와
  negative case를 포함해 PASS했다.
- 최신 `make test-manager-full-stack-docker`는 projection→migration→DB authz→RQ 순서, DB runtime
  self-verify, Redis ACL, MinIO exact policy와 API/RQ/Web/MLflow/proxy readiness를 함께 통과했고 마지막
  문자열은 `Manager full Compose stack smoke: PASS (docker_architecture=arm64)`였다.
  종료 뒤 exact project filter의 container/volume/network 조회는 모두 비어 있었다.
- dev.19 partial archive를 생성했다. Manager
  `rvc-manager-0.1.0-dev.19-linux-amd64.tar.gz` SHA-256
  `6c76684c640b92e3cc6aa9ee74f1514a81409d6d20ae71bb46183d32eb899393`, Worker
  `rvc-worker-0.1.0-dev.19-linux-amd64.tar.gz` SHA-256
  `fd63d579dcc8199463a9d0f1d70b2b18ba7f1e7b78a21b6e86f8e8629c2a8f99`이다. 첫 sidecar 명령은
  repository root에서 실행해 sidecar 내부 basename을 찾지 못했으며 archive byte 실패가 아니었다.
  Sidecar가 있는 `dist/installers`에서 재실행한 외부 checksum, 압축 해제 후 내부 `SHA256SUMS`,
  `verify-ledger`, strict `verify-bundle`, symlink/host-cache 부재를 모두 확인했다. Manager marker는
  `f5d1c8a9b240`; 두 manifest는 `GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`, empty image
  inventory이며 Worker runtime/GPU/profile/Sample gate는 모두 false다. 이후 사용자 결과 템플릿의
  `T2-MAINT` 행을 archive에도 포함하기 위해 두 bundle을 재생성했고, 위에 기록한 hash는 최종
  재생성 byte를 다시 검증한 값이다.

**남은 위험과 출시 gate**

- 이번 분리는 RQ maintenance identity를 API에서 격리했다. Redis default/operator identity는 API,
  readiness, restore 용도로 여전히 넓으므로 rate-limit, queue writer, readiness와 operator identity의
  추가 분리 및 실제 다중 replica·Redis/PostgreSQL/외부 S3 restart/timeout/partition 장애 주입이
  남아 있다. 전체 stack `start|restart`의 강제 recreate 운영 영향과 구 release rollback 시 공유
  credential 회귀를 change/rollback 절차에서 계속 차단해야 한다.
- Full Compose와 개별 storage smoke는 로컬 arm64 Docker 증거다. Clean Ubuntu linux/amd64 설치·
  upgrade/rollback, 외부 TLS/browser와 실제 large-object outage 인수는 별도 gate다.
- dev.19도 application/dependency image와 Worker RVC/CUDA runtime이 없는 partial이다. 실제 GPU
  49-case/no-network, base digest, vulnerability/container/secret scan, 완전한 SBOM/법적 license
  검토와 tracked clean Git revision 없이는 production/air-gapped/v1.0 release가 아니다.
- Repository tracked inventory는 계속 0개이고 모든 파일이 untracked다. `git diff --check`가 tracked
  whitespace/source provenance를 증명하지 못하므로 실행 테스트 PASS를 committed source 증거로
  확대하지 않는다.

## 2026-07-12

### dev.18 검증 모델 registry, explicit champion 원장과 partial 설치 번들

**목적과 변경 범위**

- 검증된 학습 결과를 비교 화면에 표시하는 데서 끝내지 않고 운영자가 후보 등록, 명시적 승인,
  폐기와 rollback promotion을 감사 가능한 PostgreSQL 원장으로 관리하도록 model registry를
  구현했다. `apps/api/alembic/versions/e4c7b9d2f610_model_registry.py`는 기존
  `JobAttempt` provenance를 임의 backfill하지 않은 채 registry/entry/operation table과 FK·unique·
  state constraint를 추가하며 schema head를 `e4c7b9d2f610`으로 올린다.
- Candidate는 exact current `completed` Job/attempt의 real `rvc_webui` 실행만 허용한다.
  `worker-claim-v1`, reviewed RVC commit과 승인 runtime image/asset digest 쌍, Manager가 검증한
  유일한 `final_small_model`과 같은 attempt의 0/1 `final_index`를 요구한다. Candidate 생성과 모든
  promotion은 DB write fence 전에 canonical object 전체를 size/SHA-256으로 다시 읽고, fence 뒤 fresh
  ledger fingerprint와 비교해 Fake, historical NULL, stale attempt, runtime 미승인과 byte 변조를
  fail-closed한다.
- `candidate -> approved -> revoked` 상태와 revoked terminal, Experiment별 active champion 0/1,
  새 champion 승인 뒤 이전 approved entry의 inactive 보존, 해당 entry의 rollback promotion과 active
  revoke 뒤 no-fallback을 구현했다. Experiment write fence 뒤 actor의 active/token-version/owner-admin
  권한을 다시 확인하고 registry/entry row-version CAS와 actor-scoped hashed idempotency/keyed
  fingerprint를 사용한다. Response·audit·operation ledger에는 원문 key, storage URI/object key,
  upload session과 raw metadata를 넣지 않으며 MLflow는 계속 파생 projection으로만 취급한다.
- Owner/admin API와 cookie-only same-origin BFF, Experiment 상세의 governance panel을 연결했다.
  Browser는 Artifact index나 runtime 값을 선택하지 않고, Fake 실행에는 candidate action이 없으며
  candidate/active champion/inactive approved/revoked 상태와 전체 checksum/runtime provenance를
  표시한다. Mutation 결과가 불명확하면 blind retry를 막고 registry 전체 재조회로 조정한다.
- `README.md`, `AGENTS.md`, `CHECKLIST.md`, `docs/ARCHITECTURE.md`, `docs/SECURITY.md`,
  `docs/DEPLOYMENT.md`, `docs/OPERATIONS_GUIDE.md`, `docs/INSTALLATION_GUIDE.md`,
  `docs/TEST_GUIDE.md`, `docs/TEST_RESULT_TEMPLATE.md`, `docs/TESTING.md`,
  `docs/SUPPLY_CHAIN.md`, `docs/REQUIREMENTS_TRACEABILITY.md`와 `dist/installers/README.md`를
  source 구현, dev.18 설치·시험 절차, 자동 회귀와 아직 남은 외부 인수 범위에 맞춰 동기화했다.
  결과 양식에는 `T3-REGISTRY`와 `MODEL-GOVERNANCE` 판정·증적 항목을 추가했다.

**검증과 산출물**

- 최종 `make check`는 Ruff, strict mypy `87 source files`, Python non-E2E
  `735 passed, 4 deselected`, Web Vitest `24 files/211 tests`, ESLint와 Next.js production build를
  통과했다.
- Backend model registry+migration 집중 회귀는 `33 passed`(registry 14, migration 19),
  `test_api`+Experiment comparison은 `29 passed`, 전체 API suite는 `271 passed`였다. Alembic head는
  `e4c7b9d2f610`이다.
- 제한 sandbox의 첫 `make test-e2e`는 localhost bind 권한으로 거부됐다. 승인된 환경에서 동일
  명령을 재실행해 `4 passed`를 확인했다.
- dev.18 partial archive는 Manager
  `rvc-manager-0.1.0-dev.18-linux-amd64.tar.gz` SHA-256
  `83de04e5d8e5fb5a4fecb041fec2e6a6aa08a14aa04622f5a36a1b3ba6e484b7`, Worker
  `rvc-worker-0.1.0-dev.18-linux-amd64.tar.gz` SHA-256
  `6e631c9f49dd62f06d9132f55ee728364eef0d08894059cd67fc3b2f6b63b1a8`이다. 외부 sidecar와
  내부 exact `SHA256SUMS`, `verify-ledger`, strict `verify-bundle`을 다시 통과했다. 두 bundle에는
  version-rendered `README.md`/`TESTING.md`/`TEST_RESULT_TEMPLATE.md`가 있고 symlink/host cache가
  없다. 최종 archive는 `T3-REGISTRY` 결과 양식을 포함하도록 다시 생성한 byte이며, Worker
  activation은 mode `0444`이고 모든 runtime/GPU/profile/Sample gate가 false다.
- 현재 repository의 모든 파일은 untracked이고 Git tracked inventory가 0개다. 따라서
  `git diff --check`는 검사할 tracked 변경이 없어 whitespace와 committed source provenance 판정이
  `BLOCKED`이며, 위 실행형 테스트 결과를 Git provenance로 확대하지 않는다.

**남은 위험과 출시 gate**

- 실제 PostgreSQL multi-replica에서의 동시 promotion 경쟁, MinIO/S3 대용량 canonical object
  재해시·tamper·timeout/outage, 실제 browser/API owner/admin·타 owner·response-loss·두 탭 promotion과
  keyboard/screen-reader E2E는 사용자 인수 대기다.
- dev.18도 `GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`, empty image inventory인 partial
  개발 번들이다. Manager application/dependency image와 Worker RVC/CUDA runtime을 포함하지 않으므로
  bundle checksum PASS를 air-gapped/native GPU/production release 증거로 해석하면 안 된다.
- Clean Ubuntu linux/amd64 설치 lifecycle, 외부 TLS/browser, NVIDIA core/49-case/no-network,
  vulnerability/container/secret scan, 완전한 SBOM과 법적 license 검토 및 tracked Git revision은
  계속 별도 출시 gate다.

### dev.17 Experiment Run 비교 화면, Worker custom CA와 사용자 설치·인수 runbook 강화

**목적과 변경 범위**

- Backend Experiment comparison 원장을 browser에 안전하게 연결하기 위해
  `apps/web/src/app/bff/experiments/[experimentId]/comparison/route.ts`, 공개 response type과
  `experiment-bff.ts` strict projection을 추가했다. Query는 canonical UUID 2~16개의
  반복 `job_ids`만 순서대로 받고 1,024 UTF-8 byte를 상한으로 한다. HttpOnly cookie는
  Manager에만 전달하고 storage URI, metadata, Authorization은 공개 response에서 제거했다.
- Experiment 상세에 `experiment-run-comparison.tsx`를 연결해 2~16 Job의 immutable
  RVC 설정, exact current attempt engine/status/학습 시간, allowlisted metric의 global sequence
  overlay·원장, 검증된 final model/index와 Sample 가용성을 보여준다. Request generation
  fence와 abort cleanup, 401/403/409/422/503 UX, Fake 경고를 유지하고 이 화면을 best-model
  자동 선정이나 운영 승인으로 표현하지 않았다.
- Worker에 `tls.py`와 `WORKER_CA_BUNDLE_PATH`를 추가했다. Installer
  `--ca-bundle-file`은 production에서 root 소유 regular non-symlink, mode `0444|0644`,
  1 byte~1 MiB의 ASCII certificate PEM만 받아 NUL/private key/잘못된 PEM을 거부한다.
  검증한 byte는 release 밖 `/etc/rvc-orchestrator/worker/ca/custom-ca.pem`에 mode `0444`로
  원자 게시하고 container의 `/etc/rvc-worker/ca/custom-ca.pem`에 read-only mount한다.
  Install/upgrade에서 option을 생략하면 기존 CA를 보존하고 replacement staging/prevalidation
  실패와 일반 uninstall에서도 기존 byte를 보존한다.
- Worker의 동기 `urllib` Manager control API, 비동기 Manager stream과 external
  Dataset/TestSet/Artifact object client는 system default trust에 optional custom CA를 추가한
  하나의 `CERT_REQUIRED`, hostname-check, TLS 1.2+ `SSLContext`를 공유한다. Environment
  proxy를 사용하지 않으며 `verify=false`/`curl -k`를 제공하지 않는다. Installed
  Compose wrapper도 start/restart/run/create 전 CA path·directory·owner·mode·PEM을 다시 검증한다.
- 설치/테스트 가이드를 명령 단위로 다시 감사해 checksum/Compose 실패 뒤 후속
  명령이 계속되거나 command substitution 실패를 빈 container 목록으로 오판하는
  shell 패턴을 제거했다. 인증된 고정 archive hash→sidecar 내용/파일명→실제
  archive hash를 모두 대조하고 새 빈 extraction root만 사용한다. Manager secret은
  최초 owner/mode/exact inventory/regular/non-symlink/size를 변경 없이 검증하고 remediation을
  설치 PASS로 소급하지 않는다. `TEST_RESULT_TEMPLATE.md`에 checksum 독립 신뢰 출처,
  `T3-CONFIG`/`MANAGER-CONFIG`를 추가했고 bundle-local Worker native negative runbook도
  fake environment·inactive/no-container 불변을 확인한다.
- `AGENTS.md`, `README.md`, `CHECKLIST.md`, 아키텍처/설치/배포/운영/테스트/공급망/
  요구사항 추적 문서와 bundle-local README/TESTING을 dev.17 계약에 맞춰 동기화했다.

**검증과 산출물**

- 최종 `make check`는 Ruff, strict mypy `85 source files`, Python non-E2E
  `720 passed, 4 deselected`, Web Vitest `21 files/181 tests`, ESLint, Next.js production build와
  shell syntax를 모두 통과했다. 현재 Git tracked file은 0개이므로 `git diff --check`
  exit 0은 whitespace/source provenance 증거가 아니다.
- 제한 sandbox의 첫 `make test-e2e`는 localhost bind `PermissionError` 4건으로만 실패했다.
  Local socket 실행 권한으로 동일 명령을 재실행해 `4 passed in 5.80s`를 확인했다.
- Experiment BFF 집중 29개와 Web 전체 181개, ESLint/production build가 PASS했다.
  Worker custom CA/lifecycle/handshake 집중 42개, Worker·installer·deployment·runtime
  packaging 327개, 최종 핵심 8개와 Worker mypy 30 files, Ruff, `bash -n`도 PASS했다.
  In-memory TLS handshake는 custom CA+정확한 hostname만 성공하고 CA 누락과 hostname
  mismatch를 `SSLCertVerificationError`로 거부했다.
- dev.17 partial archive는 Manager
  `rvc-manager-0.1.0-dev.17-linux-amd64.tar.gz` SHA-256
  `b131698fbdeb51887d808f1396323b9a0e37ef6495445e60eadbedc024b95b96`, Worker
  `rvc-worker-0.1.0-dev.17-linux-amd64.tar.gz` SHA-256
  `a4b2951b7f210501e73f2d9ab1b6fb9d78c6ce8f93aed26b59b83d898a4883e7`다. 두 외부
  sidecar, 압축 해제 뒤 `SHA256SUMS`, strict ledger/bundle verifier, version-rendered
  README/TESTING/TEST_RESULT_TEMPLATE, symlink/host `__pycache__` 부재를 검증했다. Worker
  bundle은 `common/worker_ca.py`, fixed read-only mount 문서와 mode `0444` disabled activation을
  포함하고 Manager schema marker는 `d8f2a6c4b901`이다.
- 수정한 문서의 Markdown fence/로컬 link, bundle 문서 렌더, installer 구문과 focused
  image/source/activation/packaging 회귀를 통과했다. dev.17에서 Manager 전체 Docker
  Compose smoke는 재실행하지 않았으며 마지막 실제 증거는 dev.16 source의
  `docker_architecture=arm64` PASS다.

**남은 위험과 출시 gate**

- dev.17도 `GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`, 빈 image/archive inventory인 partial
  개발 번들이다. Manager application/dependency image와 Worker RVC/CUDA runtime을 포함하지
  않아 `MANAGER-CONFIG`/`WORKER-CONFIG` 또는 별도 image의 `SOURCE-MIXED` 기능 시험만
  가능하며 actual native GPU/Sample/air-gapped/production 설치는 차단된다.
- Custom CA의 clean Ubuntu Worker container↔실제 Manager/Object hostname·stream, 외부
  TLS/browser Secure cookie/HSTS, clean linux/amd64 Manager lifecycle, NVIDIA core/49-case/no-network,
  upstream image digest, vulnerability/container/secret scan, 완전한 SBOM·법적 license review가 남았다.
- Experiment 비교의 실제 browser/API E2E, 반응형·keyboard·screen-reader QA와 model
  registry를 통한 best-model promotion/audit은 미구현이다.
- 현재 checkout은 Git tracked inventory가 0개이므로 reproducible source provenance, commit 기반
  rollback과 Git whitespace 증거를 제공하지 못한다.

### dev.16 사용자 설치·인수 runbook 보정, release readiness/closure와 비교 API

**목적과 변경 범위**

- 사용자가 직접 설치와 시험을 수행할 수 있도록 `docs/INSTALLATION_GUIDE.md`와
  `docs/TEST_GUIDE.md`를 현재 archive 기준으로 다시 감사했다. 환경별 최소 인수 묶음과
  PASS/FAIL/BLOCKED를 기록하는 `docs/TEST_RESULT_TEMPLATE.md`를 추가하고, Manager/Worker bundle
  builder가 이를 `README.md`/`TESTING.md`와 함께 archive root에 포함하도록 했다.
- 감사에서 dev.15 bundle-local 명령이 installed `current` symlink를 `image_bundle.py --root`로
  전달해 verifier가 정상적으로 exit 1 하는 오류를 재현했다. Root 가이드, Manager/Worker
  `BUNDLE_README.md`와 공통 `BUNDLE_TESTING.md`는 `readlink -f`로 physical
  `releases/<version>`을 구하고 install root 밖 escape를 거부한 뒤 ledger/image/activation을
  검사하도록 통일했다. 수정할 수 없는 dev.15 archive는 새 설치·시험 기준에서 제외했다.
- Native Worker 절차를 release build host와 clean install host로 나눴다. Archive/sidecar 전송과
  extracted exact ledger/manifest/activation 검증, `--no-start` 설치, installer가 load한 exact image와
  physical release/activation 검증, installed wrapper의 GPU/mount one-shot `--check`, 명시적 systemd
  시작 순서로 변경했다. One-shot은 Compose default network를 쓰므로 no-network 증거가 아님을
  명시하고 실제 egress/DNS flow 인수와 분리했다. Source Docker smoke/builder의 direct-daemon
  release account와 install host의 `sudo docker` 권한 모델도 분리했다.
- Host 사설 CA 설치가 Worker container trust로 전달되지 않는 현 상태를 확인했다. Public CA chain만
  현재 인수 범위로 두고 사설 CA는 `WORKER-CUSTOM-CA-UNSUPPORTED`로 차단했으며, custom CA read-only
  mount와 명시적 Manager/Object SSL context를 체크리스트와 운영/배포 문서의 미완료 gate로 남겼다.
  최초 관리자 비밀번호 권장값도 이후 lifecycle과 일치하는 16~1,024자로 통일했다.
- `installers/manager/build-self-contained-release.sh`를 추가해 committed 40-hex clean source,
  source ignore closure, Buildx linux/amd64, API/Web/MLflow build, dependency pull, exact 8-role image ID/
  platform/application user/version·revision label 검증 뒤 self-contained builder를 호출하도록 했다.
  Mutable upstream tag, scan·법적 review와 clean-host 실행은 계속 별도 release gate다.
- `infra/worker/runtime/release_readiness.py`는 source/wheelhouse/assets/build/runtime digest,
  exact 49-case qualification과 SBOM·vulnerability/container/secret/SAST/license/clean-host review
  evidence를 읽기 전용으로 전부 열거한다. Exit 0도 입력 binding 검증일 뿐이며 항상
  `activation_permitted=false`, `activation_projection_written=false`를 유지한다. Runtime base inspect도
  `linux/amd64`를 강제한다.
- MLflow는 새 `infra/mlflow/requirements.lock`의 exact boto3/psycopg2 overlay를
  `--no-deps --only-binary=:all:`로 설치한다. Supply-chain generator는 API와 MLflow lock의 공통
  package를 dedupe하면서 모든 source를 보존하고, 모든 Dockerfile/env base reference와 container
  legal-review 미완료 상태를 report에 포함한다.
- Manager API에 owner/admin 전용
  `GET /api/v1/experiments/{experiment_id}/comparison`을 추가했다. 2~16개 distinct Job을 요청 순서대로
  비교하고 immutable config, exact current-attempt engine/status/timing, allowlisted key당 최신 200개
  metric과 Manager-verified model/index/sample availability만 투영한다. Cross-Experiment/owner,
  stale pointer, non-finite metric과 storage/internal field는 fail-closed한다. BFF와 설정/metric 비교
  화면은 후속 항목으로 남겼다.
- `README.md`, `AGENTS.md`, `CHECKLIST.md`, `docs/DEPLOYMENT.md`, `docs/OPERATIONS_GUIDE.md`,
  `docs/TESTING.md`, `docs/SUPPLY_CHAIN.md`, `docs/REQUIREMENTS_TRACEABILITY.md`와
  `dist/installers/README.md`를 위 판정과 dev.16 기준으로 동기화했다.

**검증과 산출물**

- 최종 `make check`는 Ruff, strict mypy `84 source files`, Python non-E2E
  `712 passed, 4 deselected`, Web Vitest `19 files/162 tests`, ESLint, Next.js production build와 shell
  syntax를 모두 통과했다. 현재 Git tracked file은 0개이므로 내부 `git diff --check` exit 0은
  whitespace/source provenance 증거가 아니다.
- 제한된 sandbox의 첫 `make test-e2e`는 localhost bind `PermissionError` 4건으로만 실패했다.
  Local socket 실행 권한으로 같은 명령을 다시 수행해 `4 passed in 6.63s`를 확인했다.
- `make test-manager-full-stack-docker`도 첫 실행은 같은 localhost port bind 제한으로 중단됐다.
  권한 있는 실행에서 current API/MLflow/Web image를 다시 build하고
  `Manager full Compose stack smoke: PASS (docker_architecture=arm64)`를 확인했다. MLflow exact overlay
  lock 변경 뒤 별도 non-root/read-only health smoke도 PASS했다.
- Manager self-contained orchestrator, Worker readiness/runtime/qualification, deployment 회귀 집중 실행과
  수정된 bundle 문서 포함 회귀가 통과했다. Markdown local link 검사, Ruff와 builder `bash -n`도
  통과했다.
- 새 partial archive는 Manager
  `rvc-manager-0.1.0-dev.16-linux-amd64.tar.gz` SHA-256
  `9a520623010a4e640e9975bc87835640de8f7ac127830ec9d9106ce7d2939f26`, Worker
  `rvc-worker-0.1.0-dev.16-linux-amd64.tar.gz` SHA-256
  `105971694bed766ea3ae4d7c58ec27db49aa4246e3db0a83988f598e2064d612`이다. 두 외부 sidecar,
  압축 해제 뒤 `SHA256SUMS`, strict ledger/bundle verifier, version-rendered
  README/TESTING/TEST_RESULT_TEMPLATE, symlink/host `__pycache__` 부재와 Worker activation mode `0444`를
  다시 검증했다. Manager schema marker는 `d8f2a6c4b901`이다.

**남은 위험과 출시 gate**

- dev.16도 `GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`, 빈 image/archive inventory인 partial
  개발 번들이다. Manager application/dependency image와 Worker RVC/CUDA runtime을 포함하지 않아
  CONFIG-ONLY 또는 별도 image의 `SOURCE-MIXED` 기능 시험만 가능하다.
- Custom CA Worker trust, clean Ubuntu linux/amd64 Manager lifecycle, 외부 TLS/browser, NVIDIA GPU
  core/49-case/no-network, immutable upstream image digest, vulnerability/container/secret scan, 완전한
  SBOM과 법적 license review가 남았다. Readiness report나 arm64 Compose PASS가 이 gate를 열지 않는다.
- Experiment comparison은 backend projection만 구현됐다. Same-origin BFF, config/metric graph 화면,
  실제 browser 비교 E2E와 model registry 기반 best-model promotion은 미완료다.

### dev.15 release source/image user closure·설치 전환 fail-closed·가이드/재번들

**목적과 변경 범위**

- dev.14 extracted archive에서 `SHA256SUMS`를 제거하면 installer가 source-tree 실행으로 오판해
  변조된 release를 게시할 수 있었고, 과거 `upgrade.sh`로 더 낮은 schema/version을 선택하거나
  uninstall stop/down 실패를 exit 0으로 보고하는 경로가 확인됐다. dev.14 archive byte 자체는
  수정할 수 없으므로 이를 폐기 대상으로 명시하고 새 dev.15 기준선을 만들었다.
- `.gitignore`와 `.dockerignore`의 runtime output 패턴을 root 범위로 제한해
  `apps/web/src/app/bff/artifacts/[artifactId]/download/route.ts`와
  `apps/web/src/app/bff/jobs/[jobId]/artifacts/route.ts` 같은 실제 source가 `artifacts/` 패턴에
  가려지지 않게 했다. `tools/verify_release_source.py`와 `tests/infra/test_source_closure.py`는
  self-contained build 전 release-relevant source inventory를 `git check-ignore --no-index`로
  검사하고 broad-ignore 회귀를 거부한다. Manager 전체 Compose smoke는 두 route가 Web standalone
  image 안에 실제 compiled file로 존재하는지도 확인한다.
- `installers/common/image_bundle.py`의 format-2 image record에 실제 container `user`를 추가했다.
  Docker-save config member byte의 SHA-256을 digest-addressed member name과 다시 대조하고 archive,
  manifest와 load 뒤 inspect 결과를 결박한다. Manager API `10001:10001`, Web `nextjs`, MLflow
  `10002:10002`, Worker runtime `10001:10001`이 아니면 self-contained bundle 생성을 거부한다.
- `installers/common/lib.sh`와 Manager/Worker `install.sh`는 ledger 생략을 caller가 source mode로
  명시하고 physical Git top-level이 정확히 같은 실제 repository root에서만 허용한다. Extracted
  bundle은 `manifest.env`까지 함께 제거해도 ledger 누락으로 실패한다.
- Manager/Worker `upgrade.sh`와 `install.sh`는 strict SemVer forward transition만 허용한다.
  Target release와 pending env로 Compose를 먼저 render한 뒤 기존 service stop, env/current 전환,
  target start 순서로 진행한다. Activation 전 실패는 기존 env/current byte를 보존한다. Target
  start 실패 뒤에는 database migration의 임의 역행을 막기 위해 target pointer를 일관되게 유지한
  down 상태로 nonzero 종료한다. `tests/infra/test_installer_activation.py`는 Manager와 Worker의
  1.0→2.0 성공, prospective Compose 실패 보존, 2.0→1.0 거부와 dev.14→dev.15 ordering을 검증한다.
- Manager/Worker `uninstall.sh`는 systemd disable과 Compose down을 모두 시도하되 하나라도 실패,
  command/wrapper 부재가 있으면 성공 문구 없이 exit 1을 반환한다. Release/config/secret/token/
  profile/data/volume을 보존하는 deactivate 의미는 바꾸지 않았다.
- `README.md`, `AGENTS.md`, `CHECKLIST.md`, 설치/시험/배포/운영/공급망/추적 문서와
  `dist/installers/README.md`를 dev.15 기준, 정확한 checksum, 과거 installer 사용 금지,
  forward-only upgrade·uninstall 판정과 현재 CONFIG-ONLY/`SOURCE-MIXED` 제한에 맞췄다.

**검증과 산출물**

- `make check`는 Ruff, strict mypy `82 source files`, Python non-E2E
  `691 passed, 4 deselected`, Web Vitest `19 files/162 tests`, ESLint, Next.js production build와
  전체 shell syntax를 통과했다. 현재 Git tracked file은 여전히 0개이므로 마지막
  `git diff --check` exit 0은 whitespace/source provenance 증거로 인정하지 않는다.
- source closure 4, deployment config 31, installer activation 3, image bundle closure 33, Worker
  runtime packaging 13, Manager recovery 13개를 함께 실행한 집중 회귀 97개가 모두 통과했다.
  Modified Python의 Ruff format/check, image/source verifier mypy와 installer `bash -n`도 통과했다.
- 제한된 기본 sandbox의 첫 `make test-e2e`는 localhost bind `PermissionError` 4건으로만 실패했다.
  Local socket 권한이 있는 실행으로 같은 명령을 다시 수행해 `4 passed in 6.73s`를 확인했다.
- `make test-manager-full-stack-docker`는 `Manager full Compose stack smoke: PASS
  (docker_architecture=arm64)`를 기록했다. 기존 health/readiness, non-root UID, runtime secret과
  exact MinIO cross-bucket deny 외에 Web artifact BFF 두 route의 packaged presence도 통과했다.
- Manager `rvc-manager-0.1.0-dev.15-linux-amd64.tar.gz` SHA-256은
  `a0c18bc938d3ca82c1995f1100dfa7a8d5e094fb5311332e57820ecad3c3e0aa`, Worker
  `rvc-worker-0.1.0-dev.15-linux-amd64.tar.gz`는
  `4a1d942abadc86f4ef8df89d260f03a85fdac81ff4f6357b1cb27fe9524ae7d5`다. 외부 sidecar, 압축 해제
  뒤 exact `SHA256SUMS`, format-2 manifest, version-rendered bundle-local README/TESTING, symlink/
  host cache 부재와 Worker activation mode `0444`를 다시 검증했다. Manager schema marker는
  `d8f2a6c4b901`이다.

**남은 위험과 출시 gate**

- dev.15도 `GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`, 빈 image/archive inventory인 partial
  개발 번들이다. Application/dependency image와 Worker RVC/CUDA runtime/source byte가 없으므로
  CONFIG-ONLY 또는 별도 image를 조합한 `SOURCE-MIXED` 기능 시험만 허용한다.
- 이미 배포된 dev.14 이하 root-level script를 운영자가 직접 실행하면 새 forward/checksum guard를
  소급 적용할 수 없다. 과거 archive를 배포 경로에서 제외하고 target dev.15 bundle의 upgrade 또는
  installed Manager guarded rollback만 사용해야 한다.
- Target service가 activation 뒤 시작되지 않으면 자동 database downgrade/rollback하지 않고 target
  pointer를 유지한 down 상태다. Clean Ubuntu lifecycle에서 이 운영 복구 절차와 systemd exit 0 뒤
  실제 inactive/disabled post-condition을 아직 검증해야 한다.
- Clean Ubuntu 22.04/24.04 linux/amd64, 외부 TLS/browser, NVIDIA GPU 49-case/no-network matrix,
  self-contained image closure, vulnerability/container/secret scan, 완전한 SBOM과 법적 license 검토,
  tracked Git revision이 계속 출시 gate다. Arm64 full-stack PASS를 production/amd64/GPU 증거로
  확대하지 않는다.

### dev.14 Manager 전체 Compose 실행 증적·설치/시험 runbook·재번들

**목적과 변경 범위**

- dev.13에는 Manager 전체 Compose harness가 있었지만 실제 runtime PASS가 없었고, 이후 확인한
  proxy/host publish 수정도 archive에 들어 있지 않았다. 사용자가 그대로 따라 할 설치·시험
  절차와 현재 source가 일치하도록 `infra/compose/manager.compose.yml`,
  `infra/runtime/proxy-entrypoint.sh`, `tests/infra/manager_full_stack_smoke.sh`와 배포 회귀를
  보정하고 새 partial bundle을 만들었다.
- macOS Colima/Docker Desktop에서는 `/private/var/...`의 임시 secret 경로가 VM에 공유되지 않아
  container 생성이 실패했다. Harness는 Darwin에서 저장소의 `.rvc-stack-smoke/`를 기본 임시
  parent로 쓰고, 사용자가 지정한 `RVC_STACK_SMOKE_WORK_PARENT`와 Linux `/tmp` 경로는 실제
  physical path로 정규화한다. 실패 시 최종 Compose 상태/proxy log를 표시하고 각 URL을 이름 있는
  bounded wait로 검사하며 성공/실패 후 격리 project·volume을 정리한다.
- Proxy는 Compose에서 `nginx -g 'daemon off;'` command를 명시하고 entrypoint도 인자가 없을 때 같은
  안전한 기본값을 사용한다. `internal: true` storage network만으로는 Colima host의 published
  MinIO/MLflow port에 도달할 수 없어 두 service만 non-internal `host-access`에 추가 연결했다.
  PostgreSQL, Redis, API와 RQ는 이 network에 들어가지 않고 MinIO `9000/9001`, MLflow `5000`의
  default publish는 정확히 `127.0.0.1`로 회귀 검증한다.
- Runtime secret generation은 의도적으로 root:service-group mode `0710`이라 service user가
  directory를 열거할 수 없다. Full-stack smoke가 이를 결함처럼 `os.listdir`하던 부분을 제품 권한
  완화 없이 수정했다. Root verifier는 projection root `0711`, 안전한 상대 `current` symlink,
  generation `0710`, exact regular/non-empty mode `0400` inventory와 UID/GID를 검사하고, 실제
  API/RQ/MLflow user는 열거 거부와 허용된 known path의 `O_NOFOLLOW` read를 각각 증명한다.
  Harness는 MinIO 두 identity의 sole exact policy와 상대 bucket 접근 거부도 실제 stack에서 검사한다.
- `docs/INSTALLATION_GUIDE.md`는 Manager/Worker host별 bundle 검증을 분리하고 installer/systemd와
  같은 system Docker daemon 확인, fail-propagating image save/load, 외부 TLS Nginx 적용·사설 CA·
  loopback/firewall 확인과 정확한 installed verifier 경로를 추가했다. `docs/TEST_GUIDE.md`는 Git
  provenance BLOCKED와 executable test PASS를 분리하고 T0~T6 명령, 실제 E2E/full-stack 기준과
  native fail-closed 사후 검사를 갱신했다. Manager/Worker bundle-local `README.md`와 `TESTING.md`도
  component별 외부/내부 checksum, `--no-start`, 권한/ledger/Compose와 최초 관리자 또는 bootstrap
  token 경계를 독립적으로 따라 할 수 있게 만들었다.
- 두 builder는 공통 TESTING template에서 반대 component 절을 제거하고 version/component/marker가
  미치환 상태로 남으면 실패한다. `README.md`, `AGENTS.md`, `CHECKLIST.md`, deployment/operations/
  security/supply-chain/traceability 문서와 `dist/installers/README.md`를 dev.14 기준으로 동기화했다.

**검증과 산출물**

- `make test-manager-full-stack-docker`는 최종 강화된 harness에서 exit code 0과
  `Manager full Compose stack smoke: PASS (docker_architecture=arm64)`를 기록했다. PostgreSQL,
  Redis, MinIO/init, secret/init/migration, API, RQ, MLflow, Web과 proxy가 함께 기동했고 loopback
  health/readiness, non-root UID, secret scope와 MinIO cross-bucket deny를 통과했다. 이는 현재 host
  architecture의 개발 증적이며 clean linux/amd64 증거로 확대하지 않는다.
- `make test-e2e`는 localhost HTTP Manager↔Fake Worker protocol `4 passed in 7.84s`로 통과했다.
  최종 `make check`는 Ruff, strict mypy `81 source files`, Python non-E2E
  `676 passed, 4 deselected`, Web Vitest `19 files/162 tests`, ESLint, Next.js build와 shell syntax를
  통과했다. 같은 날 앞선 실행에서는 Dataset upload 경쟁 test 종료 중 SQLAlchemy/aiosqlite
  connection 정리 warning 1건이 한 번 관측됐지만 최종 재실행에는 재현되지 않았다.
- Deployment와 image-closure 집중 회귀 `56 passed`, 수정한 deployment test의 Ruff
  lint/format, Manager full-stack shell syntax와 Compose render가 통과했다. `tests/infra` 전체에
  대한 선택적 Ruff format 감사에서는 이번에 수정하지 않은 기존 여섯 test 파일이 reformat
  대상으로 보고됐지만 required `ruff check`와 수정 파일 format check는 통과했다.
- Manager `rvc-manager-0.1.0-dev.14-linux-amd64.tar.gz`의 SHA-256은
  `83ae2b7a9ec3d0f99175520ad781223314c7b677bc2ec694b43a2b675a356d70`, Worker
  `rvc-worker-0.1.0-dev.14-linux-amd64.tar.gz`는
  `792b2bdf4007509ea301a469abfd82683fa029e363023d980dd4122392b18d7b`다. 두 외부 sidecar,
  archive 내부 모든 `SHA256SUMS`, exact ledger/format 2 manifest, rendered component 문서,
  host cache 부재와 JSON 형식을 검증했다. Manager schema marker는 `d8f2a6c4b901`이다.

**남은 위험과 출시 gate**

- 두 archive는 여전히 `GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`, 빈 image/archive
  inventory인 partial 개발 번들이다. Application/dependency image, Worker RVC/CUDA runtime과
  source byte가 들어 있지 않으므로 설치 가이드의 `SOURCE-MIXED` 기능 시험을 production,
  air-gapped, rollback 또는 실제 학습 합격으로 표현하지 않는다.
- 현재 Git tracked file은 0개라 `git diff --check` exit 0을 whitespace/source provenance 증거로
  인정하지 않는다. Clean Ubuntu 22.04/24.04 linux/amd64 전체 설치, 실제 외부 TLS/browser와
  공개 object endpoint, NVIDIA GPU 49-case/no-network matrix, self-contained image closure,
  vulnerability/container/secret scan과 법적 license 검토가 계속 필요하다.
- `host-access`는 일반 bridge이므로 MinIO/MLflow의 outbound 경계도 넓어진다. Default loopback
  publish를 유지하고 특히 인증/TLS 없는 MLflow와 MinIO console을 public/LAN 주소에 bind하지
  않는다. 다른 bind가 필요한 production topology는 별도 위협 검토와 TLS/network control 없이
  허용하지 않는다.

### dev.13 Dataset integrated LUFS·설치 release closure·사용자 검증 문서

**목적과 변경 범위**

- Dataset의 clipping/silence/RMS만으로는 음량 품질을 비교할 수 없었고, historical row와 짧거나
  지원 밖인 입력에 숫자를 추정하지 않는 typed loudness 원장이 필요했다. `apps/api/src/`
  Dataset ingestion/model/schema/service/router와 Web Dataset BFF/type/projection, migration
  `d8f2a6c4b901` 및 관련 API/Web 회귀를 갱신했다.
- `itu-r-bs1770-4-mono-stereo-v1` K-weighting을 파일마다 초기화하고 400 ms/75% overlap complete
  block만 수집한다. Dataset 전체에서 strict `>-70 LUFS` absolute gate 뒤 `-10 LU` relative gate를
  적용하며 file LUFS 평균이나 파일 경계를 잇는 synthetic block을 만들지 않는다. 짧은 입력,
  absolute gate 미만, 3채널 이상과 8~384 kHz 밖 입력은 typed reason과 `integrated_lufs=null`로
  보존하고 migration 전 PCM row도 raw report에서 값을 재구성하지 않는다.
- `installers/common/image_bundle.py`, `installers/common/lib.sh`, Manager/Worker install·Compose·rollback
  경계와 infra 회귀에 archive exact inventory 검증을 추가했다. Bundle `SHA256SUMS`는 누락뿐 아니라
  추가·symlink·비정규·unsafe path를 거부하고, 설치 release는 mode `0444`
  `RELEASE_SHA256SUMS`로 전체 file inventory를 고정한다. Compose wrapper와 rollback은 이를 다시
  검증하며 manifest/image/runtime activation과 release-owned environment의 version, image,
  pull policy와 provenance가 다르면 활성화하지 않는다.
- Manager/Worker bundle-local `README.md`·`TESTING.md`, `docs/INSTALLATION_GUIDE.md`와
  `docs/TEST_GUIDE.md`를 dev.13 partial 경계, checksum, 설치 순서, 단계별 PASS/BLOCKED 판정과
  redacted 증적 기준으로 갱신했다. 이번 동기화에서 `AGENTS.md`, `CHECKLIST.md`,
  `docs/REQUIREMENTS_TRACEABILITY.md`, `docs/SECURITY.md`도 같은 불변 조건과 남은 gate를 반영했다.

**검증과 산출물**

- 최종 source의 `make check`는 Ruff, strict mypy `81 source files`, Python non-E2E
  `675 passed, 4 deselected`, Web Vitest `19 files/162 tests`, ESLint와 Next.js production build,
  shell syntax를 통과했다. 다만 현재 Git index의 tracked file이 0개라 마지막
  `git diff --check` exit 0은 whitespace 증거로 인정하지 않는다.
- Manager archive는 `rvc-manager-0.1.0-dev.13-linux-amd64.tar.gz`, SHA-256
  `a22429577f81c3f6bed65e1cbc5cf7249da62ae629fc588e80de30f1052e2ac0`, 최종 Worker archive는
  `rvc-worker-0.1.0-dev.13-linux-amd64.tar.gz`, SHA-256
  `50696d67abec28efe966c7d61406823e4b4561f041053f768279a58e05f5b01c`다. 외부 sidecar, 내부
  `SHA256SUMS`, exact ledger와 format 2 strict verifier, bundle-local 문서 marker, Worker disabled
  activation mode `0444`를 확인했다. Manager schema marker는 `d8f2a6c4b901`이다.
- 두 archive는 `GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`, 빈 image/archive inventory이며
  Worker runtime/native/GPU/profile/Sample gate는 false다. Current source에서 따로 build한 image는
  archive와 결박되지 않은 `SOURCE-MIXED` 기능 시험일 뿐이다.

**미검증 범위와 후속 gate**

- 최신 dev.13 source의 localhost E2E는 현재 최종 환경에서 재실행하지 못했고 collection 4만
  확인했다. 과거 실행이나 collection을 `4 passed`로 기록하지 않으며 사용자가 localhost socket이
  허용된 환경에서 새로 실행해야 한다.
- API/Web/MLflow, PostgreSQL, Redis, MinIO, RQ와 proxy를 함께 올리는 Manager full Compose smoke
  harness는 구현됐지만 최종 runtime PASS는 확인하지 않았다. 감사에서 `! mc ls`가 `set -e`
  문맥에서 false-positive가 될 수 있음을 찾아 명시적 `if mc ls; then exit`로 바꾸고, 실제 Web UID
  `1001` 검사와 정적 회귀를 추가했다. Deployment/image-closure 회귀 55개와 shell syntax는
  통과했지만, 정적 검사나 세 격리 Docker smoke로 전체 stack 합격을 대신하지 않는다.
- Clean Ubuntu 22.04/24.04 amd64 전체 stack, 실제 외부 TLS/browser, NVIDIA GPU/native RVC,
  self-contained image closure, 취약점/container/secret scan과 법적 license 검토는 계속 미완료다.
  Git tracked revision과 source commit provenance도 확보하기 전 production/air-gap/rollback 증거로
  승격하지 않는다.

### dev.12 역할별 runtime secret·MinIO 최소권한·실행 엔진 표시

**목적과 보안 결함**

- MLflow를 UID/GID `10002:10002`로 바꾼 첫 smoke는 SQLite와 임시 환경변수를 사용해 server
  권한만 증명했다. 실제 installer가 만드는 root 소유 mode `0600` file secret은 Compose의
  non-root API/RQ/MLflow가 직접 읽을 수 없으므로 clean Linux 시작이 실패하는 P0가 남아 있었다.
- 기존 MinIO init은 MLflow identity에도 built-in 전역 `readwrite`를 연결해 Manager canonical
  bucket까지 접근할 수 있었다. 또한 Job 화면은 설정의 희망 backend를 실행 결과처럼 fallback할
  수 있어 Fake Worker 결과가 실제 RVC WebUI 실행으로 오인될 수 있었다.

**원자적 역할별 secret projection과 storage 권한**

- `infra/runtime/manager-secrets-init.py`와 Manager Compose에 root, network-none,
  read-only initializer를 추가했다. Source secret은 initializer에만 mount하고 API, maintenance RQ,
  MLflow는 각각 다른 named runtime volume의 `current` generation만 read-only로 본다. API image의
  실행 UID/GID는 `10001:10001`, MLflow는 `10002:10002`로 고정했다.
- Projector는 모든 source를 `O_NOFOLLOW`로 먼저 읽고 regular/non-empty/size/NUL을 검사한다.
  UUID generation의 exact 파일을 mode `0400`/대상 UID·GID로 만들고 file/directory fsync 뒤
  `current` symlink를 원자 교체한다. 세 profile 중 하나라도 실패하면 이미 바뀐 symlink까지
  rollback하고 이전 세대를 유지한다. RQ에는 PostgreSQL·Redis·Manager S3 네 secret만 투영해
  JWT, Worker bootstrap/pepper와 MLflow credential을 노출하지 않는다.
- Installed `compose.sh`는 `up|start|restart|run|create` 전에 release를 검증하고 initializer를
  실행한다. 정상 `up`의 `depends_on: service_completed_successfully`도 유지해 최초 설치와 직접
  Compose 실행을 방어한다. Host source config/secret이 권위 원본이며 derived runtime volume은
  backup 대상이 아니다.
- `infra/minio/init.sh`는 Manager와 MLflow bucket별 exact object/bucket action policy를 생성하고
  각 identity에서 built-in broad policy를 모두 떼어낸 뒤 sole expected policy attachment를
  JSON으로 확인한다. 재실행해도 같은 least-privilege 상태로 수렴한다.

**authoritative engine mode와 사용자 경고**

- API `JobRead.current_attempt_engine_mode`는 exact current `JobAttempt` ID를 batch 조회해
  `attempt.job_id`까지 대조한다. Attempt가 없으면 `null`이며 `JobConfig.rvc_backend.backend_type`로
  fallback하지 않는다. 생성·목록·상세·취소·재시도 응답이 같은 projection을 사용한다.
- Web의 Overview, Job 목록과 상세는 이 필드만 사용한다. `fake`는
  `FAKE · 운영 결과 아님` badge와 `role=alert` 경고, `rvc_webui`는 `RVC WebUI`, `null`은
  `실행 전`으로 표시한다. Worker capability는 `Worker 광고 엔진`으로 별도 표시해 Job 실행
  metadata로 추측하지 않는다.

**검증, 산출물과 남은 범위**

- `make check`는 Ruff, strict mypy `81 source files`, Python non-E2E
  `660 passed, 4 deselected`, Web Vitest `19 files/156 tests`, ESLint, Next.js production build,
  shell syntax와 whitespace 검사를 통과했다. localhost Manager↔세 Fake Worker HTTP E2E는
  queued `null`, 실행/완료 `fake` metadata와 기존 telemetry/artifact 흐름을 포함해 `4 passed`다.
- Deployment 집중 회귀는 `26 passed`다. Local `linux/arm64` Docker에서
  `make test-mlflow-docker`, `make test-manager-secret-projection-docker`,
  `make test-minio-policy-docker`가 통과했다. Projection smoke는 실제 root:root mode `0600`
  source, 권한/mode와 역할별 exact visibility, A→B rotation, 빈 파일/symlink 실패 시 B 보존,
  실제 API/RQ/MLflow entrypoint의 non-root secret read를 확인한다. MinIO smoke는 양 identity의
  상대 bucket 접근 거부와 의도적으로 붙인 `readwrite`의 재실행 제거를 확인한다.
- Manager bundle은 `rvc-manager-0.1.0-dev.12-linux-amd64.tar.gz`, SHA-256
  `9a015fd5d702d832f0248a28a2caeaa8f34246056209f53f439290f8d1769bcb`, Worker bundle은
  `rvc-worker-0.1.0-dev.12-linux-amd64.tar.gz`, SHA-256
  `c1de29f84aed941ce0f3796cdd3696120e49fb2ed504da41f783f5b80fc66598`다. 외부 checksum,
  내부 `SHA256SUMS`, format 2 strict verifier, executable mode와 host cache 제외를 확인했다.
  Schema marker는 계속 `ca8d3e7f4b10`이다.
- 두 archive는 여전히 `GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`, 빈 image/archive
  inventory이며 Worker runtime/native/GPU gate가 false다. Docker 증거도 arm64 격리 smoke이므로
  clean Ubuntu amd64의 전체 PostgreSQL/Redis/MinIO/MLflow stack, 외부 TLS/browser와 실제 NVIDIA
  GPU 학습을 대신하지 않는다. 이 조건을 닫기 전에는 최종/오프라인 production 설치본으로
  승격하지 않는다.

### MLflow non-root/read-only runtime hardening

**목적과 발견한 공백**

- Manager의 API/Web/Worker는 image에서 non-root user를 고정했지만
  `ghcr.io/mlflow/mlflow:v3.1.1` base는 빈 `Config.User`, 즉 root가 기본이었다. 기존 MLflow
  Dockerfile과 Compose도 이를 override하지 않아 전체 stack을 non-root로 설명할 수 없었다.
- 단순히 Dockerfile에 `USER` 문자열을 추가하는 것만으로는 read-only filesystem, 필요한 임시
  write와 실제 server health를 증명하지 못하므로 static 구성과 Docker runtime smoke를 함께
  release 경계로 만들었다.

**구현**

- `infra/mlflow/Dockerfile`은 전용 `rvc-mlflow` 계정을 UID/GID `10002:10002`로 생성하고 숫자
  `USER`를 고정한다. Home/cache는 `/tmp`를 사용하고 bytecode와 buffered output을 제어한다.
- Manager Compose의 MLflow service도 같은 숫자 user를 이중 고정하며 read-only rootfs,
  `cap_drop: ALL`, no-new-privileges 공통 정책, PID 128과 mode `0700`/UID-owned 128 MiB `/tmp`
  tmpfs만 제공한다. Docker socket, GPU와 host filesystem write volume은 없다.
- `tests/infra/test_deployment_config.py`는 Dockerfile user/home과 Compose user/read-only/capability/
  PID/tmpfs/internal-network 경계를 정적으로 검증한다. `tests/infra/mlflow_nonroot_smoke.sh`와
  `make test-mlflow-docker`는 image를 빌드한 뒤 network-none container에서 UID/GID와 zero effective
  capability, 소유 home write 거부, private `/tmp` write, boto3/psycopg2/MLflow import와
  `/health=OK`를 실제 확인한다.

**첫 단계 검증과 이후 보정**

- deployment 집중 회귀 `23 passed`, Ruff와 shell syntax가 통과했다. Local Docker에서 image를
  새로 빌드하고 `MLflow non-root/read-only health smoke: PASS`를 확인했다. Compose render는
  `docker-compose 5.1.3`으로 통과했다.
- 이 첫 smoke는 실제 root 소유 file secret을 mount하지 않아 deployed entrypoint 시작 가능성을
  증명하지 못했다. 위 dev.12 작업에서 역할별 atomic projection과 actual-entrypoint smoke를 추가해
  해당 결함을 닫았으며 최종 deployment 집중 회귀 수는 `26 passed`다.
- 이 Docker daemon은 `linux/arm64`이고 buildx가 없다. 따라서 runtime 권한 경계는 실제 증거지만
  최종 `linux/amd64` image, PostgreSQL/MinIO 연결을 포함한 clean-host Manager smoke와 image
  vulnerability/license 검토를 대신하지 않는다. 해당 release gate는 계속 열어 둔다.

### dev.11 trusted HTTPS·시스템 telemetry·최신 metric polling과 사용자 인수 문서

**목적과 경계**

- 사용자가 현재 산출물을 직접 설치·시험할 수 있도록 source 상태와 installer 상태를 다시
  동기화했다. 이번 결과는 `0.1.0-dev.11` partial 개발 번들이며, archive 무결성과 source 기반
  기능 시험을 할 수 있다는 뜻이다. Docker image와 reviewed native RVC runtime을 포함한
  self-contained production installer라는 뜻은 아니다.
- 이전 dev.10의 proxy는 외부 TLS 종단이 전달한 scheme을 내부 HTTP 값으로 덮을 수 있었다.
  요청자가 조작할 수 있는 forwarding header와 운영자가 정한 공개 scheme을 분리하고, 실제
  브라우저 인수 시험이 가능한 형태로 계약을 고정하는 것이 우선이었다.

**trusted 공개 scheme과 TLS 경계**

- `PUBLIC_SCHEME=http|https`를 Manager API, Web, Nginx와 installer가 공유하는 운영자 소유 값으로
  추가했다. production 시작은 `https`만 허용한다. Nginx는 외부
  `X-Forwarded-Proto`/`X-Forwarded-For`를 폐기하고 검증한 scheme과 직접 연결 주소를 upstream에
  다시 쓰므로 임의 header가 HSTS, Secure cookie 또는 same-origin protocol을 바꾸지 못한다.
- upstream HSTS는 edge에서 제거하고 Nginx가 정확히 한 번만 기록한다. API도 request header가
  아니라 설정에서 HSTS를 결정하고, Web의 `SESSION_COOKIE_SECURE`는 `PUBLIC_SCHEME`이 없는
  local/legacy 실행의 fallback으로만 남겼다. 설치·upgrade 예시는
  `--public-scheme https`를 명시한다.
- 설정/entrypoint/Compose/API/Web/installer 회귀를 추가했다. 집중 Python `49 passed`, Web 전체
  `18 files/152 tests`, Web lint/build, Ruff, API mypy `41 source files`, shell syntax, Compose config와
  bundled executable/provenance 검증이 통과했다. 다만 실제 인증서·외부 TLS 종단·clean browser의
  Secure cookie/HSTS 확인은 아직 사용자의 인수 시험 및 release gate다.

**Worker 시스템 telemetry hardening**

- claim long-poll에서 얻은 오래된 capability를 첫 Job 표본으로 재사용하지 않고, attempt session
  시작 직후 GPU/디스크를 새로 관측한다. 이후 표본은 heartbeat와 독립된
  `SYSTEM_TELEMETRY_INTERVAL_SECONDS` 주기(기본 60초, 10~3600초)로 남긴다.
- `system.gpu.telemetry_available`을 추가해 성공한 0-GPU 질의와 `nvidia-smi` 실행·파싱·의미 검증
  실패를 구분한다. GPU는 최대 64개, index/UUID 고유성, VRAM 범위, finite 사용률·온도와 integral
  memory를 검증하며 잘못된 응답은 heartbeat를 종료하지 않고 unavailable로 fail-safe 처리한다.
- periodic durable-spool 저장 실패는 cancel로 잘못 분류하지 않고
  `failed / telemetry_persistence_failed`가 된다. 명시적 cancel·lease loss는 여전히 우선한다.
  Terminal에서는 producer를 먼저 봉인하고 마지막 best-effort flush를 수행하며, Manager의
  retryable 503이면 watermark 아래 bounded late replay를 위해 pending record를 보존한다.
- Worker 전체 `272 passed`, 전체 non-E2E Python `655 passed, 4 deselected`, Ruff, strict mypy와
  Worker installer shell syntax가 통과했다.

**최신 metric 조회와 대시보드 polling**

- `GET /api/v1/jobs/{job_id}/metrics?tail=true&limit=N`은 전체 결과의 최신 N개를 attempt/sequence/ID
  정방향으로 반환한다. nonzero `offset`과의 조합은 422로 거부하고 응답 offset은 실제 반환 구간의
  시작 위치다. Web BFF는 boolean `tail`만 allowlist하고 Job 상세는 최신 200개를 사용한다.
- Job 상세는 즉시 첫 요청을 한 뒤 15초마다 이전 요청과 겹치지 않게 갱신하며 unmount/filter 변경
  시 fetch를 중단한다. GPU 수집 가능 여부도 `수집 가능/수집 불가`로 표시한다. API 집중 회귀와
  Web polling/BFF 회귀 `3 files/23 tests`가 통과했다.
- live HTTP E2E는 Job 실행 중 `current_epoch`, `loss_g_total=14`, indexed GPU utilization `62.5`,
  telemetry availability `1`, 남은 디스크와 redacted log를 API로 조회한다. 완료 후 spool
  dead-letter가 없고 terminal log/metric watermark와 DB 저장 count가 같은지도 확인한다. 전체
  localhost E2E는 `4 passed`다.

**최종 검증, 설치 산출물과 사용자 문서**

- authoritative source 검증은 `make check` PASS다. Ruff와 strict mypy `80 source files`, Python
  non-E2E `655 passed, 4 deselected`, Web Vitest `18 files/152 tests`, ESLint, Next.js production
  build, shell syntax와 whitespace 검증이 모두 통과했다. 별도로 HTTP E2E `4 passed`도 확인했다.
- Manager archive는
  `dist/installers/rvc-manager-0.1.0-dev.11-linux-amd64.tar.gz`, SHA-256
  `10b74fc605ef46d21dc029f17e770940effb0f6713c57814d2324f6929e16e8f`이고 schema는
  `ca8d3e7f4b10`이다. Worker archive는
  `dist/installers/rvc-worker-0.1.0-dev.11-linux-amd64.tar.gz`, SHA-256
  `d032c2c58f416bec2e5881a6cb35ebb44d069b267ea31f250ef1685fcaad8018`이다. 외부 checksum,
  내부 `SHA256SUMS`, format 2 image manifest와 executable bit를 검증했다.
- 두 archive 모두 `GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`, 빈 image/archive inventory다.
  Worker의 runtime/native/GPU/sample qualification gate도 모두 false다. 따라서
  `docs/INSTALLATION_GUIDE.md`는 bundle integrity, source-mixed Manager smoke, Worker config-only와
  미래 native 시험을 별도 판정으로 나누고, `docs/TEST_GUIDE.md`는 자동 회귀·HTTP E2E·실제 GPU·
  clean-host/TLS 인수를 서로 대체할 수 없는 증거로 구분한다.
- 최종 문서 감사에서 bundled TLS 템플릿이 host 외부 proxy 설정으로 오해될 여지를 제거하고,
  Manager/MinIO용 127.0.0.1 upstream, Host/path/query/method 보존, object buffering/body/timeout과
  단일 HSTS 예제를 설치 가이드에 추가했다. Shell에서 redirection으로 오해되는 `<version>` 표기는
  명시적 release 변수로 바꾸고, strict image-bundle verifier와 activation mode/JSON 확인, Dataset
  ingestion/Web 및 metric tail/BFF/poller 집중 명령을 실제 설명 범위와 일치시켰다.
- Docker 문서 시험은 API/Web/Worker image user를 직접 inspect하고 network-none Worker `--check`를
  실행하도록 보정했다. MLflow Dockerfile은 upstream base user를 독립적으로 고정·검증하지 않는다는
  별도 hardening 공백을 발견해 `CHECKLIST.md`에 미완료 release 항목으로 남겼다.
- 최종 문서 명령 대조로 Job observability+Dataset ingestion Python `31 passed`, Web BFF/poller/
  metric presentation+Dataset projection `5 files/45 tests`를 다시 실행했다. 두 dev.11 archive의
  외부 checksum과 strict bundle verifier도 재통과했고 Worker activation은 mode `0444`, digest
  `null`, inference F0 목록 `[]`임을 확인했다. Markdown fence·필수 version/hash/gate assertion도
  통과했다.

**남은 위험과 다음 release gate**

- 실제 NVIDIA 장시간 RVC v1/v2 학습과 5종 F0, sample 추론, approved base digest/runtime asset,
  application/dependency image closure, clean Ubuntu/NVIDIA 및 air-gapped install/upgrade/rollback,
  외부 TLS와 실제 브라우저 검증은 아직 완료되지 않았다.
- MLflow image의 non-root 실행 사용자도 reviewed upstream/base와 Compose runtime에서 고정·검증해야
  하며, 그 전에는 전체 container hardening을 완료로 판정하지 않는다.
- Git commit provenance도 없는 상태다. 위 증거와 signed/provenanced self-contained archive가
  준비되기 전에는 dev.11을 production 설치 완료로 승격하거나 현재 열린 gate를 true로 바꾸지 않는다.

### 사용자 설치·테스트 인수 가이드 정합성 보정

**목적**

- 사용자가 직접 설치와 시험을 진행할 수 있도록 `docs/INSTALLATION_GUIDE.md`와
  `docs/TEST_GUIDE.md`를 실행 순서, 기대 결과와 판정 단위 중심으로 재감사했다. Partial archive,
  source checkout, 별도 Docker image와 실제 GPU runtime의 증거를 한 PASS로 합치지 않는 것이
  핵심이다.

**문서 변경과 발견한 제한**

- 설치 가이드 첫머리에 `BUNDLE-INTEGRITY`, Manager 기능 smoke, Worker CONFIG-ONLY와 미래 native
  시험 경로를 분리했다. Archive/source/image가 없는 dev.10에 현재 checkout image를 조합하면
  `SOURCE-MIXED`로 기록하고 production/rollback/air-gap 증거로 사용하지 않도록 명시했다.
- Fake/no-start Worker 구성 시험은 실제 bootstrap token 대신 폐기 가능한 합성 token을 사용하게
  바꿨다. 기존 fake `worker.env`를 native로 in-place 전환하면 installer가 runner-mode 변경을
  거부하므로 clean host 또는 별도 설정 migration이 필요하다는 절차도 추가했다.
- Manager 기본 backup은 active Job/upload를 drain한 maintenance window에서 일부 service를 멈추는
  작업임을 명시했다. 향후 native Worker 후보에는 systemd/Compose log, Manager online/GPU/heartbeat,
  재부팅 뒤 동일 identity까지 별도 등록 smoke로 요구했다.
- dev.10 bundled Nginx가 외부 proxy의 `X-Forwarded-Proto`를 내부 HTTP 값으로 덮어 Secure cookie와
  HSTS 판정을 어긋나게 할 수 있음을 발견했다. 외부 TLS→loopback 구성은 기능 smoke로만 두고,
  trusted-proxy 전달 수정 또는 bundled TLS 종단이 포함된 새 번들 전 production TLS는 BLOCKED로
  기록했다.
- `make check`가 HTTP E2E/Docker/installer/recovery/GPU 학습을 포함하지 않는 범위를 명시하고,
  현재 네 E2E가 실제로 증명하는 범위와 token/CAS/replay/loss/indexed-GPU 집중 회귀 범위를
  분리했다. 최신 권한 실행은 `make test-e2e` `4 passed in 7.98s`, live telemetry test 5회 반복
  PASS였다. 이후 source가 더 바뀌므로 사용자의 최종 checkout 재실행을 여전히 요구한다.
- `dist/installers/README.md`의 잘못된 current dev.9 안내를 dev.10 archive/SHA/schema와 같은 기준으로
  맞췄다. 이 문서 작업은 새 installer를 만들거나 dev.10의 빈 image/runtime closure를 닫지 않았다.

**남은 위험**

- dev.10은 계속 `GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`이며 Manager/Worker image가 없다.
  실제 NVIDIA native 학습, production TLS, clean-host와 air-gapped 설치는 BLOCKED다.
- 현재 working tree는 dev.10 archive 이후 변경돼 있다. 최종 코드가 안정화되면 새 version의
  application image와 installer를 함께 만들고 전체 `make check`, HTTP E2E와 clean-host 시험을
  다시 수행해야 한다.

### Job-bound GPU/VRAM/온도/디스크 시계열 연결

**목적과 기존 공백**

- Worker heartbeat는 현재 GPU snapshot을 `Worker.capabilities`에 덮어써 서버 목록에는 보였지만,
  Job attempt의 `Metric` 원장에는 남기지 않았다. 따라서 Job 상세의 범용 metric graph가 있어도
  사양 18.2의 GPU 사용률·VRAM·온도·남은 디스크를 실행 시간축으로 비교할 실제 데이터가 없었다.
- Native live training telemetry에서 확립한 durable spool, attempt-wide sequence와 terminal exclusive
  watermark를 우회하는 별도 system-metric 경로를 만들지 않고 같은 원장에 결합해야 했다.

**Worker와 계약 변경**

- `AttemptTelemetrySession.record_system_snapshot()`은 Job claim 시점과 각 busy heartbeat의
  `WorkerCapabilities`를 하나의 metric batch로 먼저 mode `0600` spool에 저장한다. GPU가 없어도
  `system.gpu.count`, `system.disk_free_bytes`를 남기며 GPU별로 다음 key를 기록한다.

  ```text
  system.gpu.<index>.utilization_percent
  system.gpu.<index>.vram_used_mb
  system.gpu.<index>.vram_total_mb
  system.gpu.<index>.temperature_c
  ```

- 같은 값이 연속 heartbeat에서 반복되어도 시계열 표본이므로 training source/semantic dedupe를
  적용하지 않는다. 모든 표본은 training/stage metric과 같은 단조 sequence allocator를 사용하고
  terminal count에 포함된다. Terminal 봉인과 경합해 늦게 시작한 heartbeat 표본은 새 sequence를
  만들지 않고 안전하게 생략하며, 이미 spool enqueue 중인 표본은 watermark가 완료를 기다려 포함한다.
- `WorkerAgent`는 Manager network I/O 전에 초기/heartbeat 표본을 저장한다. Spool에 저장할 수 없는
  경우 학습을 계속하며 system metric을 조용히 버리지 않고 active Job 취소를 요청한다.
- Worker wire 계약은 GPU inventory를 64개, index `0..1023`, 고유 index/UUID로 제한하고 온도 값을
  finite로 검증한다. 비정상 inventory가 metric key 충돌이나 NaN/Infinity 원장 오염을 만들지 못한다.

**Manager·Dashboard와 검증**

- Manager의 기존 metric ingest/fingerprint/write-fence/late-watermark 경계가 system key도 같은
  `Metric`과 MLflow outbox에 저장한다. Job observability API 회귀는
  `system.gpu.0.utilization_percent` exact key filter를 검증한다.
- Job 상세 화면은 metric key를 GPU 번호가 보이는 한국어 이름과 `%`, `MiB`, `°C`, `GiB` 단위로
  표시하며 제목을 학습·GPU 시스템 metric으로 명확히 했다. 알 수 없는 사용자 metric key는 그대로
  보존한다.
- Contracts, Worker telemetry/Agent, Manager observability와 Web presentation 집중 회귀를 추가했다.
  Localhost E2E도 Job이 아직 `training`일 때 `system.disk_free_bytes`가 HTTP로 조회되고 terminal
  watermark가 전체 저장 count와 일치하도록 확장했다. 최초 제한 sandbox에서는 localhost bind가
  막혔지만 이후 권한 실행에서 전체 네 E2E와 live test 5회 반복이 통과했다. 최종 source가 다시
  바뀐 뒤에는 사용자 환경 재실행 전까지 새 상태를 자동 PASS로 이어받지 않는다.
- 이 변경은 `nvidia-smi` fixture와 fake Job으로 protocol을 검증한 것이다. 실제 NVIDIA 장시간
  학습의 값 정확도·sampling 부하·GPU/no-network matrix와 clean-host 증거는 여전히 release gate다.

### Native live telemetry, terminal watermark와 ingest 동시성 경계

**목적과 발견한 결함**

- 기존 native parser는 stdout/train.log/TensorBoard 형식을 이해했지만 실제 `WorkerAgent` 실행 중
  중앙으로 내보내는 경로는 stage 완료 log/metric 위주였다. 따라서 장시간 training이 terminal에
  도달하기 전에 loss/epoch를 대시보드에서 볼 수 있다는 요구를 충족하지 못했다.
- 단순히 callback에서 Manager를 직접 호출하면 느린 network가 subprocess pipe를 막고, terminal이
  lease를 release한 뒤 local spool의 마지막 batch가 도착하면 Manager가 stale lease `409`로
  dead-letter 처리하는 문제가 있었다. SQLite에서는 `SELECT FOR UPDATE`가 실질적 write lock이
  아니어서 active ingest와 cancel/terminal commit의 순서도 결정적이지 않았다.

**Worker live telemetry 변경**

- `apps/worker/src/rvc_worker/telemetry.py`, `agent.py`, `native_runner.py`, `process.py`,
  `training_metrics.py`에 attempt-scoped live session을 연결했다. Subprocess stdout/stderr callback,
  증가분 `train.log` tail과 TensorBoard scalar polling이 같은 log/metric sequence allocator를 쓰며,
  source event와 의미가 같은 metric/log를 결정적으로 dedupe한다. `current_epoch`는 terminal을
  기다리지 않고 durable metric batch로 먼저 전달한다.
- Callback은 Manager network I/O가 아니라 mode `0600` spool에 원자 enqueue가 끝날 때까지만
  기다린다. 별도 delivery task가 전송하며 spool write lock과 flush/network lock을 분리했다.
  Remove/dead-letter file operation만 짧게 write lock으로 보호해 enqueue와 경합해 pending record를
  잃지 않는다. Spool 저장 실패는 자료를 버리고 stage를 계속하지 않고 subprocess process group을
  종료한다.
- 저장 전 bearer/named·quoted secret, JWT/API/access/private key, Worker token, URL query,
  file URI/absolute path와 control character를 제거하고 정제된 log를 16 KiB로 제한한다. Terminal
  직전 `watermarks()`는 delivery/state lock 아래 producer를 봉인하고 durable enqueue에 성공해
  실제 발급된 다음 log/metric sequence를 exclusive count로 반환한다. `completed|failed|cancelled`
  status는 두 count를 항상 함께 보고한다.

**Manager ingest, 원장과 migration 변경**

- `packages/contracts/.../worker.py`와 Manager Worker router는 terminal-only
  `telemetry_log_count`/`telemetry_metric_count` pair를 0..2,147,483,647로 제한한다. Alembic
  `ca8d3e7f4b10`과 `JobAttempt` DB constraint가 pair/range/terminal-only를 보존한다. Terminal
  commit은 이미 저장된 최대 sequence가 count 이상이면 거부한다.
- Terminal 뒤 late batch는 exact Worker/lease/Job/attempt가 같고 해당 kind의 모든
  `sequence < watermark`일 때만 허용한다. Watermark가 없는 legacy/system-recovery terminal,
  cross-worker/attempt와 상한 이상 sequence는 `409`다. Late/old-attempt metric은 Metric와 MLflow
  outbox에는 원자 기록하되 current Job의 epoch projection을 바꾸지 않는다.
- Active telemetry는 조건부 Job no-op update를 SQLite-compatible write fence로 사용한다. Cancel/
  terminal/reassignment가 먼저 이기거나 DB lock이 경합하면 `503`과 `Retry-After: 1`을 반환하고,
  Worker는 durable spool을 보존해 terminal watermark 기준으로 다시 전송한다. Terminal status도
  같은 write ordering 경계를 사용한다.
- Alembic `c7b1e4d9a260`과 `IngestBatch.payload_fingerprint`는 idempotency key를 canonical batch
  SHA-256에 결박한다. 같은 key의 exact replay만 duplicate이고 다른 payload, 같은 sequence의 다른
  값과 legacy NULL fingerprint replay는 fail-closed한다. 동시 exact replay는 한 batch로 수렴한다.
  Status/log/metric endpoint에는 인증·Pydantic parsing 전 raw 2 MiB
  `WORKER_TELEMETRY_JSON_MAX_BYTES` 상한과 UTF-8 strict JSON/non-finite number 거부를 적용했다.

**검증과 문서**

- `.venv/bin/pytest -q apps/worker/tests` — `260 passed`. Callback/spool failure process 종료,
  source/semantic dedupe, live stdout/train.log/TensorBoard, `current_epoch`, spool/flush file race,
  producer 봉인과 terminal watermark를 포함한다.
- Contracts, `apps/api/tests/test_api.py`, `test_mlflow_integration.py`와 새
  `test_telemetry_migration.py` 집중 suite — PASS. Exact replay/conflict, 2 MiB/NaN, terminal late/
  over-watermark/legacy/cross-worker, cancel-first `503` 뒤 retry, late MLflow outbox와 SQLite/
  PostgreSQL migration constraint를 포함한다.
- `tests/e2e/test_fake_worker_e2e.py`에는 실제 localhost Manager와 Agent를 사용해 Job이 여전히
  `training`일 때 `current_epoch=2`와 sanitized log가 HTTP 조회되는 회귀를 추가했다. Runner는 loss도
  발행하지만 이 E2E가 loss key를 HTTP로 조회하지는 않는다. Terminal 뒤 attempt watermark가 저장
  log/metric count와 같은지도 검사한다. 최초 제한 sandbox에서는 localhost bind가 `EPERM`이었으나,
  이후 권한 실행의 전체 네 E2E와 live test 5회 반복이 통과했다. 최신 결과는 이 날짜의 위
  사용자 설치·테스트 인수 가이드 항목을 따른다.
- 최종 `make check` PASS: Python non-E2E `639 passed, 4 deselected`, Web Vitest
  `16 files/143 tests`, Ruff, strict mypy, ESLint, Next.js production build와 shell 검증이 통과했다.
- `0.1.0-dev.10` Manager/Worker partial bundle을 생성하고 외부 SHA-256, archive 내부
  `SHA256SUMS`와 format 2 image manifest를 검증했다. Manager는 schema `ca8d3e7f4b10`, SHA-256
  `70be95abf791e63396bdf10f44cccf1d2d2e47dd94040b0cb83b7f44ca51a41f`; Worker는 SHA-256
  `6e6843ff95c9a427ab715aff9c6c44dbeba52233f45c68cde3d1bc6fece39c29`다. 둘 다
  `GIT_COMMIT=uncommitted`, `SELF_CONTAINED=false`와 빈 image/archive inventory이고 Worker
  runtime/GPU/native gate는 false다.
- `CHECKLIST.md`, `AGENTS.md`, architecture/security/requirements/testing/install/deployment 문서에
  durable-first sequence, terminal exclusive watermark, payload fingerprint, 2 MiB 상한과 운영 한계를
  같은 계약으로 기록했다.

**남은 위험과 출시 gate**

- Manager가 완전히 불가한 동안 Worker가 terminal에 도달하고 terminal status가 커밋되기 전에
  lease가 회수되면 server-side watermark가 없다. 이 old local pending telemetry를 새 attempt에
  자동 합치면 provenance가 깨지므로 현재는 fail-closed하고 운영자 조사 대상으로 남긴다. 따라서
  bounded late replay는 terminal status가 커밋된 뒤의 전송 지연에 대한 보장이지 모든 Manager
  전체 장애에서 무손실 자동 복구를 뜻하지 않는다.
- Parser와 localhost fixture는 실제 NVIDIA GPU/Torch/RVC 실행 증거가 아니다. Reviewed amd64 base
  digest, 실제 native 장시간 GPU/no-network matrix, clean Ubuntu/NVIDIA 설치와 self-contained image
  closure는 여전히 미완료이며 관련 release/capability gate는 false로 유지한다.

### 3-Worker 병렬 HTTP E2E, 관리자 사용자 lifecycle과 dev.9 partial 번들

**목적과 발견한 결함**

- 업로드 원문의 Manager 책임 첫 항목인 사용자 관리와 Worker 1/2/3/N 병렬 구조를 현재
  체크리스트·추적표에 다시 대조했다. 기존 코드는 bootstrap/login/소유권 RBAC와 단일 Worker
  HTTP E2E만 있었고 관리자 계정 생성·권한 변경·비밀번호 재설정 및 실제 세 Agent 동시 실행
  증거가 없었다.
- 세 실제 `WorkerAgent`를 같은 localhost Manager에 동시에 연결한 첫 E2E에서 세 Agent 모두
  9개 Artifact upload까지 끝냈지만 하나의 terminal `completed`가 HTTP 409로 실패했다.
  Heartbeat가 versioned `Worker` telemetry 행을 먼저 commit한 사이 terminal transition도 같은
  행의 `current_job_id/status`를 release하며 SQLAlchemy `StaleDataError`가 난 것이 원인이었다.
  테스트를 느슨하게 만들거나 terminal 실패를 성공으로 간주하지 않고 제품 경계를 수정했다.

**Manager/Worker 동시성 변경**

- `tests/e2e/test_fake_worker_e2e.py`는 같은 Dataset/Experiment에 PM, Harvest, RMVPE 세 immutable
  Job을 만들고 bounded first-claim barrier 뒤 세 실제 Agent/Fake runner를 실행한다. 세 attempt의
  시작·종료 시간이 겹치는지, Job↔Worker↔attempt↔lease가 일대일이고 서로 다른지, 상태 event,
  log/metric, 9종 Artifact/upload와 canonical object byte SHA-256이 모두 맞는지 검증한다.
- `apps/api/src/rvc_manager_api/routers/workers.py`의 terminal status commit은 optimistic CAS가
  heartbeat에 졌을 때 actor Worker ID를 request session에 보존하고 정확히 한 번만 전체 active
  lease→current Job→attempt→locked Worker fence를 다시 읽는다. 취소, lease 만료·교체, 재배정,
  attempt 변경 또는 required Artifact 변조는 기존 409 검사를 그대로 통과하지 못한다. 두 번째
  stale commit도 409로 닫아 무제한/숨은 재시도를 만들지 않았다.
- `apps/api/tests/test_api.py`는 별도 competing session이 heartbeat/Worker row version을 먼저
  commit해 실제 `StaleDataError`를 만들고, 경계 재조회가 두 번뿐이며 terminal event가 한 번만
  기록되는 회귀를 추가했다.

**관리자 사용자 lifecycle backend**

- `apps/api/src/rvc_manager_api/routers/users.py`와 schema/model을 추가해 admin-only
  `GET /api/v1/admin/users`, detail, create, role/active PATCH와 password-reset을 제공한다.
  목록은 exact email/role/active filter와 bounded pagination, mutation은 16 KiB raw JSON,
  strict unknown-field 거부, `Idempotency-Key`와 `expected_row_version`을 사용한다.
- `AdminUserOperation`은 actor+hash된 key를 unique하게 저장하고 JWT secret을 key로 한 canonical
  request fingerprint와 공개 응답만 보존한다. Password/key 원문, password hash와
  `access_token_version`은 response/audit/operation JSON에 들어가지 않는다. 같은 actor/key/body의
  replay는 동일 status/body와 `Idempotency-Replayed: true`, 다른 path/body 재사용은 409다.
- 관리자 생성/재설정 비밀번호는 16~1,024자, control character 없음, 최소 8개 서로 다른 문자,
  email local-part 비포함 및 알려진 약한 passphrase 거부 정책을 적용하고 기존 Argon2id helper로
  hash한다. Normalized email 중복은 409다.
- User에 optimistic `row_version`과 `access_token_version`을 추가했다. 역할·활성 변경 또는
  password reset은 token version을 증가시켜 발급된 모든 JWT를 즉시 영구 무효화하고 재활성화가
  이전 token을 되살리지 못하게 한다. 새 JWT는 필수 `ver` claim을 가지므로 업그레이드 전 token도
  fail-closed한다.
- 모든 lifecycle write는 `admin_bootstrap_state` singleton write fence로 직렬화하고 fence 뒤 actor
  admin/active 상태와 target을 다시 읽는다. 자기 강등/비활성화와 마지막 active admin 제거를
  차단하며, 두 admin이 서로를 동시에 강등하는 race에서도 정확히 한 명만 남는다. 생성·변경·reset
  audit는 공개 이전/새 상태와 token invalidation 여부만 남긴다.
- Alembic `b4a91d7e2c63`은 기존 User를 보존하며 두 version column/check, role/disabled/created index와
  secret-free operation table/FK/unique constraint를 추가한다. SQLite upgrade/downgrade와 PostgreSQL
  offline SQL을 모두 검증했다. Manager Compose와 `.env.example`에는
  `USER_LIFECYCLE_JSON_MAX_BYTES=16384`를 연결했다.

**Dashboard/BFF와 운영 UX**

- `/users` 관리자 화면은 계정 생성, role/active desired-state 저장, 비밀번호 재설정, active admin
  수와 row version을 표시한다. 자기 role/active control은 UI에서도 잠그지만 Manager가 최종
  권한 경계다. 자기 비밀번호 reset 성공 시 현재 session을 만료 경로로 보낸다.
- Same-origin BFF는 브라우저가 Manager path/Authorization을 선택하지 못하게 하고 4 KiB client
  body, exact key/body, 안전한 idempotency key와 public `AdminUserRead` projection만 전달한다.
  `password_hash`, token version과 임의 upstream field를 제거하고 self/last-admin/stale/email/
  idempotency 충돌을 고정 code로 매핑한다. Network/502/503처럼 commit 결과가 불명확하면 control을
  잠그고 목록 새로고침 전 blind retry하지 않는다.
- Web 회귀는 cross-origin/path/field/body/key 차단, fixed upstream path/token/key, secret 제거,
  conflict mapping, malformed success 502와 전체 pagination/duplicate fail-close를 검증한다.
  별도 transport 회귀는 server bearer와 explicit idempotency key가 실제 Manager request의 서로
  다른 고정 header로 전달되는지 확인한다.
  App shell에는 admin-only `사용자` navigation을 추가했다.
- `INSTALLATION_GUIDE.md`와 `TEST_GUIDE.md`에 계정 생성·권한·reset, 기존 session 폐기,
  3-Worker 시험과 redacted 증적 기준을 추가했다. README, AGENTS, CHECKLIST, architecture/security,
  operations/deployment/testing/supply-chain/traceability도 같은 불변 조건으로 갱신했다.

**검증과 설치 산출물**

- 최종 `make check` PASS: Ruff, strict mypy `80` source files, Python non-E2E
  `617 passed, 3 deselected`, Web Vitest `16 files/143 tests`, ESLint, Next.js production build,
  shell syntax와 whitespace 검사가 모두 통과했다.
- 제한된 sandbox의 `127.0.0.1` bind는 예상대로 `EPERM`이었고 권한 있는 동일
  `make test-e2e`는 `3 passed`. 세 조건·세 Agent 동시 E2E를 포함한다.
- Fresh SQLite를 base부터 새 단일 head `b4a91d7e2c63`까지 올려 `current`와
  `No new upgrade operations detected`를 확인했다. PostgreSQL offline upgrade SQL 974줄 생성과
  새 user table/column/FK/index를 확인했다. 로컬 Docker CLI에는 Compose plugin이 없어 문서의
  Ubuntu v2 명령 대신 설치된 standalone `docker-compose ... config --quiet`로 Manager/Worker
  렌더링을 검증했다.
- `0.1.0-dev.9` Manager/Worker partial bundle을 생성했다. Manager SHA-256은
  `135f33e71d0557798b94eb0355613b45015831b9532a4ba083d6c5dd73c4830f`, Worker SHA-256은
  `7802d1a14c10a61011a54b3be50e7c883cc46abd08682f89fb3d082b63528456`다. 외부 `.sha256`, archive
  내부 전 파일 `SHA256SUMS`, format 2 strict image manifest를 검증했다. Manager schema marker는
  `b4a91d7e2c63`; 둘 다 `SELF_CONTAINED=false`와 빈 image/archive inventory다. Worker runtime,
  GPU/profile/native Sample gate는 모두 false이고 disabled activation은 mode `0444`다.
- 한 전체 실행에서 TestSet 취소 회귀에 귀속된 aiosqlite non-checked-in connection `SAWarning`이
  한 번 보였다. 해당 테스트를 warning=error로 50회, TestSet 15개와 API 233개 전체, 매 테스트
  `gc.collect()`/pool checkout 감사를 다시 실행했으나 모두 경고 없이 통과하고 checked-out
  connection은 0이었다. Upload/heartbeat task await, 별도 session `async with`, fixture engine
  dispose도 확인돼 실제 누수 증거 없이 추측 수정하지 않았다. 마지막 전체 `make check`는 같은
  `617 passed, 3 deselected`를 warning 없이 통과했다.

**남은 위험과 출시 gate**

- 세 Agent E2E는 실제 HTTP/동시성/원장을 검증하지만 Fake runner와 SQLite를 사용한다. 실제 물리
  NVIDIA Worker, PostgreSQL 다중 replica/row-lock, MinIO/Redis 장애 주입과 장시간 학습 병렬성은
  clean 환경에서 별도 검증해야 한다.
- 관리자 lifecycle은 단기 access token version fencing을 제공하지만 refresh-token rotation UI,
  조직별 MFA/SSO, 사용자 삭제, canonical storage quota/retention은 아직 없다. 실제 browser
  접근성·반응형/Playwright와 clean Manager upgrade 후 전체 session 만료 drill도 남아 있다.
- dev.9는 application/runtime image가 없는 partial 개발 bundle이다. 실제 RVC/CUDA/CREPE 49-case,
  closed image closure, 취약점/container/secret scan과 법적 license 검토가 없으므로 native 학습,
  production Sample, air-gapped 설치와 v1.0 release는 계속 BLOCKED다.

### Runtime qualification→production Sample 활성화 경계와 dev.8 partial 번들

**목적과 변경 파일**

- 업로드 원문과 전체 체크리스트를 다시 대조해 핵심 목표인 자동 고정 TestSet Sample이 실제 GPU
  검증 부족뿐 아니라 production `create_runner()`와 capability에서 구조적으로도 열릴 수 없음을
  최우선 gap으로 선정했다. 실제 asset/base/GPU 증거 없이 기능을 켜지 않으면서, 향후 정확한
  시험 결과만 운영 capability로 투영할 수 있는 fail-closed 경계를 구현했다.
- `infra/worker/runtime/qualification.py`는 stdlib-only strict verifier다. core 8,
  training F0 5, v1/v2×40k/48k×index on/off×PM/Harvest/CREPE/RMVPE Sample 32,
  cancel/restart/spool/no-public-egress 4의 exact 49-case와 report tar.gz를 요구한다. Duplicate/unknown
  JSON key, 누락·추가·실패·중복 case, unsafe path/symlink/non-regular/빈 report, placeholder 또는
  불일치 hash/size, runtime image/build/asset/projection/base/fairseq/version mismatch를 거부한다.
  Evidence tar는 streaming으로 읽고 member 수/개별·총 크기를 제한한다. 출력은 기존 path를
  덮지 않는 mode `0444` `runtime-activation.json`이며 disabled 또는 fully-qualified 두 상태뿐이다.
- `apps/worker/src/rvc_worker/runtime_activation.py`, `runner.py`, `native_inference.py`,
  `native_runner.py`, `agent.py`, `cli.py`, `settings.py`는 activation을
  `/run/rvc-release/runtime-activation.json` 고정 경로에서만 읽는다. Env/YAML/CLI override는
  명시적으로 거부한다. Qualified projection의 세 gate, 네 F0 목록, image/qualification/asset
  digest와 실제 `assets-manifest.json`을 검증하고 dependency bind 시 asset digest를 다시 확인한
  경우에만 `NativeFixedTestSetInferenceDependency`를 주입한다. 실제 runner evidence가 있을 때만
  Agent register/heartbeat와 `--check`가 네 inference F0 및 readiness를 광고한다.
- `installers/worker/build-bundle.sh`, `install.sh`, `installers/common/image_bundle.py`와 Worker
  Compose를 연결했다. Builder는 raw qualification/evidence 두 입력을 모두 받아 exact image ID와
  runtime manifest에 교차검증한 뒤 activation을 직접 생성하며 사용자 activation/boolean을 받지
  않는다. Qualified path는 self-contained, committed clean source만 허용한다. Bundle/install/start는
  activation↔image ID↔asset↔qualification↔evidence를 다시 검증하고 Compose는 literal relative
  host file만 read-only mount한다. Python `tarfile` data filter가 archive mode 0444를 0644로
  정규화하는 호환 사례는 bundle checksum 검증 뒤 installer가 release copy를 0444로 다시 고정하고
  start verifier/Worker가 installed read-only mode를 요구하도록 처리했다.
- `docs/RUNTIME_QUALIFICATION.md`를 추가하고 README, AGENTS, CHECKLIST, architecture/security/runtime,
  deployment/install/test/operations/supply-chain/traceability 문서를 같은 기준으로 갱신했다.
  실제 증적이 없는 현재 상태에서는 disabled activation, 빈 inference capability,
  `AUTO_SAMPLE_JOBS_ENABLED=false`가 유지된다는 점을 명시했다.
- `Makefile`의 기본 Ruff/strict mypy 대상에 dependency-free qualification verifier를 포함해 이후
  schema/security 변경이 전체 `make check`를 우회하지 못하게 했다.
- 변경된 공통 verifier와 Worker 설치 경계를 담은 `0.1.0-dev.8` Manager/Worker partial bundle을
  생성했다. Manager SHA-256은
  `dcf0ace4a4531e6946d7f95aa940ea613a4c0e60e9f7c989473a150f65d12107`, Worker SHA-256은
  `0d6e358f9fd4bd67f14a4f61369dff464be2d8d477c794672209be847ee9a6b4`다. 둘 다 format 2,
  `SELF_CONTAINED=false`, 빈 image/archive inventory이고 Manager schema marker는
  `f9c4a7d2b610`이다. Worker archive의 disabled activation은 mode `0444`다.

**검증 결과**

- 최종 `make check` PASS: Ruff PASS, strict mypy `79` source files PASS, Python non-E2E
  `608 passed, 2 deselected`, Web Vitest `13 files/127 tests`, ESLint, Next.js production build와
  whitespace 검사 PASS.
- Worker 전체 `248`, qualification 전용 `19`, runtime packaging `13`, image closure/Compose
  집중 회귀가 PASS했다. 합성 49-case 증적이 self-contained builder의 true activation으로
  투영되고, 증적 없는 partial은 exact false/null/빈 F0 template으로 남으며, writeable/partial/
  unknown/tampered activation과 evidence/archive/image/asset mismatch가 거부되는지 포함한다.
- localhost 권한을 허용해 `make test-e2e`를 재실행했고 `2 passed`. 최초 sandbox 실행의 두 실패는
  코드 결함이 아니라 `127.0.0.1` bind 권한 거부였으며 권한 있는 동일 명령은 통과했다.
- fresh SQLite migration을 base부터 `f9c4a7d2b610`까지 올려 `current`와 `alembic check`를 PASS했고
  PostgreSQL offline upgrade SQL도 생성했다. Manager/Worker Compose render가 PASS했다.
- dev.8 외부 `.sha256`, archive 내부 전체 `SHA256SUMS`, image manifest strict verifier,
  Worker activation 0444와 host cache 부재를 확인했다.

**남은 위험과 출시 gate**

- 합성 qualified fixture는 제어 경계 시험일 뿐 실제 NVIDIA GPU 시험 결과가 아니다. 승인된 amd64
  base digest, 실제 CREPE weight 출처·재배포 권리·SHA, 49-case GPU/no-network report,
  vulnerability/container/secret scan과 사람의 license review가 없다. 현재 dev.8은 runtime/image가
  없는 partial archive이므로 production Sample과 native 학습은 계속 BLOCKED다.
- Qualification은 trusted release reviewer가 제공한 evidence byte를 exact runtime에 결박하지만
  독립 제3자 서명이나 보고 내용의 진실성을 대신하지 않는다. 최종 release에서는 외부 서명/승인
  정책과 clean NVIDIA VM 설치·reboot·upgrade 시험을 추가해야 한다.
- Manager의 `AUTO_SAMPLE_JOBS_ENABLED`와 `SAMPLE_APPROVED_RUNTIME_BUNDLES`는 실제 qualification
  review가 끝난 뒤에만 exact Worker pair로 설정한다. `--allow-unverified-gpu-runtime`은 core 후보
  시험용 확인일 뿐 Sample activation을 열지 않는다.

### 사용자 설치·테스트 가이드, dev.7 partial 번들과 lease 동시성 보정

**목적과 변경 파일**

- `docs/INSTALLATION_GUIDE.md`를 새 사용자 설치 진입점으로 만들었다. Manager/Worker host 요구사항,
  DNS/TLS와 두 endpoint, dev.7 외부/내부 checksum, Manager exact application/dependency image
  준비, `--no-start`→`manager.env`/TLS→Compose 검증→systemd restart→admin bootstrap 순서를
  구체화했다. Worker는 현재 runtime이 없는 partial bundle이라 fake/no-start 구성 검증만 가능하고
  production Manager가 Fake를 거부한다는 사실을 첫 화면에서 명시했다. 실제 native Worker는
  reviewed source/wheel/assets/base digest로 만든 self-contained runtime bundle 이후에만 설치하도록
  분리했다.
- `docs/TEST_GUIDE.md`에 T0 source, T1 localhost Fake protocol, T2 bundle/migration/Compose,
  T3 Manager clean-host/browser, T4 Worker fail-closed config, T5 native GPU/no-network matrix, T6 복구
  drill을 분리했다. 각 단계의 PASS/FAIL/BLOCKED, redacted 증적 양식, v1/v2×40k/48k×F0 matrix,
  artifact 의미와 현재 production Sample 차단을 기록했다. 자동 fixture와 partial archive를 실제
  GPU/air-gapped 검증으로 확대하지 못하게 했다.
- `README.md`, `AGENTS.md`, `CHECKLIST.md`, `docs/DEPLOYMENT.md`, `docs/OPERATIONS_GUIDE.md`,
  `docs/TESTING.md`, `docs/SUPPLY_CHAIN.md`, `dist/installers/README.md`와 runtime README를 같은
  기준으로 갱신했다. `rq-worker`는 Compose healthcheck가 없으므로 running과 `/readyz`의
  `rq_worker=ok`로 판정하고, 자동 rollback은 Manager 전용이며 uninstall은 압축 해제 bundle에서
  실행한다. production runner factory가 Sample dependency를 아직 주입하지 않고 capability가 false인
  상태도 숨기지 않았다.
- `installers/manager/install.sh`와 `installers/worker/install.sh`는 `oneshot + RemainAfterExit` unit이
  이미 active인 upgrade에서도 새 release를 실제 적용하도록 `systemctl enable` 뒤 `restart`한다.
  Worker installer는 data root를 runtime UID/GID 기본 `10001:10001` mode `0700`, token/profile을
  같은 소유자 mode `0600`으로 설치하며 UID/GID를 검증한다. non-root installer fixture는 현재
  uid/gid를 사용한다. `tests/infra/test_worker_runtime_packaging.py`와
  `tests/infra/test_deployment_config.py`에 소유권/mode와 active unit 재시작 회귀를 추가했다.
- 수정된 installer를 담은 `0.1.0-dev.7` Manager/Worker partial bundle을 만들었다. Manager schema
  marker는 `f9c4a7d2b610`이고 두 bundle은 format 2, `SELF_CONTAINED=false`, 빈 image/archive
  inventory다. Manager SHA-256은
  `88eacf44d63ec3f6c5092b3d408413c6df4e79688dd33a790d959f75794ad93b`, Worker SHA-256은
  `e93e4aa3ed593c3a602bf484c7af0690e207f35f8e5a3d6e3084944a2cbba9aa`다.
- 사용자용 E2E 명령을 재실행하면서 오래된 Fake fixture가 현재 auto-sample release gate를
  우회하려던 문제를 발견했다. `tests/e2e/test_fake_worker_e2e.py`는 sample을 끈 core protocol
  Job만 만들며 production Sample gate를 열지 않는다.
- 같은 E2E에서 heartbeat와 explicit lease renewal이 동시에 도착하면 versioned Worker row
  `StaleDataError` 또는 정상적인 out-of-order expiry를 회귀로 오판해 Job을 취소하는 실제 race를
  발견했다. `apps/api/.../routers/workers.py`와 `services/workers.py`는 lease renew가 Worker telemetry
  row를 갱신하지 않게 하고 JobLease를 `FOR UPDATE + populate_existing`로 읽으며 expiry/renewed-at을
  단조 증가시킨다. `apps/worker/.../agent.py`는 각 요청 시작 expiry보다 실제 연장된 응답만
  인정하되, 더 최신 Manager 응답 뒤에 도착한 성공 응답은 최신 deadline을 보존한다. 만료,
  baseline 이하, 단독 regressive 응답은 계속 fail-closed한다. API/Worker 단위 회귀도 추가했다.

**검증 결과**

- 최종 `make check` PASS: Ruff PASS, strict mypy `77` source files PASS, Python non-E2E
  `566 passed, 2 deselected`, Web Vitest `13 files/127 tests`, ESLint, Next.js production build,
  shell syntax와 whitespace 검사 PASS.
- localhost 권한을 허용해 `make test-e2e`를 재실행했고 `2 passed`. 현재 sample-disabled core
  protocol, Fake opt-in/production 차단, lease/heartbeat 동시 응답을 포함한다.
- API/Worker lease 집중 suite `39` tests, 관련 Ruff와 strict mypy PASS. Renew가 versioned Worker
  heartbeat row를 건드리지 않는지, delayed concurrent expiry의 monotonic merge, unchanged/past/
  regressive response 거부를 포함한다.
- installer shell 문법, Worker upgrade 보존/권한, active oneshot restart와 recovery infra 집중
  pytest 및 Ruff PASS.
- fresh SQLite를 base부터 `f9c4a7d2b610`까지 올리고 `current`, `alembic check`를 PASS했다.
  PostgreSQL offline upgrade SQL 생성과 Manager/Worker Compose `config --quiet`도 PASS했다.
- dev.7 두 외부 `.sha256`, archive 내부 모든 `SHA256SUMS`, image manifest strict 검증과
  `__pycache__|*.pyc|*.pyo|.DS_Store` 부재를 확인했다. 신규 문서의 필수 링크/버전/hash, balanced
  code fence, trailing whitespace와 stale current-version 표현도 정적 검사했다.

**남은 위험과 출시 gate**

- dev.7은 application/dependency image와 RVC runtime image가 없는 partial archive다. 실제
  Docker image build/save/load/inspect, container health, Manager recovery drill과 clean Ubuntu
  install/reboot/upgrade/rollback/restore는 이 환경에서 실행하지 않았다.
- Worker clean NVIDIA VM, reviewed amd64 base digest와 전체 wheel/asset/license, v1/v2 GPU 및
  no-network matrix가 없다. native/profile/sample/GPU verified gate는 false로 유지하며 실제 Sample
  Job은 production factory/capability가 연결될 때까지 BLOCKED다.
- SQLite E2E와 단위 회귀는 lease 순서 보정을 검증하지만 PostgreSQL 다중 API replica에서
  heartbeat/renew을 실제 동시에 실행해 DB expiry가 감소하지 않는 장애 주입은 추가 release gate다.
- 실제 browser↔MinIO upload/CORS, 반응형·접근성 시각 QA, vulnerability/container/secret scan과
  사람의 재배포 라이선스 검토도 남아 있다.

## 2026-07-11

### Image closure v2, CREPE safe loader, Torch 2.6/cu124 후보와 개발 번들 dev.6

**목적과 변경 파일**

- `installers/common/image_bundle.py`, 공통 `lib.sh`, Manager/Worker builder·installer·Compose
  wrapper·rollback과 Compose 설정에 bundle/image manifest format 2를 추가했다. Self-contained
  Manager는 `api|web|mlflow|postgres|redis|minio|minio-client|nginx` 8개, Worker는 verified
  runtime 1개만 허용한다. Strict JSON duplicate/unknown key, 안전한 archive 상대 경로,
  archive SHA-256/size, Docker-save `manifest.json`/Config/RepoTag, image/config digest,
  `linux/amd64`와 application OCI version/revision label을 load 전에 검증하고 load 직후 다시
  inspect한다. 알 수 없는 bundle format, symlink manifest, 누락/추가/중복 role/archive/tag/config는
  fail-closed한다.
- Manager dependency 5개는 `rvc-orchestrator-<dependency>:<version>` release-scoped alias로
  저장해 새 release load가 이전 rollback closure를 덮어쓰지 않게 했다. Self-contained 환경은 모든
  service에 `pull_policy=never`, partial/dev는 `missing`을 사용한다. Install은 같은 version의 기존
  release provenance를 load 전에 비교하고, archive load/post-load 검증 뒤에만 release/env/current를
  활성화한다. Compose start/restart/run/create와 Manager rollback은 installed manifest, release env와
  실제 tag→image ID를 재검증한다. Rollback은 target release env를 원자 게시한 뒤 start하고 실패하면
  env snapshot과 symlink를 복구한다.
- API/Web/MLflow/generic Worker와 RVC runtime Dockerfile/build Compose에 OCI release
  version/revision build arg·label을 연결했다. `Makefile`은 dependency-free image verifier도 Ruff와
  strict mypy 대상으로 검사한다. Self-contained builder는 실제 40-hex clean source revision을
  요구하므로 현재 uncommitted 저장소에서 최종 image archive를 만들 수 없고, 이를 우회하지 않았다.
- `apps/worker/src/rvc_worker/native_runner.py`, `native_inference.py`,
  `native_inference_driver.py`가 선택된 CREPE 요청에 한해 fixed
  `runtime/crepe/full.pth`를 허용한다. Host와 driver는 projection marker의 exact directory/file
  inventory, mode `0444`, size/SHA-256을 `O_NOFOLLOW` FD로 process 전·후와 replay/publication에서
  확인한다. Request/result에는 local path 대신 size/hash, `weights_only=true`, capacity `full`만
  기록한다.
- Driver는 `torchcrepe.Crepe("full")`을 만들고 검증 FD stream만
  `torch.load(..., map_location=device, weights_only=True)`로 읽어 strict state dict, `eval()`과
  `to(device)`를 적용한 뒤 `torchcrepe.infer.model/capacity`에 prebind한다. 이로써 torchcrepe의
  package cache/download loader가 호출되면 guarded loader가 unreviewed path로 거부한다.
  Same-attempt deployable model과 CREPE는 explicit `weights_only=True`, manifest-bound reviewed
  HuBERT/RMVPE는 explicit `weights_only=False`이며 다른 source, positional/pickle-module 또는 모순된
  trust mode를 거부한다. `TORCH_FORCE_WEIGHTS_ONLY_LOAD`는 reviewed asset 경계를 깨므로 설정하지
  않았다. Subprocess는 attempt-private HOME/TMP/Torch/HF cache와 HF/Transformers/Datasets offline
  flag만 받고 proxy/token 환경을 상속하지 않는다.
- `infra/worker/runtime/runtime.lock.env`, `verify_inputs.py`, `runtime_preflight.py`, builder,
  `apps/worker/Dockerfile.rvc`와 Worker bundle verifier를 Torch `2.6.0+cu124`, Torchvision
  `0.21.0+cu124`, Torchaudio `2.6.0+cu124`, CUDA 12.4/cu124, cuDNN 9 후보로 맞췄다.
  Upstream pinned RVC `pyproject.toml`의 2.4.0/0.19.0 marker는 source identity 검증용으로 그대로
  요구하되 release wheelhouse는 별도 reviewed compatibility lock을 사용한다.
  `runtime/crepe/full.pth`는 asset manifest 필수 record와 build-generated private projection에
  포함된다. Build manifest와 image label의 Python/Torch/CUDA/cuDNN/base/source/wheel/asset/
  projection/fairseq 값이 모두 일치해야 Worker bundle에 runtime을 넣을 수 있다.
- 최신 코드를 담은 `rvc-manager-0.1.0-dev.6-linux-amd64.tar.gz`와
  `rvc-worker-0.1.0-dev.6-linux-amd64.tar.gz`를 생성했다. Manager schema marker는
  `f9c4a7d2b610`이다. 둘 다 format 2 image manifest와 strict verifier를 포함하지만 image/archive
  inventory가 빈 `SELF_CONTAINED=false` 개발 번들이고, Worker runtime/native/GPU/profile/sample
  gate는 모두 false다.

**검증 결과**

- `make check` PASS: Ruff PASS, strict mypy `77` source files PASS, Python non-E2E
  `562 passed, 2 deselected`, Web Vitest `13 files/127 tests`, ESLint, Next.js production build,
  installer/infra shell syntax와 whitespace 검사 PASS.
- Worker 전체 `229` tests, CREPE/native/capability 집중 `71` tests PASS. Process 도달,
  path-free request evidence, missing/tampered/extra/unprojected asset, process 중 TOCTOU,
  result/replay tamper, strict prebind, true/false torch.load policy와 unreviewed source 거부를 포함한다.
  Capability는 계속 `supported_inference_f0_methods=[]`,
  `fixed_test_set_inference_ready=false`임을 확인했다.
- Image closure 관련 infra `64` tests와 신규 closure `17` tests PASS. Exact Manager 8/Worker 1,
  archive/config/tag tamper, arm64, dirty source, post-load mismatch, same-version pre-load 충돌,
  start/rollback identity와 legacy partial 경계를 포함한다. `docker-compose ... config --quiet`는
  Manager/Worker 모두 PASS했지만 실제 daemon은 사용하지 않았다.
- Runtime packaging `12` tests PASS. Torch 2.6/cu124 lock과 upstream 2.4 source marker의 의도적
  분리, CREPE 필수 asset/projection, CPU-level stub preflight, input TOCTOU, runtime manifest lock과
  bundle label 검증을 포함한다.
- Fresh SQLite를 base부터 `f9c4a7d2b610`까지 upgrade하고 `current`, `alembic check`를 PASS했다.
  PostgreSQL offline SQL에서도 PCM aggregate column/constraint와 단일 head를 확인했다.
- dev.6 두 외부 `.sha256`, archive 내부 모든 `SHA256SUMS`, image manifest strict 검증,
  host `__pycache__|*.pyc|*.pyo|.DS_Store` 부재와 빈 archive inventory를 확인했다. Manager SHA-256은
  `b4065076d6822a528e3939393cc4b16181a9110103aa8f2ba95fd264d2a1dcc0`, Worker SHA-256은
  `5f14e36dfedf4d4d6665097d2af07a8a9da488747358a137bdaf07aaf910cae9`다.

**남은 위험과 출시 gate**

- dev.6은 application/dependency image와 RVC runtime image를 포함하지 않는다. Clean committed
  source에서 실제 image를 빌드하고 Docker daemon의 save/load/inspect, exact no-pull start와
  previous-version rollback을 Ubuntu VM에서 검증해야 self-contained installer로 승격할 수 있다.
- PyTorch 공식 조합과 safe-loader API를 기준으로 후보를 고정했을 뿐, 실제 amd64 base digest와
  complete wheel hash를 아직 승인하지 않았고 container/OS/dependency vulnerability 및 license
  review도 하지 않았다. Torch 2.6이라는 버전만으로 untrusted pickle을 허용하지 않는다.
- 실제 `full.pth` byte의 source/license/redistribution/hash 승인, NVIDIA GPU에서 v1/v2·40k/48k·
  index on/off·PM/Harvest/CREPE/RMVPE matrix, network namespace/egress 차단과 cache/download 부재를
  증명해야 한다. 그 전까지 auto sample, fixed-TestSet capability, GPU/profile/sample verified gate는
  false로 유지한다.
- 실제 browser↔MinIO와 clean Ubuntu/NVIDIA 설치는 이 환경의 browser bind·Docker daemon/GPU 제한으로
  실행하지 않았다. 정적/fake-Docker 검증을 production smoke로 표현하지 않는다.

### Dataset exact PCM aggregate와 private 품질 보고서 경계

**목적과 변경 파일**

- `apps/api/src/rvc_manager_api/dataset_ingestion.py`가 PCM WAV를 bounded chunk로 읽을 때
  interleaved sample count, clipped/silent sample count와 `sum(sample²/full_scale²)`를 함께
  누산한다. Dataset aggregate `pcm-sample-weighted-v1`은 파일별 비율 평균이 아니라 전체 실제
  sample 수로 clipping/silence를 가중하고 `sqrt(Σ normalized_square_sum / sample_count)`로 RMS를
  계산한다. 8/16/24/32-bit와 다중 channel을 같은 정의로 처리하고 decoder 대기 파일은 집계에서
  제외한다.
- `models.py`, `schemas.py`, `services/datasets.py`, Dataset router와 migration
  `f9c4a7d2b610`에 source/skipped/rejected/duplicate count 및 PCM algorithm, validated-file/sample
  count, 세 metric과 silence threshold를 추가했다. DB constraint와 Pydantic schema는 aggregate가
  모두 null이거나 전부 존재하도록 하고 count 양수, ratio 0..1, threshold -120..<0과
  validated-file≤file-count를 강제한다. exact sample count가 없던 historical row는 raw report에서
  추정하지 않고 모두 null로 보존한다.
- `DatasetRead`에서 내부 `quality_report_json`을 제거했다. canonical/raw report는 source/member
  filename과 상세 거부 사유를 보존하는 내부 audit 자료로 남고, API는 bounded typed count와
  `pcm_quality`만 공개한다.
- `apps/web/src/lib/server/dataset-bff.ts`, API types/projection과 Dataset 목록·상세 화면은 nested
  aggregate의 exact key, 고정 algorithm, 정수 count, finite range를 검증하고 malformed upstream을
  502로 닫는다. 목록과 상세는 clipping/silence/RMS, 검증 파일/sample count와 threshold를 표시하며
  historical null을 `기존 행—재업로드 전 집계 없음`으로 명시한다. storage/raw report/member path는
  browser projection에 포함하지 않는다.

**검증 결과**

- API 전체 223 tests, 관련 Ruff와 strict mypy PASS. 8/16/24/32-bit·mono/stereo exact weighting,
  decoder-only null, schema bool/partial/non-finite 거부와 private report 비노출을 포함한다.
- Web Vitest 13 files/127 tests, ESLint와 Next.js production build PASS. BFF malformed aggregate 502,
  private nested field 비노출과 historical null 상태를 포함한다.
- fresh SQLite `alembic upgrade head`와 `alembic check` PASS. 단일 head는 `f9c4a7d2b610`이며,
  PostgreSQL offline SQL에서 `pcm_sample_count BIGINT`, aggregate constraint와 algorithm literal을
  확인했다. API 전체 실행에는 기존 TestSet fixture의 SQLAlchemy connection cleanup warning 1건이
  있었으나 실패는 없었다.

**남은 위험과 출시 gate**

- 실제 browser↔MinIO 대용량 upload, 반응형 layout, keyboard/screen-reader live 상태는 이 환경의
  local server bind 제한 때문에 E2E/시각 검증하지 못했다.
- non-WAV 격리 decoder, LUFS/noise-floor 분석은 아직 구현하지 않았다. decoder 대기 파일을 PCM
  aggregate에 섞거나 historical raw report에서 sample count를 추정하지 않는다.

### Dataset writer/finalizer fence, Experiment mutation UI와 개발 설치 번들 dev.5

**목적과 변경 파일**

- `apps/api/src/rvc_manager_api/models.py`, `config.py`, `routers/datasets.py`,
  `services/datasets.py`와 migration `e2f8b4c6a930`에 Dataset upload writer/finalizer fence를
  추가했다. local PUT은 Dataset→session 잠금 뒤 immutable session generation과 1회 write token을
  CAS claim하고 전송 중 heartbeat를 갱신한다. `expires_at`은 연결 시작 시각이 아니라 전체 body의
  절대 deadline이며, timeout은 writer coroutine/thread가 끝난 뒤 exact old staging key만 정리하고
  408로 종료한다. active/stale writer와 finalize 경합은 409로 fail-close한다.
- 새 Dataset generation은 같은 Dataset이라도 새 upload session ID를 사용하고 staging뿐 아니라
  original/prepared/manifest/quality canonical key도
  `datasets/verified/<dataset>/uploads/<session>/...` 아래 격리한다. finalize의 object spool,
  archive preparation과 네 canonical no-replace publish는 generation/finalization-token heartbeat를
  유지한다. 늦은 old writer/finalizer는 replacement session의 key를 알거나 삭제할 수 없고, 최종
  commit 직전 fresh Dataset→session lock에서 token 소유권을 다시 확인한다.
- request cancellation, publish 뒤 일반 예외와 DB commit 결과 모호성은 shield된 fresh DB session으로
  durable outcome을 다시 읽는다. 이미 `completed`면 네 canonical object를 보존하고, commit되지
  않았으면 해당 upload session이 실제 게시한 key만 정리한 뒤 pending/retryable로 token CAS 전이한다.
  cancellation-after-first-publish, commit 전 실패, commit 성공 뒤 오류 보고를 각각 회귀했다.
  DB가 완전히 불가해 outcome 자체를 읽을 수 없는 경우에는 corruption 방지를 위해 canonical을
  보존하며 operator tombstone/reconcile이 후속 gate다.
- `services/maintenance.py`의 Dataset task도 exact generation cleanup claim, active writer 보호와
  first-delete/confirmation second-delete를 적용했다. 전역 grace와 Dataset late-writer grace 중 큰
  값이 유효 grace이므로 기본은 `max(604800, 7200)=604800`초(7일), confirmation은 60초다.
  canonical key는 maintenance가 삭제하지 않는다. Dataset hard delete는 모든 expired/failed
  staging session의 이중 cleanup이 끝날 때까지 409로 거부한다.
- `e2f8b4c6a930` upgrade는 구 binary의 dataset-wide canonical key를 가진
  `pending|finalizing` row를 `expired`, `upload_fencing_upgrade_required`로 닫고 연결 Dataset을
  retryable `upload_pending`으로 되돌린다. completed legacy row/URI는 보존하고, 같은 idempotency
  payload replay는 quota를 점유하지 않는 old row 뒤에 generation+1/session-scoped key를 만든다.
  migration 자체가 구 process를 중단시키지는 않으므로 upgrade 전 API/client drain을 운영 전제로
  문서화했다.
- `.env.example`, Manager API/RQ Compose와 API 문서에 Dataset writer stale/heartbeat,
  late-writer/confirmation grace를 연결했다. RQ process도 eligibility 판단에 필요한 동일 설정을
  받는다. local PUT OpenAPI는 실제 400/401/404/408/409/410/411/413/422/503 경계를 선언한다.
- `apps/web`에는 Experiment detail의 same-origin PATCH/DELETE BFF와 설정 panel을 추가했다.
  PATCH는 query 없이 exact `{expected_row_version, description}`, DELETE는 단일 bounded
  `expected_row_version` query와 빈 body만 받는다. HttpOnly session만 Manager bearer로 보내고
  browser Authorization은 전달하지 않는다. response는 immutable identity와 row version을 포함한
  exact public projection만 `private, no-store`로 반환하며 known 409 detail만 stale/Job/MLflow safe
  code로 바꾼다.
- UI는 name/Dataset을 immutable로 표시하고 dirty description 성공에서 정확히
  `old_row_version + 1`만 수용한다. stale, 권한 실패와 응답 유실은 새 요청을 잠그고 page refresh를
  요구한다. 삭제는 Experiment 이름의 byte-for-byte 확인 뒤에만 활성화되고 성공 시 목록으로
  replace한다. demo mode에서는 mutation을 차단한다.
- 최신 infra/settings와 host-cache prune builder를 사용해
  `rvc-manager-0.1.0-dev.5-linux-amd64.tar.gz`와
  `rvc-worker-0.1.0-dev.5-linux-amd64.tar.gz`를 생성했다. Manager schema marker는
  `e2f8b4c6a930`이고 Worker runtime/GPU/profile/native-sample verified gate는 모두 false다.

**검증 결과**

- `make check` PASS — Ruff 전체 PASS, strict mypy 76 source files PASS, Python non-E2E
  `533 passed, 2 deselected`, Web Vitest 13 files/120 tests PASS, ESLint/TypeScript/Next production
  build, installer/infra shell syntax와 whitespace 검사 PASS.
- Manager 전체 `219/219`, Dataset API `22/22`, maintenance `42/42`, migration `14/14` PASS.
  Dataset slow PUT heartbeat/deadline/join, old writer/new staging 격리, slow/stale finalizer와 canonical
  격리, cancellation/commit outcome, upgrade-expired replay/quota, cleanup/delete fence를 포함한다.
  전체 부하에서 기존 TestSet 0.1초 expiry fixture가 writer 진입 전에 끝나는 scheduling flake를 두 번
  재현해 시작 margin만 1초로 늘렸다. stalled body는 10초라 절대 deadline 408 의미는 유지하며 최종
  전체 suite를 다시 통과했다.
- Experiment BFF 전용 20 tests와 전체 Web 120 tests, lint/build PASS. 실제 화면 확인을 위해 demo
  Next server를 `127.0.0.1:3100`에 열려 했으나 sandbox `EPERM`, 예외 실행은 현재 사용 한도 때문에
  승인되지 않았다. 우회하지 않았고 실제 browser/keyboard/screen-reader/API E2E는 미완료로 유지한다.
- fresh SQLite에 모든 revision을 `e2f8b4c6a930`까지 upgrade PASS,
  `alembic check`은 `No new upgrade operations detected`, 단일 head 확인 PASS. SQLite data
  upgrade/downgrade와 PostgreSQL offline migration 회귀도 PASS.
- Manager/Worker `docker-compose --env-file .env.example ... config --quiet` PASS.
- dev.5 외부 `.sha256`, 내부 모든 `SHA256SUMS`, host `__pycache__|*.pyc|*.pyo|.DS_Store` 부재,
  manifest schema/gate와 partial CycloneDX/license 경로를 검증했다. Manager SHA-256은
  `6a07b68b194cbba1c97ab759371d5744eda7c8b2e402d7db86b0161309319221`, Worker SHA-256은
  `623b42fb15d996f8adb1e2062ef3ecdd85d8ddb9a043a2cf9169eb81aa90d5f4`다.

**남은 위험과 출시 gate**

- 실제 원격 S3/MinIO에서 7일보다 긴 in-flight presigned Dataset/TestSet PUT, conditional canonical
  publish, Redis/RQ 유실과 PostgreSQL 다중 replica 경합을 장애 주입하지 않았다. DB가 완전히
  unavailable한 ambiguous finalize와 canonical delete tombstone/reconciler도 남아 있다.
- e2 production upgrade는 구 API replica/client drain, active upload 확인, migration, 동일
  idempotency generation replay와 rollback drill을 실제 PostgreSQL/MinIO에서 수행해야 한다.
- dev.5는 application image와 검증된 RVC/CUDA runtime을 포함하지 않은 개발 bundle이다. CREPE,
  Torch `>=2.6`, 실제 GPU/no-network matrix와 clean Ubuntu/NVIDIA 설치 lifecycle이 끝날 때까지
  production/self-contained installer로 표시하지 않는다.
- 실제 browser Experiment mutation/Sample A-B E2E, non-WAV sandbox decoder/LUFS,
  multipart/resume, 완전한 SBOM·취약점/container/secret scan과 법적 license 검토가 남아 있다.

### TestSet fencing/RQ cleanup, Sample 재생·A/B, Experiment 안전 CRUD와 개발 설치 번들 dev.4

**목적과 변경 파일**

- `apps/api/src/rvc_manager_api/services/test_sets.py`, TestSet router/storage 경계와 migration
  `a6c2e9f4b710`에 upload generation/write token, PUT/finalize heartbeat, cleanup claim generation과
  first/confirmation delete 시각을 추가했다. local PUT은 TestSet→session 잠금 아래 CAS heartbeat와
  session expiry 절대 deadline을 지키며 timeout은 exact generation만 `expired`로 닫는다. finalize는
  전체 byte/PCM 검증부터 no-replace canonical publish까지 finalization token heartbeat를 유지하고,
  stale generation이 새 session 또는 canonical object를 정리하지 못하게 했다.
- `services/maintenance.py`, `maintenance_queue.py`, `rq_worker.py`와 maintenance router에 TestSet
  전용 exact callable/envelope를 추가했다. PostgreSQL run의 task type을 reconciler가 보존하고,
  cleanup은 current namespace/generation/key와 claim을 다시 확인한다. 유효 grace는 전역 기본 7일과
  TestSet late-writer grace 중 큰 값이며 first-delete 뒤 기본 60초 confirmation grace를 거쳐
  second-delete까지 성공해야 완료된다. active/finalizing/completed session과 canonical key는 삭제하지
  않는다. queue 유실, poisoned/lost job과 storage retry도 기존 allowlist/bounded 원장 경계를 따른다.
- `apps/api/src/rvc_manager_api/routers/artifacts.py`와 `app.py`는 Sample WAV 응답을 canonical hash
  재검증에 결박하고 stable strong ETag, 200/206/416과 strong If-Range를 제공한다. 검증 semaphore와
  spool은 응답 완료 또는 disconnect까지 보유한 뒤 finally에서 해제하며 모든 Sample download 응답에
  `private, no-store`와 `Vary: Authorization`을 적용했다. 전용 download rate limit은 사용자별
  60회/분이다.
- `apps/web`의 Job detail은 Sample loading/empty/error/401/403, authoritative PCM metric과 provenance를
  표시하고 single Range/If-Range BFF로 재생한다. Experiment detail은 같은 TestSet item의
  current-attempt Sample 두 개만 A/B로 비교하며 stale generation과 duplicate item을 fail-close한다.
  JSON projection은 artifact/storage/query/token field를 allowlist 밖에서 제거하고 외부 redirect를
  거부한다. 실제 GPU matrix가 끝나지 않아 auto sample 생성 기본은 계속 false다.
- `apps/api/src/rvc_manager_api/routers/experiments.py`, schemas/models와 migration
  `d1e7a9c4f620`에 Experiment 안전 CRUD를 추가했다. name과 Dataset binding은 immutable이고
  description만 `expected_row_version` CAS로 PATCH한다. DELETE도 version/owner를 다시 확인하고 Job,
  MLflow 활성화 또는 projection/outbox 참조가 있으면 거부하며 FK는 `RESTRICT`다. owner/name unique
  conflict key는 신규 row에 강제하되 historical duplicate name과 owner 없는 row는 `NULL` 격리로
  ID와 Job 연결을 보존한다. POST/PATCH declared/chunked JSON은 기본 16 KiB 상한이고 create/update/
  delete를 audit한다. 현재 Alembic 단일 head는 `d1e7a9c4f620`이다.
- `AGENTS.md`, `CHECKLIST.md`, README와 Architecture/Security/Testing/Operations/Deployment/ADR/
  traceability를 위 불변조건과 남은 release gate에 맞췄다. `installers/common/lib.sh`와 두 bundle
  builder는 source tree에서 섞인 `__pycache__`, `.pyc|.pyo`, `.DS_Store`를 staging archive에서
  제거한다. packaging 회귀도 해당 host cache가 archive에 없음을 확인한다.
- 최초 생성한 `0.1.0-dev.3`은 host Python cache 포함을 발견해 사용 기준선에서 제외했다. 이를
  수정한 `rvc-manager-0.1.0-dev.4-linux-amd64.tar.gz`와
  `rvc-worker-0.1.0-dev.4-linux-amd64.tar.gz`를 새로 생성했으며 기존 archive를 덮어쓰거나 삭제하지
  않았다. Manager manifest의 schema compatibility marker는 `d1e7a9c4f620`이고 Worker manifest는
  runtime/GPU/profile/native-sample verified gate를 모두 false로 유지한다.

**검증 결과**

- `make check` PASS — Ruff 전체 PASS, strict mypy 76 source files PASS, Python non-E2E
  `517 passed, 2 deselected`, Web Vitest 12 files/103 tests PASS, ESLint와 Next.js production build,
  전체 installer/infra shell syntax와 `git diff --check` PASS.
- Experiment CRUD/migration targeted suite 7 tests PASS. TestSet/maintenance/migration targeted suite와
  Sample route, cancellation/disconnect/Range/OpenAPI 회귀 PASS. Worker rotation/revoke targeted suite
  34 tests와 revoke/status/claim 30회 연속 race 회귀도 PASS.
- 새 SQLite DB `alembic upgrade head` PASS, `alembic check`은
  `No new upgrade operations detected`. PostgreSQL offline migration SQL과 SQLite upgrade/downgrade
  회귀 PASS.
- Manager/Worker `docker-compose --env-file .env.example ... config --quiet` 모두 PASS.
- dev.4 외부 `.sha256`, archive 내부 모든 `SHA256SUMS`, host cache 부재, manifest의 schema/gate,
  partial CycloneDX SBOM과 third-party license 경로를 각각 검증했다. Manager SHA-256은
  `54b7a4cb093ae9d31b2242d3904dc058538029016777f182a9c7ab9e93114c7d`, Worker SHA-256은
  `e54f371423a043b15e1b1cf761f1fd377114e2acadea71e20727eee06ac50aae`다.

**남은 위험과 출시 gate**

- dev.4는 설치/upgrade/backup/restore script, Compose와 partial SBOM을 담은 개발 archive일 뿐
  application image와 검증된 RVC/CUDA runtime image를 포함하지 않는다. air-gapped self-contained
  installer 또는 production release로 표시하지 않는다.
- 실제 PostgreSQL 다중 replica, Redis/RQ/MinIO 장애 주입, 전역 7일 grace보다 긴 실제 S3
  in-flight TestSet PUT과 cleanup/finalize 경합, clean Ubuntu Manager/NVIDIA Worker 설치·재부팅·
  upgrade/rollback/remove smoke가 남아 있다. 이 작업 환경에서는 Docker daemon 실행 제한으로
  최신 container 통합 smoke를 재실행하지 못했다.
- CREPE의 권리·hash가 고정된 offline asset, Torch `>=2.6` safe release runtime과 실제
  v1/v2×40k/48k×F0/index/no-index GPU/no-network matrix가 남아 있다. 그 전까지
  `AUTO_SAMPLE_JOBS_ENABLED=false`, `fixed_test_set_inference_ready=false`와 bundle의 모든 GPU/
  native sample verified gate를 false로 유지한다.
- Experiment description PATCH/delete 대시보드 UI, 실제 browser/API Sample Range/A/B E2E,
  non-WAV sandbox decoder/LUFS, Dataset streaming upload fencing·canonical tombstone, 완전한 SBOM과
  vulnerability/container/secret scan 및 법적 license 검토가 남아 있다.

### Worker별 token 2단계 회전, 비상 폐기와 동일 identity 재등록

**목적과 변경 파일**

- `packages/contracts/.../worker.py`, Manager `models.py`, `routers/workers.py`,
  `services/workers.py`, `rate_limit.py`에 crash-recoverable Worker token protocol을 추가했다.
  idle/no-active-lease Worker만 prepare할 수 있고 Manager는 새 token의 HMAC-SHA256과 rotation
  ID/만료만 저장한다. pending 평문은 `private, no-store`/`Pragma: no-cache` 1회 응답이며 표준
  Worker bearer로는 쓸 수 없다. Worker가 old bearer와 pending 전용 header를 함께 증명해
  activate하면 old token을 즉시 폐기한다. pending 동안 claim은 409다. same/different prepare
  replay는 평문을 다시 내주지 않으며 abort/expiry에서 old token을 보존한다.
- `apps/worker/src/rvc_worker/credentials.py`, `token_rotation.py`, `client.py`, `cli.py`는 schema v2
  mode 0600 credential에 old/pending/rotation/expiry를 원자 저장하고 directory fsync한다.
  prepare 응답 유실은 old token으로 server pending을 abort하고, activate 응답 유실은 pending
  token으로 session을 증명해 완료한다. 관리자 revoke로 old/pending이 모두 401이면 로컬 두
  secret을 지우지 않고 fail-closed한다. 운영자는 idle Worker에서 `--rotate-token`을 실행한다.
- admin `POST /workers/{id}/token/revoke`는 admin JWT, exact Worker name과 bounded reason code를
  요구한다. 기본은 active assignment를 409로 거부하고, 명시적 force만 canonical
  lease→Job→attempt→Worker lock에서 Job/attempt cancelled, lease released, MLflow terminal
  outbox와 audit를 같은 transaction에 쓴 뒤 Worker hash를 폐기한다. inactive/unassigned row만
  shared bootstrap과 exact ID/name으로 `/workers/re-enroll`할 수 있고 Worker CLI `--re-enroll`이
  새 1회 token을 기존 credential 파일에 원자 저장한다.
- `workers`와 `jobs`에 optimistic `row_version`을 둔 migration head
  `c4d9e8f1a720`을 추가했다. ORM write와 atomic claim Core UPDATE가 version CAS를 사용하므로
  PostgreSQL row lock뿐 아니라 SQLite의 무효 `FOR UPDATE` 환경에서도 revoke terminal state를
  stale status/claim commit이 덮지 못하고 경합 패자는 rollback 409가 된다.
- Worker rotation/revoke는 Redis HMAC key 기반 전용 6회/분 rule을 사용한다. Sample WAV GET도
  등록 30회/분과 분리한 exact safe-ID download rule 60회/분을 추가했다. `.env.example`, Manager
  Compose/API README와 Architecture/Security/Testing/Operations/traceability/checklist/AGENTS를
  함께 갱신했다.

**검증 결과**

- `.venv/bin/pytest -q packages/contracts/tests apps/api/tests apps/worker/tests` — 수집된 468개
  전체 PASS. token 전용 contracts/Manager/Worker targeted suite도 PASS했다.
- revoke와 status/claim 동시 요청 회귀를 30회 연속 실행해 모두 PASS했고, 첫 optimistic
  conflict를 강제한 회귀에서 bounded fresh reload 1회 뒤 revoke 200, audit 단일 기록과 old-token
  401을 확인했다.
- `ruff check` 전체 contracts/API/Worker source+tests PASS, `mypy --strict` 75 source files PASS.
- 새 SQLite DB에서 `alembic upgrade head` PASS, `alembic check`는
  `No new upgrade operations detected`; c4 SQLite upgrade/backfill/constraint/index/downgrade와
  PostgreSQL offline SQL 회귀 PASS.
- `docker-compose --env-file .env.example ... config --quiet` Manager/Worker 모두 PASS. 이 host의
  `docker compose` plugin은 없어서 설치된 `docker-compose` binary로 렌더링했다.

**남은 위험과 출시 gate**

- 실제 PostgreSQL 다중 API replica에서 row lock+version CAS 경합, 실제 systemd/Compose host의
  stop→rotate/re-enroll→start와 관리자 침해 대응 drill은 clean VM gate에 포함해 아직 실행해야
  한다. bootstrap 자체가 노출되면 inactive Worker 재등록도 신뢰할 수 없으므로 Manager
  bootstrap secret을 먼저 교체하고 전달 경로를 조사해야 한다.

### Native 고정 TestSet 추론, 검증형 Sample 원장과 개발 설치 번들 dev.2

**목적과 변경 파일**

- `packages/contracts`의 Sample wire contract에 단일 출력 256 MiB/600초/2 channel과
  attempt 논리 출력 합계 2 GiB/3,600초를 고정했다. 등록 payload는 native inference
  manifest/request SHA-256을 필수로 담고, TestSet item은 전체 전송·Artifact session quota를
  현실적으로 검증할 수 있도록 128개를 hard cap으로 통일했다. sample-enabled `JobConfig`는
  `collect_samples`, `collect_small_model`, nonzero `index_rate`의 `collect_index`와
  `collect_added_index`가 모두 실제 게시 의미와 맞아야 생성된다.
- `apps/worker/src/rvc_worker/native_inference.py`, `native_inference_driver.py`와
  `native_runner.py`에 reviewed RVC Pipeline 기반 PM/Harvest/RMVPE fixed-TestSet inference를
  연결했다. model/index/TestSet/operator asset은 FD와 SHA-256으로 다시 확인하고, request 원문
  hash를 argv에 결박한 shell-free subprocess, timeout/cancel join, output/inventory/permission
  상한과 `pcm-normalized-v2` 지표를 적용한다. driver는 각 WAV를 쓰기 전에 단일·누적 byte/
  duration을 검사하고 host result·published manifest도 다시 검증한다. FAISS index를 요청했는데
  실제로 사용하지 않은 silent fallback을 거부한다. CREPE는 manifest-pinned offline
  torchcrepe asset이 없으므로 명시적으로 fail-closed한다.
- `apps/worker/src/rvc_worker/sample_publication.py`, `agent.py`, `uploads.py`와 `client.py`는
  native manifest의 model/index/output을 verified Artifact upload/finalize로 게시하고 논리
  Sample을 순서대로 등록한다. Artifact metadata에는 approved runtime image/asset digest,
  reviewed commit, native manifest/request SHA-256과
  `sample_model|sample_index|sample_output` 역할만 둔다. 특정 첫 item의 self-asserted 등록 payload는
  넣지 않는다. 여러 item이 동일 PCM SHA를 만들면 첫 파일만 업로드하고 나머지는 같은
  `artifact_id`를 참조하지만 item/input별 Sample row는 따로 등록한다. Manager 응답은 create
  `201`과 exact replay `200`만 허용하며 다른 2xx, 느린 trickle timeout, 취소와 typed 409/422/
  429/503을 fail-closed한다.
- `apps/api`에는 Sample 등록 raw body 64 KiB middleware, 전용 Redis 분당 30회 제한, process
  semaphore, artifact/attempt PostgreSQL advisory transaction lock과 canonical lease→Job→attempt→
  Worker row fence를 추가했다. approved runtime digest 쌍은 claim 때 attempt에 snapshot하고 등록과
  completion에서 current Worker capability까지 다시 대조한다. model/index/output verified session의
  SHA·size·type·namespace·역할·native provenance를 교차검증하고 Manager가 canonical PCM을 다시
  읽어 계산한 지표만 authoritative evidence로 저장한다. exact replay를 합계 계산 전에 분리하고
  `JobAttempt FOR UPDATE` 아래 기존+신규 출력 합계를 검사한다.
- migration head `b8e4a1c6d230`은 JobAttempt runtime attestation과 Sample native manifest/request
  hash를 추가했다. 동일 PCM을 여러 논리 Sample이 공유할 수 있도록 `uq_sample_artifact`는
  제거하되 Job/attempt/TestSet/item composite graph는 유지했다. completion은 모든 TestSet item,
  단일 provenance 집합과 현재 canonical model/index/output byte를 단일 deadline으로 다시
  검증하고 마지막 commit 직전 lease/attempt fence도 재확인한다.
- canonical local publish는 atomic no-replace link+directory fsync+read-only mode, S3 publish는
  `If-None-Match: *` conditional put을 사용한다. Sample download는 매번 current byte를 spool/
  rehash하고 same-origin presigned redirect에는 bearer/cookie를 전달하지 않는다. 같은 크기의
  canonical byte 변조, transient storage 오류, 8-bit 양/음 clipping rail과 OpenAPI operational
  status를 회귀했다.
- 요청 취소 중 `asyncio.to_thread` PCM scan이 semaphore 밖에서 계속되는 일을 막기 위해 검사
  task를 shield+join한 뒤에만 spool과 slot을 정리한다. 공용 `verify_object_to_spool`도 create/
  write/fsync/close/unlink를 cancellation-safe join 경계로 바꿔 `CancelledError`에서 열린 FD와
  partial spool을 남기지 않는다. default/TLS proxy 모두 Sample route에 64 KiB 제한을 별도로
  적용한다.
- README, Architecture, Security, Testing, Deployment, Operations, Runtime Matrix, ADR-0004,
  traceability, checklist와 이 AGENTS/history 체계를 현재 구현과 release gate에 맞췄다.
  `dist/installers`에는 최신 infra/script, partial CycloneDX/license report와 내부 checksum을 담은
  `rvc-manager-0.1.0-dev.2-linux-amd64.tar.gz` 및
  `rvc-worker-0.1.0-dev.2-linux-amd64.tar.gz`를 새로 생성했다. image/runtime을 포함하지 않은
  개발 후보이며 기존 산출물을 덮어쓰지 않았다.

**검증 결과**

- `make check` — Ruff PASS, mypy 75 source files PASS, Python `479 passed, 2 deselected`, Web
  Vitest `86 passed`, ESLint PASS, Next.js production build PASS, installer/infra shell syntax PASS.
- 독립 범위 실행 — contracts+Worker `258 tests PASS`, Manager `174 tests PASS`; Sample HTTP
  exact-status 보강 뒤 `apps/worker/tests/test_sample_publication.py` 13 tests PASS.
- 새 SQLite DB에 `alembic upgrade head` — 모든 revision PASS;
  `alembic check` — `No new upgrade operations detected`.
- `docker-compose --env-file .env.example ... config --quiet` — Manager와 Worker 구성 모두 PASS.
- dev.2 외부 SHA-256과 archive 내부 `SHA256SUMS`, partial SBOM/license path 검증 PASS.
  Manager archive SHA-256은 `46adfcb59ce8a063b56b71e7848263c73fc97d5e2c7b493b08b63f41fa1334a7`,
  Worker archive SHA-256은 `154bac4a2170af0f6f1aa23fb844deb3d464ef3974eb8492a5330579d2248cf3`다.

**남은 위험과 출시 gate**

- 현재 runtime packaging lock은 upstream 후보 Torch 2.4/cu121이고 native inference driver는
  안전 경계상 Torch `>=2.6`을 요구한다. CREPE용 권리·출처·hash가 고정된 offline asset도 없다.
  따라서 Agent는 `supported_inference_f0_methods=[]`,
  `fixed_test_set_inference_ready=false`를 광고하고 Manager
  `AUTO_SAMPLE_JOBS_ENABLED=false`, bundle `RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false`와
  `PROFILE_STAGE_SET_VERIFIED=false`를 유지한다.
- 실제 NVIDIA GPU에서 v1/v2×40k/48k×F0/index/no-index, no-network 실행, Torch/FAISS/RMVPE/
  CREPE 호환성, 2 GiB canonical object를 기본 120초 안에 검증할 MinIO 성능을 아직 측정하지
  않았다. fixture 결과를 GPU 완료로 해석하지 않는다.
- 실제 PostgreSQL 다중 replica advisory-lock 경쟁, Redis rate/RQ recovery, MinIO conditional
  write·변조·장애 주입은 Docker daemon을 사용할 수 없어 실행하지 못했다. clean Ubuntu
  Manager VM과 clean NVIDIA Worker VM의 설치/upgrade/rollback/remove smoke도 남아 있다.
- TestSet staging orphan cleanup/finalize heartbeat·late-writer fencing, Worker token rotation,
  non-WAV sandbox decoder/LUFS, sample player/A-B UI, 완전한 SBOM/취약점·법적 검토는 계속
  체크리스트의 미완료 release gate다.

### RQ exact envelope 복구와 PostgreSQL maintenance run reconciler

**목적과 변경 파일**

- `maintenance_queue.py`로 RQ Worker와 enqueue adapter가 공유하는 no-resolve execution
  policy를 옮겼다. deterministic job ID가 Redis에 있다는 이유만으로 existing을 반환하지 않고
  exact queue/origin, JSON serializer, 고정 callable, canonical run UUID 단일 args, empty
  kwargs/meta/dependency/dependent/callback/repeat/group/allow-dependency/enqueue-front, 고정
  description/timeout/result·failure·job TTL과 DB run의 bounded retry snapshot을 모두 확인한다.
  queued list, scheduled registry 또는 started/intermediate 위치도 대조한다.
- inactive poisoned/ghost/terminal job은 per-job Redis distributed enqueue lock 안에서 Lua가
  queue/intermediate/known registry/hash/dependency set만 원자 제거한다. callback/callable을
  import하거나 dependent를 enqueue하지 않고 exact job을 재생성한다. started/intermediate poison과
  해석 불가능한 상태는 실행 중 삭제/중복 생성하지 않고 typed envelope conflict를 반환한다.
  final-attempt inspection은 missing job을 만들지 않으며 exact started job은 그대로 인정한다.
- `rq_worker.py`는 같은 validator를 dequeue 직후와 perform 직전에 계속 사용한다. custom
  success/failure handler의 dependent/repeat 비실행 경계도 유지했다.
- `services/maintenance.py`에 API replica periodic reconciler를 추가했다. PostgreSQL에서는
  transaction advisory lock과 `FOR UPDATE SKIP LOCKED`, process 내에서는 non-blocking local lock을
  사용해 한 bounded cycle만 `queued|retrying|enqueue_failed`와 timeout을 넘긴 stale `running`
  원장 행을 재전달한다. 새 run을 만들지 않고 `completed|failed`를 선택하지 않는다. Redis 첫
  장애에서 cycle을 중단해 batch×socket-timeout 정체를 피하고 typed `enqueue_failed`를 남긴다.
  attempt 상한은 terminal fence로 닫되 exact started final attempt는 중복 생성하지 않는다.
  cleanup completion도 status+attempt ownership을 다시 확인해 reconciler terminal 결정을 덮지 못한다.
- `routers/maintenance.py`는 기존 queued/retrying run idempotency replay에서도 queue exactness를
  다시 검증하고, queue unavailable은 ledger-committed `enqueue_failed`, active poison/config/
  identity conflict는 typed `failed`로 보존한다. `app.py`, `routers/health.py`, `config.py`,
  `.env.example`과 Manager Compose는 15초 주기, 120초 stale readiness, cycle당 100 run 기본의
  reconciler lifecycle과 prompt shutdown을 연결했다. `/ready`는 Redis/RQ Worker와 별도로
  `maintenance_reconciler` freshness를 fail-closed한다.
- `test_maintenance.py`는 exact active job, full non-execution surface, callback/dependent 비실행,
  inactive quarantine/recreate, started poison, inspect-only final attempt, lost run 복구, attempt/
  completion fence, Redis 장애, 동시 leader, coordinator readiness/shutdown과 admin typed 상태를
  검증한다. API README, Architecture/Security/Operations/Testing/Deployment, AGENTS, checklist와
  traceability도 PostgreSQL 원장이 진실인 복구 규칙으로 갱신했다.

**검증 결과와 남은 위험**

- `.venv/bin/pytest -q apps/api/tests/test_maintenance.py` — 35 tests PASS.
- `.venv/bin/pytest -q apps/api/tests/test_maintenance.py apps/api/tests/test_api.py` —
  maintenance/readiness 포함 50 tests PASS.
- maintenance 변경 범위 Ruff — PASS. `.venv/bin/mypy apps/api/src` — strict 39 source files PASS.
- 전체 `apps/api/tests` 실행은 maintenance를 포함해 진행됐고 unrelated SSE fixture의 Python
  object ID 재사용 assertion 1건만 간헐 실패했다. 해당
  `test_log_sse_is_bounded_resumable_redacted_and_not_cacheable` 단독 재실행은 PASS했다.
- `docker-compose --env-file .env.example -f infra/compose/manager.compose.yml config --quiet` — PASS.
- 실제 Redis에서 Lua quarantine와 distributed enqueue lock, 실제 PostgreSQL에서 advisory lock을
  쓰는 다중 API replica/Worker restart·FLUSH·TTL-loss 장애 주입은 아직 실행하지 않았다. Redis
  lock 점유/heartbeat 위조/queue 삭제 DoS, maintenance 전용 PostgreSQL role, staging-prefix S3
  credential과 Redis ACL, 매우 느린 in-flight PUT generation fencing도 release gate다.

### Lease-bound TestSet claim/download와 Worker atomic PCM materializer

**목적과 변경 파일**

- `packages/contracts/src/rvc_orchestrator_contracts/worker.py`와 export는 storage-neutral
  `TestSetTransfer`/item 계약을 추가했다. claim에는 내부 URI나 presigned query 대신
  TestSet/family/revision, manifest·Job DB `sample_plan_sha256`·inline inference config hash,
  ordered item ID/key/order/size/SHA/PCM metadata와 current Job에 결박된 Manager 상대 path만
  들어간다. item ID/key/order 중복과 비정렬, inference snapshot hash 불일치, path/filename
  mismatch와 128 item·2 GiB·총 3,600초 wire 상한을 계약에서 거부한다.
  `WorkerCapabilities`는 inference F0 목록과
  `fixed_test_set_inference_ready`를 분리하며, true는 real RVC mode+ready asset+비어 있지 않은
  inference method를 모두 요구한다. sample이 enabled이고 index를 만들지 않는 Job은
  `index_rate=0`만 허용해 존재하지 않는 retrieval index를 사용하는 snapshot을 막는다.
- `apps/api/src/rvc_manager_api/services/workers.py`와 `routers/workers.py`는 sample-enabled Job을
  capability/method가 맞는 Worker에만 배정한다. claim 직전 ready TestSet manifest object의
  exact byte, DB item/manifest, Job sample-plan snapshot, completed upload/canonical URI·namespace를
  다시 검증하고 어느 하나라도 다르면 Job을 queued로 보존한다. DB `sample_plan_json`과 hash를
  독립적으로 재계산하므로 hash만 변조돼도 Worker를 점유하지 않는다. item GET도 current Worker/
  lease/attempt와 Job/TestSet/item identity, namespace와 전체 transfer snapshot을 재검증한 뒤
  Local bounded `audio/wav` stream 또는 짧은 단일 307만 반환한다.
- 새 `apps/worker/src/rvc_worker/test_sets.py`와 `client.py`는 ordered item을 attempt의
  `inputs/test_set/<item-id>.wav`로 받는다. Manager 첫 요청에만 bearer/lease/attempt를 보내고,
  외부 307은 별도 cookie-empty client와 `trust_env=false`로 열어 Authorization, lease/attempt,
  Manager response cookie와 proxy credential을 전달하지 않는다. Content-Length/MIME/encoding/
  size/SHA를 검증하고 `O_NOFOLLOW`, mode `0600`, fsync와 same-directory atomic publish를 쓴다.
  materializer는 count/item/total/duration/rate/channel과 bounded retry/cancel을 선검사하고,
  RIFF/WAVE uncompressed PCM을 최대 65,536 frame chunk로 검증한다. 전용 mode `0700` partial
  directory의 exact inventory를 재검증한 뒤 디렉터리 전체를 원자 게시하며 replay도
  symlink/extra/stale/mode/hash/PCM을 다시 검사한다. provenance marker에는 Manager가
  재검증한 sample-plan hash, inference config/hash와 ordered item PCM metadata를 기록한다.
- Dataset 외부 307도 같은 fresh-client 경계로 강화해 Manager가 응답으로 설정한 광범위 cookie,
  Authorization/lease/attempt와 environment proxy credential이 object host로 넘어가지 않는다.
  `settings.py`, CLI, `.env.example`과 Worker Compose에는 TestSet timeout/retry/count/item/total/
  PCM 상한을 연결했다. `stages.py`는 기존 Dataset/Artifact에 TestSet transfer 명시적 내부
  retry scope를 추가하고 typed integrity/cancel 분류를 사용한다.
- 현재 Agent는 `supported_inference_f0_methods=[]`,
  `fixed_test_set_inference_ready=false`만 광고한다. Manager matching이 sample Job을 배정하지
  않고, 잘못 배정돼도 Worker가 workspace 생성과 Dataset/TestSet data-plane 전에
  `worker_runtime_unready`로 거부한다. claim·lease·terminal status 같은 Manager control-plane
  요청까지 없다는 뜻은 아니다.
  `TestSetStageRunner`가 native runner까지 중첩돼도 commit/asset/claim 검증을 재귀적으로
  unwrap하므로 기존 runtime gate를 우회하지 않는다.
- `AGENTS.md`, `CHECKLIST.md`, Architecture/Traceability/Operations/Runtime Matrix/Testing 문서는
  전송 완료 범위와 외부 307 credential/cookie 경계를 반영했다. inference, canonical Artifact 뒤
  Sample 등록/completion, GPU matrix와 TestSet staging maintenance는 완료로 표시하지 않았다.

**검증 결과와 남은 위험**

- `PYTHONPATH=packages/contracts/src:apps/api/src:apps/worker/src .venv/bin/pytest -q
  packages/contracts/tests/test_contracts.py packages/contracts/tests/test_test_set_transfer_contracts.py apps/api/tests/test_test_sets.py
  apps/worker/tests/test_test_set_transfer.py apps/worker/tests/test_dataset_transfer.py` — 87 tests
  PASS. no-index `index_rate=0`, capability/method claim 차단, manifest/object/namespace와 DB
  sample-plan hash 변조 재검증, lease item GET, storage URI
  비노출, 외부 307 credential/cookie/proxy 비전달, unsafe redirect chain, exact response metadata,
  atomic/replay/WAV/상한/retry/cancel을 포함한다.
- `make lint-python`과 `make typecheck-python` — PASS. Worker Compose를 `.env.example`로 render한
  `docker-compose --env-file .env.example -f infra/compose/worker.compose.yml config -q` — PASS.
  전체 non-E2E 최종 개수는 이 항목 뒤 root 보안 패치까지 끝난 상태에서 다시 확정해야 하므로
  여기서는 targeted 87개 결과만 기준선으로 기록한다.
- 이 변경은 고정 TestSet inference 명령, 변환 output의 canonical Artifact 게시 뒤 Sample 등록,
  sample-enabled Job completion gate를 구현하지 않았다. 실제 F0 4종, v1/v2·40k/48k·index,
  Torch `>=2.6`/serialization trust와 GPU/no-network matrix도 남아 있다. 따라서
  `AUTO_SAMPLE_JOBS_ENABLED=false`, `fixed_test_set_inference_ready=false`, 빈 inference F0
  capability와 `PROFILE_STAGE_SET_VERIFIED=false`를 유지한다. 실제 원격 S3/HTTPS 307 통합,
  TestSet staging orphan cleanup/finalization fencing도 별도 release gate다.

### Dataset/Artifact exact storage namespace 결박과 검증형 historical adoption

**목적과 변경 파일**

- `storage.py`, `models.py`와 새 Alembic head `9d2f4b7c8e10`은 Dataset/Artifact upload
  session에 credential 없는 `storage_namespace_sha256`을 필수로 저장한다. local은 resolve된
  root, S3는 endpoint/bucket/region/addressing style을 fingerprint하므로 access key/secret
  회전은 같은 namespace지만 같은 backend의 다른 root/bucket/endpoint는 다르다. migration은
  과거 행이 어느 namespace의 byte인지 추측하지 않고 64자리 zero `UNBOUND` sentinel로 채운
  뒤 server default를 제거한다.
- `routers/datasets.py`, `routers/artifacts.py`, `routers/workers.py`, `services/workers.py`와
  `services/maintenance.py`는 init replay, local PUT, stale expiry/retry, finalize, Dataset delete,
  Worker claim/Dataset GET, Artifact download와 Dataset staging cleanup 전부에서 session의
  backend+fingerprint를 현재 adapter와 대조한다. mismatch/UNBOUND이면 원장과 staging/canonical
  object를 보존하고 fail-closed하며, maintenance는 `storage_namespace_mismatch`를 typed deferred
  결과로 기록하고 `cleanup_completed_at`을 쓰지 않는다.
- 새 `services/storage_adoption.py`와 `storage_adoption.py`, `pyproject.toml` console script는
  historical terminal session을 명시적으로 결박한다. Dataset completed는 original,
  prepared-flat, manifest, quality-report 네 object와 DB URI/metadata, Artifact completed는
  canonical object, failed/expired는 staging 전체의 size/SHA-256을 bounded stream으로 다시
  검증한다. `pending|finalizing`은 항상 거부한다. 기본 preview는 binding/object를 바꾸지 않지만
  audit event를 쓰며 target backend/namespace hash를 결과와 audit에 남긴다. apply는 검증된
  행만 결박하고 같은 current namespace의 explicit 재실행은 `verified/already_bound`로 멱등
  성공한다. 다른 namespace 결박, object/metadata mismatch와 missing session은 성공으로
  표시하지 않으며 CLI의 예상 밖 DB/adapter 오류는 URL/query/credential 없는 generic 오류로
  종료한다.
- `test_migrations.py`, `test_dataset_upload_api.py`, `test_artifact_storage.py`,
  `test_maintenance.py`, 새 `test_storage_adoption_cli.py`는 SQLite historical UNBOUND/default 제거/
  constraint/downgrade, PostgreSQL offline upgrade/downgrade SQL, 같은 backend의 다른 namespace
  init/PUT/finalize/delete/claim/GET/download/cleanup 차단과 object 보존, S3 credential rotation,
  Dataset/Artifact full-byte adoption preview/apply/idempotent audit와 CLI 비밀 비노출을 검증한다.
- `AGENTS.md`, `CHECKLIST.md`, API README, Architecture/Security/Operations/Testing/Deployment와
  traceability를 갱신했다. 최초 upgrade 전 active upload drain, preview도 audit write라는 의미,
  batch apply가 all-or-nothing이 아닌 점, UNBOUND가 quota/cleanup을 막을 수 있는 점과 명시적
  operator 절차를 runbook에 고정했다.

**검증 결과와 남은 위험**

- `.venv/bin/ruff check apps/api/src apps/api/tests` — PASS.
- `.venv/bin/mypy apps/api/src` — strict 38 source files PASS.
- `.venv/bin/pytest -q apps/api/tests/test_migrations.py
  apps/api/tests/test_dataset_upload_api.py apps/api/tests/test_artifact_storage.py
  apps/api/tests/test_maintenance.py apps/api/tests/test_storage_adoption_cli.py
  apps/api/tests/test_test_sets.py` — 65 tests PASS.
- `.venv/bin/pytest -q apps/api/tests` — 전체 Manager API suite PASS.
- 새 SQLite에 `alembic upgrade head` 후 `.venv/bin/alembic -c apps/api/alembic.ini check` —
  `No new upgrade operations detected`.
- `PYTHONPATH=apps/api/src .venv/bin/python -m rvc_manager_api.storage_adoption --help` — PASS.
  pre-existing venv의 console script 재설치를 위한 editable `pip install`은 local hatchling이 없고
  sandbox network가 차단되어 실행하지 못했다. parser/error-boundary 자동 테스트는 통과했으며
  fresh API image/bundle에서 console entrypoint 호출 smoke는 packaging release 검증에 남긴다.
- 실제 production PostgreSQL/MinIO의 pre-upgrade drain, historical object adoption, mixed-result
  batch와 rollback drill은 아직 실행하지 않았다. active UNBOUND는 의도적으로 adoption할 수
  없고 owner/attempt quota를 점유하며 cleanup을 보류할 수 있으므로 upgrade 전 drain이 필수다.
  terminal session의 expected object가 이미 없거나 metadata가 불완전하면 byte identity를
  증명할 수 없어 계속 rejected/UNBOUND로 남는다. 이를 자동 현재-namespace backfill이나 수동
  key 삭제로 우회하지 않고 별도 incident/retention 정책을 검토해야 한다. 실제 S3 credential
  회전과 endpoint/bucket 이관 장애 주입도 clean deployment release gate로 남는다.

### Sample inference resample 계약과 model 직렬화 신뢰 gate

**목적과 변경 파일**

- `packages/contracts/src/rvc_orchestrator_contracts/job.py`의 Job inline sample 설정과
  immutable Preset 설정에 같은 disjoint contract type을 연결해 OpenAPI까지 `resample_sr`를 미적용 `0` 또는
  upstream이 실제 resample로 처리하는 `16000..192000` Hz로 제한했다. 이전 계약이 허용한
  `1..15999`는 upstream 조건 `tgt_sr != resample_sr >= 16000`에서 조용히 미적용되어 설정
  snapshot과 실제 동작의 의미가 달랐다. boundary pre-validation은 Pydantic coercion도
  차단해 JSON boolean, 문자열과 정수값 float를 exact integer로 받아들이지 않는다.
- `packages/contracts/tests/test_contracts.py`는 두 설정의 경계값을, 새 Worker
  `test_sample_config_validation.py`는 Manager 응답 `JobClaim` parsing 경계를 검증한다.
  `apps/api/tests/test_api.py`에는 TestSet 원장 없이도 disabled sample config의 `15999`를
  실제 `POST /jobs`가 422로 거부하는 회귀를 추가했다. 병렬 작업 중인 TestSet API/model/
  migration 파일은 수정하지 않았다.
- `docs/RVC_UPSTREAM_NOTES.md`에 공식 commit
  `7ef19867780cf703841ebafb565a4e47d1ea86ff`의 `modules.py`, `pipeline.py`, `utils.py`,
  `audio.py`, `config.py` SHA-256을 기록했다. `VC.get_vc()`의 기본 `torch.load`와 class
  fallback/environment scan, `vc_single()`의 traceback 반환, FAISS no-index fallback,
  상대 HuBERT/ffmpeg 경로 때문에 upstream CLI를 그대로 실행하지 않고 typed wrapper와
  attempt-private projection을 사용해야 한다는 결정을 남겼다.
- `docs/SECURITY.md`와 `docs/RVC_RUNTIME_MATRIX.md`에는 첫 후보 Torch 2.4.0이
  CVE-2025-32434 영향 범위이며 `weights_only=True`와 현재 `ProcessSpec`이 untrusted model
  sandbox가 아니라는 점을 추가했다. 같은 pinned attempt가 만든 exact-hash model/index와
  source/license/size/SHA가 고정된 operator asset만 허용한다. Torch `>=2.6` 호환 matrix 또는
  same-attempt safetensors/strict JSON 경로와 남은 trusted pickle asset의 GPU/no-network
  matrix가 완료되기 전 sample stage를 활성화하지 않는다.
- `AGENTS.md`에도 외부 `.pth`/`.pt`/`.index` 입력 금지, same-attempt 산출물과
  manifest-pinned operator asset만 허용하는 역직렬화 경계, Torch 2.4 출시 금지 및
  Torch `>=2.6` safe loader 또는 safetensors와 실제 GPU matrix gate를 불변 조건으로 고정했다.
- `CHECKLIST.md`는 계약/source review만 완료로 세분화하고 TestSet 전송, typed wrapper,
  Sample completion과 serialization/GPU gate를 미완료로 유지했다.
  `docs/REQUIREMENTS_TRACEABILITY.md`의 `RVC-013`은 계약/API/Worker가 허용 범위 밖 값을
  거부하는 인수 조건과 검증 근거를 연결한다.

**검증 결과와 남은 위험**

- `.venv/bin/pytest packages/contracts/tests apps/worker/tests/test_sample_config_validation.py
  apps/api/tests/test_api.py -q` — PASS. `0`, `16000`, `192000` 허용과 `-1`, `1`, `15999`,
  `192001`, `false`, `"16000"`, `16000.0` 거부, 실제 Job API 422를 포함한다.
- `.venv/bin/ruff check packages/contracts/src/rvc_orchestrator_contracts/job.py
  packages/contracts/tests/test_contracts.py apps/api/tests/test_api.py
  apps/worker/tests/test_sample_config_validation.py` — PASS.
- `.venv/bin/mypy packages/contracts/src apps/api/src apps/worker/src` — strict 65 source files
  PASS.
- 이번 변경은 sample inference를 구현하거나 Torch runtime을 교체하지 않았다. lease-bound
  TestSet download, per-item timeout/cancel wrapper, WAV 검증, canonical Artifact 뒤 Sample 등록과
  completion gate, v1/v2 40k/48k·F0/non-F0·4종 F0·index matrix 및 전체 dependency/container
  scan이 남았다. 따라서 `AUTO_SAMPLE_JOBS_ENABLED=false`,
  `PROFILE_STAGE_SET_VERIFIED=false`와 unverified GPU 설치 확인을 유지한다.

### 고정 TestSet/Preset/Sample 중앙 원장과 검증형 WAV data plane

**목적과 변경 파일**

- ADR-0004의 중앙 단계에 따라 `TestSet`, `TestSetItem`, immutable `Preset` revision,
  identity-graph `Sample`과 Job의 `test_set_id/preset_id/sample_plan_json/hash`를 단일 Alembic
  head `f3a8c6d9e120`에 연결했다. `TestSetItemUploadSession`은 owner/idempotency generation,
  request fingerprint, 서버 생성 staging/canonical key, backend와 credential 없는 storage
  namespace identity SHA-256, token hash,
  expiry/finalization/failure 상태를 보존한다. Sample은 Job↔attempt, Job↔TestSet snapshot,
  item↔TestSet, Artifact↔Job/attempt composite FK 및 attempt/item/config와 Artifact 유일성을
  DB에서 강제한다. Artifact/model/index/config SHA·size·type 의미 교차검증과 등록 endpoint는
  아직 만들지 않았다.
- `POST /test-sets/{id}/item-uploads/init`은 User 행 뒤 TestSet 행을 고정 순서로 잠가 서로 다른
  TestSet을 포함한 owner session/byte quota와 같은 revision의 item key/order 예약을 직렬화한다.
  Local raw PUT 또는 S3 presigned PUT만 반환하고 object key/URI는 숨긴다. local token 원문은
  응답에만 있으며 DB에는 SHA-256만 저장한다. finalize는 staging 전체 size/SHA-256, RIFF/WAVE
  uncompressed PCM decode, duration/sample-rate/channel 상한과 allowlist namespace의 opaque
  license/provenance record ID를 검사한
  뒤 server canonical key에 게시한다. 실패 item을 명시적으로 재-init하면 이전 staging/
  canonical cleanup 성공 뒤 새 generation으로 복구한다. finalize는 TestSet→upload 순서로
  fresh row lock을 얻고 status/finalization token CAS가 성공한 패자만 canonical을 지워 stale
  request가 completed 원장/object를 덮거나 삭제하지 못한다. cleanup/retry/delete는 session의
  backend+namespace hash가 현재 adapter와 다르면 원장을 보존하고 `503`으로 닫는다.
- TestSet ready 전환은 pending/finalizing/failed session을 거부하고 item마다 정확히 하나의
  completed session, 현재 backend, server key/URI, 선언 metadata와 실제 canonical object 전체
  size/SHA를 다시 검증한다. manifest는 storage URI/presigned query/내부 item UUID 없이
  item key/order/bytes/SHA/audio/license/provenance를 canonical JSON으로 동결한다. ready revision은
  object까지 삭제할 수 없고 draft/failed delete의 object cleanup 실패는 원장을 `failed`와
  typed failure code로 보존한다. list summary는 `items_included=false`로 item 생략을 명시한다.
- Job 생성은 ready TestSet 행을 잠그고 manifest를 DB item에서 재계산한 뒤 ordered item ID와
  storage-neutral metadata, inline inference config/config hash를 `sample_plan_json`/SHA-256으로
  snapshot한다. disabled config는 `test_set_id=null`만 허용하고 resample 상한을 Preset과 같은
  192 kHz로 맞췄다. Worker runtime이 아직 없으므로 Settings, `.env.example`과 Manager Compose의
  `AUTO_SAMPLE_JOBS_ENABLED=false`가 기본이며 false에서는 enabled Job을 `409`로 거부한다.
- Preset family에 revision이 둘 이상이면 어느 revision도 hard delete하지 못하게 해 rev1 삭제
  뒤 같은 이름의 새 family rev1과 남은 old revision이 충돌하거나 번호가 재사용되는 일을 막았다.
  sole unreferenced revision만 삭제할 수 있다. API/Architecture/Security/Deployment/
  Operations/Testing/Runtime Matrix, ADR, traceability, checklist, 루트 README/AGENTS와 Web 안내를
  중앙 원장 구현 후에도 Worker transfer/inference/Sample completion gate가 남은 의미로 갱신했다.

**검증 결과와 남은 위험**

- `.venv/bin/pytest apps/api/tests/test_test_sets.py packages/contracts/tests/test_contracts.py
  apps/api/tests/test_migrations.py -q` — 30 tests PASS. owner/cross-owner, idempotency, key/order 예약,
  token hash, invalid SHA/PCM/reference, retry, wrong-token/completed CAS, storage namespace 불일치
  원장 보존, canonical object 유실, deterministic manifest/URI 비노출, Sample composite FK,
  immutable delete, Preset family 삭제 gate, 기본 auto-sample gate와 Job snapshot/tamper를 포함한다.
- `.venv/bin/pytest apps/api/tests packages/contracts/tests -q` — PASS.
  `.venv/bin/ruff check packages/contracts/src packages/contracts/tests apps/api/src
  apps/api/tests/test_test_sets.py apps/api/tests/test_migrations.py` — PASS.
  `.venv/bin/mypy packages/contracts/src apps/api/src` — strict 42 source files PASS.
- migration SQLite upgrade→f3 downgrade의 ledger table/Job column 제거와 PostgreSQL offline
  upgrade+downgrade SQL test — PASS. head 적용 DB에서
  `alembic check` — `No new upgrade operations detected`. Manager Compose render — PASS.
  Web 문구 회귀는 `npm test` 10 files/86 tests, `npm run lint`, `npm run build` 모두 PASS.
- Worker lease-bound TestSet download, inference 4종, 검증 Artifact 뒤 Sample 등록, sample-enabled
  Job completion gate와 조회/download/UI는 아직 없다. 따라서 `AUTO_SAMPLE_JOBS_ENABLED=false`,
  `PROFILE_STAGE_SET_VERIFIED=false`를 유지하고 현재 중앙 snapshot 테스트를 GPU sample E2E로
  해석하지 않는다.
- TestSet staging은 Dataset RQ cleanup 범위에 없고 item finalization heartbeat/generation fence도
  없다. 특히 local PUT이 status 검사 뒤 finalize/실패 cleanup과 경합하면 late writer가 staging을
  다시 게시할 수 있다. 전용 orphan maintenance, durable finalize fencing, 실제 PostgreSQL 동시
  init/finalize와 MinIO 장애 주입이 release gate다. 실제 권리 검토 음원과 GPU matrix도 남아 있다.
- Job 생성은 DB item에서 manifest hash를 재계산하지만 이미 게시한 `manifest.json` object의
  availability/hash를 다시 stream하지 않는다. lease-bound claim transfer를 구현할 때 manifest
  object도 현재 storage namespace에서 재검증해야 한다.

### Worker stage error taxonomy와 no-replay retry 경계

**목적과 변경 파일**

- `stages.py`에 stage, stable `error_code`, category, retryable, sanitized enum cause와 generic
  message만 갖는 `StageExecutionError`를 추가했다. 모든 실행 stage와 assigned/completed
  경계의 `StageExecutionPolicy.executor_max_attempts`는 1이다. 내부 retry scope는 원자·멱등인
  `downloading_dataset`의 Dataset transfer와 `uploading_artifacts`의 Artifact transfer만
  명시한다. cancel, shutdown, lease loss와 process cancellation은 다른 동시 오류보다 먼저
  `StageExecutionCancelled`로 정규화한다.
- subprocess timeout/nonzero는 `stage_timeout`/`stage_process_failed`, retryable Manager
  429/5xx/network와 Dataset/Artifact transport 소진은 `exhausted_transient`, workspace/
  Dataset/RVC source·asset·output 오류는 `stage_integrity_failed`, claim/GPU/commit mismatch는
  `stage_configuration_invalid`, asset/runtime 준비 실패는 `worker_runtime_unready`로 분리했다.
  deterministic Manager 거부, telemetry local persistence와 unknown도 각각
  `stage_remote_rejected`, `telemetry_persistence_failed`, `stage_internal_error`로 fail-closed한다.
- `ManagerClientError`에 status 기반 또는 명시적 `retryable`과 transport/protocol/integrity/
  configuration category를 추가했다. Dataset checksum·redirect·path 오류는 즉시 종료하고
  429/5xx/network만 기존 bounded download retry를 사용한다. Artifact도 같은 metadata로
  idempotent candidate 단위에서만 bounded retry하며 마지막 실패 뒤 stage 전체를 재실행하지
  않는다. telemetry transport는 durable spool에 deferred 상태로 남기고 stage를 재실행하지
  않는다.
- `WorkerAgent` terminal payload는 exception class/원문 대신 typed code와 고정 generic
  message만 사용한다. Agent failure/Manager 통신 로그도 raw exception을 제거하고 stage,
  category, retryable과 HTTP status 분류만 남겨 token, argv, local path, URL/query가 실패
  경로에서 노출되지 않게 했다. `RvcConfigurationError`, `RvcRuntimeIntegrityError`와 native
  claim 전용 subtype으로 claim 오류와 runtime 무결성을 구분했다.
- Worker/Architecture/Security/Operations/Testing 문서, `AGENTS.md`, 체크리스트와 `WRK-009`
  추적표에 같은 attempt의 training/checkpoint/index 재실행 금지와 새 attempt retry 원칙을
  기록했다.

**검증 결과와 남은 위험**

- `test_stage_errors.py`는 모든 stage의 timeout mapping, training/checkpoint/index nonzero
  단일 호출, unknown fallback, cancel 우선순위와 per-stage retry allowlist를 검증한다.
  Dataset 503은 설정된 3회 뒤 `exhausted_transient`, checksum/integrity는 1회에
  `stage_integrity_failed`가 되며 Artifact 503도 설정된 2회만 실행됨을 확인했다.
- Agent vertical 회귀는 claim configuration/runtime-unready 분리, telemetry 503 durable defer,
  Artifact exhaustion terminal code와 terminal report 자체의 503을 포함한다. 주입한 token,
  argv, private path와 URL/query sentinel은 terminal payload와 Agent log에 나타나지 않았다.
- `.venv/bin/ruff check apps/worker tests/infra/test_worker_runtime_packaging.py` — PASS.
  `.venv/bin/mypy apps/worker/src/rvc_worker` — strict 23 source files PASS.
  `.venv/bin/pytest -q apps/worker/tests tests/infra/test_worker_runtime_packaging.py` —
  Worker/runtime packaging 136 tests PASS. `git diff --check` — PASS.
  실제 subprocess/GPU timeout과 원격 Manager/object store 장애를 사용하는 배포 환경 smoke는
  fixture를 대체하지 않는다. `retryable=true`는 같은 attempt stage replay 권한이 아니라
  운영자가 외부 상태 복구 뒤 새 JobAttempt를 검토할 신호다. 실제 GPU matrix와 고정 TestSet은
  계속 열린 release gate다.

### Redis/RQ maintenance 실행 allowlist와 process secret 경계 독립 리뷰 수정

**목적과 변경 파일**

- `maintenance_queue.py`의 enqueue API는 callable을 고정했지만 기존 `rq_worker.py`는 RQ
  기본 `Worker`를 그대로 사용해 Redis job hash의 `func_name`을 import/실행했다. Redis write
  credential을 얻은 주체가 `os.getenv` 같은 다른 Python callable과 callback/dependent를 직접
  넣을 수 있었고, JSON serializer는 pickle만 없앨 뿐 이 실행 선택권을 제거하지 않는 P1을
  독립 리뷰에서 확인했다.
- `AllowlistedMaintenanceWorker`는 Redis를 untrusted envelope로 취급한다. dequeue 직후와
  fork된 process의 perform 직전에 fixed queue/origin, JSON serializer, exact cleanup callable,
  `rvc-maintenance-<sha256>` job ID, canonical PostgreSQL UUID 단일 인자, 빈 kwargs/meta/
  dependency/callback/repeat, bounded timeout/TTL/retry를 재검증한다. job callable/callback
  property를 resolve하지 않으며 위반 job은 worker를 종료하지 않고 비식별 job reference와
  generic FailedJobRegistry 결과로 terminal 처리한다. custom success/failure handler는 검증
  뒤 Redis가 삽입한 dependent/repeat도 실행하지 않는다.
- 기존 enqueue는 30/60초 delayed retry를 ScheduledJobRegistry에 넣지만 Worker가
  `with_scheduler=False`여서 첫 storage 실패 뒤 retry가 전달되지 않는 P1도 확인했다. 내부
  RQ scheduler를 bounded retry 승격 전용으로 켰다. 새 periodic cleanup run은 계속 admin
  API/외부 운영 scheduler만 만들며, due job도 execution allowlist를 재통과한다. RQ의 queue별
  Redis `NX` lock으로 scheduler 단일성을 보장하고 scheduler lock key가 readiness의 실제
  Worker registry heartbeat를 대신하지 않음을 회귀로 고정했다.
- `Settings`에 `PROCESS_ROLE=api|maintenance`를 추가했다. HTTP app은 `api`, RQ entrypoint와
  실제 task는 `maintenance`가 아니면 fail-closed한다. Compose는 API/migration과 RQ Worker의
  role을 literal로 고정했다. 새 `rq-worker-entrypoint.sh`는 PostgreSQL, Redis, MinIO app
  credential만 읽고 JWT secret, Worker bootstrap/token pepper, MLflow token을 mount/export하지
  않는다. RQ container에는 read-only filesystem, all-capability drop, PID 상한과 noexec tmpfs를
  추가했다.
- cleanup grace가 Dataset upload TTL보다 짧은 설정을 거부해 시작된 upload와 만료 cleanup의
  최소 안전 간격을 보존했다. API/배포 테스트에는 `os.getenv`, `os.path.basename`, success/
  failure/stopped callback 비실행, pickle/meta 거부, generic terminal failure, role confusion,
  production secret 분리와 Compose mount 경계를 추가했다. `AGENTS.md`, API/Architecture/
  Security/Deployment/Operations/Testing 문서, 체크리스트와 `SYS-005` 추적표도 같은 경계로
  갱신했다.

**검증 결과와 남은 위험**

- `.venv/bin/pytest -q apps/api/tests/test_maintenance.py tests/infra/test_deployment_config.py`
  — 38 tests PASS. `.venv/bin/pytest -q apps/api/tests tests/infra` — 175 tests PASS.
- `.venv/bin/ruff check apps/api/src apps/api/tests tests/infra` — PASS.
  `.venv/bin/mypy apps/api/src` — strict 36 source files PASS.
- `sh -n infra/runtime/rq-worker-entrypoint.sh`와
  `MANAGER_SECRETS_DIR=/tmp/rvc-secrets S3_PRESIGN_ENDPOINT_URL=https://manager.example.test:9000
  docker-compose -f infra/compose/manager.compose.yml config --quiet` — PASS. 이 개발 host의
  `docker compose` plugin은 없어서 설치된 standalone `docker-compose`로 render했다.
- execution allowlist는 Redis write 탈취의 임의 Python 실행을 닫지만 Redis queue 삭제,
  RQ heartbeat 위조와 DoS는 막지 않는다. Redis FLUSH/job TTL 유실 뒤 PostgreSQL
  `queued|retrying` run을 재enqueue/terminal reconciliation하는 경로도 없다. Worker는 아직
  API와 같은 PostgreSQL/MinIO/Redis credential을 사용하므로 전용 DB role, staging-prefix
  delete-only S3 IAM, queue/heartbeat Redis ACL과 실제 장애 주입이 release gate다.
- local/browser PUT은 시작 시 status/expiry만 검사하고 전송 중 lease/heartbeat가 없다.
  grace≥upload TTL 검증은 완화책일 뿐 매우 느린 in-flight writer가 cleanup 뒤 staging key를
  다시 게시하는 race를 근본적으로 닫지 못한다. upload lease/heartbeat 또는 generation
  fencing과 동시성 시험이 후속 gate다.

### Worker upgrade release provenance와 native projection TOCTOU 독립 리뷰 수정

**목적과 변경 파일**

- `installers/worker/install.sh`가 기존 `worker.env` 전체를 보존해 1.0→2.0 upgrade 뒤에도
  `ORCHESTRATOR_VERSION`과 `WORKER_IMAGE`가 1.0을 가리키던 P1을 수정했다. bundle manifest는
  regular non-symlink assignment file, 전체 key uniqueness와 exact PRODUCT/COMPONENT/VERSION/
  WORKER_IMAGE를 검사하고 image는 bundle version의 고정 Worker tag만 허용한다. 기존 env도
  정규화된 absolute config root의 regular non-symlink file과 unique assignment만 허용한다.
- upgrade는 같은 config directory의 mode `0600` temporary를 만들어 release 소유 key인
  `ORCHESTRATOR_VERSION`, `WORKER_IMAGE`, `RVC_GPU_SMOKE_VERIFIED`,
  `RVC_PROFILE_STAGE_SET_VERIFIED`만 새 manifest 값으로 바꾼 뒤 rename한다. 그 밖의 timeout,
  endpoint, `CUSTOM_SETTING`, runner mode와 explicit native acknowledgement는 그대로 보존한다.
  token/profile/data도 건드리지 않는다. 이미 설치된 같은 version release의 manifest가 새
  bundle provenance와 다르거나 release/env path가 symlink이면 fail-closed한다.
- shared source가 claim 검증 뒤 Dataset stage 동안 바뀌면 첫 projection이 변조된 byte를
  신뢰하던 P1을 수정했다. `verify_inputs.py`와 runtime builder는 reviewed archive에서 추출한
  `infer`, `configs`, pretrained/HuBERT/RMVPE/mute 전체의 path/size/SHA-256/source mode strict
  projection manifest를 만든다. manifest hash는 lock file, runtime build manifest,
  Docker image label과 Worker bundle provenance에 함께 결박된다.
- `PinnedRvcRunner`는 source/asset manifest hash와 projection lock/inventory를 시작·claim
  직전에 확인한다. private projection 생성은 inventory path만 `O_NOFOLLOW` FD로 열어
  `fstat` size/mode와 streaming SHA-256을 검사하면서 바로 그 byte를 비공개 temporary tree에
  쓴 뒤 전체 tree를 원자 게시한다. 이후 stage마다 shared source가 아니라 build-time expected
  inventory를 기준으로 private file hash/mode와 marker 전체를 재검증한다. claim 뒤 source
  교체와 게시 뒤 private file 변조 모두 subprocess 전에 거부한다.
- Worker의 모든 positive float 및 native stage timeout은 NaN/±infinity도 거부하도록 finite
  검증을 추가했다. Deployment, Operations, Testing, Security, runtime/supply-chain 문서와
  history의 “환경 전체 보존” 표현을 release-owned key 갱신/사용자 설정 보존 의미로 교정했다.

**검증 결과와 남은 위험**

- 실제 1.0.0/2.0.0 Worker bundle을 각각 생성·설치한 회귀에서 current symlink, version/image와
  두 release gate가 2.0으로 전환되고 custom env, native ack=true, token, profile, job data가
  보존됨을 확인했다. duplicate/invalid WORKER_IMAGE manifest와 symlink worker.env도 거부됐다.
- claim 검증 후 Python source byte 교체, expected source symlink, private projection 게시 후
  변조, asset manifest tamper와 NaN/inf timeout fixture를 포함한 Worker/runtime 대상 test — PASS.
- Worker/runtime Ruff와 strict mypy, 변경 installer/runtime `bash -n`, Worker Compose render —
  PASS. 개발 host에 `shellcheck`가 없어 별도 ShellCheck는 계속 clean build gate다.
- 실제 Docker image build에서 새 projection label을 inspect하는 경로는 stubbed Docker로
  검증했으며 실제 GPU image rebuild/smoke는 아직 남아 있다. Archive-level source provenance와
  build-generated file inventory는 결박했지만 release image 자체의 distribution digest/signature,
  실제 GPU matrix와 TestSet은 여전히 open gate다.

### Experiment/Job UI 독립 리뷰 P2와 완전한 bounded pagination

**목적과 변경 파일**

- `lib/client/job-submission.ts`와 `experiments/[experimentId]/job-matrix-builder.tsx`에
  제출별 확정 key와 현재 in-flight key를 분리했다. 첫 POST 성공 뒤 다음 POST의 fetch가
  reject돼도 이미 확정한 success/conflict/error를 덮어쓰지 않는다. 응답 유실 가능성이
  있는 현재 Job만 `error / 원장 확인 필요`, 실제 시작하지 않은 후보만 `blocked`로
  바꾸는 순수 reducer를 사용한다. 401, 429와 5xx의 확정 응답도 같은 상태 규칙을 따른다.
- `lib/client/experiment-submission.ts`와 Experiment create form은 `idle → pending →
  submitted|uncertain` 상태와 즉시 ref lock을 사용한다. 정상 2xx 또는 검증된
  `ledger_committed=true` 503을 받은 뒤에는 `finally`가 form을 idle로 되돌리지 않아
  navigation이 늦어도 중복 클릭할 수 없다. 2xx body가 손상돼도 commit 확인 상태를
  terminal로 유지하고 목록 확인을 안내한다.
- `lib/server/dashboard-data.ts`는 Dataset, Experiment, Job, Worker를 page size 200으로
  끝까지 읽는 공통 helper를 사용한다. total/offset/limit, page 진행, total 안정성,
  item ID와 page 간 중복을 검증하고 자원별 10,000개를 넘으면 partial collection을 버린다.
  전역 Experiment run/completed count와 상세 Experiment Job도 모든 page를 사용한다.
  valid total이 상한을 넘는 경우에는 `ListLimitation`을 반환해 `ListLimitNotice`와 각 화면이
  실제 total/상한을 표시하며 일부 목록을 빈 목록처럼 또는 전체처럼 표시하지 않는다.
  Dataset upload 뒤 client refresh도 같은 complete bounded pagination을 적용한다.
- Web README, Testing/운영 가이드, `AGENTS.md`, 체크리스트와 `UI-002`/`UI-003` 추적
  설명을 같은 변경에서 갱신했다. API Python은 변경하지 않았다.

**검증 결과와 남은 위험**

- `cd apps/web && npm test` — 10 files, 86 tests PASS. 첫 Job success 뒤 다음 POST 응답
  유실 시 terminal 행 보존, 정상/committed 503 Experiment terminal lock, 여러 Dataset/
  Experiment/Job page의 완전 수집과 run count, 상한 초과 명시 상태와 비진행 pagination
  fail-closed를 포함한다.
- `cd apps/web && npm run lint`, `npm run build` — PASS. 추가 client reducer와 모든
  Server Component limitation 상태가 production TypeScript build를 통과했다.
- Experiment create 요청 자체의 HTTP 응답이 commit 뒤 완전히 유실되면 browser는 resource
  ID를 알 수 없다. API에 create idempotency key가 없으므로 자동 재시도는 중복 원장을 만들
  수 있어 현재 form은 `uncertain`으로 잠그고 목록 확인과 page reload를 요구한다. 새
  page/client의 중복까지 막는 API idempotency 확장은 이번 Web 수정 범위를 넘어 후속
  항목으로 남긴다. offset pagination 중 total 또는 ID
  순서가 바뀌면 안전하게 전체 화면 오류로 중단하므로, 매우 활발한 원장에서는 cursor/
  snapshot pagination API가 후속 개선이다. 10,000개 상한을 넘는 운영은 filter/cursor API나
  보존 정책이 필요하다.

### PostgreSQL 원장 기반 RQ maintenance와 Dataset staging orphan 정리

**목적과 변경 파일**

- API runtime에 `rq==2.6.1`과 transitive `croniter==6.2.4`를 exact lock하고 package script
  `rvc-manager-rq-worker`를 추가했다. license catalog에는 RQ `BSD-2-Clause`, croniter
  `MIT` declared metadata를 추가해 partial SBOM 생성이 누락 dependency를 fail-closed로
  거부하는 규칙을 유지했다.
- `maintenance_queue.py`, `rq_worker.py`, `maintenance_tasks.py`는 고정
  `dataset_staging_cleanup` callable만 `rvc-maintenance` queue에 넣는다. job payload는
  JSON serializer와 server-created PostgreSQL run UUID 하나뿐이고 client가 callable,
  args/kwargs, object key, timeout/retry/batch를 정할 API가 없다. 결정적 job ID의 기존 RQ
  job도 function/argument가 정확히 일치하지 않으면 fail-closed한다.
- `MaintenanceTaskRun` 원장, Dataset upload cleanup claim/completion column과 Alembic
  `e7c9a1b4d260`을 추가했다. admin-only enqueue/status API는 actor/idempotency key/dry-run을
  hash한 결정적 job ID를 사용하며 queue 장애도 `enqueue_failed`, typed error와 audit로
  DB에 보존한다. Redis result는 상태 원장으로 사용하지 않는다.
- cleanup task는 bounded batch/time/attempt와 exponential backoff를 사용한다. 삭제 직전
  upload row를 잠그고 grace, status, stale claim, configured storage backend와 정확한
  `datasets/staging/<dataset>/<session>` key를 다시 확인한다. 만료 `pending`은 `expired`로
  닫고, 실패/만료 session의 staging key만 멱등 삭제한다. 유효 `pending`, `finalizing`,
  `completed`, unsafe key 및 canonical original/flat/manifest/quality object는 삭제하지
  않는다. dry-run은 object/session 상태를 바꾸지 않고 typed 후보 집계와 audit만 남긴다.
- Manager Compose에 API image의 non-root `rq-worker`를 별도 service로 추가했다. host port,
  Docker socket, GPU/runtime/device 권한이 없고 internal `storage` network만 사용한다. API
  production readiness는 Redis ping과 queue의 최근 RQ Worker heartbeat를 모두
  fail-closed로 확인한다. backup/restore/rollback writer stop 목록에도 RQ Worker를 포함했다.
  built-in RQ scheduler는 켜지 않으며 외부 cron/systemd/운영 automation이 admin HTTPS
  endpoint만 호출하는 경계를 API/배포/운영/보안 문서와 `AGENTS.md`에 기록했다.

**검증 결과와 남은 위험**

- `.venv/bin/pytest -q apps/api/tests/test_maintenance.py` — 6 tests PASS. active/canonical
  보호, dry-run, competing run과 replay 멱등성, storage 실패 typed retry, admin-only
  deterministic enqueue, Redis+stale RQ heartbeat fail-closed를 외부 Redis 없이 검증했다.
- `.venv/bin/pytest -q apps/api/tests/test_migrations.py apps/api/tests/test_maintenance.py
  tests/infra/test_deployment_config.py tests/infra/test_supply_chain.py` — 30 tests PASS.
  SQLite upgrade/downgrade, PostgreSQL offline SQL, non-root/internal Compose 권한과 exact
  dependency/license inventory를 확인했다.
- `.venv/bin/pytest -q apps/api/tests tests/infra` — PASS. `.venv/bin/ruff check apps/api/src
  apps/api/tests tests/infra`, `.venv/bin/mypy --strict apps/api/src`(34 source files),
  supply-chain tests, Manager Compose render와 변경 shell `bash -n` — 모두 PASS.
- 실제 Redis/RQ process kill/restart와 PostgreSQL/MinIO 장애 주입 clean-Compose test는 아직
  남아 있다. RQ cleanup만 durable task로 분리됐고 Dataset validation/finalize는 요청
  process의 bounded thread에서 inline 실행되므로 `SYS-005`는 Partial이다. embedded
  scheduler, S3 multipart abort, canonical Dataset delete tombstone/retry, 여러 RQ Worker의
  실제 PostgreSQL `SKIP LOCKED` 부하 시험도 후속 gate다.

### Guarded native RVC mode와 offline runtime 설치 gate 연결

**목적과 변경 파일**

- `WorkerSettings`, CLI와 `runner.create_runner`에 명시적 `native` mode를 추가했다. source는
  기본 `/opt/rvc-webui`인 정규화 절대 경로만 받고 Python executable, CPU worker 수,
  device/use-half와 preprocess/extraction/training/index/small-model timeout을 typed 설정으로
  검증한다. factory의 local import로 `native_runner → runner` protocol 의존성과 import
  cycle을 만들지 않으며 expected commit은 설정으로 노출하지 않고 reviewed
  `7ef19867780cf703841ebafb565a4e47d1ea86ff`로 고정했다.
- `PinnedRvcRunner`는 시작 시 `assets-manifest.json`의 strict schema/중복 key/안전한 상대
  path/필수 asset/size/SHA-256/executable mode와 source revision을 검증한다. 각 claim 직전
  manifest 전체 byte와 revision을 다시 읽어 startup digest와 대조하고, Job backend
  repository/commit과 training/RMVPE GPU ID를 새로 수집한 visible capability index에 맞춘다.
  Agent는 Dataset wrapper 안쪽 runner의 commit/assets readiness를 보고하며 mismatch는
  workspace/Dataset/RVC subprocess 전에 terminal failure로 처리한다.
- profile/native 모두 기존 `DatasetStageRunner`를 통해 lease-bound canonical ZIP을 받는다.
  Native의 sample-disabled Job은 Dataset부터 preprocess/F0/feature/train/G-D/index/small model,
  no-sample evaluation과 artifact manifest까지 실행한다. 고정 TestSet dependency가 없는
  sample-enabled Job은 `GENERATING_SAMPLES`/`EVALUATING`에서 의도적으로 fail-closed한다.
- `Dockerfile.rvc` 기본을 `native`로 바꾸고 fixed runtime 설정을 추가했다. Compose와 env,
  Worker bundle manifest는 native availability, GPU/stage/TestSet open gate를 전달한다.
  설치기는 verified offline runtime/commit/build+asset manifest가 없으면 native를 거부하고,
  현재 `RVC_GPU_SMOKE_VERIFIED=false`에서는 `--allow-unverified-gpu-runtime` 없이는 시작하지
  않는다. runtime entrypoint도 같은 확인을 재검증한다. `PROFILE_STAGE_SET_VERIFIED=false`는
  별도 release 경고로 유지하며 GPU 확인 옵션이 전체 stage 검증을 뜻하지 않게 했다.
- 기존 `worker.env` mode와 재설치 CLI mode가 다르면 설정을 조용히 보존한 채 다른 mode를
  검증하는 오류를 막기 위해 명시적으로 실패시킨다. 이미 native/ack=true인 재설치는
  idempotent하게 유지한다. README, Architecture, Security, Deployment, Operations, Testing,
  runtime matrix, `AGENTS.md`, 체크리스트와 요구사항 추적표를 같은 변경에서 갱신했다.

**검증 결과와 남은 위험**

- `.venv/bin/pytest -q apps/worker/tests` — PASS. native setting/factory, strict asset tamper,
  commit/GPU mismatch, wrapper capability, sample-disabled 전체 plan과 sample-enabled
  fail-closed를 포함한다.
- `.venv/bin/pytest -q tests/infra/test_worker_runtime_packaging.py
  tests/infra/test_deployment_config.py` — PASS. runtime 없는 native 설치, unverified GPU
  entrypoint, verified bundle manifest와 기존 profile→native mode mismatch 거부를 포함한다.
- Worker/runtime 관련 Ruff와 strict mypy, installer/runtime `bash -n` shell syntax 및 Worker
  Compose render — PASS. 현재 개발 host에는 `shellcheck` executable이 없어 ShellCheck는
  실행하지 못했으며 clean build 환경의 후속 정적 검사로 남겼다.
- 실제 NVIDIA GPU에서 v1/v2 × 40k/48k × F0/non-F0, RMVPE GPU/multi-GPU, 장시간 timeout,
  CUDA OOM과 산출물 의미를 검증하지 않았다. 고정 TestSet/Sample API도 미구현이므로
  `PROFILE_STAGE_SET_VERIFIED=false`, `RVC_GPU_SMOKE_VERIFIED=false`, RVC-006/WRK-007/DEP-002
  Partial을 유지한다. Native asset 전체 재검증은 Job 시작 latency와 storage I/O가 크므로
  실제 asset 크기에서 성능 기준을 측정해야 한다.

### HttpOnly Experiment 생성과 동일 Dataset 다중 Job matrix

**목적과 변경 파일**

- `apps/web/src/app/bff/experiments/**`, `app/bff/jobs/route.ts`와
  `lib/server/experiment-bff.ts`에 Experiment list/detail/create, Experiment별 Job 이름
  list와 Job create BFF를 추가했다. Origin/forwarded Host/Fetch Metadata와 고정 ID/query를
  검증하고, browser가 Manager path/header를 선택하거나 cookie의 JWT를 JSON으로 읽지
  못하게 했다. Experiment/Job body는 exact-key schema와 streaming byte 상한을 적용하며
  Manager 응답도 public field만 다시 투영한다. `409`, `422`, `429`와 숫자형
  `Retry-After`만 안전하게 보존한다. MLflow fail-closed `503`이 원장 commit 뒤 발생하면
  검증된 `ledger_committed`와 safe resource ID만 보존해 이미 생성된 원장을 재시도하지 않는다.
- `experiments/new`에서 `ready`이면서 `is_usable=true`인 Dataset만 선택해 Experiment를
  만든다. `experiments/[experimentId]`와 `lib/client/job-matrix.ts`는 v1/v2, 40k/48k,
  use_f0와 pm/harvest/dio/rmvpe/rmvpe_gpu Cartesian 조건, epoch/batch/checkpoint/GPU IDs,
  index, VRAM/tag/priority를 immutable JobConfig로 preview한다. 한 요청의 조합은 16개로
  제한하고, 조건과 설정 signature를 포함한 128자 이하 안전 Job 이름을 결정적으로 만든다.
- 제출 직전 `/bff/experiments/{id}/jobs`를 200개씩 최대 10,000개까지 순회해 기존 이름을
  모두 검사한다. 중복은 서버에 보내지 않고 나머지를 단건 API로 순차 생성한다. 각 조합의
  성공, 사전 중복, 동시 `409`, 서버 `422`, `429`/5xx 이후 미제출을 분리해 표시하므로
  부분 성공 원장을 되돌리지 않는다.
- TestSet/Sample 원장이 아직 구현되지 않았으므로 모든 생성 config는
  `auto_inference_samples.enabled=false`, `test_set_id=null`, `collect_samples=false`로
  강제한다. UI에도 미구현 경계를 명시했으며 sample player와 A/B 비교 항목은 완료 처리하지
  않았다. Web README, `AGENTS.md`, 체크리스트, `SYS-003`/`UI-003` 추적 상태를 함께 갱신했다.

**검증 결과와 남은 위험**

- `cd apps/web && npm test` — 9 files, 77 tests PASS. 결정적/고유 이름, 16개 상한, no-F0,
  GPU/tag 검증, auto sample 비활성화와 함께 Origin/Host/Fetch Metadata, arbitrary
  path/query/field/header, body 상한, HttpOnly JWT 비노출, public projection,
  409/422/429, committed-ledger 503과 `Retry-After`를 포함한다.
- `cd apps/web && npm run lint`, `npm run build` — PASS. 네 생성/조회 BFF와
  `/experiments/new`, `/experiments/[experimentId]`가 production route에 포함됐다.
- 실제 browser에서 다수 Job을 생성하는 E2E, 생성 도중 session 만료/네트워크 단절,
  10,000개를 넘는 장기 Experiment, 여러 사용자가 같은 이름을 동시에 제출하는 PostgreSQL
  통합 시험은 남아 있다. 이름 unique constraint와 API의 Dataset readiness 재검증이 최종
  경계이며, 물리 Worker 병렬 실행 검증 전 `SYS-003`/`UI-003`은 계속 Partial이다.

### 고정 TestSet·Preset·Sample provenance 결정

- `ADR-0004`에 TestSet immutable revision, 권리/provenance manifest, Job 시점 Preset
  snapshot, lease-bound item 전송, 검증 Artifact 뒤 Sample 원장 row와 completion gate를
  확정했다. storage URI/Worker path를 Job config로 넘기거나 ready TestSet을 제자리 수정하는
  방식은 재현성과 권한 경계를 깨뜨리므로 사용하지 않는다.
- 아직 model/API/추론이 구현된 것은 아니므로 관련 체크리스트와 `RVC-012` 상태는 바꾸지
  않았다. 구현은 중앙 model/data plane → Worker pinned inference → UI A/B 순서로 진행한다.

### Manager의 GPU/RVC 실행 경계 자동 검증

- Manager Compose에 Worker service, GPU/runtime/device request와 NVIDIA 설정이 없고
  API/Web/proxy에 Docker socket이 연결되지 않는지를 회귀 테스트로 고정했다.
- API image/project가 Worker source, `Dockerfile.rvc`, RVC checkout, PyTorch/FAISS/NVIDIA
  dependency를 포함하지 않는지도 검사한다. 학습 실행은 authenticated Worker claim/lease
  protocol 밖에서 Manager로 역유입할 수 없다.
- 루트 README의 초기 골격 문구를 현재 구현 범위와 남은 v1.0 출시 gate로 갱신했다.
- 배포 구성 test 16개와 Ruff — PASS. `SYS-001`을 Verified로 갱신했다. 이 검증은 다중
  물리 Worker 동시 실행(`SYS-002`)이나 실제 GPU smoke를 대신하지 않는다.

### Durable MLflow projection과 명시적 장애 정책

**목적과 변경 파일**

- `apps/api/src/rvc_manager_api/services/mlflow.py`에 MLflow REST 2.0 adapter와
  `MlflowCoordinator`를 추가했다. Experiment는 Manager UUID의 결정적 이름, Run은
  `rvc_manager_job_id` tag로 먼저 조회하고 생성한다. JobConfig scalar parameter, attempt
  prefix metric, 검증된 Artifact ID/type/크기/SHA-256과 권한 검사 Manager download path,
  terminal status를 투영한다. 공식 REST 한도에 맞춰 metric 999개/parameter 99개 단위로
  나누고 각 조각에 event-key SHA-256 marker를 함께 기록해 정상 replay를 건너뛴다.
- `MlflowSyncEvent`와 Alembic `d6f41e92ab30` migration이 원장 변경과 같은 transaction의
  durable outbox를 제공한다. API commit 뒤 즉시 투영하고 실패 시 generic error code와
  지수 backoff를 남긴다. lifespan background loop가 stale processing claim을 회수해
  재처리한다. 같은 process의 request/background 경쟁은 lock으로 직렬화하고 DB conditional
  update가 replica 간 event 소유권을 제한한다.
- Experiment/Job 생성, Worker metric batch와 legacy fake artifact, checksum 검증 artifact
  finalize, terminal status에 outbox hook을 연결했다. duplicate metric/artifact 요청은 새
  event를 만들지 않고 기존 event를 재동기화한다. `MLFLOW_ENABLED=false`는 event를 만들지
  않으며, fail-open은 원장 응답을 유지하고, fail-closed는 `/ready`와 commit 뒤 projection
  실패 응답을 `503`으로 만든다. 오류 body는 `ledger_committed=true`와 resource ID를
  반환해 create 재시도가 새 원장 row를 만들지 않게 한다. 다른 projector가 event를 이미
  claim한 경우에도 fail-closed request가 이를 성공으로 오인하지 않는다.
- tracking URI는 절대 HTTP(S)만 허용하고 userinfo/query/fragment/whitespace를 거부한다.
  선택 bearer는 `SecretStr`/file로 읽으며, REST client는 redirect와 environment proxy/
  `.netrc`를 모두 비활성화한다. pretrained path, storage URI, presigned query, token,
  credential, 임의 Artifact metadata와 backend response body는 payload/error/log에 넣지
  않는다.
- `config.py`, `app.py`, `dependencies.py`, health, Compose/`.env.example`, API runtime exact
  lock, Manager API/배포/운영/테스트/아키텍처 문서, `AGENTS.md`, 체크리스트와 `SYS-007`
  추적 상태를 함께 갱신했다. API는 MLflow container health를 hard startup dependency로
  두지 않아 disabled/fail-open 원장이 함께 멈추지 않는다.

**검증 결과와 남은 위험**

- `.venv/bin/pytest -q apps/api/tests/test_mlflow_integration.py` — 8 tests PASS. API outbox
  hook/민감 path 제거, disabled/fail-open/fail-closed와 복구, bounded readiness와 concurrent claim 처리,
  URI/token 설정, environment proxy 무시, stateful REST parameter/metric/artifact/terminal
  projection과 replay를 검증했다.
- `.venv/bin/pytest -q apps/api/tests tests/infra/test_deployment_config.py
  tests/infra/test_supply_chain.py` — 119 tests PASS. Ruff와 API strict mypy — PASS.
- 새 SQLite에서 전체 migration upgrade, `d6f41e92ab30 → c2b7d4e8f901` downgrade와 재-upgrade,
  PostgreSQL offline SQL 생성 — PASS. `docker-compose --env-file .env.example -f
  infra/compose/manager.compose.yml config --quiet` — PASS.
- 실제 MLflow container/PostgreSQL/MinIO를 장애 주입하는 통합 test는 아직 없다. MLflow
  `log-batch`의 부분 성공 뒤 marker 전 process가 죽으면 같은 timestamp/step/value가
  history에 물리적으로 중복될 수 있으나 PostgreSQL Metric 원장은 중복되지 않는다.
  여러 API replica가 서로 다른 첫 event에서 같은 Job의 Run을 정확히 동시에 처음 만들면
  MLflow REST에 client 지정 run ID/tag unique constraint가 없어 중복 Run 가능성이 남는다.
  배포 기본 1 replica를 넘기기 전 PostgreSQL advisory lock 또는 단일 projector service와
  실제 MLflow 장애/recovery smoke가 필요하다. terminal commit 뒤 fail-closed `503`은 lease가
  이미 종료됐으므로 background outbox가 복구 주체이며 Worker가 이를 성공 ack로 해석하는
  protocol 개선도 후속 항목이다.

### Dataset direct upload BFF와 품질·삭제 대시보드

**목적과 변경 파일**

- `apps/web/src/app/bff/datasets/**`와 `lib/server/dataset-bff.ts`에 Dataset
  list/detail/init/finalize/delete BFF를 추가했다. browser는 HttpOnly JWT를 읽지 않고
  same-origin BFF만 호출한다. BFF는 입력 field와 query/ID를 allowlist로 제한하고 Manager
  응답을 `DatasetRead` public field로 재구성하므로 storage URI나 향후 내부 field를 그대로
  전달하지 않는다.
- upload init 응답은 `PUT`, HTTP(S), HTTPS downgrade 금지, userinfo/fragment 금지,
  `DATASET_UPLOAD_ALLOWED_ORIGINS`, Content-Type/Length와 RVC/AWS checksum header allowlist를
  모두 통과해야 한다. presigned query와 upload token은 upload 함수의 일회성 local 변수로만
  사용하고 화면·로그·Dataset 목록/상세에 보존하지 않는다. 외부 object origin에는
  credential 없는 XHR progress를, 같은 origin local target에는 session cookie를 빼는
  `fetch(credentials: "omit")`를 사용한다.
- `lib/client/sha256.ts`는 파일 전체를 메모리에 올리지 않는 incremental SHA-256을 제공한다.
  Dataset UI는 hashing/init/upload/finalize 단계, byte progress, 중단, 동일 idempotency key
  재시도, 새 세션, 429 `Retry-After`와 실패 상태를 처리한다. finalize 중 browser를 닫아도
  서버 처리가 계속될 수 있음을 명시하고 애매한 중단 버튼은 제공하지 않는다.
- Dataset 목록 검색과 상세 화면에서 상태, 원본 크기/MIME, duration/file/sample rate,
  duplicate/rejected/skipped/decoder pending, 네 checksum과 삭제를 제공한다. 삭제 `409`는
  Experiment/Job 참조 또는 활성 upload/finalize 충돌로 안내한다. 현재 API가 manifest의
  PCM clipping/silence/RMS를 DatasetRead aggregate로 제공하지 않으므로 UI는 값을 만들지
  않고 명시적으로 미제공으로 표시한다.
- Manager Compose Web에는 presign origin allowlist를, MinIO에는 Dashboard origin 기반 CORS와
  wildcard credential 비활성화를 추가했다. Web README, 운영 가이드, 체크리스트와 UI-002
  추적 상태도 함께 갱신했다.

**검증 결과와 남은 위험**

- `cd apps/web && npm test` — 62 tests PASS. 교차 origin/임의 request field, 허용되지 않은
  upload origin, HTTPS downgrade, userinfo/fragment, credential header, private URI 제거,
  429 전달, finalize/delete와 SHA-256 known vector를 포함한다.
- `cd apps/web && npm run lint`, `npm run build` — PASS. `/datasets`, Dataset detail과
  list/detail/init/finalize/delete BFF 동작이 production build에 포함됐다.
- `.venv/bin/pytest -q tests/infra/test_web_dataset_upload_config.py
  tests/infra/test_deployment_config.py` — 17 tests PASS. Web presign allowlist와 MinIO explicit
  CORS/noncredential 설정을 확인했다. `docker-compose --env-file .env.example -f
  infra/compose/manager.compose.yml config --quiet` — PASS.
- 실제 browser↔MinIO CORS/presigned PUT, multi-GiB progress/cancel, 만료 직전 재시도와
  finalize 장시간 동작은 clean 배포 E2E가 남아 있다. same-origin local target은 JWT 비전송을
  우선해 byte-level upload progress가 없고 단계/완료만 표시한다. 이 항목들과 PCM aggregate
  API projection이 남아 있어 `UI-002`와 상위 체크리스트는 계속 Partial이다.

### lease-bound canonical Dataset 수신과 Worker 재검증 평탄화

**목적과 변경 파일**

- `rvc_orchestrator_contracts.worker`에 내부 storage URI 대신 Manager 상대 download path,
  고정 `prepared_flat.zip` filename/MIME, 정확한 size/SHA-256을 담는 `DatasetTransfer`를
  추가했다. path는 claimed Job과 정확히 일치해야 하며 기존 `dataset_storage_uri`는
  응답 계약에서 제거했다. test 전용 legacy URI claim만 transfer 없이 격리한다.
- `services/workers.py`는 `ready`, `is_usable=true`, 완료된 server-owned upload session과
  canonical metadata가 있는 Dataset만 실제 Worker에 배정한다. `routers/workers.py`의
  `GET /workers/jobs/{job_id}/dataset`은 Worker bearer와 현재 lease/attempt/Job 소유를 모두
  확인하고 내부 URI를 노출하지 않는다. Local adapter는 exact-size bounded stream,
  S3/MinIO는 기본 60초 presigned GET 307을 반환하며 download audit에는 URL을 남기지 않는다.
- `rvc_worker.client`에 취소 가능한 async Dataset downloader를 추가했다. Manager
  Authorization/lease header는 첫 요청에만 사용하고 외부 307에는 전달하지 않는다.
  HTTPS downgrade, userinfo/fragment, 상대 redirect와 redirect chain을 거부하고
  Content-Length/size/SHA-256을 대조한다. mode `0600`, `O_NOFOLLOW` partial을 fsync한 뒤
  hard-link 기반 no-replace 원자 게시하며 취소/오류 partial을 정리한다.
- `rvc_worker.datasets`는 real/profile mode의 downloading/validating/preparing stage 앞에
  주입되는 독립 materializer다. Manager가 만든 canonical ZIP도 다시 regular stored member,
  flat numeric name, traversal/symlink/duplicate/encryption/CRC/file count/file byte/total byte/
  compression 상한으로 전체 streaming 검증한다. 추출도 같은 검증을 반복하고 job workspace
  안의 `inputs/prepared_flat`만 원자 게시한다. Fake runner 동작은 그대로 유지한다.
- Worker settings, Compose, installer, `.env.example`, API/Worker README, 아키텍처/보안/
  배포/테스트 문서, `AGENTS.md`, 체크리스트와 `DATA-007` 추적 상태를 함께 갱신했다.

**검증 결과와 남은 위험**

- contracts + Dataset API + Worker 전체 + deployment config 표적 suite 113 tests — PASS.
  Worker 전송 전용 16 tests는 Manager/external header 분리, downgrade/userinfo/fragment/chain,
  Content-Length/checksum, symlink destination, 취소 stream close/partial 정리와 ZIP
  traversal/symlink/duplicate/bomb/CRC를 포함한다.
- `.venv/bin/pytest -q packages/contracts/tests apps/api/tests apps/worker/tests` — PASS.
- `.venv/bin/ruff check packages/contracts apps/api apps/worker tests/e2e/test_fake_worker_e2e.py
  tests/infra/test_deployment_config.py`, `.venv/bin/mypy packages/contracts/src apps/api/src
  apps/worker/src` — PASS.
- `bash -n installers/worker/install.sh`, Manager/Worker Compose `config --quiet` — PASS.
- localhost Uvicorn E2E fixture는 legacy URI seed 대신 실제 Dataset
  `init → PUT → finalize → claim → binary GET`을 수행하도록 갱신했다. 제한된 환경의 socket
  승인 사용량 때문에 이 변경 뒤 `make test-e2e`는 아직 재실행하지 못했다.
- 실제 MinIO/TLS presigned Dataset GET, 대형 전송 중 lease 만료/재발급, crash 뒤 미완료
  workspace retention과 clean GPU VM real runner smoke는 후속 배포 gate다.

### Partial CycloneDX dependency inventory와 license declaration

**목적과 변경 파일**

- `tools/generate_supply_chain_report.py`가 Manager API exact Python lock, Worker
  Agent/Fake exact lock, Web npm v3 lock과 기본 container image reference를 결정적으로
  읽어 CycloneDX 1.6 JSON과 third-party license declaration report를 생성한다. lock의
  version/license/integrity 누락과 Python version별 catalog 누락은 report를 조용히
  생략하지 않고 실패시킨다.
- `apps/worker/requirements.lock`을 추가하고 Agent/Fake Docker build도 exact dependency
  wheel을 만든 뒤 runtime에서 `--no-index`로 설치하게 했다. 실제 RVC runtime은 기존
  별도 `--require-hashes` offline wheelhouse 경계를 유지한다.
- Manager/Worker bundle builder가 두 report를 `supply-chain/`에 넣고 `SHA256SUMS`와 외부
  archive checksum 범위에 포함한다. 설치 release에도 report와 runtime provenance를
  보존한다. SBOM root version은 bundle version과 일치하며 manifest에는
  `SBOM_STATUS=partial-release-gates-open`을 명시한다.
- `docs/SUPPLY_CHAIN.md`, 배포/요구사항/AGENTS/체크리스트를 갱신해 exact inventory가
  취약점 없음, 법적 재배포 검토 또는 완전한 release attestation을 의미하지 않게 했다.

**검증 결과와 남은 위험**

- supply-chain 결정성/coverage, npm SHA-512 integrity, open-gate property, 누락/미포함 report
  installer 거부, Worker lock Docker 사용, 배포 구성, 실제 Manager/verified-runtime Worker
  bundle 생성과 Manager install/upgrade 등 대상 22 tests — PASS.
- generator/test Ruff, generator strict mypy와 네 installer script shell syntax — PASS.
  `make check`도 supply-chain tool과 infra test source를 lint/typecheck 범위에 포함한다.
- API/Agent Python distribution hash, tag 기반 기본 image digest, real-RVC wheel/asset과 OS
  package의 단일 SBOM merge, vulnerability/container/secret/SAST scan과 사람의 license
  검토는 남아 있다. 이 때문에 `DEP-006`과 출시 보안 항목은 계속 Partial이다.

### 역할별 설치·운영 runbook과 dependency-aware readiness

**목적과 변경 파일**

- `docs/OPERATIONS_GUIDE.md`에 Manager 신규 설치, TLS/S3 경계, 최초 admin bootstrap,
  Worker secret 전달·등록·credential 보존, 일반 사용자 Dataset/Experiment/Job 흐름,
  일상 점검·장애 대응, backup/upgrade/rollback과 남은 출시 gate를 역할별 runbook으로
  작성했다. 아직 UI가 제공하지 않는 Dataset/Experiment write와 Worker token rotation,
  실제 GPU 검증은 완료된 기능처럼 안내하지 않는다.
- `infra/proxy/templates/default.conf.template`과 TLS 예제에 `/readyz`를 추가했다.
  `/healthz`는 Nginx liveness만 반환하고 `/readyz`는 FastAPI `/ready`의 PostgreSQL/Redis
  결과를 전달하므로 load balancer가 dependency 장애 중 새 요청을 drain할 수 있다.
- `README.md`, `AGENTS.md`, `CHECKLIST.md`에 runbook 링크와 동기화 규칙을 추가했다.
  설치 문서 자체는 완료했지만 clean-VM 설치 및 실제 GPU smoke 상태는 변경하지 않았다.

**검증 결과와 남은 위험**

- `.venv/bin/pytest -q tests/infra/test_deployment_config.py` — 15 tests PASS.
- `.venv/bin/ruff check tests/infra/test_deployment_config.py`, Manager installer shell syntax
  검사 — PASS.
- `/readyz`의 실제 Nginx→API 응답과 TLS load-balancer 연동은 clean Ubuntu 배포 smoke에서
  확인해야 한다. 현재 문서는 개발 기준선이며 v1.0 운영 인증을 의미하지 않는다.

### Reviewed RVC의 job-local typed stage adapter

**목적과 변경 파일**

- `apps/worker/src/rvc_worker/native_runner.py`에 `PinnedRvcRunner`를 추가했다. 실행 전
  `7ef19867780cf703841ebafb565a4e47d1ea86ff`를 정확히 검증하고 `infer`, `configs`,
  pretrained v1/v2, HuBERT/RMVPE, mute fixture의 허용된 regular file만 attempt의
  `work/rvc`로 복사한다. 복제 파일별 size/SHA-256과 source 재대조를 수행하며 공유
  checkout의 `logs`, `assets/weights`, `weights`에는 절대 쓰지 않는다.
- typed stage는 preprocess, optional F0와 feature의 다중 shard 병렬 실행, training 직전
  `filelist.txt`/`config.json`, stdout/train.log metric metadata, 최신 G/D 동일 epoch pair,
  internal deterministic FAISS CLI, deployable weight 수집 또는 공식 checkpoint extractor,
  final index 정규화와 config/environment/artifact checksum manifest를 연결한다. 모든
  subprocess는 argv, `shell=False`, attempt cwd/log/home/tmp, timeout과 process-group 취소
  경계를 유지하고 선언한 산출물을 workspace 내부 non-symlink regular file로 재검증한다.
- `small_model.py`는 일반 checkout의 Git 검증을 그대로 기본으로 유지하면서, typed runner가
  먼저 hash 검증한 private projection에 한해서만 명시적
  `--allow-reviewed-projection` marker 검증을 허용한다. checkpoint byte 복사는 여전히
  deployable model로 인정하지 않는다.
- `apps/worker/tests/test_native_runner.py`, `AGENTS.md`, Worker/Testing/runtime 문서와 체크리스트,
  `RVC-009` 추적 상태를 갱신했다. Dataset download/validation/flat preparation과 고정
  test-set sample inference는 별도 protocol dependency가 없으면 fail-closed다. 이 runner는
  아직 `create_runner`나 `RVC_RUNNER_MODE`에 노출하지 않았고
  `PROFILE_STAGE_SET_VERIFIED=false`를 유지한다.

**검증 결과와 남은 위험**

- `.venv/bin/pytest -q apps/worker/tests/test_native_runner.py` — 8 tests PASS. v1/no-F0,
  v2 RMVPE-GPU/feature 다중 shard, shared source 불변, checkpoint 누락/엇갈림, index와
  official small-model fallback, metric metadata, peer 실패 취소/timeout, 외부 cancel,
  source symlink와 workspace escape를 포함한다.
- `.venv/bin/pytest -q apps/worker/tests` — 65 tests PASS.
- `.venv/bin/ruff check apps/worker/src apps/worker/tests`,
  `.venv/bin/mypy --strict apps/worker/src` — PASS.
- subprocess 산출물은 fixture이며 실제 reviewed RVC source, Torch/CUDA/FAISS와 GPU에서
  실행한 결과가 아니다. 원격 Dataset signed GET/manifest/checksum, sample inference,
  TensorBoard live projection과 실제 v1/v2 × 40k/48k × F0 matrix가 남아 있어 runtime
  production 활성화와 GPU smoke gate는 열어 두었다.

### Redis 원자 API rate limit

- 일반 API, login, Worker register, upload init/finalize에 서로 다른 분당 상한을 적용하는
  Redis limiter를 추가했다. Lua `INCR`/`EXPIRE` 한 연산으로 여러 API replica 사이의
  counter와 TTL 경쟁을 제거한다.
- bearer 또는 client IP 원문은 Redis에 저장하지 않고 JWT signing secret으로 HMAC한
  route별 key만 사용한다. `429`는 `Retry-After`와 `RateLimit-Limit/Remaining/Reset`을
  반환한다.
- 설치 Compose는 limiter와 fail-closed를 기본 활성화한다. Redis 장애 시 API 요청은
  503으로 차단하되 `/health`는 대상에서 제외하고 `/ready`의 Redis 검사로도 장애를
  노출한다. 직접 단위 테스트 설정은 외부 Redis 없이 실행되도록 비활성화할 수 있다.
- atomic counter/identity 비노출, route별 거부 header, fail-closed/fail-open 정책,
  구성 검증과 Compose 회귀 테스트 — PASS. 실제 다중 replica Redis 부하 시험은 남아 있다.

### 소유권 기반 Dataset upload/finalize data plane과 canonical 게시

**목적과 변경 파일**

- `models.py`, Alembic `c2b7d4e8f901`, `schemas.py`, `routers/datasets.py`와
  `services/datasets.py`에 DatasetUploadSession과 사용자 JWT 기반
  `init → raw PUT/presigned PUT → finalize` 흐름을 추가했다. client filename은 표시용으로만
  보존하고 staging/canonical object key와 내부 URI는 Manager UUID로 생성하며 응답에서는
  URI를 제거했다.
- 확장자/MIME, content signature, 정확한 size/SHA-256, 단일 5 GiB와 owner별 동시
  8 session/20 GiB quota를 적용했다. idempotency payload가 같으면 같은 session을,
  다르면 `409`를 반환하며 만료 session은 staging을 정리하고 generation을 올린다.
- finalize는 staging 전체를 bounded spool로 재검증한 뒤 mode `0700` Manager snapshot에서
  기존 안전 ingestion core를 실행한다. 원본, 결정적 `prepared_flat.zip`, manifest와
  quality report 네 object가 모두 게시된 뒤에만 DB를 완료한다. 부분 게시 실패는 게시한
  key를 역순 정리하고 typed failure/retry 상태를 남긴다.
- 장시간 verify/ingestion/publish는 session별 finalization token과 별도 DB heartbeat로
  stale 회수와 중복 finalize를 막았다. 중단된 token만 CAS로 pending에 되돌린다. 안전하게
  종료할 수 없는 Python thread에 완료 후 elapsed를 timeout처럼 취급하던 방식은 제거했다.
  현재 처리는 event loop 밖이지만 동일 HTTP 요청 안의 동기 경계이며 durable hard
  timeout/cancel/restart는 Redis/RQ 또는 별도 subprocess 후속 작업이다.
- PCM WAV는 분석 후 `ready`가 된다. FLAC/MP3/M4A/OGG/AAC는 실제 격리 decoder가 없으므로
  manifest/report와 flat 결과는 게시하되 `decoder_pending`, `is_usable=false`로 격리한다.
  Experiment/Job 생성과 Worker claim이 Dataset 행 잠금 아래 readiness를 다시 검사한다.
- Dataset 삭제는 행을 `FOR UPDATE`로 잠그고 참조 Experiment/Job과 활성 upload/finalize를
  거부한다. `deleting`을 먼저 commit해 동시 Experiment 생성 race를 막고 모든 세대의
  staging/canonical object를 정리한다. 실패하면 `delete_failed`를 보존한다. legacy client
  URI 등록은 test 또는 non-production admin으로만 격리하고 production은 차단했다.
- `infra/compose/manager.compose.yml`에 non-root `rvc`가 쓰는 전용 Dataset ingestion
  volume과 root one-shot mode `0700` init service를 추가했다. `.env.example`, API/보안/
  아키텍처/테스트 문서, 체크리스트와 요구사항 추적표도 같은 변경에서 갱신했다.

**검증 결과와 남은 위험**

- `apps/api/tests/test_dataset_upload_api.py` 8 tests — PASS. owner/cross-owner,
  init/PUT/finalize/read, URI 비노출, 멱등/충돌/quota/만료 generation, signature/SHA/악성 ZIP,
  non-WAV 격리, partial publish cleanup/retry, stale CAS, 삭제 race와 Job readiness gate를
  포함한다.
- `.venv/bin/pytest -q apps/api/tests tests/infra/test_deployment_config.py` — 99 tests PASS.
  `.venv/bin/pytest -q apps/api/tests/test_migrations.py` — SQLite upgrade/downgrade와
  PostgreSQL offline SQL 2 tests PASS.
- `.venv/bin/ruff check apps/api/src apps/api/tests tests/infra`,
  `.venv/bin/mypy apps/api/src apps/worker/src packages/contracts/src` — PASS.
- 실제 PostgreSQL 동시 quota/row-lock race와 실제 MinIO presigned Dataset PUT 통합은 아직
  실행하지 않았다. multipart/resume, orphan retention scheduler, durable RQ 처리와
  non-WAV sandbox decoder/LUFS도 남아 있다.
- Worker claim은 unusable Dataset을 거부하지만 아직 원격 Worker용 presigned Dataset GET과
  manifest/checksum 계약을 반환하지 않는다. 따라서 `DATA-007`과 Worker download 항목은
  완료 처리하지 않았다.

### HttpOnly 관측 BFF와 실제 Job 로그·메트릭·Artifact 화면

**목적과 변경 파일**

- `apps/web/src/app/bff`, `src/lib/server/bff-{security,proxy}.ts`와
  `manager-api.ts`에 browser가 JWT를 읽지 않는 same-origin 관측 BFF를 추가했다.
  Job/Artifact ID와 query key/value 개수·길이·형식을 allowlist로 검증하고
  Origin/forwarded Host 및 Fetch Metadata를 대조한다. 401은 session cookie를 지우며
  JSON, SSE, download 응답은 `no-store`다.
- 로그 SSE는 upstream body를 명시적 reader로 relay한다. browser/request 취소 시
  upstream reader와 fetch signal을 함께 취소하고 `X-Accel-Buffering: no`를 유지한다.
  EventSource 재연결의 최신 `Last-Event-ID`가 남아 있는 최초 `after` query와 충돌하지
  않도록 reconnect 요청에서는 header만 Manager에 전달한다.
- `jobs/[jobId]/job-observability.tsx`와 `globals.css`에 실제 tail/cursor/attempt 로그와
  SSE 연결 상태, 조회분 level/message 검색, attempt/key/epoch/step metric 필터와 표·key별
  SVG 그래프, Artifact type/size/SHA-256/attempt와 만료 download 버튼을 구현했다.
  storage URI와 presigned URL은 목록 JSON에 추가하지 않는다.
- Job 목록은 Manager의 `status`와 `experiment_id` 필터를 사용하고 최대 200개 조회분을
  browser에서 작업명·실험명·Worker·F0로 검색한다. 병렬 Dataset data-plane 변경에 맞춰
  `ApiDataset`/projection도 URI 필드를 제거하고 실제 status, 원본/준비본 checksum,
  quality/failure/retry 필드를 보존하도록 갱신했다.
- 관련 요구사항은 `SYS-008`, `UI-004`, `UI-005`, `SEC-003`, `SEC-004`다. 만료 다운로드
  browser E2E가 아직 없으므로 `UI-005` 추적 상태는 `Partial`로만 올렸다.

**검증과 남은 위험**

- `cd apps/web && npm test` — 5 files, 42 tests PASS. Origin/path/query 거부, HttpOnly bearer
  server-side 전달, 401/403/404, SSE no-buffer와 downstream cancel 전파, reconnect cursor,
  HTTPS download downgrade 차단, Job API filter를 포함한다.
- `npm run lint`, `npm run build` — PASS. 관측 BFF 5개를 포함한 제품 route가 production
  TypeScript build에 포함됐다.
- Manager 로그 API는 cursor 이후 조회만 지원하므로 tail 100개보다 오래된 과거 로그를
  역방향 page하는 UI는 없다. metric 화면도 한 요청의 최대 200개를 그리며 downsampling은
  없다. 실제 browser EventSource 재연결과 만료 S3 redirect/local 대용량 stream은
  Playwright/배포 E2E가 남아 있고 sample 전용 player도 별도 항목이다.

### Query 비노출 구조화 API 로그와 기본 보안 헤더

- Manager 요청 로그를 timestamp/level/logger/request ID/method/path/status/latency의 작은
  JSON schema로 고정했다. 임의 `LogRecord` 전체를 직렬화하지 않으며 Authorization,
  bearer/JWT, password/secret과 presigned query를 formatter에서도 redaction한다.
- Uvicorn raw access log는 query string에 서명 credential이 포함될 수 있어 운영 command와
  Python entrypoint 모두에서 비활성화했다. middleware는 query 없는 `request.url.path`만
  기록한다.
- 외부 request ID는 128자 안전 문자 패턴을 만족할 때만 사용하고 나머지는 UUID로
  교체한다. 모든 API 응답에 `nosniff`, frame 차단, no-referrer, Permissions Policy와
  API 전용 CSP를 적용하고 production HTTPS에는 HSTS를 적용한다.
- formatter secret 회귀, query 비기록, request ID 교체, 보안 header와 Compose 운영
  command 테스트 — PASS. refresh token/session rotation은 별도 후속 항목이다.

### 유실 Worker lease 회수와 제한된 자동 재배정

**목적과 구현**

- lease가 만료됐다는 이유만으로 즉시 중복 배정하지 않고, 해당 Worker의 마지막
  heartbeat도 `WORKER_OFFLINE_SECONDS`를 넘긴 경우에만 abandoned attempt를 회수한다.
- 여러 Worker가 동시에 polling해도 잠긴 unfinished attempt 하나만 `failed`로 닫고
  `failed → retrying → queued` event를 남긴다. 이전 attempt/log/metric/artifact는 보존하며
  새 claim은 새 attempt와 lease를 만든다.
- 자동 재배정은 기본 3 attempts로 제한한다. 반복 인프라 장애가 상한에 도달하면 Job을
  `worker_lease_expired` 실패로 유지해 무한 실행을 막고, 사용자가 명시적으로 진단·재시도할
  수 있게 한다. 이미 cancel 요청된 유실 Job은 재큐잉하지 않고 `cancelled`로 닫는다.
- 만료 write를 거부하는 경로가 lease만 비활성화해 Job을 고착시키지 않도록, 회수 전까지
  abandoned lease를 발견 가능한 상태로 유지한다. Worker 재시작도 즉시 종료하지 않고
  Manager 회수를 기다리며 polling한다.

**검증과 남은 위험**

- 만료 write 거부, offline grace, 자동 재배정, attempt 이력, cancel 우선, retry 상한과
  Worker 재시작 회귀 테스트 — PASS.
- 회수는 heartbeat/claim 요청에 의해 구동된다. Worker가 전혀 없는 장기 무인 환경에서도
  즉시 상태를 정리하는 별도 scheduler와 실제 process/network-partition E2E는 남아 있다.

### 검증형 Artifact data plane과 Worker 무손실 전송

**목적과 구현**

- Manager에 Local filesystem 및 S3/MinIO adapter를 추가하고 내부 object endpoint와
  Worker가 접근하는 presign endpoint를 분리했다. 운영 custom S3 endpoint는 외부
  `S3_PRESIGN_ENDPOINT_URL`을 별도로 요구한다.
- Worker lease/attempt에 묶인 `init → raw PUT → finalize` session을 구현했다. 임시
  object를 bounded stream으로 다시 읽어 size와 SHA-256을 대조한 뒤 canonical key로
  게시하며, 검증된 DB Artifact가 없으면 Job 완료를 거부한다.
- upload session은 멱등키와 type/checksum dedupe를 검증한다. 만료된 같은 멱등키는
  generation을 올린 새 session으로 재발급하고, 파일 크기에 따라 PUT TTL을 최대
  3600초까지 늘린다.
- 다운로드는 owner/admin만 가능하며 S3는 짧은 presigned GET, Local adapter는 인증된
  streaming을 사용한다. API 응답에는 내부 storage URI와 presigned query를 남기지
  않는다.
- Worker publisher는 local `file://` metadata 등록을 제거하고 실제 byte를 비동기
  streaming한다. HTTPS Manager에서 HTTP upload URL로의 downgrade와 redirect를
  거부하며, 작업 취소 시 PUT/finalize 연결을 닫는다. 대형 finalize는 전용 3600초
  timeout과 `finalizing` polling을 사용한다.
- 로그와 metric은 0600 atomic disk spool에 먼저 기록한다. 전송 실패 후 재시작해도
  동일 멱등키로 재전송하고, 영구 4xx와 손상 record는 삭제하지 않고 dead-letter에
  보존한다.
- Worker는 object 5 GiB, attempt 256 files/100 GiB를 기본 상한으로 강제하고 G/D
  checkpoint는 각각 최신 20개만 선택한다. 초과 시 일부를 조용히 버리지 않고 Job을
  명시적으로 실패시킨다.
- Compose에 remote Worker용 presign 설정과 non-root API가 쓰는 전용 verification
  spool volume/init service를 추가했다. Manager installer는 시작 시 외부 presign URL을
  요구하고 loopback 개발 기본값을 경고한다.

**검증과 남은 위험**

- Local Manager↔Fake Worker 실제 HTTP E2E에서 binary PUT, Manager checksum finalize,
  canonical object, 완료 gate까지 2 tests — PASS. 만료 generation 보완 직후 재실행은
  도구 승인 사용량 제한으로 시작되지 못했으나 API generation 회귀 테스트와 최신
  Worker 55 tests는 PASS했다.
- Worker Ruff/strict mypy, async cancel/stream/polling, quota, spool restart/dead-letter
  tests — PASS.
- Manager spool ENOSPC/stale-finalizing 복구, API attempt quota와 상태별 retry contract,
  로그 DB 저장 전 redaction, bounded SSE의 재인증/세션 해제를 회귀 테스트로 검증했다.
  multipart/resume와 orphan staging cleanup scheduler는 아직 남아 있다.

### 인증된 실데이터 Dashboard와 프런트엔드 회귀 테스트

**구현**

- `/login`과 보호된 dashboard route group, `/session/login|logout|expired` BFF를
  추가했다. JWT는 browser JavaScript에 노출하지 않고 HttpOnly, SameSite=Strict,
  Path=/ cookie에만 저장하며 forwarded HTTPS에서 Secure를 적용한다.
- state-changing session route는 Origin/Host/protocol을 대조한다. Nginx가 외부 포트를
  잃지 않도록 `Host`와 `X-Forwarded-Host`를 `$http_host`로 전달한다.
- Worker/Dataset/Experiment/Job 목록과 Job 상세를 Manager API에서 server-side로
  읽고, admin Worker 메뉴와 owner/admin 경계를 반영했다. 실제 cancel/retry server
  action을 연결하고 API가 제공하지 않는 loss/artifact/storage 값은 `unknown`으로
  표시한다.
- Demo fixture는 `DASHBOARD_DEMO_MODE=true`일 때만 사용한다. 연결되지 않은 create,
  upload, filter 동작은 disabled 상태와 이유를 표시한다.

**검증과 제한**

- Vitest 24 tests, ESLint, Next production build — PASS. root `make check`가 `npm test`도
  실행하도록 Makefile을 갱신했다.
- 미인증 redirect, login cookie, logout revocation, Manager 401 session 제거와
  cross-origin 403을 localhost에서 검증했다.
- Job log/metric/artifact read 및 SSE API와 연결 작업은 진행 중이며 Dataset upload,
  Job/Experiment 생성과 sample 비교 UI는 아직 남아 있다.

### Manager backup/restore와 schema-safe rollback

**구현**

- Manager PostgreSQL 2개 DB와 MinIO 2개 bucket을 staging에 백업한 뒤 내부/외부
  SHA-256과 manifest를 기록하고 원자 게시한다. secret/config는 archive 대상에서
  제외한다.
- restore는 정확한 파괴 동의 flag, tar path/type, manifest/version/schema/checksum을
  검증하고 기본적으로 복원 전 안전 백업을 만든다. write service maintenance 뒤 DB와
  bucket을 범위 제한해 복원하고 migration/readiness를 확인한다.
- rollback은 release checksum과 schema compatibility marker가 맞을 때만 current
  symlink/image env를 전환하며 DB downgrade를 하지 않는다. readiness 실패 시 symlink,
  env와 service 상태를 직전 release로 돌린다.
- 실제 PostgreSQL/MinIO volume을 사용하는 opt-in
  `make test-manager-recovery-docker` drill을 추가했다.

**검증과 제한**

- 복구 정적/격리 통합 7 tests와 shell syntax — PASS. 두 release install→upgrade 보존
  fixture도 PASS했다.
- 첫 Docker volume drill은 DB/MinIO와 storage backup까지 실행한 뒤 test API의
  Alembic executable 누락으로 실패했다. shim을 추가했지만 승인 사용량 제한으로
  수정본 실제 재실행은 아직 못 했으므로 clean volume 복구 gate는 열린 상태다.

### 안전한 Dataset archive ingestion과 canonical flat manifest

**목적과 구현**

- 단일 WAV/FLAC/MP3/M4A/OGG/AAC 또는 ZIP을 job temporary root 안에서만 처리하는
  순수 ingestion core를 추가했다.
- ZIP `extractall`을 사용하지 않고 absolute/drive/`..`/backslash 경로, symlink와
  special file, encrypted/중복 member, 미지원 압축, CRC 오류를 거부한다.
- metadata와 실제 streaming byte 양쪽에서 entry 수, 파일별/전체 비압축 크기와
  압축률을 제한해 zip bomb와 위조 size를 방어한다.
- archive member를 Unicode-normalized 경로로 정렬한 뒤 원래 파일명을 버리고
  `000001.ext` 형식으로 flat화한다. SHA-256 중복은 한 번만 포함하고 source 관계를
  quality report에 남긴다.
- staging directory에 manifest/report를 완성한 뒤 lock과 rename으로 publish한다.
  destination이 이미 있거나 처리 중 실패하면 기존 경로를 덮어쓰거나 부분 결과를
  노출하지 않는다.
- PCM WAV는 bounded chunk로 duration/sample rate/channel/sample width, peak,
  clipping, silence와 RMS를 분석한다. 손상 WAV는 제외하며 다른 codec은 거짓 검증
  대신 명시적으로 `decoder_pending` 상태를 기록한다.

**검증과 제한**

- Dataset 전용 27 tests, API 전체 46 tests, Ruff, API strict mypy와 whitespace — PASS.
- traversal, symlink/FIFO, 암호화, duplicate name, CRC, size/ratio bomb와 publish race
  fixture를 포함한다.
- multipart upload router/object storage/RQ 연동은 아직 없고, non-WAV는 격리된
  ffmpeg decoder 검증 및 LUFS 분석 전까지 usable로 승격하면 안 된다.

### 사용자 access JWT, 소유권 RBAC와 설치용 관리자 bootstrap

**목적**

공개 상태였던 Manager CRUD를 사용자 인증 경계 뒤로 옮기고, Worker token과 사용자
credential을 교차 사용할 수 없게 한다. 최초 설치 시 비밀번호를 CLI 인자,
환경변수 또는 영구 Manager secret으로 남기지 않고 관리자를 생성한다.

**구현**

- Argon2id password hash와 email normalization, enumeration을 줄이는 dummy verify,
  15분 HS256 access JWT의 issuer/audience/sub/jti/iat/exp 검증을 추가했다.
- `/api/v1/auth/login`, `/auth/me`, `/auth/logout`을 구현했다. 로그인 응답은
  `Cache-Control: no-store`, logout은 JTI를 DB에 저장해 만료까지 재사용을 거부한다.
- `admin|user` 역할과 Dataset/Experiment/Job owner 정책을 적용했다. 일반 사용자는
  자기 리소스를 생성·조회·취소·재시도할 수 있고 타 사용자 및 legacy NULL owner는
  `404`로 숨긴다. admin은 전체 리소스와 Worker 목록/상세를 조회한다.
- Worker bearer와 User JWT dependency를 route 단위로 분리해 서로의 credential이
  상대 API에서 `401`이 되도록 했다.
- migration은 기존 `is_active` 의미를 보존하며 `disabled`, role constraint,
  `admin_bootstrap_state`, `revoked_access_tokens`를 추가하고 downgrade/재-upgrade를
  검증한다.
- bootstrap CLI는 DB lock 상태를 사용해 정확히 최초 관리자 한 명만 생성한다.
  password CLI/환경변수는 금지하고 0600 non-symlink 파일만 허용한다.
- Manager Compose의 migration/API에 별도 file-backed `jwt_secret`을 mount하고
  installer가 재설치 시 이를 보존한다. `bootstrap-admin` one-shot wrapper는 password
  파일을 읽기 전용 mount하며 원문을 process argument나 Manager 저장소에 남기지
  않는다.
- localhost Manager↔Fake Worker E2E도 실제 관리자 bootstrap/login/Bearer를 거쳐
  Dataset/Experiment/Job을 생성하도록 갱신했다.

**검증**

- API/contract Ruff 및 strict mypy — PASS.
- API/contract test 25개 — PASS; auth 전용 10개와 migration upgrade/downgrade 포함.
- `make test-e2e` — PASS, localhost 실제 HTTP 2개.
- 배포 정적/관리자 secret 전달 test 8개 — PASS.

**남은 제한**

- refresh token/session rotation과 계정 잠금이 없어 장기 browser session은 아직
  완성되지 않았다. 이후 Redis login rate limit은 별도 변경에서 추가했다.
- Artifact owner download/delete, 사용자 관리/비밀번호 변경, Worker token rotation과
  전체 request-context audit가 남았다.
- Web dashboard의 HttpOnly cookie BFF 연동은 별도 진행 중이다.

### FAISS index와 공식 small-model 추출 runtime 경계

**목적과 구현**

- RVC runtime의 NumPy/FAISS/scikit-learn을 지연 로딩하고 v1 256차원, v2 768차원을
  엄격히 검사하는 deterministic index builder를 추가했다.
- `.npy`는 pickle을 금지하고 regular non-symlink, shape/dtype/finite/입력 한도를
  검증한다. seeded shuffle, 20만 행 초과 MiniBatchKMeans 뒤 `total_fea.npy`,
  trained/added IVF-Flat index를 원자적으로 게시한다.
- small-model wrapper는 pinned upstream의
  `infer.lib.train.process_ckpt.extract_small_model`만 호출한다. 임의 checkpoint 복사를
  금지하고 repository revision, 경로, 안전한 이름, 공식 반환값, inference metadata와
  원본과 다른 결과 byte를 확인한 뒤 원자적으로 승격한다.

**검증과 제한**

- Worker 전체 37 tests, 소유 파일 Ruff와 strict mypy, upstream revision 검증 — PASS.
- 실제 NumPy/FAISS/Torch/CUDA image smoke와 runner stage 연결은 아직 남았다.
- KMeans는 BLAS/library version까지 고정해야 완전한 수치 재현성을 보장할 수 있다.

### 고정 RVC CLI 명령과 학습 metric 파서 구현

**목적**

실제 RVC 실행을 추측한 shell 문자열에 맡기지 않고, 검토한 upstream commit의 CLI를
순수 argv와 재현 가능한 학습 입력으로 표현한다. GPU 선택과 v1/v2/F0 분기를
subprocess 시작 전에 검증하고, 비구조화 학습 로그를 중앙 metric 어휘로 바꿀 수
있는 기반을 만든다.

**Upstream 검증과 핵심 결정**

- 공식 저장소 HEAD `7ef19867780cf703841ebafb565a4e47d1ea86ff`를 조회하고 해당
  tree의 `infer-web.py`, preprocess, F0, feature, train, checkpoint 처리 source를
  직접 대조했다.
- 전처리는 `infer/modules/train/preprocess.py`, CPU F0는
  `extract/extract_f0_print.py`, GPU RMVPE는 shard별 `extract_f0_rmvpe.py`, HuBERT는
  GPU shard별 `extract_feature_print.py`를 사용한다.
- upstream 학습 CLI의 GPU 구분자는 쉼표가 아니라 `0-1` 형태의 하이픈이다. 명령
  빌더가 Worker의 visible GPU 집합 밖 ID를 거부하도록 했다.
- v2 40k config는 해당 commit의 WebUI와 동일하게 `configs/v1/40k.json`을 사용하고,
  v2 48k는 `configs/v2/48k.json`을 사용한다.
- 기존 설치 예시 profile의 오래된 전처리 script명을 현재 경로로 바로잡고 정확한
  commit을 기록했다. 다만 이 예시는 전체 stage가 없는 최소 문법 예시이며 운영
  활성화 파일이 아님을 명시했다.

**변경 파일**

- `apps/worker/src/rvc_worker/rvc_commands.py`: 전처리, 5종 training F0,
  v1/v2 HuBERT, multi-GPU 학습 argv와 GPU inventory 검증
- `apps/worker/src/rvc_worker/training_inputs.py`: 안전한 example 교집합,
  deterministic `filelist.txt`, mute row, job-local config 복사
- `apps/worker/src/rvc_worker/training_metrics.py`: epoch/step/loss/train log 파서와
  지연 로딩 TensorBoard scalar 정규화
- `apps/worker/tests/test_rvc_commands_metrics.py`: v1/v2, F0/non-F0, RMVPE shard,
  GPU 거부, filelist/config, 로그 metric snapshot 9개
- `infra/worker/rvc-profile.example.yaml`, Worker README와 upstream 검증 메모

**검증**

- `ruff check apps/worker` — PASS.
- `mypy apps/worker/src` — PASS, strict mode 17 source files.
- `pytest -q apps/worker/tests` — PASS, 29 tests.
- `git diff --check` — PASS.

**남은 제한**

- command builder와 parser는 검증됐지만 전체 profile runner의 stage별 실행 및
  실시간 중앙 전송에는 아직 연결되지 않았다.
- index 생성, small model 공식 추출, sample inference 명령과 실제 CUDA/PyTorch 및
  asset checksum 검증이 남았다.
- RVC repository를 job workspace 안에 안전하게 materialize하는 방식과 중앙 binary
  artifact publisher가 준비되기 전에는 profile runner가 완료 상태로 갈 수 없다.

### Phase 1 실행 가능한 수직 흐름과 설치 기반 완성

**목적**

빈 저장소에서 공통 계약, 중앙 API/DB, Fake 학습 Worker, 운영 대시보드, 전체 중앙 stack과 Manager/Worker 설치 bundle까지 실행 가능한 첫 수직 흐름을 만든다. 실제 RVC와 object storage binary upload가 준비되지 않은 상태를 운영 완료로 오인하지 않도록 안전 gate를 둔다.

**공통 계약과 중앙 API**

- Pydantic contract에 v1/v2, 40k/48k, training/inference F0, version별 feature directory와 Job 상태 전이를 구현했다.
- Worker/GPU capability, fake/rvc_webui engine mode, register/session/heartbeat/claim/lease와 idempotent log/metric/artifact batch를 정의했다.
- FastAPI `/api/v1`에 Dataset metadata, Experiment, Job, cancel/retry, Worker register/me/heartbeat/claim/lease/status/log/metric/artifact를 구현했다.
- SQLAlchemy async 모델과 Alembic migration에 User, Worker, Dataset, Experiment, Job/Attempt/Lease/Event, Log, Metric, Artifact, Audit를 포함했다.
- Worker token 원문은 최초 한 번만 반환하고 DB에는 pepper를 사용한 HMAC-SHA256만 저장한다.
- production은 PostgreSQL과 명시적 secret을 강제하고 Fake Worker를 거부한다.

**Worker Agent**

- CLI/YAML/env 설정, 0600 원자 credential 저장, 재시작 `/workers/me`, heartbeat와 lease task를 구현했다.
- job/attempt별 workspace, disk 검사, `nvidia-smi`, shell 없는 argv subprocess와 process-group cancel을 구현했다.
- FakeRvcRunner가 모든 상태, version별 feature, G/D checkpoint, small model, final index, sample, metric과 manifest를 생성한다.
- pretrained resolver와 artifact parser는 v1/v2, F0/non-F0, checkpoint epoch와 ambiguous index를 검증한다.
- 실제 profile은 고정 RVC commit과 명시적 command profile 없이는 실행되지 않는다. central binary publisher가 없으므로 profile 결과의 `completed` 승격도 거부한다.

**배포와 설치 파일**

- API/Web/Worker multi-stage non-root image와 MLflow image를 만들었다.
- PostgreSQL, Redis, MinIO/init, MLflow, API migration/API, Web, Nginx의 Compose와 health dependency를 구현했다.
- Manager/Worker 각각 preflight/install/upgrade/uninstall/Compose wrapper/tar bundle builder를 제공한다.
- versioned release, current symlink, 0600 secret/config, 재설치 보존, non-destructive uninstall, 내부/외부 SHA-256 정책을 구현했다.
- 개발 설치물 `rvc-manager-0.1.0-dev.1-linux-amd64.tar.gz`와 `rvc-worker-0.1.0-dev.1-linux-amd64.tar.gz`를 생성하고 외부 checksum을 검증했다. image archive는 포함하지 않은 개발용 online bundle이다.

**통합 검증 중 발견하고 수정한 문제**

- Alpine BusyBox `mktemp`는 suffix 뒤의 `XXXXXX` 형식을 요구해 Redis config template을 suffix 없는 형태로 수정했다.
- Redis official entrypoint가 root에서 `redis` user로 권한을 내리므로 0600 임시 config의 소유자를 `redis:redis`로 바꿨다.
- Nginx container의 `localhost` health가 IPv6로 해석되어 실패하므로 health URL을 `127.0.0.1`로 고정했다.
- 수정 후 전체 9개 service/one-shot container가 성공 또는 healthy 상태가 되었고 reverse proxy `/healthz`, dashboard `/`, `/api/v1/jobs`가 HTTP 200을 반환했다.

**자동 검증 결과**

- `make check` — PASS: Ruff, mypy strict 34 source files, Python 34 unit/integration, Frontend lint/build, shell syntax, whitespace.
- `make test-e2e` — PASS: 실제 localhost Uvicorn 기반 Manager↔Fake Worker 2 test.
- Python 전체 — 36 passed.
- Alembic SQLite upgrade/check와 PostgreSQL offline SQL — PASS.
- API, Web, Worker, MLflow Docker image build — PASS.
- API `/health`와 `/ready`, Web `/`, Worker `--check` container smoke — PASS.
- 전체 Manager Compose runtime smoke — PASS. 데이터 volume을 삭제하지 않고 test container/network만 종료했다.

**남은 제한과 다음 작업**

- 사용자 JWT/RBAC가 없어 Manager CRUD는 아직 외부 공개할 수 없다.
- Dataset streaming upload/안전한 archive flatten/품질 검사가 없다.
- artifact API는 metadata 단계이며 MinIO presigned upload와 checksum finalize가 없다.
- 실제 RVC command adapter, CUDA/PyTorch/asset pin과 GPU smoke가 없다.
- dashboard는 제품 UI와 Fake fixture 단계이며 실제 API/auth/write 연결이 남았다.
- 개발 bundle은 최종 설치 파일이 아니며 Ubuntu clean-VM, offline image, SBOM/서명과 upgrade/rollback 검증이 남았다.

### 중앙 대시보드 제품 UI 기준선

**목적**

Backend contract를 연결하기 전에 실제 운영자가 사용할 정보 구조와 Fake runner 수직 흐름의 표시 기준을 실행 가능한 Next.js 앱으로 만든다.

**변경 파일**

- `apps/web`: Next.js 16.2 App Router, TypeScript/ESLint 구성과 lockfile
- `src/components/app-shell.tsx`: 반응형 navigation, 환경/engine badge, 접근성 skip link
- `src/app/page.tsx`: Worker/queue/Job/service 상태 overview
- `src/app/workers`, `datasets`, `experiments`, `jobs`: 주요 관리 화면의 제품별 UI 기준선
- `src/lib/types.ts`, `demo-data.ts`: UI projection과 명시적인 Fake fixture
- `src/app/globals.css`: 반응형 운영 dashboard design system

**핵심 결정**

- 요구사항의 self-hosted Manager 설치 범위를 유지하므로 Sites용 별도 hosting project나 중첩 Git 저장소를 만들지 않았다.
- 첫 viewport는 일반 관리 템플릿이 아니라 Worker, queue, loss, VRAM과 artifact 상태를 중심으로 구성했다.
- 아직 API write가 연결되지 않은 버튼은 shell로 남기고 화면 상단에 `FAKE ENGINE`을 고정 표시했다.
- 사용자 JWT는 browser local storage에 임시 구현하지 않고 Backend와 secure cookie/BFF 계약을 확정한 뒤 연결한다.

**검증**

- `cd apps/web && npm run lint` — PASS.
- `cd apps/web && npm run build` — PASS. Next.js production build, TypeScript 검사와 `/`, `/workers`, `/datasets`, `/experiments`, `/jobs`의 7개 static page 생성 완료.

**남은 제한**

- 현재 화면은 Fake fixture 기반이며 API fetch/mutation, 로그인, loading/error/empty 상태와 E2E가 남았다.
- Dataset upload, Job cancel/retry, 실험 생성과 비교 player는 시각적 shell이라 체크리스트 기능 완료로 표시하지 않았다.

### 보안 위협 모델과 보존 기본값 확정

**변경 파일**

- `docs/SECURITY.md`: 보호 대상, 신뢰 경계, 인증·파일·subprocess·무결성·로그 통제와 삭제/보존 정책
- `CHECKLIST.md`: 위협 모델과 데이터 보존 기준선 완료 반영

**핵심 결정**

- 사용자 Dataset과 참조 중인 Artifact는 retention을 명시적으로 켜기 전 자동 삭제하지 않는다.
- orphan temporary object와 완료된 Worker workspace만 검증 후 유예 기간을 두고 정리한다.
- uninstall은 데이터 보존이 기본이며 영구 삭제는 별도 명시적 흐름으로 제한한다.
- Worker를 완전히 신뢰하지 않고 lease 소유권, attempt, checksum을 모든 쓰기에서 검증한다.

**검증과 제한**

- 업로드 설계의 JWT/Worker token/만료 download/audit 요구를 포함하고 archive, command, stale Worker와 공급망 위협을 추가했다.
- 조직별 tenant와 법적 보존 기간은 운영 주체가 정해지면 확장해야 한다.

### 요구사항 추적과 핵심 ADR, upstream 근거 추가

**목적**

임시 첨부 문서의 요구가 구현 과정에서 누락되지 않게 고유 ID와 검증 가능한 인수 조건으로 전환하고, 원문 안의 queue/Worker와 Dataset 책임 충돌을 해소한다.

**변경 파일**

- `docs/REQUIREMENTS_TRACEABILITY.md`: 시스템, API, Dataset, Job, Worker, RVC, UI, 보안, 설치 요구사항 ID와 인수 조건
- `docs/adr/0001-remote-worker-job-claim.md`: PostgreSQL 원장 기반 HTTP claim/lease 결정
- `docs/adr/0002-canonical-dataset-preparation.md`: Manager canonical flatten과 Worker checksum 재검증 결정
- `docs/adr/0003-installation-platform.md`: Ubuntu 22.04/24.04와 Manager/Worker 분리 bundle 결정
- `docs/RVC_UPSTREAM_NOTES.md`: 공식 저장소, Wiki, FAQ와 현재 source 대조 결과
- `AGENTS.md`, `CHECKLIST.md`: 추적표 필독 및 완료 상태 반영

**핵심 결정과 근거**

- 원격 GPU Worker에 Redis를 노출하지 않고 API claim/lease를 사용한다. RQ는 중앙 내부 task용이다.
- canonical flat Dataset은 Manager가 한 번 만들며 Worker는 manifest/checksum을 재검증한다.
- 첫 contract는 요구사항대로 v1/v2의 40k/48k만 지원한다. 공식 현재 source의 v2 32k는 호환 matrix 확정 전 제외한다.
- 공식 WebUI가 내부적으로 shell command를 사용하더라도 Worker adapter는 검증된 argv와 `shell=False`만 사용한다.

**검증**

- `git diff --check` — PASS, whitespace 오류 없음.
- 공식 RVC 저장소 README, Training Wiki, FAQ, LICENSE와 현재 `infer-web.py`를 확인해 feature/F0/pretrained/index/command 구조를 대조했다.
- RVC source는 MIT로 표시되지만 개별 model asset의 bundle 재배포 권리는 아직 검증되지 않아 release gate로 유지했다.

**남은 위험과 다음 작업**

- RVC commit과 runtime/asset checksum은 실제 adapter 전에 고정해야 한다.
- 각 요구사항 상태는 코드와 자동 테스트가 들어온 뒤 `Partial` 또는 `Verified`로 바꾼다.

### 저장소 기반선과 협업 규칙 수립

**목적**

빈 Git 저장소에서 1,343줄 분량의 RVC 중앙 관리 시스템 설계를 구현 가능한 단계로 나누고, 여러 작업자가 동일한 기준으로 이어서 개발할 수 있게 한다.

**확인한 요구사항**

- 중앙 Manager는 FastAPI/Next.js/PostgreSQL/Redis/MinIO/MLflow 구조이며 학습을 직접 수행하지 않는다.
- GPU Worker가 RVC 전처리, F0, HuBERT feature, 학습, index, small model, sample 생성과 업로드를 담당한다.
- v1의 `3_feature256`과 v2의 `3_feature768`, training/inference F0 선택지, checkpoint와 small model의 의미를 구분해야 한다.
- Dataset flatten/품질 검사, Worker heartbeat, capability 기반 배정, 상태·로그·metric 수집, experiment 비교, 인증과 설치 파일이 최종 범위다.

**변경 파일**

- `README.md`: 제품 목적, 구성과 핵심 원칙
- `AGENTS.md`: 작업 시작/종료 절차, 아키텍처 불변 조건, 테스트·보안·패키징 지침
- `CHECKLIST.md`: 요구사항부터 v1.0 설치 검증까지 단계별 추적 목록
- `docs/ARCHITECTURE.md`: 서비스 책임, 상태 머신, 저장소, RVC 호환과 배포 기준선
- `docs/DEVELOPMENT_HISTORY.md`: 본 상세 이력
- `.gitignore`: 비밀, 데이터셋, 모델, 캐시와 빌드 산출물 제외

**핵심 결정**

- API는 처음부터 `/api/v1` prefix를 사용한다.
- PostgreSQL을 작업 원장으로 두고 Redis는 queue/realtime/coordination 용도로 제한한다.
- 중복 학습 방지를 위해 Worker 배정에 명시적인 lease/attempt 개념을 추가한다.
- 1차 설치 대상은 Ubuntu 22.04/24.04 x86_64로 가정한다. Manager는 Docker, Worker는 NVIDIA Container Toolkit 기반으로 패키징한다.
- 설치물은 Manager와 Worker를 분리하고 manifest와 SHA-256을 함께 생성한다.

**검증**

- 업로드 문서 전체 1,343줄을 범위별로 읽고 설계의 1~28장을 확인했다.
- `git status --short --branch`로 저장소가 `No commits yet on master`인 빈 초기 상태임을 확인했다.
- 로컬 도구는 Python 3.13.9, Node 25.9.0, npm 11.13.0, Docker 29.4.3이다.
- 현재 Docker CLI에는 `docker compose` plugin이 없어 Compose 기반 통합 검증 전 별도 준비가 필요하다.

**남은 위험과 다음 작업**

- RVC upstream commit/CUDA/PyTorch 조합과 pretrained weight 배포 권한은 아직 확정되지 않았다.
- 운영 조직 모델과 데이터 보존 정책이 미정이다.
- 다음 작업은 중앙 API/DB 기반과 Worker protocol의 실행 가능한 contract를 구현하고 테스트하는 것이다.

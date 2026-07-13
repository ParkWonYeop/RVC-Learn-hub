# 배포와 설치 파일

현재 검증된 개발 기준선은 `0.1.0-dev.20`의 비대칭 설치 파일 쌍이다. Manager는 정확한 8개
linux/amd64 image를 포함한 self-contained 개발 후보이고 Worker는 image/runtime이 없는 partial이다.
공통 source commit은 `298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a`다. Dataset/TestSet writer/finalizer fencing과 전용
RQ 이중 cleanup, Worker token 회전/폐기/재등록, Experiment 안전 CRUD와 mutation UI,
fixture 기반 PM/Harvest/CREPE/RMVPE Sample publication/completion 경계와 Sample Range/A-B UI,
Dataset typed PCM aggregate, image closure v2, Torch 2.6.0/cu124 후보 lock, active oneshot unit
재시작과 Worker runtime UID/GID 권한 보정, 49-case qualification→read-only activation→production
factory/capability 경계, native live telemetry와 terminal watermark, Dataset integrated LUFS,
strict installed release file/environment closure와 Manager 전체 Compose smoke 보정을 반영했다.
dev.18은 여기에 exact current real attempt provenance와 canonical model/index 재해시를 요구하는
model registry, `candidate -> approved -> revoked`, Experiment별 active champion 0/1, 이전 approved
entry rollback promotion, owner/admin CAS·hashed idempotency·audit와 same-origin BFF/UI를 추가했다.
dev.19는 maintenance 전용 PostgreSQL column/function role, staging delete-only MinIO identity,
exact RQ Redis ACL과 long-operation CAS heartbeat를 추가했다.
dev.20 Manager archive는 `BUNDLE_FORMAT_VERSION=2`, image manifest v2와
`SELF_CONTAINED=true`를 사용하고 application 3개와 version-scoped dependency alias 5개를 포함한다.
Worker archive는 같은 format/source version이지만 `SELF_CONTAINED=false`이며 image/runtime을
포함하지 않는다. 따라서 이 두 archive를 최종 v1.0 설치물로 취급하지 않는다. Reviewed immutable
amd64 base source digest와 Worker GPU/no-network matrix, vulnerability/container/license 검토, refresh/session, 실제
PostgreSQL/Redis/MinIO 장애 주입, model registry의 S3 대용량 재해시·다중 replica
promotion·browser response-loss와 clean-VM 설치·token drill이 release gate다. 중간 `dev.3`은
host Python cache 포함을 발견해
사용하지 않으며 cache-pruned `dev.4`가 이를 대체했고, e2 Dataset fence가 포함된 `dev.5`, f9
Dataset aggregate와 image closure v2를 포함한 `dev.6`, unit/권한 보정 `dev.7`이 그 뒤를 이었다.
`dev.8`은 qualification activation을 추가했고, `dev.9`는 `b4a91d7e2c63` 관리자 사용자
lifecycle과 3-Worker terminal/heartbeat race 보정을 추가했다. `dev.10`은 source head
`ca8d3e7f4b10`의 live stdout/train.log/TensorBoard telemetry, payload fingerprint, terminal
exclusive watermark, active ingest write fence와 2 MiB raw body 제한을 추가했다. dev.11은
같은 schema 위에 operator-owned public scheme, 60초 system metric sampling/GPU availability,
typed telemetry failure와 latest metric tail/15초 UI polling을 추가했다. dev.12는 root
source secret의 API·maintenance·MLflow별 atomic runtime projection, Manager/MLflow exact MinIO
bucket policy, exact current JobAttempt engine metadata와 Fake 결과 경고를 추가했다.
dev.13은 역사적으로 `d8f2a6c4b901` Dataset integrated LUFS, exact checksum inventory와 mode `0444`
installed `RELEASE_SHA256SUMS`, start/restart/rollback 재검증, partial environment binding 및
bundle-local 설치·시험 문서를 추가했다. dev.14는 MinIO·MLflow loopback publish가 실제
동작하도록 두 서비스만 별도 `host-access` bridge에 연결하고, bundled proxy의 명시적 foreground
command와 zero-argument fallback을 고정했다. 전체 stack smoke는 역할별 runtime secret의 root
exact-inventory 검사와 non-root known-file read/no-enumeration 경계를 분리해 검증한다. dev.15는
release source ignore closure, Docker config byte/user binding, bundle ledger 누락 거부,
forward-only staged upgrade와 uninstall failure propagation을 추가했다. dev.16은 physical
installed-release runbook, bundle-local 결과 템플릿, MLflow exact overlay lock, Manager release
orchestrator와 Worker read-only readiness report를 추가했다. dev.17은 Experiment 비교
BFF/UI source, Worker custom CA의 installer·fixed read-only mount·공통 strict SSL context,
bundle-local native negative runbook과 fail-fast/fixed-hash/config-only/secret pre-state 가이드
보정을 추가했다. dev.18은 model registry source/schema/API/BFF/UI를 추가했고 dev.19은 이 경계를
누적해 maintenance 최소권한 source/schema/Compose/installer 회귀를 추가했다. dev.20은 tracked clean source, cross-architecture
target image materialization, 별도 OCI/config digest와 single-platform Docker-save closure를 추가했다.
dev.17 archive는 immutable 역사 증거이며 dev.20 schema/source와 Manager image를 포함하지 않으므로
신규 설치·업그레이드 기준으로 사용하지 않는다.
dev.20 source의 전체 `make check`는 Python `752 passed, 4 deselected`, strict mypy 88 files와 Web
`24 files/211 tests`를 포함해 통과했고 localhost HTTP E2E도 `4 passed in 6.68s`였다. dev.20
release-image 전체 Compose smoke도 PASS했다. 이 자동 회귀와 arm64 emulation 증거는
Worker runtime 부재나 아래 clean-host·GPU·storage·browser 출시 gate를 대신하지 않는다.
사용자 실행 절차는 `docs/INSTALLATION_GUIDE.md`를 우선한다.

## 설치 bundle 생성

```bash
installers/manager/build-self-contained-release.sh \
  --version 0.1.0-dev.20 \
  --schema-compatibility f5d1c8a9b240 \
  --output-dir dist/installers
installers/worker/build-bundle.sh \
  --version 0.1.0-dev.20 \
  --output-dir dist/installers
```

`--schema-compatibility`는 두 Manager release 사이에서 기존 database schema를 그대로
사용해도 되는지를 운영자가 검토한 뒤 부여하는 marker다. 생략하면 `unknown`이며 자동
rollback은 거부된다. migration head가 같다는 사실만으로 호환성을 가정하지 말고, 이전
application code가 현재 schema를 읽고 쓸 수 있는지도 확인해야 한다.

기본 출력:

```text
dist/installers/rvc-manager-<version>-linux-amd64.tar.gz
dist/installers/rvc-manager-<version>-linux-amd64.tar.gz.sha256
dist/installers/rvc-worker-<version>-linux-amd64.tar.gz
dist/installers/rvc-worker-<version>-linux-amd64.tar.gz.sha256
```

현재 dev.20 Manager archive는 `667617422` byte이고 SHA-256은
`c6488dad47c7f38c082ed6fa68f1fe3691c069110aef0bbf68a9d7ba5e6f5b70`이다. Worker archive는
`108488` byte이고 SHA-256은
`7f36cbf27100bf70425c2780142d4fa3f6e6e76d0acf410d3e3fb698aa50558b`다. 두 manifest의 source는
`298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a`, Manager schema marker는 `f5d1c8a9b240`이다. 폐기된 개발 archive
구분은 `dist/installers/README.md`에 기록한다. Linux에서는 배포 전에 각 `.sha256`을
`sha256sum -c`로 확인한다. 압축 해제 뒤에는 `sha256sum -c SHA256SUMS`와
`python3 common/image_bundle.py verify-ledger --root . --ledger-name SHA256SUMS`를 모두 실행해
나열된 byte뿐 아니라 추가·누락·unsafe 파일도 거부한다.

각 archive 안에는 `manifest.env`, 내부 `SHA256SUMS`, 현재 version으로 렌더링된
`README.md`/`TESTING.md`와 사용자용 `TEST_RESULT_TEMPLATE.md`, Compose/infra와
install/upgrade/uninstall/preflight 및 Manager backup/restore/rollback script가
들어간다. 또한 exact lock 기반 CycloneDX 1.6 dependency inventory와 declared-license
report를 `supply-chain/`에 포함한다. 현재 상태는 취약점/법적 검토와 일부 distribution
hash/image digest가 빠진 `partial-release-gates-open`이며 완전한 release attestation이
아니다. dev.20 Manager는 exact 8-role linux/amd64 image/archive inventory와
`SELF_CONTAINED=true`를 명시한다. 외부 checksum, 내부 ledger, image closure와 load 뒤 identity,
release image 전체 Compose smoke가 PASS했다. 다만 이 smoke는 arm64 Colima의 amd64 emulation에서
실행했으므로 clean Ubuntu native production 증거가 아니다. Worker는 빈 `images`/`archives`
inventory와 `SELF_CONTAINED=false`를 명시한다. Compose가 기대하는 exact tag
`rvc-orchestrator-worker:0.1.0-dev.20` image와 실제 RVC runtime이 archive에 없으므로 native/profile
학습이나 air-gapped Worker 설치에 사용할 수 없다. 두 bundle의 source provenance가 같더라도
Worker에 별도 image를 결합한 결과는 검증된 dev.20 release closure가 아니다.

self-contained Manager는 `--self-contained`와 role-qualified `--include-image`로 exact
8-image closure를 제공해야 한다.

```bash
installers/manager/build-bundle.sh \
  --version <version> \
  --schema-compatibility <reviewed-marker> \
  --self-contained \
  --include-image api=rvc-orchestrator-api:<version> \
  --include-image web=rvc-orchestrator-web:<version> \
  --include-image mlflow=rvc-orchestrator-mlflow:<version> \
  --include-image postgres=postgres:16-alpine \
  --include-image redis=redis:7.4-alpine \
  --include-image minio=minio/minio:RELEASE.2025-04-22T22-12-26Z \
  --include-image minio-client=minio/mc:RELEASE.2025-04-16T18-13-26Z \
  --include-image nginx=nginx:1.27-alpine
```

image closure v2는 추가/누락/중복 role과 archive, unsafe Docker-save member, reference,
image/config digest, OS/architecture 또는 application version/revision label 불일치를 거부한다.
dependency source tag는 manifest provenance로 보존하되 실행 reference는
`rvc-orchestrator-postgres|redis|minio|minio-client|nginx:<version>` alias로 만들어 새 upgrade가
rollback release의 dependency tag를 덮지 않게 한다. build 전 source는 real 40-hex commit과
clean tree여야 한다. Arm64 release host의 Buildx 경로는 `docker pull --platform`의 성공 문구만
신뢰하지 않고 각 dependency를 target `linux/amd64` zero-layer image로 materialize한 뒤 검사한다.
Dependency image에 `Config.User` key가 없으면 빈 값으로 정규화하고, OCI index `.Id`와
Docker-save config byte digest는 각각 manifest에 기록한다. 두 digest가 다르다는 이유로 거부하지
않지만 archive config byte와 load 전후 OCI identity 검증을 모두 통과해야 한다.
Single-platform Docker-save closure에는 Buildx default provenance attestation을 포함하지 않도록
`--provenance=false`로 export한다. 이는 source commit label, bundle manifest, 외부 SBOM·scan과
release review를 대체하는 attestation이 아니며 해당 공급망 gate는 계속 별도로 남는다.

Worker의 검증된 real-RVC image는 일반 `--include-image`와 구분한다. image tag는
installer가 선택하는 `rvc-orchestrator-worker:<version>`과 같아야 하고, 원본 asset
root/manifest와 offline build manifest를 함께 제공해야 한다. bundle builder는 asset을
다시 hash하고 image의 RVC/base/wheelhouse/fairseq/asset label을 모두 대조한 뒤에만
image archive와 manifest를 포함한다. 구체 명령과 manifest schema는
`infra/worker/runtime/README.md`를 본다.

self-contained install/upgrade는 load 전에 strict image manifest/archive를 검사하고, Docker
load 뒤 exact loaded identity를 다시 검사한 뒤에만 release를 게시·활성화한다. 설치된
Manager/Worker compose wrapper도 `up|start|restart|run|create` 전에 release manifest, env와
loaded identity를 재검증한다. Manager rollback 대상도 symlink/env 전환과 시작 전에 같은 검증을
통과해야 한다. 모든 self-contained service는 `RVC_IMAGE_PULL_POLICY=never`를 사용하므로
registry pull로 missing/mismatched image를 조용히 보충하지 않는다.

Partial/self-contained 여부와 무관하게 installer는 release tree를 원자 게시하면서 mode `0444`
`RELEASE_SHA256SUMS`를 만든다. 재설치, Compose wrapper와 Manager rollback은
`verify-ledger --ledger-name RELEASE_SHA256SUMS`로 exact file inventory를 다시 검사하고,
partial bundle도 version, image reference, pull policy와 Worker provenance/gate가 manifest와
일치하는지 확인한다. 이 검증을 위해 installed ledger의 쓰기 권한을 열거나 다시 생성하지 않는다.

## Manager 설치

Ubuntu 22.04/24.04 x86_64에서 archive와 외부 SHA-256을 확인하고 압축을 푼다. dev.20 Manager
archive는 application 3개와 dependency alias 5개를 이미 포함하며 installer가 게시 전 load와
exact identity를 검증한다. archive 밖의 동일 tag image를 대신 사용하지 않는다. TLS/DNS/CORS 설정
전에는 서비스를 시작하지 않는다. 아래 clean Ubuntu native lifecycle은 아직 출시 gate이므로 최초
시험은 `--no-start`로 설정을 검토한 뒤 진행한다.

```bash
sudo ./preflight.sh
sudo ./install.sh \
  --no-start \
  --public-scheme https \
  --s3-presign-endpoint-url https://objects.example.com

sudoedit /etc/rvc-orchestrator/manager/manager.env
# PUBLIC_SERVER_NAME=manager.example.com
# PUBLIC_SCHEME=https
# CORS_ORIGINS=https://manager.example.com
# HTTP_BIND_ADDRESS=127.0.0.1
# S3_PRESIGN_ENDPOINT_URL=https://objects.example.com
# S3_VERIFY_TLS=true
# WORKER_TELEMETRY_JSON_MAX_BYTES=2097152

sudo /opt/rvc-orchestrator/manager/bin/manager-compose config --quiet
sudo systemctl daemon-reload
sudo systemctl enable rvc-orchestrator-manager.service
sudo systemctl restart rvc-orchestrator-manager.service

sudo /opt/rvc-orchestrator/manager/bin/bootstrap-admin \
  --email admin@example.com \
  --password-file /root/rvc-admin-password
```

같은 호스트의 TLS reverse proxy는 Manager를 loopback `8080`, object endpoint를 loopback `9000`에
연결하고 S3 signed Host/path/query/method/body를 보존해야 한다. 전체 실행 순서와 image 준비 명령은
`docs/INSTALLATION_GUIDE.md`를 따른다.
PostgreSQL·Redis와 application backend는 `internal: true` network에만 남기고, host port가 필요한
MinIO·MLflow만 별도 `host-access` bridge에도 연결한다. 모든 published address는 계속
`127.0.0.1` 기본값이며 외부 TLS proxy나 승인된 운영자 접근 없이 공인 interface에 bind하지 않는다.
Bundled Nginx는 Compose의 명시적 `nginx -g 'daemon off;'` command로 foreground 실행하며 custom
entrypoint도 인자가 없을 때 같은 command를 보충한다. 이를 제거해 proxy가 성공 코드로 즉시
종료·재시작하는 상태를 정상 기동으로 해석하지 않는다.

기본 위치:

- application release: `/opt/rvc-orchestrator/manager/releases/<version>`
- current symlink: `/opt/rvc-orchestrator/manager/current`
- config/secret: `/etc/rvc-orchestrator/manager`
- persistent data: Docker named volumes
- systemd: `rvc-orchestrator-manager.service`

Manager 설치기는 production에서 Fake Worker를 허용하지 않고 JWT signing key를
포함한 secret을 개별 0600 파일로 생성한다. 관리자 password는 설치 저장소에
복사하거나 환경 변수로 넘기지 않고, 명시한 0600 파일을 one-shot container에 읽기
전용 mount해 최초 관리자 한 명만 bootstrap한다. 나중에 초기화하려면
`/opt/rvc-orchestrator/manager/bin/bootstrap-admin --email ... --password-file ...`를
실행한다. 일반 API/RQ/MLflow는 이 root 소유 source file을 직접 mount하지 않는다. Root,
network-none `manager-secrets-init`가 source를 검증해 API, maintenance, MLflow와 database-authz
전용 named volume의 완전한 generation으로 투영하고 `current` symlink를 원자 교체한다. API/RQ는
UID/GID `10001:10001`, MLflow는 `10002:10002` 소유 mode `0400` 파일만 읽으며 RQ에는
JWT/bootstrap/pepper/MLflow credential을 제공하지 않는다. Installed Compose wrapper는
`up|start|restart|run|create` 전에 이 projection을 다시 수행하고 실패하면 이전 generation을
보존한 채 시작을 중단한다. Maintenance DB/Redis/S3 값이 대응하는 API 값과 같아도 새 generation을
게시하지 않는다.

PostgreSQL, Redis, MinIO, non-root RQ maintenance Worker, MLflow, migration,
API, Web, Nginx를 함께 시작한다. RQ Worker는 내부 network만 사용하고 Docker socket/GPU/
host port가 없으며 read-only filesystem/capability drop/PID 상한을 적용한다. 전용 entrypoint는
PostgreSQL·Redis·S3 secret만 읽고 API JWT, Worker bootstrap/pepper, MLflow token은 mount하지
않는다. Compose는 API/migration의 `PROCESS_ROLE=api`와 RQ Worker의
`PROCESS_ROLE=maintenance`를 외부 환경으로 override할 수 없는 literal로 고정한다. API
`/ready`는 Redis, 최근 RQ heartbeat와 PostgreSQL-ledger reconciler cycle freshness를
각각 fail-closed로 검사한다. 모든 API replica가 reconciler를 실행하지만 PostgreSQL
transaction advisory lock과 row `SKIP LOCKED`로 한 bounded cycle만 원장을 재전달한다.
MLflow image와 Compose runtime은 UID/GID `10002:10002`, read-only root filesystem,
capability drop, PID 128과 mode `0700` UID-owned `/tmp` tmpfs로 고정한다. Docker release smoke는
network-none 상태에서도 dependency import와 `/health=OK`를 확인하며 최종 amd64 image에서 다시
실행해야 한다.
MinIO init은 Manager app과 MLflow에 서로 다른 bucket-scoped policy를 만들고 built-in
`readwrite`를 제거한다. 각 identity에는 expected policy 하나만 남아야 하며 상대 bucket write는
거부돼야 한다. Maintenance identity에는 Manager bucket의 `datasets/staging/*`와
`test-sets/staging/*` `DeleteObject` 하나만 허용하고 list/read/write, canonical key와 MLflow
bucket 접근을 거부한다. Versioned bucket의 delete marker를 완료로 오인하지 않도록 bundled
MinIO bucket versioning이 활성화돼 있으면 init은 fail-closed한다.
API는 MLflow 장애 중에도 PostgreSQL outbox를 보존해야 하므로 MLflow container health를
hard startup dependency로 두지 않는다. `MLFLOW_FAIL_CLOSED=false` 기본은 `/ready`에
`mlflow=unavailable`을 표시하면서 원장 API를 유지하고, `true`는 `/ready`와 즉시
projection 실패 write를 `503`으로 만든다. 완전히 끄려면 `MLFLOW_ENABLED=false`를 쓴다.

Migration 뒤 `maintenance-db-authz` one-shot은 `rvc_maintenance` login과 `NOLOGIN` function owner를
만들고 exact column ACL, upload-id 기반 parent row-lock 함수와 함수 실행 ACL을 적용·재검증한다.
RQ 시작 전에는 main DB password 없이 maintenance login 자체로 같은 경계를 다시 검사한다.
Redis는 별도 maintenance password와 exact RQ lifecycle key/command ACL을 사용하며 generic
callback/dependent/repeat registry cleanup, pub/sub control과 `FLUSH*|CONFIG|ACL|MODULE|KEYS|SCAN`
등 관리 경로를 허용하지 않는다. Installed `start|restart`는 one-shot completion을 우회하지 않도록
service 단위 인자를 거부하고 full `up --force-recreate`로 권한 initializer를 다시 적용한다.
Redis 유실 뒤 기존 DB run reconciliation은 구현됐지만 API/운영자 Redis identity 세분화와 실제
다중 API replica/PostgreSQL/Redis/외부 S3 restart·partition 장애 주입은 release gate다.

`--s3-presign-endpoint-url`은 원격 Worker가 접근할 수 있는 HTTPS object endpoint다.
MinIO API 기본 bind는 `127.0.0.1`이므로 같은 host의 안전한 TLS reverse proxy를
권장한다. 별도 reverse-proxy host에서 MinIO에 직접 연결해야 할 때만 방화벽으로 제한한
interface를 `--minio-api-bind-address <private-ip>`로 명시하고, MinIO의 평문 port를
인터넷에 직접 노출하지 않는다.

Manager API의 verification 임시 파일은 Compose named volume `artifact_spool`의 0700
directory에 저장된다. 기본 상한은 단일 object/PUT 5 GiB, attempt당 유효 session 256개와
선언 용량 합계 100 GiB다. 현재 전송은 단일 PUT만 지원하며 multipart/resume는 구현되지
않았으므로, 대형 checkpoint의 네트워크 단절 복구는 아직 release gate다.

Dataset archive snapshot은 별도 named volume `dataset_ingestion`에 저장한다.
`dataset-ingestion-init` one-shot service가 API 시작 전에 non-root `rvc` 소유 mode `0700`
directory를 만들고 API는 이를 `DATASET_INGESTION_ROOT`로 사용한다. 원본 5 GiB, owner별
동시 8 session/20 GiB, ZIP 10,000 entry와 전체 20 GiB 비압축 상한이 기본이다. 이 volume은
canonical 보존소가 아니므로 backup 대상에서 제외하고 restore 시 비운다.

관리자 사용자 mutation JSON은 API 앞단에서 `USER_LIFECYCLE_JSON_MAX_BYTES` 기본 16 KiB로
제한한다. Browser BFF는 필요한 create/access/reset payload만 허용하므로 더 엄격한 4 KiB다.
비밀번호는 환경 파일이나 명령행이 아니라 HTTPS body로만 전달하고 response/audit/idempotency
ledger에는 보존하지 않는다.

Worker status/log/metric raw JSON은 API 앞단에서
`WORKER_TELEMETRY_JSON_MAX_BYTES=2097152` 기본 2 MiB로 제한한다. Declared Content-Length와
chunked 실제 byte를 모두 검사하고 strict UTF-8 JSON의 NaN/Infinity를 Pydantic/auth 처리 전에
거부한다. Reverse proxy limit을 더 크게 설정해도 이 application 상한은 유지한다. Worker의
sanitized 단일 log는 16 KiB, local spool record는 기본 2 MiB이므로 API 상한을 임의로 늘리기 전에
세 경계와 memory/DoS 영향을 함께 검토한다.

Live telemetry는 stdout/stderr, 증가분 `train.log`와 TensorBoard scalar를 Manager I/O 전에 local
spool에 durable 저장한다. Active ingest는 Job write fence를 잡고 terminal/cancel에 지면 retryable
`503`을 반환한다. Terminal status가 저장한 log/metric count는 exclusive upper watermark이며
exact Worker/lease/Job/attempt의 상한 미만 late batch만 수용한다. Idempotency key는 canonical
payload fingerprint와 결박되므로 같은 key의 다른 payload를 replay로 처리하지 않는다.

Active Job system metric은 session 시작 뒤 fresh GPU/disk observation을 즉시 저장하고
`SYSTEM_TELEMETRY_INTERVAL_SECONDS` 기본 60초(허용 10~3,600초)마다 반복한다. GPU query가 성공한
empty inventory와 실행/semantic 실패를 `system.gpu.telemetry_available` 1/0으로 구분한다. Spool
저장 실패는 typed `telemetry_persistence_failed`로 Job을 중단하며 producer seal 뒤 final flush가
실패하면 watermark 미만 pending을 late replay용으로 보존한다.

Worker claim은 내부 object URI 대신 canonical `prepared_flat.zip`의 size/SHA-256과
Manager 상대 download path를 제공한다. Local backend는 lease-bound bounded stream,
S3/MinIO는 `DATASET_DOWNLOAD_TTL_SECONDS`(기본 60초) presigned GET으로 307한다. 원격
Worker는 Manager endpoint와 object endpoint 모두에 접근해야 하며 production Manager가
HTTPS일 때 object endpoint도 HTTPS여야 한다. Worker 기본 방어 상한은 archive 5 GiB,
10,000 file, file/total 2/20 GiB이며 installer의 `worker.env`에서 더 낮출 수 있다.

## Worker 설치

Worker에는 호환 NVIDIA driver, Docker Engine, Compose plugin, NVIDIA Container Toolkit이 먼저
있어야 한다. driver를 설치기가 임의로 변경하지 않는다. **현재 dev.20 Worker는 runtime이 없는 partial
bundle이므로 아래 구성 시험만 가능하다.**

```bash
sudo ./install.sh \
  --manager-url https://manager.example.com \
  --worker-name gpu-01 \
  --token-file /root/worker-bootstrap-token \
  --runner-mode fake \
  --allow-fake-dev \
  --no-start
```

GPU 없는 일회용 구성 시험에서만 `--skip-gpu-check`를 추가한다. 이 service는 시작하지 않는다.
production Manager는 Fake Worker를 거부하므로 dev.20 Worker를 설치형 Manager에 등록하거나 학습에
사용할 수 없다. `--runner-mode native --allow-unverified-gpu-runtime`도 runtime 누락을 우회하지
않으며 설치기는 이를 의도적으로 거부한다.

기본 위치:

- application release: `/opt/rvc-orchestrator/worker/releases/<version>`
- config/token/profile: `/etc/rvc-orchestrator/worker`
- optional custom CA: `/etc/rvc-orchestrator/worker/ca/custom-ca.pem`
- job/credential data: `/var/lib/rvc-orchestrator/worker`
- systemd: `rvc-orchestrator-worker.service`

dev.17에서 도입되어 현재 dev.20 Worker bundle에도 포함된 사설 CA 경계는 source 파일을 root 소유
regular non-symlink, mode `0444` 또는 `0644`, 1 byte 이상 1 MiB 이하의 ASCII certificate PEM으로 준비하고
설치 명령에 다음 option을 추가한다. Private key, NUL, 잘못된 PEM과 다른 owner/mode는 거부된다.

```bash
sudo ./install.sh \
  --manager-url https://manager.example.com \
  --worker-name gpu-01 \
  --token-file /root/worker-bootstrap-token \
  --ca-bundle-file /root/rvc-worker-custom-ca.pem \
  --runner-mode native \
  --allow-unverified-gpu-runtime \
  --no-start
```

설치기는 byte를 `/etc/rvc-orchestrator/worker/ca/custom-ca.pem` mode `0444`로 원자 게시하고,
host CA directory를 container `/etc/rvc-worker/ca:ro`에 mount한다. Worker environment에는 fixed
container path `/etc/rvc-worker/ca/custom-ca.pem`만 기록한다. Public CA만 사용하는 설치는 option을
생략하며 환경 경로도 빈 값이어야 한다. Host trust store에 CA를 추가한 것만으로 container 검증을
대신하거나 `verify=false`, `curl -k`, image CA store 수동 변경으로 우회하지 않는다.

Worker는 system default trust에 custom CA를 추가한 하나의 strict SSL context를 Manager와 external
Dataset/TestSet/Artifact object request에 함께 사용한다. Hostname 검증과 `CERT_REQUIRED`, TLS 1.2
이상, environment proxy 비사용은 custom CA 여부와 관계없이 유지된다. CA는 trust anchor일 뿐
transport encryption을 만들지 않으므로 production Manager/Object URL은 계속 HTTPS여야 한다.

기본 선택값은 기존 `profile` mode이지만 첫 설치에는 `--profile-file`이 필요하고 generic Agent
image에는 `/opt/rvc-webui`가 없다. 따라서 reviewed repository와 profile을 포함한 custom runtime
image 없이는 profile도 실행할 수 없다. 검증된 offline runtime bundle의 typed adapter를 사용할 때만
`--runner-mode native`를 선택한다. bundle manifest의 `RVC_RUNTIME_INCLUDED`와
`RVC_NATIVE_RUNNER_AVAILABLE`가 모두 true가 아니거나 reviewed commit/runtime manifest가
빠졌으면 설치기가 거부한다. 현재 `RVC_GPU_SMOKE_VERIFIED=false`이므로 위 확인 옵션 없이는
설치기와 runtime entrypoint 모두 시작을 거부한다. runtime 포함 self-contained 시험 번들이
준비된 뒤에만 다음 native 명령을 사용한다.

```bash
sudo ./install.sh \
  --manager-url https://manager.example.com \
  --worker-name gpu-01 \
  --token-file /root/worker-bootstrap-token \
  --runner-mode native \
  --allow-unverified-gpu-runtime
```

Fake는 source/development에서만 `--runner-mode fake --allow-fake-dev`로 명시한다.

### 실제 RVC runtime image 기반

`apps/worker/Dockerfile`은 계속 Agent/Fake 검증용이다. 실제 학습 후보는 별도
`apps/worker/Dockerfile.rvc`와 `infra/worker/runtime`의 offline builder를 사용한다.
builder는 움직이는 branch, VCS requirement, online pip/apt, build 중 model download를
허용하지 않는다. operator가 다음을 미리 제공해야 한다.

- 공식 RVC commit `7ef19867780cf703841ebafb565a4e47d1ea86ff` source archive와
  SHA-256/size/source/MIT license manifest
- Python 3.11 linux/amd64용 완전 고정 `requirements.lock`, wheelhouse와 각 wheel의
  SHA-256/size/license/source 및 fairseq source commit
- v1/v2 40k/48k F0/non-F0 weight, HuBERT, RMVPE, mute fixture,
  `runtime/crepe/full.pth`, FFmpeg/FFprobe와 파일별 SHA-256/size/license/source manifest
- 로컬에 미리 load한
  `pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:<reviewed-amd64-digest>`

upstream `pyproject.toml`의 Torch 2.4/Torchvision 0.19/Torchaudio 2.4 값은 reviewed source
archive marker로 계속 검사하지만 image dependency로 설치하지 않는다. 별도 release 후보 lock은
Torch `2.6.0+cu124`, Torchvision `0.21.0+cu124`, Torchaudio `2.6.0+cu124`, CUDA runtime
12.4다. 위 `<reviewed-amd64-digest>`가 아직 선택·검증되지 않았으므로 이 조합은 release가 아니다.

검증만 수행하는 명령은 GPU나 Docker가 필요하지 않다.

```bash
infra/worker/runtime/build-runtime-image.sh \
  --source-archive /offline/source/rvc-source.tar.gz \
  --source-manifest /offline/source/source-manifest.json \
  --wheelhouse /offline/wheelhouse \
  --assets /offline/assets \
  --verify-only
```

실제 build는 digest-pinned base, image tag와 output build manifest를 추가하며 Docker
network와 pull을 비활성화한다. image build에서는 CPU 수준 import/asset preflight를,
container 시작 시에는 CUDA GPU가 필수인 runtime preflight를 실행한다. 이 구조가 있다는
사실은 GPU 학습 완료를 뜻하지 않는다. v1/v2 × 40k/48k × F0/non-F0 one-epoch,
RMVPE/multi-GPU, artifact 의미와 clean-VM offline rebuild는 여전히 release gate다.
Self-contained Worker bundle의 infra/installer/verifier/document/SBOM 입력도 clean commit의 exact
Git archive에서만 stage한다. Runtime build manifest는 qualification 전에도 exact schema와
image/release/orchestrator identity를 검증하며 disabled activation을 mode `0444`로 고정한다.
Buildx runtime build는 default provenance descriptor를 섞지 않도록 `--provenance=false`를 사용하되,
별도 SBOM·scan·source attestation gate는 계속 요구한다.
Worker의 `native` mode는 reviewed source를 job-local로 투영해
preprocess/extract/train/index/export를 수행한다. 생성 시와 claim 직전에 source commit과
`assets-manifest.json`의 모든 byte/hash를 검증하고, claim GPU/RMVPE ID를 현재 visible GPU와
대조한다. `auto_inference_samples.enabled=false`인 Job은 artifact manifest까지 실행하지만
sample-enabled 구성요소도 lease-bound TestSet transfer, pinned PM/Harvest/CREPE/RMVPE inference,
canonical Artifact publication과 Manager Sample 등록/completion 경계를 fixture로 검증한다.
CREPE는 strict asset/private projection의 고정 `runtime/crepe/full.pth`를 같은 FD byte로
재검증하고 `torchcrepe.Crepe("full")`에 `weights_only=True` strict state dict를 pre-bind한다.
같은-attempt small model도 `weights_only=True`, manifest-verified HuBERT/RMVPE operator byte는
명시적 `weights_only=False`로 분리한다. Production `create_runner()`는 release-owned activation이
fully qualified일 때만 Sample dependency를 주입하고 실제 binding evidence가 있을 때만 capability를
광고한다. Activation은 `docs/RUNTIME_QUALIFICATION.md`의 exact 49-case report archive와 runtime
image/build/asset identity에서 builder가 생성하며 env/YAML/CLI로 직접 선택할 수 없다. 현재는 실제
amd64 base digest, v1/v2 GPU/no-network matrix,
vulnerability/container/license 검토가 남아 있으므로 Manager의
`AUTO_SAMPLE_JOBS_ENABLED=false`, Worker의 빈 inference capability와
`fixed_test_set_inference_ready=false`, 후보 image/build/bundle의
`RVC_GPU_SMOKE_VERIFIED=false`, `PROFILE_STAGE_SET_VERIFIED=false`,
`RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false`를 명시한다.

Release installer 후보는 두 단계 factory로 만든다. 1단계는 exact runtime image를 만들고 disabled
activation core archive만 게시한다.

```bash
export RELEASE_VERSION=0.1.0-rc.1
export CORE_OUTPUT_DIR=/offline/candidates/core
export QUALIFIED_OUTPUT_DIR=/offline/candidates/qualified
install -d -m 0700 /offline/candidates
test ! -e "$CORE_OUTPUT_DIR" && test ! -e "$QUALIFIED_OUTPUT_DIR"

installers/worker/build-self-contained-release.sh \
  --version "$RELEASE_VERSION" \
  --source-archive /offline/source/rvc-source.tar.gz \
  --source-manifest /offline/source/source-manifest.json \
  --wheelhouse /offline/wheelhouse \
  --wheelhouse-manifest /offline/wheelhouse/wheelhouse-manifest.json \
  --assets /offline/assets \
  --asset-manifest /offline/assets/assets-manifest.json \
  --base-image 'pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:<reviewed-amd64-digest>' \
  --output-dir "$CORE_OUTPUT_DIR"
```

Core archive는 GPU qualification용 engineering 입력이며 public release가 아니다. 외부 checksum,
내부 exact ledger와 bundle closure를 검증해 별도 private directory에 추출한다. 생성 직후 Docker
image ID와 extracted `images-manifest.json`의 단일 runtime `image_id`를 비교하고 그 값을 qualification
handoff로 고정한다.

```bash
export CORE_BUNDLE=/offline/candidates/core-extracted/rvc-worker-$RELEASE_VERSION-linux-amd64
export CORE_BUILD_MANIFEST="$CORE_BUNDLE/runtime/build-manifest.env"
CORE_IMAGE_ID=$(docker image inspect --format '{{.Id}}' \
  "rvc-orchestrator-worker:$RELEASE_VERSION")
export CORE_IMAGE_ID
CORE_MANIFEST_IMAGE_ID=$(python3 -c '
import json, pathlib, sys
data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
images = data["images"]
assert len(images) == 1 and images[0]["role"] == "runtime"
print(images[0]["image_id"])
' "$CORE_BUNDLE/images-manifest.json")
test "$CORE_IMAGE_ID" = "$CORE_MANIFEST_IMAGE_ID"
```

49-case GPU/no-network matrix와 reviewer evidence는 반드시 이 `CORE_IMAGE_ID`를 대상으로 한다. 모든
case와 release review가 끝난 뒤에만 2단계 qualified factory를 실행한다.

```bash
installers/worker/build-qualified-release.sh \
  --version "$RELEASE_VERSION" \
  --runtime-image-id "$CORE_IMAGE_ID" \
  --runtime-build-manifest "$CORE_BUILD_MANIFEST" \
  --assets /offline/assets \
  --asset-manifest /offline/assets/assets-manifest.json \
  --qualification /offline/review/runtime-qualification.json \
  --qualification-evidence /offline/review/runtime-evidence.tar.gz \
  --output-dir "$QUALIFIED_OUTPUT_DIR"
```

2단계는 existing exact image와 build manifest/assets/qualification/evidence만 검증·재포장하며 image를
build/pull/retag/remove하지 않는다. Core와 qualified archive basename은 같으므로 output directory를
반드시 분리하고 기존 archive/sidecar를 덮어쓰지 않는다. Builder는 입력 activation boolean을 받지
않고 검증된 증적에서 mode `0444` projection을 직접 만든다. 설치/start 시 exact image ID,
asset/qualification/evidence hash와 고정 read-only mount를 다시 검증한다. 증적이 없으면 qualified
factory를 실행하지 않고 core의 disabled projection과 세 false gate를 유지한다.

Qualified archive도 vulnerability/container/secret scan, SBOM·license/redistribution 검토, 별도
release reviewer attestation, clean Ubuntu 설치·재부팅·upgrade와 실제 외부 Manager/Object TLS/browser
검증 전에는 production release가 아니다. 현재 실제 49-case/release 증적이 없으므로 모든 runtime
gate는 false이며 qualified factory를 실행할 수 있는 승인 상태가 아니다.

## Manager backup과 restore

설치 후 다음 command가 `/opt/rvc-orchestrator/manager/bin`에 배치된다. backup은
Manager PostgreSQL과 MLflow PostgreSQL을 각각 custom-format dump로 만들고, Manager와
MLflow MinIO bucket의 object byte, Content-Type/header, user metadata와 tag inventory를
snapshot한다. bucket versioning이 enabled/suspended였거나 version history가 있으면 같은
version ID를 재현할 수 없으므로 fail-closed한다.

```bash
sudo /opt/rvc-orchestrator/manager/bin/backup
# 다른 filesystem을 사용할 때
sudo /opt/rvc-orchestrator/manager/bin/backup \
  --destination /srv/rvc-backups
```

기본 위치는 `/var/backups/rvc-orchestrator/manager`다. 성공한 backup은
`rvc-manager-backup-<version>-<UTC timestamp>/` directory로 한 번에 publish되며 그
안에 `tar.gz`와 외부 `.sha256`이 있다. archive 내부에는 component manifest, 모든
component file의 `SHA256SUMS`, service/version/Alembic schema metadata가 들어간다.
중간 실패는 최종 이름으로 publish하지 않고 기존 backup을 덮어쓰지 않는다. backup
directory는 0700, archive/dump/manifest/checksum은 0600이다. `manager.env`, password,
JWT/Worker token 등 config와 secret은 포함하지 않으므로 별도의 보호된 config backup
정책이 필요하다. 네 runtime secret volume은 source config/secret에서 다시 만드는 민감한
derived data이므로 backup archive에 넣지 않는다. 일반 uninstall은 이를 포함한 volume을
보존하지만 source secret을 권위 원본으로 관리한다. PostgreSQL과 object store를 가로지르는 업무 단위의 완전한 시점
일관성을 위해 기본 backup은 proxy/Web/API/MLflow writer를 중지하고, pending/finalizing
Dataset·artifact upload session이 하나라도 있으면 거부한다. 완료 또는 만료 정리 후 다시
시도한다. 장애 조사 목적으로 writer를 멈출 수 없을 때만 `--online-inconsistent`를
명시할 수 있으며, 이 archive는 restore 때도 `--allow-online-inconsistent-backup` 없이는
거부된다.

restore는 database row와 대상 두 bucket의 내용을 교체하는 파괴적 작업이므로 정확한
확인 flag가 없으면 시작하지 않는다.

```bash
sudo /opt/rvc-orchestrator/manager/bin/restore \
  --backup /var/backups/rvc-orchestrator/manager/rvc-manager-backup-1.2.3-20260711T030303Z \
  --confirm-destructive-restore
```

restore는 archive/checksum을 symlink를 따라가지 않는 방식으로 private 0700 staging에
먼저 복사한 뒤 그 snapshot만 사용한다. 압축 archive byte, member 수, unpacked byte,
가용 disk/inode 한도를 검사하고 regular file/directory만 자체 추출한 다음 외부·내부
SHA-256, product/component/version/schema와 현재 database/bucket 이름을 검증한다.
기본은 현재 상태를 새 backup으로 만든 뒤 writer를 중지한다. Redis DB, artifact
verification spool과 `/var/lib/rvc-dataset-ingestion` 작업 내용은 과거 PostgreSQL과 미래
transient state가 섞이지 않도록 비운다. 두 PostgreSQL database를 drop/create한 빈
상태에 dump를 복원하므로 backup 뒤 추가된 table/schema도 남지 않는다. object restore는
추가 key를 제거하고 byte/header/user metadata/tag를 다시 읽어 비교한다. 이어 backup
manifest와 설치 release의 전체 Alembic revision/head set을 대조하고 `upgrade heads`를
실행한다. 마지막에는 healthcheck가 있는 PostgreSQL, Redis, MinIO, MLflow, API, Web, proxy가
running/healthy이고, healthcheck가 없는 `rq-worker`는 running이며 `/readyz`의 `rq_worker=ok`여야
완료된다. source version이 다르면 검토 후
`--allow-version-mismatch`가 필요하다. 사전 backup을 만들 수 없는 비상 상황에서만
`--skip-pre-restore-backup`을 명시한다.

복원 도중 실패하면 write service는 중지된 채 유지되고 출력에 사전 backup을 이용한
recovery command가 표시된다. 원인을 확인하기 전에 volume을 삭제하거나 전체 MinIO를
초기화하지 않는다. 복구 후에는 job/artifact 참조와 운영 monitoring을 별도로 확인한다.

실제 PostgreSQL/MinIO volume을 사용하는 opt-in 복구 drill은 다음과 같이 실행한다.
일반 `make check`에는 Docker 의존성을 넣지 않는다.

```bash
make test-manager-recovery-docker
```

Docker daemon과 Compose v2 또는 `docker-compose`가 필요하다. 로컬에 없으면
`postgres:16-alpine`, `redis:7.4-alpine`, 고정 MinIO/MinIO Client image와
`python:3.11-slim-bookworm`을 가져올 수 있어야 한다. metadata recovery용 실제 API
dependency image가 없으면 drill이 `apps/api/Dockerfile`로 먼저 build한다. drill은 고유한
`rvc-recovery-drill-<timestamp>-<pid>` Compose project와 PostgreSQL, MinIO, Redis,
artifact spool, Dataset ingestion 작업 volume만 만든다. host port는 열지 않는다.
종료·실패 시에도 그 project에 대해서만 `down --volumes`를 실행한다.

drill은 두 database와 두 bucket에 표식 데이터를 넣고 backup한 뒤 row를 변조·삭제하고
후발 table을 추가하며 object byte/metadata/tag와 Redis/spool/ingestion 작업 상태를
바꾼다. restore 후 원래 row/object metadata가 돌아오고 후발 table/object 및 transient
state가 제거되었는지 확인하며, archive에 config/secret path나 값이 없는지도 검사한다.
PostgreSQL, MinIO, Redis는 실제 image/volume을 사용한다. Alembic과 제품 HTTP service는
격리 fixture이므로 실제 migration을 포함한 clean-VM release drill을 대체하지 않는다.

## 업그레이드, Manager rollback과 제거

- dev.20 upgrade는 새 versioned release와 pending environment로 target Compose를 먼저 렌더링한
  뒤에만 기존 service를 stop하고 env/`current`를 전환한다. Activation 전 오류는 기존 env/current를
  보존하고, strict SemVer상 같은/낮은 version은 거부한다. Target start 실패 뒤에는 database
  migration 역행을 피하기 위해 target pointer를 일관되게 유지한 down 상태로 종료한다.
- dev.14 이하 root-level installer/upgrade script에는 ledger 누락·downgrade guard가 없으므로
  실행하지 않는다. 신규 Upgrade는 target dev.20 bundle의 `upgrade.sh`, 낮은 Manager version 전환은
  installed guarded rollback만 사용한다. dev.17 archive는 custom CA/Experiment 비교의 역사 증거일
  뿐 model registry/maintenance migration과 source를 포함하지 않으므로 dev.20 설치 대체물로 사용하지 않는다.
- `f5d1c8a9b240` 적용 전에는 quiesced Manager backup과 기존 Alembic head
  `e4c7b9d2f610`을 확인한다. 새 migration은 maintenance parent-lock 함수를 추가하고, 이전
  `e4c7b9d2f610` migration은 model registry 원장과 future Worker claim provenance를
  추가하지만 historical JobAttempt의 NULL provenance를 승인 값으로 backfill하지 않는다. 따라서
  과거/Fake attempt가 candidate가 되지 않는 것이 의도된 fail-closed 동작이다. dev.17 application은
  새 registry API/원장을 이해하지 못하므로 compatibility marker를 강제로 맞춰 자동 rollback하지
  않는다.
- `9d2f4b7c8e10`을 처음 적용하기 전에는 Dataset/Artifact `pending|finalizing` upload를 0으로
  drain하고 backup한다. migration은 historical session을 `UNBOUND`로 표시하므로 terminal
  object는 현재 namespace에서 전체 size/SHA-256을 확인하는 operator adoption을 거쳐야 한다.
  active UNBOUND는 자동 adoption/cleanup하지 않으며 quota를 점유할 수 있다. 정확한 preview,
  apply와 rollback 판단은 `OPERATIONS_GUIDE.md`의 storage namespace runbook을 따른다.
- `e2f8b4c6a930` 적용 전에도 모든 구 API replica와 Dataset upload client를 중지하고 active
  `pending|finalizing`을 0으로 drain한다. migration은 남아 있는 구 active row를 의도적으로
  `expired`, `upload_fencing_upgrade_required`로 닫고 completed legacy row만 보존한다. 동일
  idempotency payload를 다시 init해 generation+1/session-scoped key를 받은 뒤 진행한다. 구
  process가 migration 뒤 계속 old staging/canonical을 쓰는 상태는 지원하지 않는다.
- `b4a91d7e2c63` 적용은 기존 User를 보존하지만 JWT에 필수 token-version claim을 도입한다.
  이전 release에서 발급한 browser/API access token은 upgrade 뒤 모두 401이므로 maintenance
  공지와 재로그인 절차를 준비한다. Role/active/password 변경 뒤에는 같은 계정의 과거 token이
  재활성화되지 않는다. `dev.8` 코드로의 rollback은 새 user lifecycle API/claim을 이해하지
  못하므로 자동 schema-compatible rollback으로 취급하지 않는다.
- `c7b1e4d9a260`/`ca8d3e7f4b10` telemetry migration 전에는 active Job을 drain하고 가능한
  Worker pending spool을 전송한 뒤 backup한다. 기존 ingest row의 NULL fingerprint와 historical
  terminal attempt의 NULL watermark는 추정 backfill하지 않는다. 따라서 과거 idempotency key replay와
  watermark 없는 terminal의 late batch는 fail-closed한다. Manager migration/readiness를 확인한 뒤
  같은 dev.10 Worker를 재시작하고, 구 Worker가 count 없이 terminal 처리한 attempt의 pending
  telemetry를 자동 복구됐다고 간주하지 않는다.
- Manager rollback은 설치된 release manifest와 mode `0444` `RELEASE_SHA256SUMS`의 exact
  inventory를 검증한다. image manifest v2가
  있으면 대상 env와 loaded image/config digest도 먼저 확인하고, 그 뒤에만 `current` symlink와
  release-owned env를 원자적으로 바꿔 시작/readiness를 검사한다. 실패하면 직전 release의
  symlink/env와 verified image identity로 복구한다.
- Manager upgrade/rollback은 `ORCHESTRATOR_VERSION`, `API_IMAGE`, `WEB_IMAGE`,
  `MLFLOW_IMAGE`, `POSTGRES_IMAGE`, `REDIS_IMAGE`, `MINIO_IMAGE`, `MINIO_CLIENT_IMAGE`,
  `NGINX_IMAGE`, `RVC_IMAGE_PULL_POLICY`를 대상 manifest 값으로 원자 갱신한다. Worker upgrade는
  `ORCHESTRATOR_VERSION`, `WORKER_IMAGE`, `RVC_IMAGE_PULL_POLICY`와 GPU/stage provenance gate를
  새 bundle 값으로 원자 갱신한다. self-contained dependency alias도 release version에 결박돼야
  한다. 환경 전체를 그대로 보존하는 것이 아니며 release 소유 key는 반드시 새 version을
  가리킨다. 그 밖의 사용자 env, native acknowledgement, secret, Worker token, profile,
  credential과 데이터는 덮어쓰지 않는다. duplicate manifest/env key와 symlink `worker.env`는
  갱신 전에 거부한다.
- Worker는 upgrade를 제공하지만 자동 rollback script는 없다. 이전 Worker bundle로 돌아가려면
  별도 변경 절차와 runtime/data 호환 검토가 필요하다.
- dev.20 Worker upgrade에서 `--ca-bundle-file`을 생략하면 설치된 custom CA와
  활성 fixed path를 보존한다. 새 파일을 전달하면 검증된 pending byte와 target Compose를 먼저
  준비하고 전환하므로, 잘못된 PEM·owner·mode나 activation 실패는 기존 CA byte/environment/current를
  보존해야 한다. CA 제거를 env 편집이나 파일 삭제로 수행하는 절차는 제공하지 않으며 별도 승인된
  configuration migration 없이는 둘 중 하나만 바꾸지 않는다.
- uninstall은 설치된 `bin`이 아니라 압축 해제한 해당 bundle의 `sudo ./uninstall.sh`로 실행한다.
  기본적으로 서비스를 중지·비활성화할 뿐 config, secret, release와 volume을 삭제하지 않는다.
  dev.20은 systemd disable 또는 Compose down 일부 실패를 nonzero로 전파하고 성공 문구를 내지
  않는다. Exit 0 뒤에도 실제 systemd/Compose 상태를 별도로 확인한다. Worker config 보존에는
  `/etc/rvc-orchestrator/worker/ca/custom-ca.pem`도 포함된다.
- 데이터 영구 삭제는 현재 자동 제공하지 않으며 backup/retention 정책과 별도 명시적 절차가 필요하다.

```bash
sudo /opt/rvc-orchestrator/manager/bin/rollback --to-version 1.2.2
```

현재 release와 대상 release의 non-`unknown` `SCHEMA_COMPATIBILITY` marker가 같아야 자동
rollback한다. rollback 전에 실제 database revision set과 현재/대상 image의 모든
Alembic head도 조회한다. marker 또는 revision set이 다르면 database 호환성을 별도로
확인한 뒤 `--allow-schema-mismatch`와 `--confirm-schema-mismatch-risk` 값
`I_UNDERSTAND_NO_DATABASE_DOWNGRADE`를 함께 명시해야 한다. script는 먼저 mandatory
backup을 publish한다. rollback은 어떤 경우에도 Alembic downgrade나 database restore를
자동 실행하지 않는다. stop 실패, signal, 대상 readiness 실패 모두 EXIT recovery가
symlink/environment를 직전 release로 되돌리고 직전 service를 다시 시작한다.

## 현재 제한

- 개발 bundle은 partial CycloneDX inventory와 license declaration을 포함하지만 서명,
  vulnerability/container/secret scan, 법적 license 검토와 완전한 runtime SBOM은 없다.
- offline RVC/CUDA/PyTorch packaging 기반은 있으나 검증된 base digest, 전체 wheel/asset
  license manifest와 실제 GPU smoke를 통과한 release image는 아직 포함하지 않는다.
- Worker는 검증형 비동기 object-storage publisher와 guarded `native` typed runner,
  PM/Harvest/CREPE/RMVPE Sample 원장 흐름과 CREPE manifest-pinned safe loader를 제공하지만,
  실제 2.6.0/cu124 GPU/no-network end-to-end 검증이 없어 real-runtime Job 완료를 아직
  release로 인증하지 않는다.
- access JWT login/logout, owner RBAC, 관리자 lifecycle API/UI와 token-version 폐기는 구현됐지만
  refresh-token/session rotation, MFA/SSO와 실제 browser lifecycle E2E는 아직 없다.
- TLS 예제는 제공하지만 인증서와 domain을 운영자가 설정해야 한다. Dev.11은 operator-owned
  `PUBLIC_SCHEME`로 client forwarding header를 대체하고 production HTTPS/Secure cookie/HSTS를
  강제한다. 실제 외부 TLS 종단과 clean browser 검증은 여전히 release gate다.
- dev.17에서 도입되고 dev.20에도 유지된 Worker fixed read-only custom CA mount와 Manager/object
  공통 strict SSL context는 clean Ubuntu Worker에서 실제 사설 CA Manager/Object hostname과 전송을
  확인하는 인수 시험이 아직 release gate다. dev.16은 이 custom CA 경계를 포함하지 않은 과거
  partial이다.
- recovery object snapshot은 versioning-enabled bucket을 지원하지 않는다. self-contained
  image closure v2는 archive와 loaded identity를 강제하고 dev.20 Manager에 exact 8개 image가
  포함됐지만, 실제 clean-VM archive/load/start/rollback lifecycle은 release gate다. dev.20 Worker에는
  image/runtime이 없다.
- Model registry API/BFF/UI와 자동 회귀는 구현됐지만 현재 partial bundle에는 승인된 production
  runtime digest pair가 없다. 실제 S3/MinIO 대용량 canonical 재해시·tamper/outage, PostgreSQL
  다중 replica promotion 경쟁과 browser/API response-loss 인수 전에는 registry를 production 모델
  승인 증거로 사용하지 않는다.
- Manager 전체 장애 중 terminal status가 커밋되기 전에 lease가 회수되면 server watermark가 없어
  old attempt의 local pending telemetry를 자동 수용하지 않는다. Pending/dead-letter와 원장
  status/lease event를 보존해 operator reconcile 대상으로 두며 새 attempt와 섞지 않는다.
- clean Ubuntu Manager VM의 reboot/upgrade/rollback/restore와 NVIDIA Worker VM의
  install/upgrade/uninstall smoke test가 남았다.

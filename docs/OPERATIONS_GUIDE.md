# 설치·운영·사용 가이드

이 문서는 현재 개발 기준선에서 Manager 운영자, GPU Worker 운영자와 일반 사용자가
실제로 따라야 할 절차를 한곳에 정리한다. 실행형 설치 순서와 현재 Manager self-contained/Worker
partial 경계는
[`INSTALLATION_GUIDE.md`](INSTALLATION_GUIDE.md), 설치 bundle의 상세 옵션과 복구 알고리즘은
[`DEPLOYMENT.md`](DEPLOYMENT.md), 사용자 시험 판정은 [`TEST_GUIDE.md`](TEST_GUIDE.md), 자동화
범위는 [`TESTING.md`](TESTING.md)를 함께 본다.
현재 산출물은 개발 후보이며 `CHECKLIST.md`의 clean-VM/GPU 출시 gate를 통과하기 전에는
운영 v1.0으로 배포하지 않는다.

dev.12는 operator-owned `PUBLIC_SCHEME=https`를 Nginx/API/Web의 단일 browser-facing scheme으로
사용하고 client forwarding scheme을 무시한다. Production start는 다른 값을 거부한다. 그래도
외부 TLS 종단·인증서·Host와 실제 Secure cookie/HSTS는 clean browser에서 확인해야 한다.
dev.13은 역사적으로 이 경계를 보존하며 Dataset integrated LUFS와 strict release
file/environment closure를 추가했다. dev.14는 MinIO·MLflow loopback port용 `host-access`
bridge, bundled proxy foreground command/fallback과 실제 Manager 전체 Compose smoke를 더했고,
dev.16은 dev.15의 source/image-user closure와 checksum·forward-upgrade·uninstall failure
gate를 유지하고 physical installed-release 검증 runbook과 결과 템플릿을 보강했다. dev.17은
Experiment 비교 BFF/UI source, Worker custom CA strict projection/context, bundle-local native
negative runbook과 audited 설치·시험 가이드를 추가했다. dev.18은 model registry의
candidate/approval/revoke 원장, active champion 0/1, rollback promotion과 same-origin BFF/UI를
추가했고, dev.19은 maintenance DB/Redis/S3 최소권한과 long-operation heartbeat를 더했다.
현재 dev.20 Manager schema head는 `f5d1c8a9b240`이다. 두 archive는 source commit
`298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a`에 결박된다. Manager archive는
`667617422` byte, SHA-256
`c6488dad47c7f38c082ed6fa68f1fe3691c069110aef0bbf68a9d7ba5e6f5b70`이고 정확히 8개의
`linux/amd64` image를 포함한 `SELF_CONTAINED=true` 후보다. Worker archive는 `108488` byte,
SHA-256 `7f36cbf27100bf70425c2780142d4fa3f6e6e76d0acf410d3e3fb698aa50558b`이며
`SELF_CONTAINED=false`, 빈 image inventory이고 GPU/native/Sample gate가 닫혀 있다. 이후 source
HEAD가 달라져도 archive provenance는 이 committed snapshot으로 유지된다. dev.20 tag로 현재
checkout의 image를 다시 빌드하거나 archive와 섞지 않는다. Manager도
`SBOM_STATUS=partial-release-gates-open`이므로 기능 시험 가능 상태를 공급망 또는 production
승인으로 기록하지 않는다.
dev.17 archive는 custom CA와 Experiment 비교의 immutable 역사 증거일 뿐 dev.20 schema/source를
포함하지 않으므로 신규 설치·업그레이드 기준으로 쓰지 않는다.

## 역할과 네트워크 경계

- Manager host는 Web/API만 TLS reverse proxy로 공개한다. PostgreSQL, Redis와 내부
  MinIO/MLflow port는 인터넷에 공개하지 않는다.
- PostgreSQL·Redis와 application backend는 `internal: true` network에만 유지한다. Host port가
  필요한 MinIO·MLflow만 별도 `host-access` bridge를 함께 사용하되 기본 publish address
  `127.0.0.1`을 유지한다. 이 bridge 이름을 공인 interface 공개 허가로 해석하지 않는다.
- 원격 Worker는 Manager의 HTTPS URL과 presigned object HTTPS endpoint에 접근할 수
  있어야 한다. Manager에서 Worker로 들어오는 연결은 필요하지 않다.
- Worker bearer와 lease/attempt header는 Manager endpoint에만 보낸다. Dataset/TestSet의
  외부 307 object 요청은 fresh client로 열려 Manager response cookie와 environment proxy
  credential도 전달하지 않으므로 object endpoint가 별도 Worker 인증을 요구하게 구성하지 않는다.
- Manager 운영자는 secret, backup, upgrade와 Worker 등록 bootstrap 값을 관리한다.
- Worker 운영자는 NVIDIA driver/Container Toolkit, reviewed offline runtime/profile과
  local disk를 관리한다. Worker에 Docker socket 또는 privileged 권한을 주지 않는다.
- 일반 사용자는 자신의 Dataset/Experiment/Job만 다룬다. Worker와 사용자 목록은 admin 전용이다.

## Manager 신규 설치 runbook

지원 기준은 Ubuntu 22.04/24.04 x86_64, Docker Engine과 Compose v2, 최소 20 GiB 여유
공간이다. 모델·Dataset·backup 규모에 맞춰 별도 용량 계획을 세운다.

1. 별도 신뢰 채널로 받은 archive의 외부 checksum을 먼저 검사한다.

   ```bash
   archive=rvc-manager-0.1.0-dev.20-linux-amd64.tar.gz
   extract_root="$PWD/rvc-manager-0.1.0-dev.20-verified"
   test "$(stat -c '%s' "$archive")" = 667617422
   test "$(sha256sum "$archive" | awk '{print $1}')" = \
     c6488dad47c7f38c082ed6fa68f1fe3691c069110aef0bbf68a9d7ba5e6f5b70
   sha256sum --check "$archive.sha256"
   test ! -e "$extract_root"
   install -d -m 0700 "$extract_root"
   tar -xzf "$archive" -C "$extract_root"
   cd "$extract_root/rvc-manager-0.1.0-dev.20-linux-amd64"
   sha256sum --check SHA256SUMS
   python3 common/image_bundle.py verify-ledger --root . --ledger-name SHA256SUMS
   python3 common/image_bundle.py verify-bundle \
     --root . \
     --component manager \
     --version 0.1.0-dev.20 \
     --source-commit 298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a
   ```

   마지막 명령은 checksum에 나열된 파일의 byte뿐 아니라 추가·누락·symlink/비정규 파일도
   거부한다. Bundle 안의 version-rendered `README.md`와 `TESTING.md`도 읽는다.

2. 포함된 exact 8-image archive를 load하고 identity를 확인한다. 이 단계는 API/Web/MLflow와
   PostgreSQL/Redis/MinIO/MinIO client/Nginx를 모두 검증한다. 별도 build, pull 또는 retag로
   실패를 우회하지 않는다.

   ```bash
   gzip -dc images/manager-images.tar.gz | sudo docker load
   sudo python3 common/image_bundle.py verify-loaded \
     --root . \
     --component manager \
     --version 0.1.0-dev.20 \
     --source-commit 298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a \
     --docker-command docker
   ```

   수동 load를 생략하면 installer가 같은 archive load와 identity 검증을 자동 수행한다. Dev.20
   Manager는 `RVC_IMAGE_PULL_POLICY=never`로 설치된다. TLS가 적용되고 원격 Worker가 접근 가능한
   S3/MinIO endpoint를 별도로 준비한다. 번들 기본 loopback HTTP endpoint는 production에서 사용할
   수 없다.

3. preflight 후 서비스를 시작하지 않고 설치한다.

   ```bash
   sudo ./preflight.sh
   sudo ./install.sh \
     --no-start \
     --public-scheme https \
     --s3-presign-endpoint-url https://objects.example.com
   sudo /opt/rvc-orchestrator/manager/bin/manager-compose config --quiet
   ```

4. `/etc/rvc-orchestrator/manager/manager.env`의 `PUBLIC_SERVER_NAME`, `PUBLIC_SCHEME=https`, `CORS_ORIGINS`,
   `HTTP_BIND_ADDRESS=127.0.0.1`, `S3_PRESIGN_ENDPOINT_URL`, `S3_VERIFY_TLS=true`,
   `WORKER_TELEMETRY_JSON_MAX_BYTES=2097152`를 실제 환경에
   맞춘다. Manager와 object endpoint의 TLS/DNS proxy를 연결한 뒤 구성 검증 후 시작한다.

   ```bash
   sudo /opt/rvc-orchestrator/manager/bin/manager-compose config --quiet
   sudo systemctl daemon-reload
   sudo systemctl enable rvc-orchestrator-manager.service
   sudo systemctl restart rvc-orchestrator-manager.service
   ```

5. liveness와 readiness를 구분해 검사한다.

   ```bash
   curl --fail https://manager.example.com/healthz
   curl --fail https://manager.example.com/readyz
   sudo /opt/rvc-orchestrator/manager/bin/manager-compose ps -a
   ```

   `/healthz`는 Nginx process만 확인한다. 트래픽 투입과 load-balancer health check에는
   PostgreSQL/Redis와 설정된 MLflow 장애 정책을 반영하는 `/readyz`를 사용한다.

   Arm64 Colima에서 dev.20 amd64 image를 emulation으로 실행한 release stack smoke는 PASS했다.
   이는 기능 시험이 가능한 증거지만 clean Ubuntu x86_64, 외부 TLS/browser, 재부팅·upgrade·restore,
   장시간 안정성 또는 production 인수 결과는 아니다. 해당 환경에서 직접 검증한 행만 PASS로
   기록한다.

6. 관리자 비밀번호를 mode `0600` regular file로 준비하고 service readiness 뒤에 bootstrap한다.

   ```bash
   sudo install -m 0600 /dev/null /root/rvc-admin-password
   sudoedit /root/rvc-admin-password
   sudo /opt/rvc-orchestrator/manager/bin/bootstrap-admin \
     --email admin@example.com \
     --password-file /root/rvc-admin-password
   ```

   `https://manager.example.com/login`에서 최초 admin으로 로그인한다.

설정과 secret은 `/etc/rvc-orchestrator/manager`, versioned release는
`/opt/rvc-orchestrator/manager/releases`, 영속 데이터는 Docker named volume에 있다.
설치된 각 release의 mode `0444` `RELEASE_SHA256SUMS`는 exact inventory 원장이며
`manager-compose up|start|restart|run|create`와 rollback이 실행 전에 다시 검증한다. 운영자가
checksum을 재작성하거나 mode를 완화해 실패를 우회하지 않는다.
`manager.env`와 `secrets/`를 ticket, 채팅, Git 또는 일반 로그에 붙이지 않는다.
Source secret은 root 소유 mode `0600`을 유지한다. API/RQ/MLflow가 이를 직접 읽는 구조가 아니며
`manager-compose up|start|restart|run|create`가 역할별 runtime secret generation을 원자 갱신한 뒤
명령을 실행한다. Secret을 회전했으면 source file을 안전하게 교체한 뒤 wrapper로 영향 service를
restart하고 `/readyz`를 확인한다. Projector 실패 때 file mode를 완화하거나 runtime volume을 직접
수정하지 말고 source의 regular/non-empty/size/NUL 조건과 owner/mode를 고친다. Derived runtime
volume은 backup 원본이 아니며 일반 uninstall에서는 보존된다.

Maintenance DB/Redis/S3 credential을 회전하면 대응 API credential과 다른 값인지 먼저 확인한다.
Installed wrapper의 `start|restart`는 migration→DB authz, Redis ACL, MinIO policy one-shot을 다시
통과시키기 위해 전체 stack을 `up --force-recreate`하며 service 이름 인자를 받지 않는다. 이는 짧은
전체 Manager 재생성 window를 만들므로 Job/upload를 drain하고 maintenance window에서 실행한다.
Raw `docker compose restart rq-worker`나 runtime volume 직접 수정으로 이 순서를 우회하지 않는다.

## GPU Worker 등록과 설치 runbook

Worker는 지원 Ubuntu x86_64, 호환 NVIDIA driver, Docker/Compose, NVIDIA Container
Toolkit과 최소 50 GiB 여유 공간이 필요하다. 실제 학습에는 검토한 offline RVC runtime
image와 모든 source/asset/wheel manifest가 포함된 Worker bundle을 사용한다.

현재 dev.20은 Compose가 기대하는 `rvc-orchestrator-worker:0.1.0-dev.20` image를 포함하지 않고
`RVC_RUNTIME_INCLUDED=false`, `RVC_NATIVE_RUNNER_AVAILABLE=false`인 partial bundle이라
실제 등록/학습에 사용할 수 없다. 설치기/구성만 확인할 때는
`--runner-mode fake --allow-fake-dev --no-start`를 사용하고 service를 시작하지 않는다. production
Manager는 Fake Worker를 거부한다. 아래 등록·token 운영 절차는 runtime 포함 self-contained Worker
bundle이 별도로 준비된 뒤에만 적용한다. dev.17에서 도입되어 dev.20에도 유지된 custom CA
projection은 clean Ubuntu의 실제
Manager/Object endpoint 연결 증거 전에는 production 사설 CA 인수를 완료로 판정하지 않는다.

향후 self-contained Worker release engineering은 2단계다. Core factory는 disabled activation을 가진
exact runtime image/archive만 만들고, 운영자는 그 image ID와 core archive
`images-manifest.json`의 runtime ID가 같은지 확인한 뒤 49-case를 수행한다. Qualified factory는 같은
ID를 `--runtime-image-id`로 받아 existing image/build manifest/assets/qualification/evidence를
재포장할 뿐 image를 build/retag/remove하지 않는다. Core와 qualified archive는 basename이 같으므로
별도 output directory에 보존하고 core를 덮어쓰지 않는다. Core는 public release가 아니며 qualified
archive도 scan·license·reviewer·clean-host·실제 외부 TLS/browser gate 전에는 production 운영에
투입하지 않는다. 현재 해당 실제 증적은 없어 모든 runtime gate는 false다.

1. Manager host의 `/etc/rvc-orchestrator/manager/secrets/worker_bootstrap_token`을
   운영 secret 전달 수단으로 Worker의 임시 mode `0600` 파일에 복사한다. 값을 terminal
   명령행, 메신저나 CI 로그에 출력하지 않는다.

   사설 CA를 쓰는 dev.20에서는 조직이 승인한 certificate chain만 별도
   `/root/rvc-worker-custom-ca.pem`으로 준비한다. Production source는 root 소유 regular
   non-symlink, mode `0444` 또는 `0644`, 1 byte 이상 1 MiB 이하의 ASCII certificate PEM이어야
   한다. Private key와 NUL은 넣지 않는다. CA는 secret은 아니지만 trust anchor이므로 bootstrap
   token과 섞거나 일반 사용자가 교체할 수 있는 경로에 두지 않는다.

2. bundle의 `manifest.env`에서 `RVC_RUNTIME_INCLUDED=true`,
   `RVC_NATIVE_RUNNER_AVAILABLE=true`, reviewed `RVC_SOURCE_COMMIT`을 확인한다. 기존
   command-profile 호환 모드를 쓸 때만 `infra/worker/rvc-profile.example.yaml`을 복사해
   commit과 허용 stage를 검토한다. generic Agent image에는 `/opt/rvc-webui`가 없으므로 profile도
   reviewed repository를 포함한 custom runtime image 없이는 실행할 수 없다.

3. bundle 외부/내부 checksum을 Manager와 같은 방식으로 검사하고 설치한다.

   ```bash
   ./preflight.sh
   sudo ./install.sh \
     --manager-url https://manager.example.com \
     --worker-name gpu-01 \
     --token-file /root/worker-bootstrap-token \
     --runner-mode native \
     --allow-unverified-gpu-runtime
   ```

   위 command는 runtime image의 default public CA trust를 쓴다. 사설 CA 환경이면 dev.20에서
   `--ca-bundle-file /root/rvc-worker-custom-ca.pem`을 같은 install command에
   추가한다. Installer는 검증된 byte를 release 밖
   `/etc/rvc-orchestrator/worker/ca/custom-ca.pem`에 mode `0444`로 원자 게시한다. Container에는
   `/etc/rvc-orchestrator/worker/ca` 전체가 `/etc/rvc-worker/ca:ro`로 mount되고 environment는
   fixed `/etc/rvc-worker/ca/custom-ca.pem`만 가리킨다. Public CA면 option을 생략하고 해당
   environment 값은 비어 있어야 한다.

   마지막 옵션은 현재 `RVC_GPU_SMOKE_VERIFIED=false`인 개발 후보를 시작한다는 명시적
   확인이다. full GPU/TestSet stage set이 검증됐다는 뜻은 아니며 설치기는 별도 경고를
   유지한다. 기존 `worker.env`의 mode와 다른 `--runner-mode`를 재설치에 넘기면 설정을
   조용히 바꾸지 않고 실패한다. service를 중지하고 변경 위험을 검토한 뒤 구성 migration을
   별도로 수행해야 한다.
   기존 native 구성에 acknowledgement가 false인 경우도 CLI 확인값만 받고 env를 조용히
   보존하지 않으며 실패하므로, 동일한 명시적 구성 migration 절차를 따른다.

4. Worker가 최초 등록하면 Manager가 Worker별 bearer credential을 한 번 발급하고 Agent가
   `/var/lib/rvc-orchestrator/worker/credentials/worker.json`에 mode `0600`으로 원자 저장한다.
   그 뒤 재시작은 이 credential을 사용한다. 응답과 파일을 출력하거나 backup/log에 평문으로
   복사하지 않는다.

   정기 회전은 Worker가 idle이고 active lease가 없을 때만 수행한다. 서비스가 새 Job을 claim하지
   않도록 중지한 뒤 one-shot 명령을 실행하고 다시 시작한다.

   ```bash
   sudo systemctl stop rvc-orchestrator-worker.service
   sudo /opt/rvc-orchestrator/worker/bin/worker-compose run --rm worker --rotate-token
   sudo systemctl start rvc-orchestrator-worker.service
   ```

   Manager는 old token으로 prepare하고 새 token hash만 pending 상태로 저장한다. Worker가 새
   credential을 0600 파일에 fsync한 뒤 old+pending 두 secret을 함께 증명해 activate하면 old
   token은 즉시 401이 된다. prepare 응답 유실은 old token으로 pending record를 abort하고,
   activate 응답 유실은 새 token으로 `/workers/me`를 증명해 복구한다. pending 동안 claim은
   409로 닫히며 기본 600초가 지나면 old token을 유지한 채 다시 prepare할 수 있다.

   노출이 의심되면 정기 회전을 신뢰하지 말고 admin의
   `POST /api/v1/workers/{worker_id}/token/revoke`를 사용한다. 기본 요청은 active assignment가
   있으면 409로 거부한다. 침해가 확정돼 즉시 차단해야 할 때만
   `force_cancel_active=true`를 명시한다. 이 경우 Manager는 canonical lease→Job→attempt→Worker
   순서로 잠그고 Job/attempt를 `cancelled`, lease를 released로 기록한 뒤 token hash 자체를
   폐기하고 Worker를 inactive/draining으로 만든다. stale status/claim은 optimistic version
   CAS에서 409가 되며 audit와 MLflow terminal outbox가 같은 transaction에 남는다.

   폐기 뒤에는 같은 이름으로 일반 register하지 않는다. bootstrap secret을 다시 안전하게
   준비하고 해당 host의 기존 credential metadata를 보존한 채 다음 명령으로 동일 Worker ID를
   inactive-only re-enroll한다. Manager는 새 token을 한 번만 반환하고 기존 old/pending token은
   모두 무효다.

   ```bash
   sudo systemctl stop rvc-orchestrator-worker.service
   sudo /opt/rvc-orchestrator/worker/bin/worker-compose run --rm worker --re-enroll
   sudo systemctl start rvc-orchestrator-worker.service
   ```

5. 상태를 확인한다.

   ```bash
   sudo systemctl status rvc-orchestrator-worker.service
   sudo /opt/rvc-orchestrator/worker/bin/worker-compose ps
   sudo /opt/rvc-orchestrator/worker/bin/worker-compose logs --tail=100 worker
   nvidia-smi
   ```

   Custom CA를 사용하면 secret 전체 dump 대신 다음 allowlist만 확인한다.

   ```bash
   sudo stat -c '%u:%g %a %n' \
     /etc/rvc-orchestrator/worker/ca \
     /etc/rvc-orchestrator/worker/ca/custom-ca.pem
   sudo awk -F= \
     '$1 == "WORKER_CA_BUNDLE_HOST_DIR" || $1 == "WORKER_CA_BUNDLE_PATH" {print}' \
     /etc/rvc-orchestrator/worker/worker.env
   sudo /opt/rvc-orchestrator/worker/bin/worker-compose config --quiet
   ```

   Production root install의 directory는 `root:root 755`, file은 `root:root 444`, container path는
   정확히 `/etc/rvc-worker/ca/custom-ca.pem`이어야 한다. Installed wrapper는
   `up|start|restart|run|create`마다 directory/path/owner/mode/PEM을 다시 검증한다. 실패 때 mode를
   완화하거나 environment/file 중 하나만 손대지 말고 Worker를 drain한 뒤 승인된 CA replacement
   절차를 다시 수행한다.

로그를 공유하기 전에 token, presigned URL query, 사용자 경로와 음성 관련 값을 제거한다.
실제 GPU smoke를 통과하지 않은 bundle의 `GPU_SMOKE_VERIFIED=false` 또는
`PROFILE_STAGE_SET_VERIFIED=false` 표시는 절대로 수정해 우회하지 않는다.
native Worker의 PM/Harvest/CREPE/RMVPE 고정 TestSet inference 안전 경계, canonical Artifact
publication, Sample 등록과 completion은 fixture로 검증돼 있다. Production runner factory는
release-owned qualification activation이 fully qualified일 때만 dependency를 주입한다. 현재 실제
asset/base/GPU/no-network 49-case 증적이 없으므로 운영에서는 계속
sample이 꺼진 Job만 실행하고 `AUTO_SAMPLE_JOBS_ENABLED=false`를 유지한다. Worker는
`supported_inference_f0_methods=[]`와
`fixed_test_set_inference_ready=false`를 광고하고 잘못 배정된 sample Job도 workspace 생성 전에
거부한다. Env/YAML/CLI로 이를 임의로 true로 바꾸거나 inference method를 광고하는 것은 지원하지
않는다. 실제 활성화 절차는 `RUNTIME_QUALIFICATION.md`를 따른다.

Worker TestSet 수신 상한의 배포 기본값은 다음과 같다. Manager 상한보다 크게 바꾸어도
Manager 검증을 우회하지 않으며, 낮추면 해당 Worker가 더 엄격하게 fail-closed한다. 운영 변경은
음원 규모와 attempt disk 여유를 계산하고 Worker를 drain한 뒤 수행한다.

```dotenv
TEST_SET_DOWNLOAD_TIMEOUT_SECONDS=3600
TEST_SET_DOWNLOAD_MAX_ATTEMPTS=3
TEST_SET_MAX_ITEMS=128
TEST_SET_MAX_ITEM_BYTES=268435456
TEST_SET_MAX_TOTAL_BYTES=2147483648
TEST_SET_MAX_DURATION_SECONDS=600
TEST_SET_MIN_SAMPLE_RATE_HZ=8000
TEST_SET_MAX_SAMPLE_RATE_HZ=192000
TEST_SET_MAX_CHANNELS=2
TEST_SET_DURATION_TOLERANCE_SECONDS=0.000001
```

수신 파일은 attempt workspace의 `inputs/test_set/<item-id>.wav` mode `0600`에만 게시된다.
중단 뒤 `.test_set.*.partial`이 남으면 Agent는 조용히 재사용하거나 삭제하지 않고 무결성
오류로 멈춘다. 해당 attempt가 실행 중이지 않음을 확인하고 보존/incident 판단을 끝낸 뒤에만
attempt workspace 정리 정책으로 처리한다. 개별 WAV를 수동 복사하거나 mode/hash 검사를
완화해 replay를 통과시키지 않는다.

Sample 등록은 단일 출력 256 MiB/600초/2 channel, attempt 논리 출력 합계 2 GiB/3,600초,
raw JSON 64 KiB, Manager 검증 120초와
기본 동시 2개/분당 30회 상한을 적용한다. approved runtime bundle은
`sha256:<image-digest>@<asset-manifest-sha256>` 쌍으로만 설정한다. GPU matrix가 검증되기 전에는
이 allowlist를 채우고 `AUTO_SAMPLE_JOBS_ENABLED=true`로 바꾸는 것을 production 활성화로
간주하지 않는다.

## 관리자 사용자 lifecycle

최초 bootstrap 이후의 계정 관리는 대시보드 `사용자` 화면 또는 고정
`/api/v1/admin/users` API에서 수행한다. 일반 사용자는 목록·상세·mutation 모두 403이며 사용자
열거를 할 수 없다.

- 새 계정 비밀번호는 16자 이상, 제어문자 없음, 최소 8개 이상의 서로 다른 문자로 구성하고
  이메일 local-part와 무관한 passphrase를 사용한다. 비밀번호는 응답·audit·멱등 원장에 저장하지
  않으며 DB에는 Argon2id hash만 남는다.
- 역할·활성 상태 변경과 비밀번호 재설정은 화면에 표시된 row version을 사용한다. `stale_user`면
  최신 목록을 읽기 전 같은 변경을 다시 보내지 않는다.
- 모든 mutation은 요청별 `Idempotency-Key`를 사용한다. 응답이 유실되면 같은 key와 동일 body로만
  재조회할 수 있고, key를 다른 경로나 body에 재사용하면 409다. 대시보드는 결과 불명확 상태에서
  자동 재전송하지 않고 목록 새로고침을 요구한다.
- 역할·활성 상태가 바뀌거나 비밀번호가 재설정되면 해당 계정의 기존 access token이 모두 즉시
  무효화된다. 계정을 다시 활성화해도 이전 token은 살아나지 않는다.
- 현재 관리자는 자신을 강등하거나 비활성화할 수 없고, 동시 cross-demotion도 database singleton
  fence가 직렬화해 활성 관리자 한 명을 보존한다. 보호를 우회하려고 DB를 직접 수정하지 않는다.
- 감사 원장에는 `admin.user.created`, `admin.user.access_updated|unchanged`,
  `admin.user.password_reset`이 actor/target/변경된 공개 상태와 함께 남는다. 비밀번호, hash,
  idempotency key 원문은 남지 않아야 한다.

`dev.8` 이하에서 `dev.9`로 업그레이드하면 이전 token에 version claim이 없으므로 기존 세션은 모두
401로 닫힌다. 이는 migration 실패가 아니라 의도된 fail-closed 전환이다. 새 head
`b4a91d7e2c63`, readiness와 관리자 재로그인을 확인한 뒤 사용자에게 재로그인을 안내한다.

## 일반 사용자 작업 흐름

대시보드는 현재 로그인, Worker/Job 관측, log/metric/artifact 조회·다운로드와 Job
cancel/retry를 제공한다. Dataset upload UI는 파일 선택 뒤 browser에서 SHA-256을 계산하고
Manager가 허용한 MinIO/S3 target으로 직접 전송한 다음 finalize한다. 운영자는
`S3_PRESIGN_ENDPOINT_URL`이 browser에서 접근 가능한 HTTPS origin인지, `CORS_ORIGINS`에
대시보드 public origin이 있는지 확인해야 한다. Experiment 화면은 ready/usable Dataset만
선택하고 같은 Dataset의 v1/v2·sample rate·F0 조건 Job을 최대 16개까지 순차 생성한다.
목록은 200개 단위로 최대 10,000개까지 완전히 검증하며 이를 넘으면 일부 결과 대신 명시적
상한 경고를 표시한다. 이 경우 filter/cursor API 또는 보존 정책으로 범위를 줄여야 한다.
TestSet/Preset 원장은 API로 준비한다. 검증된 기존 Sample은 Job 상세에서 Range/If-Range
재생할 수 있고 Experiment 화면에서 동일 TestSet item의 current-attempt 출력 두 개를 A/B로
비교할 수 있다. 같은 화면의 model registry는 자동 점수로 best model을 정하지 않고 사용자가
검증을 요청한 real completed Run만 candidate로 등록한 뒤 명시적으로 champion 승인·폐기한다.
Experiment 상세에서 description만 row-version CAS로 수정할 수 있고 exact
이름 확인 뒤 삭제를 요청할 수 있지만 Job/MLflow 참조는 Manager가 거부한다. 다만 CREPE와 실제
GPU/no-network matrix가 끝나지 않아 운영 auto sample은 강제로 꺼져 있다. 자동화용 운영 API
base는 `https://manager.example.com/api/v1`이다. 향후 gate 검증 fixture나 자동화가
sample-enabled config를 만들 때 `index.build_index=false`이면 `index_rate=0`을 함께 보내야
하며, 없는 index에 nonzero retrieval rate를 지정하면 JobConfig가 거부된다.

1. Dataset으로 올릴 WAV 또는 ZIP을 준비한다. ZIP 내부 중첩 폴더는 허용하지만 symlink,
   특수 파일, 암호화 entry, 절대/상위 경로와 과도한 압축률은 거부된다. 현재 non-WAV는
   signature 검사 후 `decoder_pending`으로 격리되며 학습에 사용할 수 없다.
2. `POST /datasets/uploads/init`에 표시명, basename, 정확한 MIME, byte size, SHA-256과
   재시도에 재사용할 idempotency key를 보낸다.
3. 응답의 `method`, `upload_url`, `upload_headers`를 그대로 사용해 raw body를 PUT한다.
   URL query와 upload header는 credential이므로 저장하거나 로그에 남기지 않는다.
4. `POST /datasets/uploads/{upload_session_id}/finalize`를 호출하고 Dataset이 `ready`이고
   `is_usable=true`인지 확인한다. 화면의 typed rejected/duplicate/decoder count와
   sample-count 가중 clipping/silence/RMS, integrated LUFS를 검토한다. LUFS는 파일별 값의 평균이
   아니라 `itu-r-bs1770-4-mono-stereo-v1` Dataset-global gate 결과다. 짧은 입력, 절대 gate 미만,
   지원 밖 channel/sample rate는 typed unavailable reason으로 표시되며 migration 전 historical
   행은 nested loudness `null`이다. `pcm_quality=null`이나 historical loudness를 0으로 해석하지
   말고 exact 집계가 필요하면 원본을 새 upload session으로 다시 검증한다.
5. 고정 비교 음원이 필요하면 `POST /test-sets`로 draft revision을 만들고 각 PCM WAV에 대해
   `POST /test-sets/{id}/item-uploads/init` → 응답 target raw PUT →
   `POST /test-sets/item-uploads/{session_id}/finalize`를 수행한다. 안전한 `item_key`, 유일한
   `sort_order`, 정확한 size/SHA-256, license/provenance reference가 필수다. 실패 session은
   같은 항목의 새 init으로 정리한 뒤 `POST /test-sets/{id}/finalize`로 manifest를 동결한다.
   upload URL/header를 저장하거나 로그에 남기지 않고 ready revision은 수정/삭제하지 않는다.
6. 대시보드의 새 Experiment 화면에서 준비된 Dataset을 묶고, Experiment 상세의 조건
   matrix에서 Job 이름과 config를 preview한 뒤 생성한다. UI는 제출 직전 기존 이름을
   다시 조회하고 단건 결과를 보존한다. 후속 POST 오류가 나도 이미 성공/충돌이 확정된
   행은 유지하며, 응답 유실 행과 실제 미제출 행을 구분한다. 자동화에서는
   `POST /experiments`와 완전한 `JobConfig`의 `POST /jobs`를 사용하며 각 `job_name`을
   고유하게 유지한다.
7. Job 상세 화면에서 상태, attempt, 실시간 log와 loss/GPU metric을 본다. GPU 표본은
   시작 직후와 기본 60초마다 `system.gpu.<index>.*`, `system.gpu.telemetry_available`,
   `system.disk_free_bytes` key로 현재 attempt에 표시돼야 한다. 화면은 최신 200개를 15초마다
   non-overlapping tail 조회한다. 취소는 cooperative 방식이라 실행 process 종료와 Worker 확인까지
   시간이 걸릴 수 있다.
8. `completed` 뒤 model/index와 manifest checksum을 확인하고 same-origin 다운로드
   버튼을 사용한다. presigned object URL을 다른 사용자에게 전달하지 않는다.
9. 비교 화면에서 exact current attempt, model/index 전체 SHA-256을 확인하고 `후보 등록`을
   누른다. Manager는 reviewed commit과 승인 runtime provenance, canonical byte를 다시 검증한다.
   등록된 candidate를 별도 확인 후 promotion하면 active champion이 된다. 이전 champion은
   approved inactive 이력으로 보존되며 자동 폐기되지 않는다. 잘못되거나 더 이상 사용할 수 없는
   candidate/approved entry만 bounded reason code로 revoke하고 다른 모델로 자동 fallback하지 않는다.

상태가 `failed`면 오류 code와 마지막 attempt log를 보존한 뒤 원인을 먼저 구분한다.
`stage_configuration_invalid`, `worker_runtime_unready`, `stage_integrity_failed`와
`stage_remote_rejected`는 설정/runtime/무결성을 수정하기 전 반복 제출하지 않는다.
`exhausted_transient`는 Manager/object endpoint가 회복됐는지 확인한 뒤에만 retry한다.
`stage_timeout`, `stage_process_failed`, `telemetry_persistence_failed`와
`stage_internal_error`는 Worker subprocess log, disk/spool과 runtime 상태를 조사한다.
Worker는 같은 attempt의 stage를 자동 재실행하지 않는다. Dataset/Artifact 전송만 해당
원자·멱등 operation 내부에서 bounded retry하고, telemetry transient는 spool에 보류한다.
사용자 retry는 이전 attempt의 log/metric/artifact를 지우지 않고 새 attempt를 만든다. `decoder_pending`,
`upload_pending` Dataset 또는 model/index 게시가 완료되지 않은 Job을 완료로 간주하지
않는다.

## Model registry 승인과 응답 유실 runbook

Registry lifecycle은 `candidate -> approved -> revoked`이고 `revoked`는 terminal이다. 새 candidate를
자동 champion으로 만들지 않으며 한 Experiment의 active champion은 0개 또는 1개다. 새 promotion은
이전 champion을 `approved` inactive 이력으로 남기고, rollback은 그 approved entry를 다시 명시적으로
promotion하는 새 mutation이다. Active entry를 revoke하면 champion은 비며 다른 approved entry로
자동 fallback하지 않는다.

운영 API는 아래 고정 경로만 사용한다. 각 mutation 전에 GET으로 현재 version과 entry 상태를 읽고,
pagination은 `limit<=200` 범위에서 `total`을 끝까지 확인한다. 아직 registry row가 없으면 GET은
`registry_row_version=0`, 빈 `items`, `active_entry_id=null`을 반환한다.

- `GET /api/v1/experiments/{experiment_id}/model-registry`
- `POST /api/v1/experiments/{experiment_id}/model-registry/candidates`
- `POST /api/v1/experiments/{experiment_id}/model-registry/entries/{entry_id}/promote`
- `POST /api/v1/experiments/{experiment_id}/model-registry/entries/{entry_id}/revoke`

Browser BFF는 page를 렌더한 actor UUID를 mutation의 `X-RVC-Expected-Actor-ID`로 고정 전달하며,
Manager는 같은 요청을 인증한 actor가 다르면 원장 write 전에 `409`로 거부한다. 직접 API client도
`GET /api/v1/auth/me`에서 읽은 현재 actor ID를 이 header에 넣어 request intent를 session에 결박한다.
Header 값이나 actor ID를 ticket, screenshot, 일반 로그에 남기지 않는다.

Candidate body에는 현재 GET의 `expected_registry_row_version`과 exact current completed
`source_job_id`, `source_attempt_id`, `model_artifact_id`만 보낸다. Index Artifact나 runtime digest를
client가 선택하지 않는다. Manager가 같은 attempt의 유일한 verified `final_index`와 frozen provenance를
원장에서 결박한다. Promotion은 현재 registry/entry row version 두 개를 보내며 candidate 또는 inactive
approved entry에만 사용한다. Revoke도 두 version과 `quality_rejected|security_issue|operator_request`
중 하나인 reason code를 요구한다. Browser는 HttpOnly session을 same-origin BFF 밖으로 보내지 않으며
Manager path, runtime digest 또는 index Artifact를 직접 다루지 않는다.

Candidate 생성과 promotion은 model/index canonical object 전체를 다시 hash하므로 대형 object에서
응답 시간이 길 수 있다. 같은 화면/자동화에서 mutation을 겹치거나 짧은 client timeout 뒤 새 요청을
보내지 않는다. `409`이면 현재 registry를 다시 읽고 stale version, entry state, duplicate candidate
또는 canonical byte 불일치를 구분한다. Hash/ledger 불일치는 같은 요청을 반복하지 말고 source
Job/attempt와 object incident를 조사한다. `404`는 타 소유자와 잘못된 Experiment/Job/attempt/Artifact를
구분해 열거하지 않는 경계이므로 DB 조회로 우회하지 않는다.

Mutation 중 연결 종료, abort, invalid success projection 또는 모든 `5xx`는 commit 여부가 불명확할
수 있다. 새 key로 자동 재시도하지 않는다. Registry GET으로 target candidate/approved/revoked와
version/active champion을 확인한다. 원하는 상태가 보이면 성공으로 기록하고, 다른 actor의 변경이
보이면 새 현재 상태에서 다시 승인 판단한다. Registry와 entry가 완전히 unchanged임을 확인한 경우에만
client가 보존한 **같은 key와 byte-identical body**로 명시적으로 재확인한다. `503`의 `Retry-After`는
storage/spool 회복을 기다리는 최소값이지 자동 승인 재시도 허가가 아니다.

Dashboard에서 불명확 상태가 보이면 tab을 닫거나 hard reload하지 말고 먼저 `원장 재확인`을 누른다.
이 동작은 mutation을 보내지 않고 전체 page를 안정된 version으로 읽어 target applied, 완전 unchanged,
다른 변경을 구분한다. Applied면 재전송 없이 끝나고, unchanged에서만 `같은 요청 재확인` 또는
`보존 요청 폐기`가 열린다. 최초 key/body는 raw 값을 durable browser storage에 남기지 않고 현재
component memory에만 보존한다. Tab/reload로 이 intent를 잃었다면 key를 추측해 재구성하지 말고 최신
원장과 operation/audit 결과를 확인한 뒤 별도 운영 판단으로 새 작업을 시작한다.
다른 tab에서 logout/login해 actor가 바뀌면 최초 intent는 즉시 폐기하고 같은 key/body를 새 actor로
보내지 않는다. 화면이 새 actor로 다시 렌더되지 않거나 `로그인 사용자가 변경되었습니다` gate가
나오지 않은 채 재확인 버튼이 활성화되면 작업을 중단하고 incident로 취급한다.

Fake, historical provenance NULL, unreviewed commit, 승인 목록 밖 runtime pair는 candidate 0건이
정상이다. 현재 dev.20 Worker partial bundle의 닫힌 runtime qualification을 registry 시연을 위해 환경변수나 SQL로
열지 않는다. Active entry를 revoke하면 champion은 비며, 운영자가 별도 approved entry를 명시적으로
promotion하기 전까지 빈 상태를 유지한다. Audit와 operation ledger에는 URI/object key, upload session,
원문 idempotency key가 없어야 한다.

## Live telemetry 장애 runbook

- Training 중 `current_epoch`/loss/log가 멈추면 먼저 Worker heartbeat와 lease expiry, Worker data
  filesystem의 telemetry `pending`/`dead-letter` 용량, API의 `413|409|503` 분류를 확인한다. Spool
  파일 내용을 일반 ticket에 붙이지 말고 mode `0600` 보존본에서 token/query/path가 redacted됐는지
  확인한다.
- Worker 목록의 GPU 값은 갱신되는데 Job 상세의 `system.gpu.*`/`system.disk_free_bytes`가 멈췄다면
  heartbeat 자체보다 60초 sampling deadline과 attempt telemetry spool/delivery를 먼저 조사한다.
  `system.gpu.telemetry_available=0`이면 nvidia-smi 실행/semantic 검증 실패이고, `1`과 count 0은
  성공한 empty inventory다. GPU 값이 변하지 않았어도 설정 cadence의 표본은 dedupe되지 않으므로
  sequence와 `occurred_at`이 계속 증가해야 한다.
- `413`은 단일 status/log/metric raw body가 기본 2 MiB를 넘은 것이다. API 상한을 즉시 늘리지 말고
  Worker batch/per-record와 전체 spool quota, proxy body limit을 함께 점검한다. `503`과
  `Retry-After`는 active ingest가 cancel/terminal write fence에 진 retryable 경계이므로 durable
  record를 삭제하거나 stage를 다시 실행하지 않는다.
- Terminal status가 Manager에 커밋돼 attempt에 log/metric exclusive watermark가 있으면 exact
  Worker/lease/attempt의 `sequence < count` pending batch만 late replay된다. 상한 이상, 다른
  attempt/Worker, watermark 없는 legacy terminal의 `409`를 수동 DB 수정으로 우회하지 않는다.
  같은 idempotency key의 다른 payload도 corruption 신호다.
- Healthy Manager에서는 producer seal 뒤 final flush가 실행돼 terminal 직후 pending이 비어야 한다.
  Manager 503/단절이면 watermark 미만 record가 pending에 남는 것이 정상이며 삭제하지 않는다.
- Manager 전체 장애 중 terminal status 미커밋 상태로 lease가 회수된 old attempt에는 server
  watermark가 없다. 이 pending telemetry는 자동 복구 범위가 아니며 새 attempt에 합치지 않는다.
  Worker 보존본, Job status event, lease/attempt ID와 장애 시간을 함께 보존해 운영자 reconcile
  대상으로 기록한다.

## 일상 운영 점검

매일 또는 monitoring에서 다음 조건을 확인한다.

- `/readyz`가 200이고 `database`, `redis`, `rq_worker` check가 모두 `ok`, `mlflow`가 `ok` 또는
  의도적으로 끈 `disabled`인지 확인한다. fail-open에서 `mlflow=unavailable`이어도 200일
  수 있으므로 이는 별도 경보 대상이다.
- `manager-compose ps`에서 healthcheck가 있는 PostgreSQL, Redis, MinIO, MLflow, API, Web, proxy는
  healthy인지 확인한다. `rq-worker`에는 Compose healthcheck가 없으므로 running 상태와
  `/readyz`의 `rq_worker=ok`를 함께 확인한다.
- 대시보드에서 Worker `online`, heartbeat 시각, GPU/VRAM/disk와 현재 Job을 확인한다.
- `failed`, 오래 지속되는 `processing/finalizing`, 반복 lease recovery와
  `staging_cleanup_pending/delete_failed`를 조사한다.
- Manager object storage, database volume, artifact verification spool와 Worker data
  filesystem의 여유 공간을 경보한다.
- JSON log의 `request_id`로 요청을 추적하되 Authorization/header/query 원문을 수집하지
  않는다.

장애 조사용 기본 명령:

```bash
sudo /opt/rvc-orchestrator/manager/bin/manager-compose ps
sudo /opt/rvc-orchestrator/manager/bin/manager-compose logs --tail=200 api
sudo /opt/rvc-orchestrator/manager/bin/manager-compose logs --tail=200 rq-worker
sudo /opt/rvc-orchestrator/manager/bin/manager-compose logs --tail=200 redis minio
sudo journalctl -u rvc-orchestrator-manager.service --since '1 hour ago'
```

Redis 장애 또는 최근 RQ Worker heartbeat 부재 시 production API rate limiter와
readiness는 fail-closed한다. Redis를 우회해 요청 제한/RQ readiness를 끄지 말고 원인을
복구한다. Worker 연결이 끊긴 Job은 lease 만료와 offline grace가 모두 확인된 뒤에만 자동
재배정되며, 원 Worker가 실행 중일 가능성이 있으면 먼저 격리한다.

`maintenance job rejected`와 비식별 `job_reference`가 보이면 Redis job envelope가 execution
policy를 위반한 것이다. 임의 callable/callback을 시험 실행하거나 Redis에서 직접 job을
고쳐 재queue하지 말고 Redis credential/접근 로그와 queue key 변조를 조사한 뒤 credential을
회전한다. `rq-worker`는 `PROCESS_ROLE=maintenance` 및 전용 entrypoint로만 시작하며 JWT,
Worker bootstrap/pepper, MLflow token이 container environment나 secret mount에 나타나면
배포를 중단한다.
RQ에는 `rvc_maintenance` PostgreSQL/Redis identity와 staging-delete MinIO identity만 있어야 한다.
PostgreSQL `verify-runtime`, Redis ACL 또는 MinIO exact policy가 실패하면 password/policy를 broad
권한으로 교체하지 말고 initializer와 source secret collision을 조사한다.

## Dataset/TestSet staging retention maintenance

RQ Worker의 내부 scheduler는 storage 실패의 bounded delayed retry만 due 시점에 queue로
되돌리고 새 정기 작업을 생성하지 않는다. 운영 scheduler/cron은 Redis에 직접
연결하거나 임의 RQ callable을 넣지 않고, HTTPS Manager의 admin-only API만 호출한다.
조직의 retention 검토 주기에 맞춰 먼저 dry-run을 실행하고 결과를 확인한 뒤 실제 run을
별도 호출한다. 각 호출은 8~128자의 새 `Idempotency-Key`를 사용하고 네트워크 재시도에는
같은 key를 재사용한다. 예시는 짧은 수명의 admin access token을 발급받은 직후 실행한다.

```bash
curl --fail-with-body -X POST \
  -H 'Authorization: Bearer <short-lived-admin-token>' \
  -H 'Idempotency-Key: dataset-cleanup-20260711-dry' \
  -H 'Content-Type: application/json' \
  --data '{"dry_run":true}' \
  https://manager.example.com/api/v1/admin/maintenance/dataset-staging-cleanup

curl --fail-with-body \
  -H 'Authorization: Bearer <short-lived-admin-token>' \
  https://manager.example.com/api/v1/admin/maintenance/<run-id>

curl --fail-with-body -X POST \
  -H 'Authorization: Bearer <short-lived-admin-token>' \
  -H 'Idempotency-Key: testset-cleanup-20260711-dry' \
  -H 'Content-Type: application/json' \
  --data '{"dry_run":true}' \
  https://manager.example.com/api/v1/admin/maintenance/test-set-staging-cleanup
```

기본 전역 grace는 7일, run당 250 session, 300초, 총 3 attempt이며 환경 변수 상한 안에서만
조정한다. grace는 Dataset upload TTL보다 짧게 설정할 수 없다. Dataset/TestSet의 유효 grace는
각각 전역 grace와 해당 late-writer grace 중 큰 값이고, 첫 삭제 뒤 기본 60초 confirmation grace가
지난 다음 exact generation/key를 다시 삭제해야만 완료된다. 기본 late-writer 값 7200초 자체는
2시간이지만 전역 604800초가 더 크므로 실효값은 7일이다. `staging_cleanup_confirmation_pending`은
예상되는 중간 상태이므로 동일 DB run의 bounded retry를 기다린다. 결과의 `eligible`, `deleted`,
`failed`, `limit_reached`, `time_limit_reached`와
`failure_codes`를 확인한다. `staging_cleanup_pending`은 다음 bounded retry 대상이고 attempt
소진 뒤에는 새 maintenance run 전에 object storage와 DB 상태를 조사한다. task는 staging
key만 지우므로 `finalizing`, `completed`, 아직 유효한 `pending` 또는 canonical Dataset/TestSet
object를 수동으로 삭제해서는 안 된다. 범용 RQ worker/scheduler를 별도로 추가하지 않으며, 주기
automation용 별도 service account/session rotation은 후속 운영 과제다. 내부 scheduler가
돌려보낸 due retry도 custom Worker의 dequeue/perform allowlist를 반드시 다시 통과한다.
scheduler는 queue별 Redis `NX` lock으로 하나만 활성화되며 scheduler lock의 갱신만으로
`/readyz`가 성공하지 않는다. readiness는 실제 RQ Worker registry heartbeat와 API replica의
`maintenance_reconciler` cycle freshness를 각각 확인한다. 기본 reconcile 주기/상한은
15/120초, cycle당 run은 100개다. 운영에서 reconciler를 끄거나 stale 상한을 주기의 두 배
이하로 낮추지 않는다.

RQ task는 기본 15초마다 exact `run_id + attempt_count + running` PostgreSQL CAS heartbeat를
갱신한다. Parent/session lock, S3 delete가 오래 걸리거나 confirmation grace를 기다리는 동안에도
heartbeat가 계속되어야 한다. `/readyz`는 RQ registry heartbeat와 reconciler freshness를 구분해
표시하며 둘 중 하나라도 stale이면 새 cleanup을 시작하지 않는다. Run heartbeat ownership을 잃은
task가 보이면 해당 attempt를 수동 재queue하지 말고 PostgreSQL run/audit와 Redis execution/WIP/result
material을 함께 조사한다.

`e2f8b4c6a930` upgrade 전에는 모든 구 API replica와 upload client를 drain하고 진행 중 Dataset
PUT/finalize가 없음을 확인한다. migration은 구 binary의 dataset-wide canonical key를 가진
`pending|finalizing` session을 `expired`, `upload_fencing_upgrade_required`로 닫고 Dataset을
retryable `upload_pending`으로 되돌린다. 완료된 legacy session/URI는 보존된다. 동일 idempotency
payload를 다시 init하면 quota를 점유하지 않는 old row를 남긴 채 generation+1의 새 session ID와
격리된 canonical key를 받는다. migration 직전 writer가 계속 실행되는 상태에서 upgrade하지 말고,
새 init 전에 old process 종료와 object storage staging 상태를 확인한다.

Redis restore/FLUSH/job TTL 만료 뒤 reconciler는 PostgreSQL의 기존
`queued|retrying|enqueue_failed`와 task timeout을 넘긴 stale `running`을 자동 대조한다.
PostgreSQL advisory transaction lock과 row `SKIP LOCKED` 때문에 여러 API replica가 같은 cycle을
중복 전달하지 않는다. deterministic job ID가 있어도 exact JSON envelope와 실제
queued/scheduled/started 위치를 확인하며 ghost/terminal/inactive poison은 callback/dependent를
실행하지 않고 quarantine 후 재생성한다. exact started final attempt는 중복 생성하지 않는다.
Redis 장애는 run을 `enqueue_failed`와 `maintenance_queue_unavailable`로 남기고 해당 cycle을
첫 실패에서 중단한다. Redis가 복구되면 같은 DB run을 재전달하며 새 run을 만들지 않는다.

`maintenance_queue_poisoned_active`, `maintenance_queue_job_state_invalid`,
`maintenance_queue_identity_invalid` 또는 `maintenance_queue_retry_policy_unsupported`로 run이
`failed`면 자동 재실행하지 않는다. Redis key를 수동 수정하거나 callback/callable을 import해
검사하지 말고 API/RQ write traffic을 격리한 뒤 credential과 Redis audit를 조사한다.
`completed|failed` run은 reconciler 대상이 아니며 새 idempotency key로 같은 삭제를 우회 실행하기
전에 기존 DB result/object 상태를 검토한다. 매우 느린 in-flight PUT은 여전히 전송
lease/heartbeat가 없어 보수적인 grace 뒤 cleanup과 경합할 수 있으므로 장시간 upload 중에는
cleanup을 실행하지 않는 운영 완화가 필요하며 generation fencing 전에는 근본 보장이 아니다.

`failure_codes`에 `storage_namespace_mismatch`가 있으면 해당 session은 현재 local root/S3
namespace에 결박되지 않은 것이다. task는 이 경우 object와 `cleanup_completed_at`을 그대로
보존한다. backend 이름이나 key가 같다는 이유로 수동 삭제하지 말고 아래 upgrade/adoption
절차로 실제 byte를 검증한다.

MLflow는 PostgreSQL 원장이 아니라 durable outbox의 파생 projection이다. 기본
`MLFLOW_FAIL_CLOSED=false`에서 장애가 나면 API 응답은 유지되고
`mlflow_sync_events.status=pending`이 누적된다. MLflow를 복구한 뒤 pending 수와
`last_error_code`가 감소하고 `/readyz`의 `mlflow`가 `ok`로 돌아오는지 확인한다.
`MLFLOW_FAIL_CLOSED=true`의 write `503`은 body의 `ledger_committed=true`와 resource ID를
확인해야 하며, 같은 Experiment/Job create를 반복해 새 원장 row를 만들지 않는다.
브라우저가 Experiment create의 HTTP 응답 자체를 받지 못하면 commit 여부를 알 수 없어
form을 잠그고 목록 확인을 요구한다. 현재 API에는 create idempotency key가 없으므로 새
page/client에서 확인 없이 재제출하면 중복 Experiment가 생길 수 있다.

## Backup, restore, upgrade와 Manager rollback

최소한 매일, 그리고 모든 upgrade/restore 전에 Manager backup을 만든다.

```bash
sudo /opt/rvc-orchestrator/manager/bin/backup
```

성공한 archive와 외부 checksum을 Manager host와 다른 암호화 저장소로 복제하고 복원
drill을 정기 수행한다. backup에는 `/etc/rvc-orchestrator/manager` secret/config가
포함되지 않으므로 별도의 접근 통제된 secret backup이 필요하다. 기본 quiesced backup이
active upload 때문에 거부되면 session을 완료/만료 처리한다. 일반 운영에서
`--online-inconsistent`로 우회하지 않는다.

`9d2f4b7c8e10` storage namespace migration을 포함한 release로 처음 올릴 때는 먼저 새
upload 유입을 차단하고 Dataset/Artifact의 `pending|finalizing` session 수가 모두 0인지
승인된 DBA 연결에서 확인한다.

```sql
SELECT 'dataset' AS kind, count(*)
FROM dataset_upload_sessions WHERE status IN ('pending', 'finalizing')
UNION ALL
SELECT 'artifact' AS kind, count(*)
FROM artifact_upload_sessions WHERE status IN ('pending', 'finalizing');
```

0이 아니면 완료 또는 정상 만료까지 기다리고 upgrade하지 않는다. migration은 기존 행을
현재 storage 설정으로 추측해 backfill하지 않고 64자리 zero sentinel `UNBOUND`로 둔다.
upgrade 뒤 active UNBOUND가 발견되면 adoption이나 임의 SQL로 결박하지 말고 writer를 중지한
채 pre-upgrade backup/rollback 또는 승인된 incident 절차를 사용한다. 이 행은 owner/attempt
quota를 계속 점유할 수 있고 Dataset cleanup도 `storage_namespace_mismatch`로 보류된다.

terminal historical session은 현재 release의 API image와 설정으로 먼저 preview한다. preview는
binding/object를 바꾸지 않지만 operator audit event를 기록한다. 출력의
`target_storage_backend`, `target_storage_namespace_sha256`, 각 item code와
`remaining_unbound`를 change record에 남긴다. URL, object key, credential은 출력되지 않는다.

```bash
sudo /opt/rvc-orchestrator/manager/bin/manager-compose run --rm --no-deps api \
  rvc-manager-adopt-storage-sessions --kind dataset --limit 100
sudo /opt/rvc-orchestrator/manager/bin/manager-compose run --rm --no-deps api \
  rvc-manager-adopt-storage-sessions --kind artifact --limit 100

sudo /opt/rvc-orchestrator/manager/bin/manager-compose run --rm --no-deps api \
  rvc-manager-adopt-storage-sessions --kind dataset \
  --session-id <verified-dataset-session-uuid> --apply
sudo /opt/rvc-orchestrator/manager/bin/manager-compose run --rm --no-deps api \
  rvc-manager-adopt-storage-sessions --kind artifact \
  --session-id <verified-artifact-session-uuid> --apply
```

Dataset `completed`는 original/flat/manifest/quality 네 object, Artifact `completed`는 canonical
object 전체와 DB canonical URI/metadata를 대조한다. `failed|expired`는 staging object의
전체 byte가 session의 선언 size/SHA-256과 일치하는지 확인한다.
`pending|finalizing`은 항상 거부된다. 같은 current namespace에 이미 결박된 explicit session은
`verified/already_bound`로 멱등 성공한다. rejected item이 있으면 CLI는 exit 2이며 batch는
all-or-nothing이 아니므로 각 item의 `adopted|verified|rejected` 결과를 확인한다. 모든 대상의
`remaining_unbound=0`을 확인한 뒤에만 write traffic과 cleanup을 다시
연다. S3 access key/secret만 회전한 경우 fingerprint는 유지된다. root/endpoint/bucket/region/
addressing style을 바꿨다면 새 namespace에서 adoption하지 말고 원래 namespace byte를 별도
검증·이관하는 change를 먼저 수행한다.

upgrade 전에는 새 bundle checksum, schema compatibility marker, image provenance와 현재
backup을 확인한다. 신규 설치·upgrade는 위 dev.20 archive hash, source commit과 schema head
`f5d1c8a9b240`을 대조하고 새 dev.20 bundle의 `upgrade.sh`만 사용한다. dev.18은 model registry를
포함하지만 maintenance DB/S3/Redis 최소권한 경계가 없고 dev.17 이하는 더 오래된 기준이므로
신규 upgrade script로 사용하지 않는다. Upgrade는 strict SemVer forward-only이고 pending env/target
Compose 검증 실패 전에는 env/current를 바꾸지 않는다. Target start가 실패해 nonzero가 되면 새 target pointer를 유지한
down 상태로 진단하며 임의로 DB를 downgrade하지 않는다. Manager 자동 rollback은 같은 compatibility marker와 Alembic revision set만
허용한다. 강제 schema mismatch 옵션은 검토·mandatory backup 없이 사용하지 않는다.
dev.17의 `d8f2a6c4b901`에서 올릴 때 historical JobAttempt의 NULL registry provenance를 SQL이나 현재
Worker 설정으로 backfill하지 않는다. 이 행은 dev.20에서도 candidate가 될 수 없는 것이 정상이다.
dev.17 application은 새 원장을 이해하지 못하므로 compatibility marker를 강제로 맞춘 자동 rollback을
허용하지 않는다.
파괴적 restore의 정확한 확인 flag와 상세 실패 복구 절차는 `DEPLOYMENT.md`를 따른다.
Worker upgrade는 새 bundle의 단일 `WORKER_IMAGE`와 version/gate를 `worker.env`에 원자
반영하지만 endpoint, timeout, custom setting, native acknowledgement, token/profile/data는
보존한다. upgrade 뒤 `worker.env`와 `worker-compose config`에서 새 version/image를 확인하고,
env 전체가 보존된다고 가정해 구 release image를 계속 실행하지 않는지 점검한다.
dev.17에서 도입되어 dev.20에도 유지된 custom CA는 option을 생략한 install/upgrade와 일반
uninstall에서 보존된다. CA를 회전할 때는 Worker를 drain하고 새 root-owned mode `0444|0644` source를
`--ca-bundle-file`로 전달한다. Installer는 replacement를 먼저 staging/prevalidate하고 target
Compose/activation 실패 시 이전 CA byte와 environment/current를 복구한다. 이 보존을 믿고 수동
`mv`/`rm`하거나 `worker.env`만 편집하지 않는다.
Worker에는 자동 rollback script가 없다. 이전 bundle로 되돌아가는 작업은 runtime/data 호환성을
검토한 별도 변경 절차로 수행한다. Manager/Worker uninstall은 설치된 `bin`이 아니라 압축 해제한
해당 bundle의 `sudo ./uninstall.sh`에서 실행한다. Stop/down 일부 실패는 dev.20에서 nonzero이지만,
exit 0 뒤에도 systemd/Compose가 실제로 inactive인지 확인한다. Worker config 보존에는 custom CA도
포함된다.

## 운영 배포를 막는 현재 출시 gate

- dev.17에서 도입되고 dev.20에 유지된 custom CA projection/strict SSL context를 clean Ubuntu
  Worker와 실제 Manager/Object HTTPS hostname에서 확인하는 설치·재부팅·upgrade·negative 인수 시험
- clean Ubuntu Manager 설치/재부팅/upgrade/rollback/restore smoke
- clean NVIDIA GPU Worker 설치와 실제 RVC v1/v2 matrix sample Job
- runtime image digest, source/wheel/asset provenance, license와 SBOM 검증
- multipart/resume, non-WAV sandbox decoder, clean-VM token rotate/revoke/re-enroll drill
- 실제 S3의 7일보다 긴 in-flight Dataset/TestSet PUT, 다중 replica fencing/이중 cleanup 장애 주입
- CREPE offline asset, Torch `>=2.6` safe runtime과 전체 GPU/no-network sample matrix
- Sample A/B, Experiment description PATCH/delete와 model registry candidate/promotion/revoke의 실제
  browser/API response-loss·접근성 E2E
- Model registry의 실제 PostgreSQL 다중 replica promotion 경쟁과 MinIO/S3 대용량 canonical
  재해시·tamper/outage 인수
- 관리자 사용자 lifecycle의 기존 session 폐기, 반응형·keyboard·screen-reader browser E2E
- 실제 PostgreSQL/Redis/MinIO 장애·복구와 성능/보안 검증
- historical `UNBOUND` Dataset/Artifact의 production-like drain/adoption/rollback drill
- terminal status 미커밋 Manager 전체 장애 뒤 watermark 없는 Worker spool의 operator reconcile

이 항목을 통과하기 전에 개발 bundle을 `v1.0.0`, production-ready 또는 검증된 GPU
installer로 표시하지 않는다.

# 설치 가이드

이 문서는 중앙 관리 서버(Manager)와 학습 서버(Worker)를 Ubuntu 호스트에 설치하는 사용자용
절차다. 패키징 구조와 내부 보안 경계는 `docs/DEPLOYMENT.md`, 설치 후 일상 운영은
`docs/OPERATIONS_GUIDE.md`를 함께 본다.

## 이 문서를 읽는 가장 빠른 순서

현재 산출물은 `0.1.0-dev.20` 개발 후보다. Manager archive는 정확히 8개의
`linux/amd64` image를 포함한 self-contained 후보이고, Worker archive는 image/runtime/GPU gate가
없는 partial 구성 시험용이다. 먼저 아래에서 수행할 시험을 하나 선택한 뒤 해당 절만 따라간다.

| 목적 | 따라갈 절 | 지금 가능한 판정 |
|---|---|---|
| Manager archive와 설치기 자체 확인 | 1~4절 | 가능 — 포함 image 검증·load, `--no-start`, 기능 smoke까지 수행 가능 |
| Worker archive와 설치기 자체 확인 | 1절, 2절의 Worker 블록, 5절 | 가능 — `fake --no-start` 구성 시험만 수행 |
| 중앙 Manager 화면/API 확인 | 1~4절 | 가능 — image는 포함됨; 실제 환경의 TLS/DNS 설정은 별도 필요 |
| 실제 NVIDIA GPU 학습 | 1절, 2절의 Worker 블록, 6절 | 현재 dev.20 Worker로 불가능 — runtime 포함 별도 Worker 번들 필요 |
| 설치 후 사용자 인수 시험 | 7절 이후와 `docs/TEST_GUIDE.md` | 단계별 가능/차단 항목을 분리 판정 |

### 사용자가 지금 우선 수행할 최소 설치 확인

현재 `dev.20`을 곧바로 production으로 승격하지 말고, 폐기 가능한 Ubuntu x86_64 VM에서 아래
순서로 설치 파일과 Manager 기능을 먼저 확인한다.

1. Manager archive는 2절의 외부 checksum, 내부 exact ledger와 manifest 검증을 수행한다.
2. Manager archive에 든 image를 3절처럼 load·identity 검증하고 4.1절의 `--no-start` 설치를
   수행한다. Installer에 load를 맡기면 같은 검증을 설치 과정에서 자동 수행한다.
3. 시험용 TLS/DNS를 구성할 수 있으면 Manager를 시작해 화면/API와 `/readyz`를 확인한다.
4. Worker archive도 2절의 Worker 검증을 수행한 뒤 5절의 `fake --no-start` 설치만 수행한다.
5. Worker가 `native`를 거부하는 5절의 보호 동작까지 확인하되 service를 enable/start하지 않는다.
6. 결과는 source 저장소의 `docs/TEST_RESULT_TEMPLATE.md` 또는 압축 해제한 bundle root의
   `TEST_RESULT_TEMPLATE.md`를 복사해 기록하고 `docs/TEST_GUIDE.md`의 T2/T4 합격 기준으로
   판정한다.

Manager application/dependency image는 archive에 모두 들어 있으므로 따로 build하거나 pull하지
않는다. 화면의 production 경로를 확인하려면 설치 환경의 TLS/DNS가 필요하다. 실제 RVC 학습은
self-contained Worker runtime 후보와 NVIDIA GPU 검증이 추가로 필요하며, 현재 Worker service가
시작되지 않는 것은 partial 배포 범위의 의도된 차단이다.

저장소에서 전달해야 하는 파일은 `dist/installers/` 아래의 archive와 같은 이름의 `.sha256`
파일이다. Manager 호스트에는 Manager 두 파일만, Worker 호스트에는 Worker 두 파일만 신뢰된
전송 수단으로 복사한다. 압축을 풀기 전에 외부 checksum부터 확인한다.

```text
rvc-manager-0.1.0-dev.20-linux-amd64.tar.gz
rvc-manager-0.1.0-dev.20-linux-amd64.tar.gz.sha256
rvc-worker-0.1.0-dev.20-linux-amd64.tar.gz
rvc-worker-0.1.0-dev.20-linux-amd64.tar.gz.sha256
```

중요: dev.20 Manager와 Worker manifest는 모두 source commit
`298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a`에 결박된다. 이후 저장소 HEAD가 달라져도 이미 만든
archive의 provenance가 바뀌는 것은 아니다. dev.20 tag로 현재 checkout의 image를 다시 빌드하거나
archive 안 image를 교체하지 말고, 변경된 source로 후보를 만들 때는 새 version을 사용한다.

`dev.17` 이하 archive는 새 설치·업그레이드에 사용하지 않는다. `dev.14` 이하 설치기는 archive에서
`SHA256SUMS`를 제거한 변조를 실제 Git source tree로 오판할 수 있었고 구 `upgrade.sh`로 낮은
version 전환도 막지 못했다. dev.15는 code guard를 보강했지만 bundle-local runbook이 verifier에
`current` symlink를 root로 넘겨 정상 설치에서도 실패한다. dev.16은 physical release resolve를
보정했지만 Worker custom CA와 이번 fail-fast/fixed-hash/config-only/secret pre-state runbook
보정이 없다. dev.17은 해당 경계와 Experiment 비교를 제공했던 immutable 과거 archive지만 model
registry schema/API/BFF/UI를 포함하지 않는다. 기존 archive byte는 고칠 수 없으므로 반드시 아래
dev.20 외부 SHA-256과 내부 exact ledger를 확인한 새 bundle만 사용한다. dev.17의 과거 SHA-256은
Manager `b131698fbdeb51887d808f1396323b9a0e37ef6495445e60eadbedc024b95b96`, Worker
`a4b2951b7f210501e73f2d9ab1b6fb9d78c6ce8f93aed26b59b83d898a4883e7`이며 dev.20 검증값으로
재사용하지 않는다.

## 먼저 확인할 현재 배포 상태

현재 설치 기준선은 `0.1.0-dev.20` 개발 후보 두 개다.

| 구성요소 | 현재 가능한 범위 | 현재 불가능한 범위 |
|---|---|---|
| Manager | exact 8-image archive 검증/load, 설치·화면/API·저장소 기능 시험 | clean Ubuntu/TLS/보안·공급망 gate를 통과한 production 승인 |
| Worker | checksum, preflight, 설치기와 구성 파일을 `--no-start`로 검증 | dev.20 번들만 이용한 native/profile 학습 및 production Manager 연동 |
| Sample | fixture 기반 자동 회귀 | 실제 production Sample Job |

dev.20은 dev.19까지의 trusted `PUBLIC_SCHEME`, 역할별 runtime secret projection, exact MinIO policy,
MLflow UID/GID `10002:10002`·read-only rootfs와 authoritative engine 표시를 유지한다. dev.13에서
추가된 `itu-r-bs1770-4-mono-stereo-v1` Dataset integrated loudness, mode `0444`
`RELEASE_SHA256SUMS` exact inventory와 bundle-local 문서도 보존하고, dev.14의 Manager 전체
Compose smoke, proxy foreground command와 loopback `host-access` 경계를 포함한다. dev.15의
release source ignore closure, Docker-save config content digest와 application `Config.User`,
extracted bundle의 필수 `SHA256SUMS`, strict SemVer forward-only upgrade, pending env Compose
prevalidation과 uninstall 실패 전파도 유지한다. dev.16의 physical installed-release runbook,
bundle-local 결과 템플릿, MLflow exact overlay lock, Manager self-contained release orchestrator와
Worker read-only readiness report도 보존한다. dev.17의 Experiment 비교 BFF/UI source,
Worker custom CA installer·fixed read-only mount·공통 strict SSL context, bundle-local native
negative runbook과 audited fail-fast/fixed-hash/config-only/secret pre-state 가이드를 보존한다.
dev.18은 exact current real `rvc_webui` attempt의 reviewed commit·승인 runtime provenance와
canonical model/index 전체 재해시를 요구하는 model registry를 추가한다. Registry는
`candidate -> approved -> revoked`, Experiment별 active champion 0/1, 이전 승인 모델의 명시적
rollback promotion, row-version CAS·멱등·audit API와 same-origin BFF/UI를 제공한다.
dev.19은 maintenance 전용 PostgreSQL/Redis/S3 최소권한과 long-operation heartbeat를 추가했다.
Job 화면은 exact current
attempt의 engine mode만 표시하며 Fake 실행에는 `FAKE · 운영 결과 아님` 경고를 유지한다.
dev.20 Manager는 source commit
`298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a`에서 만든 API/Web/MLflow와
PostgreSQL/Redis/MinIO/MinIO client/Nginx의 정확히 8개 `linux/amd64` image를 포함한다.
Manager manifest는 `SELF_CONTAINED=true`이고 설치기는 image archive와 config/descriptor/layer,
loaded image ID를 검증한 뒤 `RVC_IMAGE_PULL_POLICY=never`로 실행한다. 반면 dev.20 Worker manifest는
`SELF_CONTAINED=false`이고 image inventory가 비어 있으며 다음 값이 모두 `false`다.

```dotenv
RVC_RUNTIME_INCLUDED=false
RVC_NATIVE_RUNNER_AVAILABLE=false
RVC_GPU_SMOKE_VERIFIED=false
RVC_PROFILE_STAGE_SET_VERIFIED=false
RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED=false
```

이 문서에서 dev.20 Worker를 `fake --no-start`로 설치하는 단계는 **설치기/구성 시험**일
뿐이다. 설치형 Manager는 production에서 Fake Worker를 거부한다. 실제 학습은 이 문서 후반의
검증된 runtime 포함 번들이 별도로 만들어진 뒤에만 진행한다.

dev.20 Manager archive는 외부 checksum, 내부 exact ledger/image closure와 실제 load identity
검증을 통과했다. Arm64 Colima에서 포함된 amd64 image를 에뮬레이션해 실행한 release stack 기능
smoke도 PASS했다. 이는 archive와 실행 흐름이 실제로 동작한다는 개발 증거지만 native amd64 clean
Ubuntu 설치, 실제 TLS/browser, 장시간 운영 또는 production 인수가 아니다. Worker는 image가 없어
NVIDIA/RVC 학습을 시험할 수 없다. Manager의 `SBOM_STATUS=partial-release-gates-open`도 유지되므로
기능 smoke 성공을 공급망 승인으로 해석하지 않는다.
Model registry도 실제 browser/API response-loss, 실제 S3 대용량 전체 재해시·outage와 PostgreSQL
다중 replica promotion 경쟁이 아직 `BLOCKED`이므로 자동 회귀만으로 production 합격을 선언하지 않는다.

## 1. 권장 구성과 사전 준비

### 호스트 요구사항

| 항목 | Manager | Worker |
|---|---|---|
| OS/CPU | Ubuntu 22.04 또는 24.04, x86_64 | Ubuntu 22.04 또는 24.04, x86_64 |
| 여유 공간 | 최소 20 GiB | 최소 50 GiB, 실제 학습량에 맞게 추가 확보 |
| 공통 도구 | Docker Engine, Compose v2, systemd, `sudo`, `bash`, `python3`, `curl`, `tar`, `gzip`, `awk`, GNU coreutils(`sha256sum`, `install`, `stat`, `df`, `od`, `tr`), findutils | Manager와 동일 |
| GPU | 필요 없음 | NVIDIA GPU/driver와 NVIDIA Container Toolkit |
| 권한 | 설치 시 `sudo` | 설치 시 `sudo` |

Installer와 systemd unit은 system Docker daemon을 사용한다. Rootless daemon은 현재 설치형
systemd 경로의 지원 범위가 아니다. 먼저 root daemon과 Compose가 정상인지 확인한다.

```bash
sudo docker info
sudo docker compose version
sudo docker info --format 'daemon={{.ID}} root={{.DockerRootDir}}'
```

일반 사용자 `docker info`도 성공한다면 같은 format 명령을 `sudo` 없이 실행해 daemon ID와
Docker root가 위 결과와 정확히 같은지 비교한다. 다르면 rootless/별도 context의 image를 installer가
볼 수 없으므로 중단한다. 이 문서의 build/load/inspect 예시는 안전하게 `sudo docker`로 통일한다.
조직이 같은 system daemon에 대한 non-root 권한을 제공한 경우에만 `sudo`를 생략할 수 있으며,
Docker group 권한을 문서 실행을 위해 임의로 넓히지 않는다.

Source tree의 `make test-*-docker`, Manager self-contained builder와 Worker runtime/bundle builder는
내부에서 `docker`를 직접 호출하는 **release build host용 명령**이다. 이 명령을 실행하는 CI 또는
release 계정은 같은 system daemon에 대한 직접 접근 권한이 있어야 한다. 설치 host처럼
`sudo docker`만 허용된 계정에서 `sudo make`나 임의의 Docker group 추가로 우회하지 말고, 승인된
별도 build host에서 실행한다. Build host와 설치 host의 daemon ID/image inventory를 같은 것으로
가정하지 않는다.

Worker에서는 다음도 통과해야 한다.

```bash
nvidia-smi -L
command -v nvidia-ctk || command -v nvidia-container-cli
sudo docker info --format '{{json .Runtimes}}'
```

CDI를 사용하면 Docker runtime 목록에 `nvidia`가 보이지 않을 수 있지만, NVIDIA Container
Toolkit 명령과 실제 GPU container smoke는 반드시 통과해야 한다. dev.20 partial Worker는
runtime image가 없으므로 해당 GPU container smoke를 제공하지 못하며, 이는 6절의 실제
runtime 후보에서만 실행한다.

`--allow-unsupported-os`, `--skip-daemon-check`, `--skip-gpu-check`는 원인 분석용 우회다. 이 옵션을
사용한 결과를 clean-host 설치 합격 증거로 기록하지 않는다.

### DNS, TLS와 네트워크

운영형 시험에는 두 개의 HTTPS 이름을 권장한다.

- `manager.example.com` → Manager UI/API용 `127.0.0.1:8080`
- `objects.example.com` → MinIO API용 `127.0.0.1:9000`

같은 호스트의 외부 TLS reverse proxy가 두 upstream을 대리하도록 구성하고 외부에는 443만
노출한다. Object proxy는 S3 서명을 깨지 않도록 원래 `Host`, path, query, HTTP method와 body를
보존하고 path를 다시 쓰지 않아야 한다. 인증서는 사용자 브라우저와 모든 Worker가 신뢰해야
한다. MinIO console `9001`과 MLflow `5000`은 기본처럼 loopback에 유지한다. 번들의
`infra/proxy/examples/tls.conf.example`은 **Compose 내부에서 bundled Nginx가 직접 TLS를 종단할
때의 템플릿**이며 Docker DNS 이름 `api`, `web`을 사용한다. 아래의 host proxy 구성과 혼용하거나
외부 Nginx 설정으로 그대로 복사하지 않는다.

예를 들어 같은 Manager 호스트에서 별도 Nginx가 공개 TLS를 종단한다면 핵심 server block은
다음과 같다. 인증서 경로와 도메인은 실제 값으로 바꾸되 `proxy_pass` 뒤에 URI를 덧붙이지 않는다.
Public edge가 bundled proxy의 HSTS를 숨기고 정확히 한 번 다시 기록하므로 중복 header도 막는다.

```nginx
server {
    listen 443 ssl http2;
    server_name manager.example.com;

    ssl_certificate /etc/letsencrypt/live/manager.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/manager.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    proxy_hide_header Strict-Transport-Security;
    add_header Strict-Transport-Security "max-age=31536000" always;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }
}

server {
    listen 443 ssl http2;
    server_name objects.example.com;

    ssl_certificate /etc/letsencrypt/live/objects.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/objects.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    client_max_body_size 5g;

    add_header Strict-Transport-Security "max-age=31536000" always;

    location / {
        proxy_pass http://127.0.0.1:9000;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header Connection "";
        proxy_request_buffering off;
        proxy_buffering off;
        proxy_connect_timeout 10s;
        proxy_read_timeout 1h;
        proxy_send_timeout 1h;
    }
}
```

인터넷에서 8080/9000/9001/5000으로 직접 접근할 수 없어야 한다. 설정 반영 전
`sudo nginx -t`를 통과시키고, object upload 시험에서는 request method·Host·path·query가 그대로
MinIO에 도달하는지 확인한다. 조직 표준 proxy가 Nginx가 아니라면 같은 불변 조건을 해당 제품에
옮긴다. HTTP 80을 열 경우 Manager는 308 HTTPS redirect만 허용하고, production presigned object
URL 자체는 처음부터 `https://objects.example.com`으로 발급해야 한다.

dev.20은 dev.17의 경계를 보존해 client가 보낸 forwarding scheme을 신뢰하지 않고 operator-owned
`PUBLIC_SCHEME=https`를 Nginx/API/Web의 단일 기준으로 사용한다. Bundled Nginx는 upstream에 이
값을 고정 전달하고 Secure session cookie와 edge-owned HSTS를 일관되게 적용한다. Production
start는 `https`가 아니면 거부된다. 다만 실제 외부 TLS 종단, 인증서, Host 전달과 browser cookie/
HSTS는 설치 환경마다 다르므로 clean browser 시험 전에는 production TLS를 PASS로 판정하지 않는다.
Object endpoint도 별도 proxy에서 TLS·S3 서명 보존을 직접 검증해야 한다.

Worker에는 다음 outbound 경로가 필요하다.

- `https://manager.example.com`의 API/heartbeat
- `https://objects.example.com`의 Dataset/Artifact 전송

인증서가 아직 발급되지 않았거나 DNS를 변경할 권한이 없다면 이 단계는 운영/인프라 담당자의
선행 작업으로 기록하고 Manager를 `--no-start` 상태에 둔다. 인증서 경로와 위 server block을
`/etc/nginx/sites-available/rvc-orchestrator.conf`에 저장한 뒤 Ubuntu host Nginx를 쓰는 경우의
적용 순서는 다음과 같다. 인터넷이 차단된 호스트에서는 조직 package mirror로 Nginx를 먼저
준비한다.

```bash
(
  set -Eeuo pipefail
  sudo apt-get update
  sudo apt-get install -y nginx
  sudo ln -s /etc/nginx/sites-available/rvc-orchestrator.conf \
    /etc/nginx/sites-enabled/rvc-orchestrator.conf
  sudo nginx -t
  sudo systemctl reload nginx
)
```

이미 같은 symlink가 있으면 새로 만들지 말고 대상이 정확한지 확인한다. 아래 host `curl`은 DNS와
certificate chain의 선행 확인일 뿐 Worker container의 TLS 증거는 아니다.

```bash
(
  set -Eeuo pipefail
  curl --fail --silent --show-error https://manager.example.com/healthz
  curl --fail --silent --show-error \
    https://objects.example.com/minio/health/ready
)
```

현재 dev.20 Worker bundle은 installer option
`--ca-bundle-file /root/rvc-worker-custom-ca.pem`을 사용할 수 있다. Installer는 다음 조건을 모두
검증한다.

- production source는 root 소유 regular non-symlink file이고 mode가 `0444` 또는 `0644`다.
- 크기는 1 byte 이상 1 MiB 이하이고 ASCII certificate PEM만 포함한다.
- NUL, private key, 불완전하거나 parse할 수 없는 certificate는 거부한다.
- 검증한 byte만 release 밖 `/etc/rvc-orchestrator/worker/ca/custom-ca.pem`에 mode `0444`로
  원자 게시한다.
- host directory는 container `/etc/rvc-worker/ca:ro`에 mount하고 environment는 fixed path
  `/etc/rvc-worker/ca/custom-ca.pem`만 가리킨다.

Worker는 system default trust에 이 CA를 추가하며 hostname 검증, `CERT_REQUIRED`, TLS 1.2 이상을
유지한다. Manager의 동기 `urllib`/비동기 `httpx` 요청과 external Dataset/TestSet/Artifact object
client가 같은 SSL context를 쓰고 environment proxy는 사용하지 않는다. Custom CA는 HTTP를 HTTPS로
바꾸지 않으므로 production Manager와 object endpoint는 계속 `https://`여야 한다. Host trust
store만 수정하거나 `curl -k`, `verify=false`, image CA store 수동 변경으로 우회하지 않는다.
Public/custom CA 어느 쪽이든 T5의 실제 Worker one-shot·등록·object 전송 시험이 별도로 통과해야
하며, dev.20의 clean Ubuntu 실제 endpoint 증거는 아직 release gate다. Immutable dev.16 archive에는
이 custom CA 기능이 없으므로 사설 CA 시험 기준선으로 사용하지 않는다.

Manager host에서는 내부 port가 loopback에만 bind됐는지 확인한다. `9001`과 `5000`은 TLS/인증
경계가 준비돼 있지 않으므로 public 또는 private LAN 주소에 bind하지 않는다. 이 문서의 same-host
object proxy 구성에서는 `9000`도 loopback이어야 한다.

```bash
sudo ss -ltnp | awk '$4 ~ /:(8080|9000|9001|5000)$/ {print}'
```

출력의 네 port 주소는 `127.0.0.1`이어야 한다. 외부의 별도 시험 호스트에서는 443의 두 HTTPS
endpoint가 성공하고 Manager host의 `8080/9000/9001/5000` 직접 연결이 실패하는지 방화벽 정책과
함께 확인한다. `curl -vk`나 인증서 검증 비활성화 옵션의 성공을 TLS 합격으로 기록하지 않는다.

## 2. 번들 무결성 확인

현재 파일, 고정 SHA-256과 byte 크기는 다음과 같다. 크기는 압축 archive 자체의 크기다.

| 파일 | byte | SHA-256 |
|---|---:|---|
| `rvc-manager-0.1.0-dev.20-linux-amd64.tar.gz` | `667617422` | `c6488dad47c7f38c082ed6fa68f1fe3691c069110aef0bbf68a9d7ba5e6f5b70` |
| `rvc-worker-0.1.0-dev.20-linux-amd64.tar.gz` | `108488` | `7f36cbf27100bf70425c2780142d4fa3f6e6e76d0acf410d3e3fb698aa50558b` |

게시 직전 두 파일은 외부 sidecar, 내부 `SHA256SUMS`, exact `verify-ledger`와 `verify-bundle`을
통과했다. 아래 재검증은 전달받은 사용자의 사본이 그 byte와 같은지 확인하는 절차다.

각 호스트에는 해당 component의 두 파일만 있어도 된다. 현재 호스트에 맞는 아래 블록 하나만
실행한다.

Manager 호스트:

```bash
(
  set -Eeuo pipefail
  archive=rvc-manager-0.1.0-dev.20-linux-amd64.tar.gz
  sidecar="$archive.sha256"
  expected=c6488dad47c7f38c082ed6fa68f1fe3691c069110aef0bbf68a9d7ba5e6f5b70
  expected_size=667617422
  source_commit=298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a
  extract_root="$PWD/rvc-manager-0.1.0-dev.20-verified"

  test -f "$archive" && test ! -L "$archive"
  test -f "$sidecar" && test ! -L "$sidecar"
  test "$(wc -l < "$sidecar" | tr -d '[:space:]')" = 1
  read -r sidecar_hash sidecar_name sidecar_extra < "$sidecar"
  sidecar_name=${sidecar_name#\*}
  test -z "${sidecar_extra:-}"
  test "$sidecar_hash" = "$expected"
  test "$sidecar_name" = "$archive"
  test "$(stat -c '%s' "$archive")" = "$expected_size"
  test "$(sha256sum "$archive" | awk '{print $1}')" = "$expected"
  sha256sum -c "$sidecar"

  test ! -e "$extract_root"
  install -d -m 0700 "$extract_root"
  tar -xzf "$archive" -C "$extract_root"
  cd "$extract_root/rvc-manager-0.1.0-dev.20-linux-amd64"
  sha256sum -c SHA256SUMS
  python3 common/image_bundle.py verify-ledger \
    --root . \
    --ledger-name SHA256SUMS
  python3 common/image_bundle.py verify-bundle \
    --root . \
    --component manager \
    --version 0.1.0-dev.20 \
    --source-commit "$source_commit"
)
```

Worker 호스트:

```bash
(
  set -Eeuo pipefail
  archive=rvc-worker-0.1.0-dev.20-linux-amd64.tar.gz
  sidecar="$archive.sha256"
  expected=7f36cbf27100bf70425c2780142d4fa3f6e6e76d0acf410d3e3fb698aa50558b
  expected_size=108488
  source_commit=298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a
  extract_root="$PWD/rvc-worker-0.1.0-dev.20-verified"

  test -f "$archive" && test ! -L "$archive"
  test -f "$sidecar" && test ! -L "$sidecar"
  test "$(wc -l < "$sidecar" | tr -d '[:space:]')" = 1
  read -r sidecar_hash sidecar_name sidecar_extra < "$sidecar"
  sidecar_name=${sidecar_name#\*}
  test -z "${sidecar_extra:-}"
  test "$sidecar_hash" = "$expected"
  test "$sidecar_name" = "$archive"
  test "$(stat -c '%s' "$archive")" = "$expected_size"
  test "$(sha256sum "$archive" | awk '{print $1}')" = "$expected"
  sha256sum -c "$sidecar"

  test ! -e "$extract_root"
  install -d -m 0700 "$extract_root"
  tar -xzf "$archive" -C "$extract_root"
  cd "$extract_root/rvc-worker-0.1.0-dev.20-linux-amd64"
  sha256sum -c SHA256SUMS
  python3 common/image_bundle.py verify-ledger \
    --root . \
    --ledger-name SHA256SUMS
  python3 common/image_bundle.py verify-bundle \
    --root . \
    --component worker \
    --version 0.1.0-dev.20 \
    --source-commit "$source_commit"
)
```

고정 `expected` 값은 `.sha256` 파일 자체가 아니라 승인된 배포 공지나 별도 서명 채널에서 확인해야
한다. 위 블록은 그 값과 sidecar의 hash/파일명, archive에서 직접 계산한 hash를 각각 기계적으로
대조한다. Sidecar만 archive와 함께 바꾼 경우에는 통과할 수 없다. 또한 기존 directory 위에 압축을
덮지 않고 새 `*-verified` extraction root가 존재하지 않을 때만 만든다. 재검증할 때는 기존 root를
재사용하지 말고 검토 후 이름을 바꾸거나 안전하게 정리해 새 빈 root를 사용한다. 검증된 설치 bundle은
각 extraction root 아래의 component directory다.

`sha256sum -c`는 기록된 파일의 hash를 확인하고, `verify-ledger`는 목록 밖 extra file·누락·중복·
symlink·비정상 경로까지 거부한다. `SHA256SUMS`를 새로 만들거나 실패 파일을 교체해 설치를
계속하지 않는다. `install.sh`도 같은 검증과 manifest/supply-chain 검증을 다시 수행한다.
세 strict verifier는 각 bundle에서 모두 exit code 0이어야 한다. Manager 검증은 exact 8-image
archive와 `SELF_CONTAINED=true`까지 확인한다. Worker 검증은 빈 image/archive inventory와
`SELF_CONTAINED=false`가 의도된 partial 상태임을 확인할 뿐 image가 포함됐다는 뜻이 아니다.

## 3. Manager 포함 image load와 identity 확인

dev.20 Manager archive에는 다음 정확한 role이 모두 들어 있다.

```text
api web mlflow postgres redis minio minio-client nginx
```

모든 image는 `linux/amd64`다. API/Web/MLflow는 dev.20 version tag를, dependency 5개는 manifest에
기록된 PostgreSQL/Redis/MinIO/MinIO Client/Nginx 고정 release tag를 사용한다. API/Web/MLflow 실행
user는 각각 `10001:10001`, `nextjs`, `10002:10002`다. 설치 호스트에서 별도 image를 build, pull,
save 또는 retag하지 않는다. 2절에서 검증한 Manager bundle directory에서 다음처럼 포함 archive를
load하고 manifest에 결박된 실제 image identity를 확인할 수 있다.

```bash
(
  set -Eeuo pipefail
  source_commit=298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a

  gzip -dc images/manager-images.tar.gz | sudo docker load
  sudo python3 common/image_bundle.py verify-loaded \
    --root . \
    --component manager \
    --version 0.1.0-dev.20 \
    --source-commit "$source_commit" \
    --docker-command docker
)
```

`verify-loaded`는 정확히 8개 reference의 loaded OCI identity, architecture, config digest, application
user와 version/revision label을 `images-manifest.json`과 대조한다. 실패 시 tag를 덮어쓰거나
manifest/checksum을 다시 만들지 않는다. Root Docker daemon/context가 1절에서 검사한 것과 같은지
확인하고 archive부터 다시 검증한다.

수동 load를 생략해도 된다. `install.sh`는 release를 게시하기 전에 동일한 archive 검증, `docker
load`, loaded identity 검증을 자동 수행한다. Self-contained Manager의 설치 환경에는
`RVC_IMAGE_PULL_POLICY=never`가 기록되므로 설치·시작 중 registry에서 누락 image를 받지 않는다.
따라서 4절에서는 검증된 extracted bundle을 그대로 사용하며 dependency image를 별도 pull하지
않는다.

현재 dev.20 archive는 arm64 Colima에서 amd64 emulation으로 포함 image를 load하고 전체 release
stack을 실행한 기능 smoke가 PASS했다. 이 결과는 clean Ubuntu x86_64, 외부 TLS/browser,
재부팅·upgrade·restore, 장시간 안정성 또는 production 보안 인수를 대신하지 않는다.

### Release engineer가 새 Manager 후보를 만들 때

dev.20 archive를 현재 checkout에서 다시 만들지 않는다. 변경된 source를 포함한 후속 후보는 새
version과 clean committed HEAD에서 전용 builder로 만들고, 결과 archive의 source commit과 hash를
별도로 게시한다.

```bash
NEW_VERSION=0.1.0-rc.1
installers/manager/build-self-contained-release.sh \
  --version "$NEW_VERSION" \
  --schema-compatibility f5d1c8a9b240 \
  --output-dir dist/installers
```

Builder는 정확히 8개 `linux/amd64` role, application user/label, Docker-save
descriptor/config/layer와 loaded identity를 검증한다. 그러나 immutable upstream digest 고정,
완전한 SBOM, 취약점/container/secret scan, 법적 license 검토와 clean-host 인수가 끝나기 전에는 새
archive도 production 승인본이 아니다.

## 4. Manager 설치

### 4.1 시작하지 않고 설치

Manager bundle 디렉터리에서 실행한다.

```bash
(
  set -Eeuo pipefail
  sudo ./preflight.sh
  sudo ./install.sh \
    --no-start \
    --public-scheme https \
    --s3-presign-endpoint-url https://objects.example.com

  MANAGER_RELEASE=$(sudo readlink -f /opt/rvc-orchestrator/manager/current)
  case "$MANAGER_RELEASE" in
    /opt/rvc-orchestrator/manager/releases/*) ;;
    *) echo "Manager current resolves outside releases" >&2; exit 1 ;;
  esac
  sudo stat -c '%U:%G %a %n' \
    "$MANAGER_RELEASE/RELEASE_SHA256SUMS"
  sudo python3 /opt/rvc-orchestrator/manager/lib/image_bundle.py \
    verify-ledger \
    --root "$MANAGER_RELEASE" \
    --ledger-name RELEASE_SHA256SUMS
  sudo python3 /opt/rvc-orchestrator/manager/lib/image_bundle.py \
    verify-loaded \
    --root "$MANAGER_RELEASE" \
    --component manager \
    --version 0.1.0-dev.20 \
    --source-commit 298ee1ec112cc7dc3a55d8374bba8c9e38f9f55a \
    --docker-command docker
  sudo /opt/rvc-orchestrator/manager/bin/manager-compose config --quiet
)
```

설치된 ledger는 `root:root 444`여야 하고 loaded identity 검증과 Compose render도 성공해야 한다.
Mutable 운영 설정과 secret은 release 밖에 있으므로
이 inventory에 포함되지 않는다. Ledger 불일치가 나면 파일을 임의 수정하지 말고, 원본 bundle을
다시 검증해 재설치하기 전에는 service를 시작하지 않는다.

기본 위치는 다음과 같다.

- release: `/opt/rvc-orchestrator/manager/releases/0.1.0-dev.20`
- current symlink: `/opt/rvc-orchestrator/manager/current`
- 환경/secret: `/etc/rvc-orchestrator/manager`
- Compose project: `rvc-orchestrator-manager`
- unit: `rvc-orchestrator-manager.service`

Compose logical volume의 용도는 다음과 같다. 실제 Docker volume 이름에는 Compose project
prefix가 붙을 수 있으므로 이름을 직접 조합하지 말고 label과 `manager-compose` 결과를
기준으로 한다.

| 구분 | logical volume | 취급 |
|---|---|---|
| 원장 데이터 | `postgres_data`, `minio_data` | upgrade/uninstall에서 보존, 정식 backup/restore 대상 |
| 운영 상태 | `redis_data` | upgrade/uninstall에서 보존하지만 PostgreSQL 원장을 대체하지 않음 |
| 진행 중 작업 | `artifact_spool`, `dataset_ingestion` | active upload/finalize 중 삭제 금지, canonical backup 대상은 아님 |
| 파생 secret projection | `api_runtime_secrets`, `maintenance_runtime_secrets`, `mlflow_runtime_secrets`, `database_authz_runtime_secrets` | host source secret에서 매 start 원자 재생성, 별도 backup 대상은 아님 |

volume을 `docker volume prune`로 일괄 정리하지 않는다. Runtime secret volume은 파생 자료지만
실행 중에 삭제하면 non-root service가 secret을 읽지 못한다. 재생성은 service를 중지하고
root source secret이 정상임을 확인한 뒤 `manager-compose up|start|restart`를 통해서만 수행한다.

### 4.2 환경과 TLS 설정

`/etc/rvc-orchestrator/manager/manager.env`를 root로 열어 최소 다음 값을 실제 도메인에 맞춘다.

```dotenv
PUBLIC_SERVER_NAME=manager.example.com
PUBLIC_SCHEME=https
CORS_ORIGINS=https://manager.example.com
HTTP_BIND_ADDRESS=127.0.0.1
MINIO_API_BIND_ADDRESS=127.0.0.1
MINIO_CONSOLE_BIND_ADDRESS=127.0.0.1
S3_PRESIGN_ENDPOINT_URL=https://objects.example.com
S3_VERIFY_TLS=true
S3_BUCKET=rvc-orchestrator
MLFLOW_S3_BUCKET=rvc-mlflow
USER_LIFECYCLE_JSON_MAX_BYTES=16384
WORKER_TELEMETRY_JSON_MAX_BYTES=2097152
```

`ENVIRONMENT=production`, `ALLOW_FAKE_WORKERS=false`, release image tag, secret 경로는 설치기가
관리한다. production Manager에서 Fake Worker를 허용하도록 바꾸지 않는다. 비밀번호, token,
access key를 `manager.env`나 명령행에 복사하지 않는다.

`PUBLIC_SCHEME`은 외부 client header에서 추측하지 않는 운영자 소유 값이다. TLS가 외부 proxy에서
종단돼 bundled proxy와는 HTTP로 통신하더라도 browser-facing 주소가 HTTPS이면 반드시 `https`다.
Production compose wrapper와 proxy entrypoint는 값이 없거나 `http`이면 시작을 거부한다.

#### Root source secret과 역할별 runtime projection

Manager installer가 만든 `manager.env`와 `/etc/rvc-orchestrator/manager/secrets/*`는 host의
root만 읽을 수 있어야 한다. Upgrade로 가져온 기존 파일도 시작 전에 먼저 **변경 없이** 검사한다.
다음 블록은 내용은 출력하지 않고 owner/mode, exact 파일명, regular/non-symlink, non-empty와
16 KiB 상한을 검증한다.

```bash
(
  set -Eeuo pipefail
  config_root=/etc/rvc-orchestrator/manager
  secret_root="$config_root/secrets"
  secret_names=(
    postgres_password maintenance_postgres_password mlflow_postgres_password
    redis_password maintenance_redis_password minio_root_user
    minio_root_password minio_app_access_key minio_app_secret_key
    maintenance_s3_access_key maintenance_s3_secret_key
    mlflow_s3_access_key mlflow_s3_secret_key worker_bootstrap_token
    worker_token_pepper jwt_secret
  )

  sudo stat -c '%U:%G %a %n' \
    "$config_root" "$config_root/manager.env" "$secret_root"
  sudo find "$secret_root" -mindepth 1 -maxdepth 1 \
    -printf '%u:%g %m %f\n' | LC_ALL=C sort

  sudo test -d "$config_root" && sudo test ! -L "$config_root"
  sudo test -d "$secret_root" && sudo test ! -L "$secret_root"
  sudo test -f "$config_root/manager.env" && sudo test ! -L "$config_root/manager.env"
  test "$(sudo stat -c '%U:%G %a' "$config_root")" = 'root:root 700'
  test "$(sudo stat -c '%U:%G %a' "$secret_root")" = 'root:root 700'
  test "$(sudo stat -c '%U:%G %a' "$config_root/manager.env")" = 'root:root 600'
  expected_names=$(printf '%s\n' "${secret_names[@]}" | LC_ALL=C sort)
  actual_names=$(sudo find "$secret_root" -mindepth 1 -maxdepth 1 -printf '%f\n' | LC_ALL=C sort)
  test "$actual_names" = "$expected_names"
  for name in "${secret_names[@]}"; do
    path="$secret_root/$name"
    sudo test -f "$path" && sudo test ! -L "$path" && sudo test -s "$path"
    test "$(sudo stat -c '%U:%G %a' "$path")" = 'root:root 600'
    size=$(sudo stat -c '%s' "$path")
    (( size <= 16384 ))
  done
  echo 'Manager source secret initial permissions/inventory: PASS'
)
```

이 최초 검사가 실패하면 그 결과를 `FAIL`로 보존하고 서비스를 시작하지 않는다. Symlink,
directory, 빈 파일, 16 KiB를 넘는 secret은 권한 보정 대상으로 취급하지 말고 안전한 원본을 다시
확보한다. 파일 byte와 exact inventory가 정상이고 owner/mode만 틀렸을 때에만 다음 보정을 수행한다.

```bash
(
  set -Eeuo pipefail
  config_root=/etc/rvc-orchestrator/manager
  secret_root="$config_root/secrets"
  secret_names=(
    postgres_password maintenance_postgres_password mlflow_postgres_password
    redis_password maintenance_redis_password minio_root_user
    minio_root_password minio_app_access_key minio_app_secret_key
    maintenance_s3_access_key maintenance_s3_secret_key
    mlflow_s3_access_key mlflow_s3_secret_key worker_bootstrap_token
    worker_token_pepper jwt_secret
  )

  sudo test -d "$config_root" && sudo test ! -L "$config_root"
  sudo test -d "$secret_root" && sudo test ! -L "$secret_root"
  sudo test -f "$config_root/manager.env" && sudo test ! -L "$config_root/manager.env"
  expected_names=$(printf '%s\n' "${secret_names[@]}" | LC_ALL=C sort)
  actual_names=$(sudo find "$secret_root" -mindepth 1 -maxdepth 1 -printf '%f\n' | LC_ALL=C sort)
  test "$actual_names" = "$expected_names"
  for name in "${secret_names[@]}"; do
    path="$secret_root/$name"
    sudo test -f "$path" && sudo test ! -L "$path" && sudo test -s "$path"
    size=$(sudo stat -c '%s' "$path")
    (( size <= 16384 ))
  done

  sudo chown root:root "$config_root" "$config_root/manager.env" "$secret_root"
  sudo chmod 0700 "$config_root" "$secret_root"
  sudo chmod 0600 "$config_root/manager.env"
  for name in "${secret_names[@]}"; do
    sudo chown root:root "$secret_root/$name"
    sudo chmod 0600 "$secret_root/$name"
  done

  test "$(sudo stat -c '%U:%G %a' "$config_root")" = 'root:root 700'
  test "$(sudo stat -c '%U:%G %a' "$secret_root")" = 'root:root 700'
  test "$(sudo stat -c '%U:%G %a' "$config_root/manager.env")" = 'root:root 600'
  for name in "${secret_names[@]}"; do
    test "$(sudo stat -c '%U:%G %a' "$secret_root/$name")" = 'root:root 600'
  done
  echo 'Manager source secret permissions: PASS (REMEDIATED; initial result remains FAIL)'
)
```

보정 뒤 성공은 `REMEDIATED` 상태일 뿐 최초 설치 검사를 소급해 PASS로 바꾸지 않는다. 보정 명령과
재검증 결과를 모두 증적에 남기고, 원인을 해결한 새 설치/upgrade run에서 최초 검사가 바로
통과해야 clean-host PASS로 기록한다. 두 directory는 `root:root 700`, `manager.env`와 각 source
secret은 `root:root 600`이어야 한다.

`manager-compose`는 `up|start|restart|run|create` 전에 network 없는 root one-shot
`manager-secrets-init`을 실행한다. 이 one-shot은 source secret을 서비스에 직접 mount하지
않고 다음 배치로 새 generation을 완성한 뒤 `current` symlink를 원자 교체한다.

- API/migration: UID/GID `10001:10001`, mode `0400`; DB, Redis, Manager S3,
  Worker bootstrap/pepper, JWT만 포함
- maintenance RQ: UID/GID `10001:10001`, mode `0400`; 전용 maintenance DB, Redis,
  staging-delete S3 credential만 포함
- MLflow: UID/GID `10002:10002`, mode `0400`; MLflow DB와 MLflow S3만 포함
- database-authz: UID/GID `10001:10001`, mode `0400`; main DB와 maintenance DB password만 포함

Host source secret을 `10001`/`10002`로 `chown`하거나 동일 volume을 네 profile에 공유하지 않는다.
투영이 실패하면 이전 generation을 보존하고 종속 service 시작을 차단하므로, raw
`docker compose restart`로 우회하지 말고 반드시 설치된 `manager-compose`를 사용한다.

#### MinIO bucket·policy 경계

`minio-init`은 `S3_BUCKET`(기본 `rvc-orchestrator`)과 `MLFLOW_S3_BUCKET`(기본
`rvc-mlflow`)을 만들고 서로 다른 service user에 다음 exact policy만 연결한다.

- `rvc-manager-app`: Manager bucket의 location/list/multipart와 object get/put/delete만 허용
- `rvc-mlflow-artifacts`: MLflow bucket의 동일 필요 작업만 허용
- `rvc-maintenance-staging-cleanup`: Manager bucket의 `datasets/staging/*`와
  `test-sets/staging/*` `DeleteObject`만 허용

매 시작의 init은 기존 `readwrite|readonly|writeonly|consoleAdmin|diagnostics` 첨부를 제거하고
해당 user의 policy 목록이 정확히 하나인지 검증한다. 403을 임시로 해결하려고 built-in
`readwrite`를 다시 붙이지 않는다. 예상하지 않은 추가 custom policy가 있으면 init이 fail-closed하므로
원인과 audit 기록을 확인한 뒤 정책을 정리한다. 기존 data가 있는 호스트에서 bucket 이름을
바꾸는 것은 rename이 아니라 새 bucket·policy를 만드는 변경이므로 별도 migration 없이 수행하지 않는다.

Maintenance RQ가 staging을 list/read/write하거나 canonical/MLflow object를 삭제할 수 있으면
설치를 실패로 판정한다. Manager/MLflow bucket versioning이 활성화돼 delete marker만 생성될 수
있는 상태도 init이 거부한다. Migration 뒤 `maintenance-db-authz`가 exact PostgreSQL column/function
ACL을 적용하고 RQ entrypoint가 maintenance login으로 `verify-runtime`을 통과해야 한다. Redis도
`rvc_maintenance` ACL user의 exact queue/job/worker/scheduler/result key와 필요한 RQ command만
허용한다. 이 three-store initializer 중 하나라도 실패하면 raw Compose로 우회하지 않는다.

`WORKER_TELEMETRY_JSON_MAX_BYTES`는 Worker status/log/metric 각각의 Content-Length와 실제
chunked raw body를 인증·JSON parsing 전에 제한한다. 기본 2 MiB는 Worker spool의 record 상한과
맞춘 값이다. Reverse proxy에도 같거나 더 작은 client-body 상한을 적용할 수 있지만, API 값을
늘리기 전에는 Worker per-record/spool quota와 memory·DoS 영향을 함께 검토한다. NaN/Infinity는
상한 안이어도 strict JSON 오류로 거부된다.

외부 TLS proxy와 DNS를 구성한 뒤 Compose 렌더링을 검증한다.

```bash
sudo /opt/rvc-orchestrator/manager/bin/manager-compose config --quiet
```

### 4.3 서비스 시작

```bash
(
  set -Eeuo pipefail
  sudo systemctl daemon-reload
  sudo systemctl enable rvc-orchestrator-manager.service
  sudo systemctl restart rvc-orchestrator-manager.service
)
```

초기 기동은 image pull과 migration 때문에 시간이 걸릴 수 있다. 상태와 로그는 다음으로 본다.

```bash
sudo systemctl status rvc-orchestrator-manager.service --no-pager
sudo /opt/rvc-orchestrator/manager/bin/manager-compose ps -a
sudo /opt/rvc-orchestrator/manager/bin/manager-compose logs --tail=200
```

정상 기준은 다음과 같다.

- `postgres`, `redis`, `minio`, `mlflow`, `api`, `web`, `proxy`: `running (healthy)`
- `rq-worker`: `running` — 이 service에는 Compose healthcheck가 없다.
- `manager-secrets-init`, `minio-init`, `api-migrate`, `artifact-spool-init`,
  `dataset-ingestion-init`: 성공 종료(0)
- `/readyz`: `database`, `redis`, `rq_worker`, `maintenance_reconciler`가 `ok`
- 정상 기준에서는 `mlflow=ok`; fail-open 설정 중 장애라면 상태를 별도로 기록한다.
- `mlflow`는 UID/GID `10002:10002`, read-only rootfs, capability drop과 PID 128로 실행된다.
- `minio-init` log에 `MinIO buckets and service users are ready`가 보이고 두 service user가
  서로의 bucket에 접근하지 못한다.

```bash
(
  set -Eeuo pipefail
  curl --fail --silent --show-error https://manager.example.com/healthz
  curl --fail --silent --show-error https://manager.example.com/readyz \
    | python3 -m json.tool
  sudo /opt/rvc-orchestrator/manager/bin/manager-compose logs --tail=100 \
    manager-secrets-init minio-init
  sudo /opt/rvc-orchestrator/manager/bin/manager-compose exec -T api \
    sh -ec 'test "$(id -u):$(id -g)" = 10001:10001; test "$(stat -Lc "%u:%g %a" /run/secrets/current/jwt_secret)" = "10001:10001 400"'
  sudo /opt/rvc-orchestrator/manager/bin/manager-compose exec -T rq-worker \
    sh -ec 'test "$(id -u):$(id -g)" = 10001:10001; test "$(stat -Lc "%u:%g %a" /run/secrets/current/minio_app_secret_key)" = "10001:10001 400"; test ! -e /run/secrets/current/jwt_secret; test ! -e /run/secrets/current/worker_bootstrap_token'
  sudo /opt/rvc-orchestrator/manager/bin/manager-compose exec -T web \
    sh -ec 'test "$(id -u):$(id -g)" = 1001:1001'
  sudo /opt/rvc-orchestrator/manager/bin/manager-compose exec -T mlflow \
    sh -ec 'test "$(id -u):$(id -g)" = 10002:10002; test "$(stat -Lc "%u:%g %a" /run/secrets/current/mlflow_s3_secret_key)" = "10002:10002 400"; test ! -e /run/secrets/current/minio_app_secret_key; test ! -w /home/rvc-mlflow'
  sudo docker volume ls \
    --filter label=org.rvc-orchestrator.component=manager-sensitive-runtime
)
```

위 명령은 secret의 소유자·mode·역할별 존재 여부만 검사하고 내용은 출력하지 않는다.
API와 RQ가 같은 UID를 쓰더라도 서로 다른 volume inventory를 받아야 한다. MLflow
home write 거부는 read-only rootfs 증거이며, mode `0700`·UID-owned `/tmp` tmpfs write와
`/health=OK`는 앞의 `make test-mlflow-docker` 또는 동등한 clean-host smoke로 별도 확인한다.

systemd unit은 `oneshot + RemainAfterExit`이므로 `active (exited)`만으로 합격시키지 않는다.
Compose 상태와 `/readyz`를 반드시 함께 확인한다.

### 4.4 최초 관리자 생성

비밀번호 파일은 절대 경로의 regular non-symlink 파일이어야 하며 group/other 권한이 없어야
한다. Bootstrap API의 하한은 12자지만 이후 관리자 사용자 lifecycle 정책과 일관되게
16~1,024자로 준비한다.

```bash
(
  set -Eeuo pipefail
  sudo install -m 0600 /dev/null /root/rvc-admin-password
  sudoedit /root/rvc-admin-password
  sudo /opt/rvc-orchestrator/manager/bin/bootstrap-admin \
    --email admin@example.com \
    --password-file /root/rvc-admin-password
)
```

명령은 비밀번호 파일을 Manager 저장소에 복사하지 않는다. bootstrap 완료 뒤 파일은 조직의
secret 관리 정책에 따라 안전하게 폐기하거나 보관한다. 출력이나 테스트 증적에 내용을 남기지
않는다.

### 4.5 사용자 계정 생성과 권한 관리

최초 관리자로 로그인한 뒤 좌측 `사용자` 메뉴에서 계정을 관리한다.

1. 이메일, 16~1,024자의 초기 비밀번호와 `사용자` 또는 `관리자` 역할을 입력한다. 비밀번호에는
   제어문자를 넣지 않고 최소 8개 서로 다른 문자를 사용하며 이메일 local-part와 알려진 약한
   passphrase를 포함하지 않는다.
2. 일반 계정은 기본 활성 상태로 생성하고, 필요할 때 역할·활성 상태를 선택한 뒤 `변경 저장`을
   누른다.
3. 비밀번호 재설정은 대상 행의 `재설정`을 사용한다. 성공 즉시 대상 사용자의 기존 로그인
   token이 모두 무효화되므로 다시 로그인해야 한다.
4. 현재 로그인한 관리자는 자기 역할을 낮추거나 계정을 비활성화할 수 없다. 최소 한 명의 활성
   관리자를 항상 유지한다.

관리자 생성·변경·비밀번호 재설정 요청은 멱등 키와 row version으로 보호된다. “실행 결과가
불명확”하다는 경고가 나오면 같은 버튼을 반복 누르지 말고 목록을 새로고침해 실제 상태를 먼저
확인한다. 비밀번호나 session cookie를 screenshot, 브라우저 console 또는 운영 log에 남기지 않는다.

### 4.6 Model Registry 설치 상태 확인

dev.20 Manager migration head `f5d1c8a9b240`이 적용되면 Experiment 상세의 비교 영역 아래에
`Model Registry`가 보인다. 신규 Experiment의 정상 초기 상태는 registry version `0`, 현재
Champion 없음, 빈 후보/승인/폐기 목록이다. 화면이 보인다는 사실만으로 실제 모델 승인 경로가
합격한 것은 아니다.

후보 등록은 exact current `completed` real `rvc_webui` attempt와 `worker-claim-v1`, reviewed RVC
commit, 승인된 runtime image/asset digest, Manager가 검증한 유일한 final small model과 선택적인
동일 attempt index가 모두 있어야 열린다. Manager는 후보 등록과 promotion마다 canonical object
전체를 size/SHA-256으로 다시 읽는다. 따라서 Fake 결과, migration 전 provenance NULL attempt,
미승인 runtime과 변조된 object가 거부되는 것은 정상이다. 현재 dev.20 Worker archive는 runtime과
GPU qualification이 없는 partial이므로 이 archive만으로 eligible candidate를 새로 만들 수 없다.
UI/빈 원장 확인까지만 수행하고 실제 후보·Champion·rollback 시험은 검증된 self-contained Worker와
object storage가 준비될 때 `docs/TEST_GUIDE.md` 6.3절 기준으로 별도 판정한다.

상태 변경 중 응답 유실 또는 stale row-version 경고가 나오면 새 요청으로 반복하지 않는다. 페이지
전체를 다시 불러 실제 candidate/Champion/revoked 상태를 확인한 뒤에만 다음 명시적 작업을 시작한다.
MLflow run/tag를 직접 바꾸어 Registry를 우회하거나 storage URI/object key를 증적에 기록하지 않는다.

## 5. Worker dev.20 설치기/구성 시험

이 절은 실제 학습 설치가 아니다. dev.20 Worker는 runtime image가 없으므로 `native`를 선택하면
정상적으로 거부되어야 한다. 기본 `profile`도 generic image에 `/opt/rvc-webui`가 없고 검증된
profile/repository가 필요하므로 사용할 수 없다.

### 5.1 구성 시험용 token 파일 준비

이 절의 fake/no-start 구성 시험에는 실제 Manager bootstrap token을 전달하지 않는다. 폐기 가능한
합성 문자열을 mode `0600` 파일에 넣고, 시험 종료 뒤 실제 Worker credential로 재사용하지 않는다.
설치기는 secret 파일의 안전한 복사와 권한만 검사하며 service는 시작하지 않는다.

```bash
(
  set -Eeuo pipefail
  sudo install -m 0600 /dev/null /root/worker-config-only-token
  sudoedit /root/worker-config-only-token
)
```

파일에는 예를 들어 `CONFIG_ONLY_DO_NOT_USE`처럼 실제 Manager에서 유효하지 않은 값을 넣는다.
실제 runtime 포함 Worker를 등록할 때에만 Manager의
`/etc/rvc-orchestrator/manager/secrets/worker_bootstrap_token`을 조직의 secret 전달 수단으로
Worker의 별도 mode `0600` 파일에 전달한다. 값을 화면, shell history, 메신저나 이슈에 붙이지 않는다.

### 5.2 `--no-start` 설치

Worker bundle 디렉터리에서 실행한다. 실제 NVIDIA 호스트에서는 `--skip-gpu-check`를 빼고
실행한다. GPU가 없는 일회용 VM에서 구성만 검사할 때에만 이 옵션을 사용한다.

```bash
(
  set -Eeuo pipefail
  sudo ./preflight.sh --skip-gpu-check
  sudo ./install.sh \
    --manager-url https://manager.example.com \
    --worker-name gpu-01 \
    --token-file /root/worker-config-only-token \
    --runner-mode fake \
    --allow-fake-dev \
    --skip-gpu-check \
    --no-start

  WORKER_RELEASE=$(sudo readlink -f /opt/rvc-orchestrator/worker/current)
  case "$WORKER_RELEASE" in
    /opt/rvc-orchestrator/worker/releases/*) ;;
    *) echo "Worker current resolves outside releases" >&2; exit 1 ;;
  esac
  sudo stat -c '%U:%G %a %n' \
    "$WORKER_RELEASE/RELEASE_SHA256SUMS"
  sudo python3 /opt/rvc-orchestrator/worker/lib/image_bundle.py \
    verify-ledger \
    --root "$WORKER_RELEASE" \
    --ledger-name RELEASE_SHA256SUMS
)
```

설치 결과를 확인한다.

```bash
(
  set -Eeuo pipefail
  sudo /opt/rvc-orchestrator/worker/bin/worker-compose config --quiet
  sudo readlink -f /opt/rvc-orchestrator/worker/current
  sudo stat -c '%u:%g %a %n' \
    /var/lib/rvc-orchestrator/worker \
    /etc/rvc-orchestrator/worker/secrets/worker_token \
    /etc/rvc-orchestrator/worker/rvc-profile.yaml
)
```

기본 root 설치에서는 data directory가 `10001:10001 700`, token/profile이
`10001:10001 600`이어야 한다. 이 구성의 systemd service를 시작하지 않는다. generic Agent image를
별도로 build하더라도 Fake Worker는 production Manager에 등록할 수 없고 RVC 학습도 하지 않는다.
Fake protocol 검증은 source tree에서 `make test-e2e`로 수행한다.

Worker runtime의 mount 경계는 다음과 같다.

- host `/var/lib/rvc-orchestrator/worker` → container `/var/lib/rvc-worker`: UID/GID
  `10001:10001`이 쓰는 유일한 Job workspace/spool 경로
- host `rvc-profile.yaml` → container `/etc/rvc-worker/rvc-profile.yaml:ro`
- 설치된 release의 `runtime-activation.json` →
  `/run/rvc-release/runtime-activation.json:ro`; installer가 mode `0444`와 bundle byte 일치를 다시 검증
- host `worker_token` → Docker secret `/run/secrets/worker_token`; 환경 변수나 profile에 token 원문을
  복사하지 않음

Manager source secret은 root 소유를 유지하고 역할별 volume으로 투영하지만, Worker token/profile은
root-only config directory 안에서 실제 container user `10001:10001`이 읽을 수 있도록 해당
소유자와 mode `0600`을 사용한다. 두 정책을 서로 바꾸지 않는다. Data directory, token,
profile이 다른 UID이거나 activation이 쓰기 가능하면 Worker를 시작하지 않는다.

실제 runtime 후보의 `worker.env`는 `SYSTEM_TELEMETRY_INTERVAL_SECONDS=60`을 기본으로 사용한다.
허용 범위는 10~3,600초다. Heartbeat 주기와 독립적으로 Job 시작 직후 한 번, 이후 이 간격으로
GPU/disk를 spool한다. 값을 지나치게 줄이면 GPU 수에 비례해 Metric/MLflow row가 증가하므로 용량
시험 없이 60초보다 낮추지 않는다.

### 5.3 dev.20 사설 CA 준비와 구성 확인

dev.20 bundle에 `--ca-bundle-file`과 `common/worker_ca.py`가 있는지 확인한 뒤 사용한다. 조직에서 전달받은
CA certificate chain을 신뢰된 임시 위치에서 production source로 복사한다. CA는 공개 정보일 수
있지만 trust anchor이므로 root만 교체할 수 있게 한다.

```bash
(
  set -Eeuo pipefail
  sudo install -o root -g root -m 0644 \
    /trusted-transfer/organization-ca-chain.pem \
    /root/rvc-worker-custom-ca.pem
  sudo stat -c '%u:%g %a %s %n' /root/rvc-worker-custom-ca.pem
  sudo python3 common/worker_ca.py validate \
    --path /root/rvc-worker-custom-ca.pem \
    --required-uid 0
)
```

출력은 `0:0 644`, 1..1,048,576 byte여야 한다. Source가 symlink이거나 mode `0600|0664`, private
key/NUL/non-ASCII/invalid PEM을 포함하면 고쳐 쓰지 말고 승인된 certificate-only 원본을 다시
받는다. Validator 실패를 무시하고 install을 진행하지 않는다.

첫 설치의 기존 `install.sh` 명령에 다음 option을 추가한다.

```bash
--ca-bundle-file /root/rvc-worker-custom-ca.pem
```

설치 후 service를 시작하기 전에 다음을 확인한다.

```bash
(
  set -Eeuo pipefail
  sudo stat -c '%u:%g %a %n' \
    /etc/rvc-orchestrator/worker/ca \
    /etc/rvc-orchestrator/worker/ca/custom-ca.pem
  sudo python3 /opt/rvc-orchestrator/worker/lib/worker_ca.py validate \
    --path /etc/rvc-orchestrator/worker/ca/custom-ca.pem \
    --required-uid 0
  sudo awk -F= '
    $1 == "WORKER_CA_BUNDLE_HOST_DIR" ||
    $1 == "WORKER_CA_BUNDLE_PATH" {print}
  ' /etc/rvc-orchestrator/worker/worker.env
  sudo /opt/rvc-orchestrator/worker/bin/worker-compose config --quiet
)
```

Production root install에서 directory는 `0:0 755`, file은 `0:0 444`여야 한다. Environment는
host directory `/etc/rvc-orchestrator/worker/ca`와 container fixed path
`/etc/rvc-worker/ca/custom-ca.pem`을 각각 정확히 한 번 가져야 한다. Installed wrapper는
`up|start|restart|run|create`마다 release/env 검증에 이어 directory/path/owner/mode/PEM을 다시
검사한다. Public CA 환경은 option을 생략하고 `WORKER_CA_BUNDLE_PATH=`가 비어 있으며 host
`custom-ca.pem`도 없어야 한다.

재설치/upgrade에서 option을 생략하면 기존 CA byte와 활성 path를 보존한다. Replacement를 전달하면
새 byte를 staging/prevalidate한 뒤 environment/release와 함께 전환하며, target Compose나 activation
실패 시 이전 byte를 복구한다. 제거를 위해 파일이나 env 한쪽만 수동 삭제하지 않는다. 일반
uninstall도 config와 CA를 보존한다.

## 6. 실제 native Worker를 설치하기 위한 추가 단계

실제 GPU 학습에는 아래 입력으로 만든 **runtime 포함 self-contained Worker bundle**이 필요하다.

- reviewed RVC commit `7ef19867780cf703841ebafb565a4e47d1ea86ff` source archive/manifest
- Python 3.11 linux/amd64 전체 hashed wheelhouse와 exact fairseq commit
- HuBERT, RMVPE, v1/v2 40k/48k pretrained/mute, CREPE, FFmpeg 자산과 출처·라이선스·SHA manifest
- 검토한 `pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:<digest>` amd64 base
- clean committed source tree와 runtime build manifest

Self-contained bundle builder는 현재 working tree를 archive에 복사하지 않는다. Infra, installer,
검증기, 문서와 supply-chain 입력은 선언한 clean 40-hex commit의 exact Git export에서 가져오며,
runtime build manifest는 qualification 유무와 관계없이 exact schema, release version,
orchestrator commit과 reviewed runtime provenance를 통과해야 한다. Disabled activation도 extracted
archive에서 mode `0444`여야 한다. 이 검증 실패를 manifest 편집이나 chmod 사후 보정으로 우회하지
않고 새 clean source와 새 release version으로 다시 만든다.

먼저 Docker/GPU 없이 입력만 검증할 수 있다.

```bash
infra/worker/runtime/build-runtime-image.sh \
  --source-archive /offline/source/rvc-source.tar.gz \
  --source-manifest /offline/source/source-manifest.json \
  --wheelhouse /offline/wheelhouse \
  --assets /offline/assets \
  --verify-only
```

검토한 base digest를 로컬에 미리 load한 뒤 1단계 core factory를 실행한다. 이 factory만 runtime
image를 만들며 qualification 입력을 받지 않는다.

```bash
export RELEASE_VERSION=0.1.0-rc.1
export REVIEWED_BASE_IMAGE='pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime@sha256:REPLACE_WITH_REVIEWED_AMD64_DIGEST'
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
  --base-image "$REVIEWED_BASE_IMAGE" \
  --output-dir "$CORE_OUTPUT_DIR"
```

`REPLACE_WITH_REVIEWED_AMD64_DIGEST`를 실제 64자리 digest로 바꾸지 않은 명령은 의도적으로
실패해야 한다. 예시 release version도 조직의 실제 후보 version으로 바꾼다. Factory는 clean 40-hex
source, release source closure와 amd64 Docker daemon을 요구한다. Exact image/build manifest와 false
pre-qualification gate를 검증하고 bundle을 private directory에서 다시 검사한 뒤에만 no-clobber로
게시한다.

1단계 결과는 세 activation gate가 false인 `NATIVE-CANDIDATE-UNVERIFIED` core 후보이며 public
release가 아니다. 다음처럼 외부 checksum, 내부 ledger/bundle closure를 검증해 private directory에
추출하고 Docker ID와 archive runtime ID를 하나의 handoff 값으로 고정한다.

```bash
(
  set -Eeuo pipefail
  CORE_ARCHIVE="$CORE_OUTPUT_DIR/rvc-worker-$RELEASE_VERSION-linux-amd64.tar.gz"
  CORE_EXTRACT_ROOT=/offline/candidates/core-extracted
  test -f "$CORE_ARCHIVE" && test ! -L "$CORE_ARCHIVE"
  test -f "$CORE_ARCHIVE.sha256" && test ! -L "$CORE_ARCHIVE.sha256"
  (cd "$CORE_OUTPUT_DIR" && sha256sum -c "$(basename "$CORE_ARCHIVE.sha256")")
  test ! -e "$CORE_EXTRACT_ROOT"
  install -d -m 0700 "$CORE_EXTRACT_ROOT"
  tar -xzf "$CORE_ARCHIVE" -C "$CORE_EXTRACT_ROOT"

  CORE_BUNDLE="$CORE_EXTRACT_ROOT/rvc-worker-$RELEASE_VERSION-linux-amd64"
  cd "$CORE_BUNDLE"
  sha256sum -c SHA256SUMS
  python3 common/image_bundle.py verify-ledger --root . --ledger-name SHA256SUMS
  SOURCE_COMMIT=$(awk -F= '$1 == "GIT_COMMIT" {print $2; exit}' manifest.env)
  python3 common/image_bundle.py verify-bundle \
    --root . \
    --component worker \
    --version "$RELEASE_VERSION" \
    --source-commit "$SOURCE_COMMIT"

  CORE_IMAGE_ID=$(docker image inspect --format '{{.Id}}' \
    "rvc-orchestrator-worker:$RELEASE_VERSION")
  CORE_MANIFEST_IMAGE_ID=$(python3 -c '
import json, pathlib, sys
data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
images = data["images"]
assert len(images) == 1 and images[0]["role"] == "runtime"
print(images[0]["image_id"])
' images-manifest.json)
  test "$CORE_IMAGE_ID" = "$CORE_MANIFEST_IMAGE_ID"
  test -f runtime/build-manifest.env && test ! -L runtime/build-manifest.env
)
```

검증된 core archive의 `images-manifest.json`이 이후 handoff의 권위 원장이다. 모든 49-case report와
`runtime-qualification.json`은 그 exact ID를 사용해야 한다. Tag를 rebuild/retag한 뒤 같은 이름으로
증적을 재사용하지 않는다. 49-case 수행·case ID·evidence schema는 이 가이드 8절과
[Worker runtime qualification](RUNTIME_QUALIFICATION.md)을 따른다.

GPU/no-network 49-case와 reviewer evidence가 준비되면 read-only readiness report로
source/wheel/asset/build/runtime/qualification/review의 누락과 identity 불일치를 한 번에 열거한다.

```bash
export CORE_BUNDLE=/offline/candidates/core-extracted/rvc-worker-$RELEASE_VERSION-linux-amd64
export CORE_BUILD_MANIFEST="$CORE_BUNDLE/runtime/build-manifest.env"
CORE_IMAGE_ID=$(python3 -c '
import json, pathlib, sys
data = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
images = data["images"]
assert len(images) == 1 and images[0]["role"] == "runtime"
print(images[0]["image_id"])
' "$CORE_BUNDLE/images-manifest.json")
export CORE_IMAGE_ID
CURRENT_CORE_IMAGE_ID=$(docker image inspect --format '{{.Id}}' \
  "rvc-orchestrator-worker:$RELEASE_VERSION")
test "$CURRENT_CORE_IMAGE_ID" = "$CORE_IMAGE_ID"

python3 infra/worker/runtime/release_readiness.py \
  --source-manifest /offline/source/source-manifest.json \
  --source-archive /offline/source/rvc-source.tar.gz \
  --wheelhouse-manifest /offline/wheelhouse/wheelhouse-manifest.json \
  --wheelhouse-root /offline/wheelhouse \
  --asset-manifest /offline/assets/assets-manifest.json \
  --asset-root /offline/assets \
  --runtime-build-manifest "$CORE_BUILD_MANIFEST" \
  --runtime-image-digest "$CORE_IMAGE_ID" \
  --qualification-manifest /offline/review/runtime-qualification.json \
  --qualification-evidence /offline/review/runtime-evidence.tar.gz \
  --release-review /offline/review/release-review.json \
  --review-evidence-root /offline/review \
  --output /offline/review/worker-release-readiness.json
```

Exit code `0`은 열거한 source/wheel/asset/build/runtime/49-case/review evidence의 구조, byte hash와
identity binding이 맞다는 뜻만 가진다. `1`은 report에 `missing`, `invalid` 또는
`blocked-dependency`가 있음을 뜻하고, `2`는 CLI/output publication 오류다. 이 도구는 Docker/network,
scan 실행 또는 법적 판단을 하지 않고 activation을 만들지도 않는다. Exit `0`이어도 report의
`activation_permitted=false`, `activation_projection_written=false`가 유지되는 것이 정상이며 실제
builder/qualification/start gate를 대신하지 않는다.

Readiness 입력 검증과 release review가 끝난 뒤에만 2단계 qualified factory를 실행한다. 이 명령은
기존 exact image, extracted core build manifest, 원래 asset과 qualification/evidence를 재검증하고
새 image를 build/pull/retag/remove하지 않는다.

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

Core와 qualified archive basename은 모두
`rvc-worker-$RELEASE_VERSION-linux-amd64.tar.gz`이다. `CORE_OUTPUT_DIR`와
`QUALIFIED_OUTPUT_DIR`은 반드시 서로 달라야 하며 기존 archive/sidecar를 삭제하거나 덮어쓰지
않는다. Core는 qualification 입력 원장으로 계속 보존한다. Qualified factory 성공도 production
승인은 아니다. Vulnerability/container/secret scan, SBOM·license/redistribution 검토, 별도 reviewer
attestation, clean Ubuntu install/reboot/upgrade와 실제 Manager/Object 외부 TLS/browser gate를 모두
통과해야 한다.

Schema, report ID와 증적 수집 규칙은 [Worker runtime qualification](RUNTIME_QUALIFICATION.md)을
따른다. Builder가 activation을 직접 생성하므로 activation JSON이나 verified boolean을 입력으로
전달하지 않는다. 증적이 없으면 core bundle의 disabled projection과 세 false gate를 유지하고
qualified factory를 실행하지 않는다.

구체 manifest schema와 입력 목록은 `infra/worker/runtime/README.md`를 따른다. 현재 저장소에는
승인된 실제 base digest와 재배포 가능한 전체 자산 byte가 없으므로 위 placeholder를 임의 값으로
채워 release라고 선언하면 안 된다.

이 6절의 여기까지는 **미래의 self-contained 후보를 만드는 release build host 절차**다. dev.20
archive에는 정확한 committed source 기준선이 있지만 Worker runtime image와 승인된 base digest,
재배포 가능한 전체 asset/wheel 및 qualification 증적이 없다. 따라서 clean-source gate를 통과할 수
있다는 사실만으로 위 placeholder를 채우거나 dev.20 tag를 재사용해 runtime 후보를 만들지 않는다.
또한 5절의 fake/no-start 구성을 설치한 호스트에서 runner mode만 `native`로 바꾸는 upgrade는
설치기가 거부한다. Native
후보는 clean Worker 호스트에 새로 설치하거나, service 중지·credential/data 보존·설정 마이그레이션을
별도 검토한 뒤 진행한다.

Release build host에서는 생성된 archive와 sidecar가 실제로 생겼고 외부 checksum이 맞는지 먼저
확인한다. Clean-host qualification engineering이면 core directory를 명시하되 public release로
전달하지 않는다. Qualified acceptance이면 별도 qualified directory를 명시한다. 아래 예시는
2단계 결과를 선택하며 core archive를 덮어쓰거나 같은 directory로 합치지 않는다.

```bash
(
  set -Eeuo pipefail
  : "${RELEASE_VERSION:?set RELEASE_VERSION to the candidate version}"
  : "${QUALIFIED_OUTPUT_DIR:?set the separate qualified output directory}"
  WORKER_ARCHIVE="$QUALIFIED_OUTPUT_DIR/rvc-worker-$RELEASE_VERSION-linux-amd64.tar.gz"
  test -f "$WORKER_ARCHIVE" && test ! -L "$WORKER_ARCHIVE"
  test -f "$WORKER_ARCHIVE.sha256" && test ! -L "$WORKER_ARCHIVE.sha256"
  (cd "$QUALIFIED_OUTPUT_DIR" && sha256sum -c "$(basename "$WORKER_ARCHIVE.sha256")")
)
```

Qualification과 남은 release gate가 끝나기 전에는 이 pair도 production 설치 파일로 배포하지
않는다. Clean Worker acceptance host에는 선택한 candidate의 두 파일만 신뢰된 전송 수단으로
전달한다.

### Clean Worker host에서 archive 검증과 `--no-start` 설치

Clean Worker host의 두 파일이 있는 디렉터리에서 version을 다시 지정하고, image를 load하기 전에
archive와 extracted bundle을 검증한다. 이 시점에는 `docker image inspect`가 실패할 수 있으며
정상이다. Self-contained image load와 load 뒤 identity 검증은 installer가 수행한다.

```bash
export RELEASE_VERSION=0.1.0-rc.1
(
  set -Eeuo pipefail
  WORKER_ARCHIVE="rvc-worker-$RELEASE_VERSION-linux-amd64.tar.gz"
  EXTRACT_ROOT="$PWD/rvc-worker-$RELEASE_VERSION-verified"
  test -f "$WORKER_ARCHIVE" && test ! -L "$WORKER_ARCHIVE"
  test -f "$WORKER_ARCHIVE.sha256" && test ! -L "$WORKER_ARCHIVE.sha256"
  sha256sum -c "$WORKER_ARCHIVE.sha256"
  test ! -e "$EXTRACT_ROOT"
  install -d -m 0700 "$EXTRACT_ROOT"
  tar -xzf "$WORKER_ARCHIVE" -C "$EXTRACT_ROOT"
  cd "$EXTRACT_ROOT/rvc-worker-$RELEASE_VERSION-linux-amd64"

  sha256sum -c SHA256SUMS
  python3 common/image_bundle.py verify-ledger \
    --root . \
    --ledger-name SHA256SUMS
  SOURCE_COMMIT=$(awk -F= '$1 == "GIT_COMMIT" {print $2; exit}' manifest.env)
  python3 common/image_bundle.py verify-bundle \
    --root . \
    --component worker \
    --version "$RELEASE_VERSION" \
    --source-commit "$SOURCE_COMMIT"
  stat -c '%a %n' infra/worker/runtime/runtime-activation.json
  python3 -m json.tool infra/worker/runtime/runtime-activation.json
)
```

Activation mode는 `444`여야 한다. JSON의 disabled 또는 fully-qualified 상태가
`manifest.env`, runtime image/build/asset과 qualification evidence에 맞아야 하며, source tree의
동명 파일을 대신 검사하면 안 된다. 검증이 끝난 **이 extracted bundle directory 안에서** preflight와
설치를 실행한다. Fully-qualified 후보는 첫 번째 명령을 사용한다. Runtime image의 default public
CA trust가 아닌 조직 사설 CA가 필요하면 5.3절의 source 검증을 먼저 끝내고 dev.20 이후 후보의
각 `install.sh` 명령에 `--ca-bundle-file /root/rvc-worker-custom-ca.pem`을 추가한다.

```bash
(
  set -Eeuo pipefail
  cd "rvc-worker-$RELEASE_VERSION-verified/rvc-worker-$RELEASE_VERSION-linux-amd64"
  sudo ./preflight.sh
  sudo ./install.sh \
    --manager-url https://manager.example.com \
    --worker-name gpu-01 \
    --token-file /root/worker-bootstrap-token \
    --runner-mode native \
    --no-start
)
```

Core runtime은 포함됐지만 `RVC_GPU_SMOKE_VERIFIED=false`인 승인된 release-engineering 후보만
아래처럼 명시적 위험 확인을 추가한다. 이 결과는 `NATIVE-CANDIDATE-UNVERIFIED`이며 production
합격이 아니다.

```bash
(
  set -Eeuo pipefail
  cd "rvc-worker-$RELEASE_VERSION-verified/rvc-worker-$RELEASE_VERSION-linux-amd64"
  sudo ./install.sh \
    --manager-url https://manager.example.com \
    --worker-name gpu-01 \
    --token-file /root/worker-bootstrap-token \
    --runner-mode native \
    --allow-unverified-gpu-runtime \
    --no-start
)
```

설치가 image archive를 load한 뒤에야 installed release, loaded identity, activation과 Compose를
검증한다. Secret 원문이나 전체 image inspect JSON은 출력하지 않는다.

```bash
(
  set -Eeuo pipefail
  : "${RELEASE_VERSION:?set RELEASE_VERSION to the verified bundle version}"
  WORKER_ROOT=/opt/rvc-orchestrator/worker
  INSTALLED_RELEASE=$(sudo readlink -f "$WORKER_ROOT/current")
  case "$INSTALLED_RELEASE" in
    "$WORKER_ROOT"/releases/*) ;;
    *) echo "Worker current resolves outside releases" >&2; exit 1 ;;
  esac
  INSTALLED_SOURCE_COMMIT=$(sudo awk -F= \
    '$1 == "GIT_COMMIT" {print $2; exit}' \
    "$INSTALLED_RELEASE/manifest.env")

  sudo python3 "$WORKER_ROOT/lib/image_bundle.py" verify-ledger \
    --root "$INSTALLED_RELEASE" \
    --ledger-name RELEASE_SHA256SUMS
  sudo python3 "$WORKER_ROOT/lib/image_bundle.py" verify-loaded \
    --root "$INSTALLED_RELEASE" \
    --component worker \
    --version "$RELEASE_VERSION" \
    --source-commit "$INSTALLED_SOURCE_COMMIT"
  sudo stat -c '%U:%G %a %n' \
    "$INSTALLED_RELEASE/infra/worker/runtime/runtime-activation.json"
  sudo python3 -m json.tool \
    "$INSTALLED_RELEASE/infra/worker/runtime/runtime-activation.json"
  sudo "$WORKER_ROOT/bin/worker-compose" config --quiet

  WORKER_IMAGE=$(sudo awk -F= '$1 == "WORKER_IMAGE" {print $2; exit}' \
    /etc/rvc-orchestrator/worker/worker.env)
  sudo docker image inspect --format \
    'id={{.Id}} architecture={{.Architecture}} user={{.Config.User}} version={{index .Config.Labels "org.opencontainers.image.version"}} revision={{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "$WORKER_IMAGE"
)
```

Verifier는 exact manifest/digest가 load된 것을 확인해야 하고 activation은 `root:root 444`, image는
`linux/amd64`, user `10001:10001`, 예상 version/committed revision이어야 한다. 이 정적 검사
뒤에도 GPU host에서 아래 one-shot, CUDA/cuDNN/Torch import와 RVC stage matrix를 따로 실행해야
한다. 아래 Compose one-shot은 default Worker network를 사용하므로 no-network 증거가 아니다.
No-network 판정은 테스트 가이드 8.5절의 외부 egress/DNS 차단과 flow 증적으로만 수행한다.

Service를 시작하기 전에 installed wrapper를 통과하는 one-shot health check를 실행한다. Wrapper는
`run` 전에 release ledger, environment, loaded image와 activation을 다시 검증하며 Compose의 실제
GPU/mount 설정을 사용한다.

```bash
(
  set -Eeuo pipefail
  WORKER_ROOT=/opt/rvc-orchestrator/worker
  sudo "$WORKER_ROOT/bin/worker-compose" run --rm --no-deps worker --check \
    | python3 -m json.tool
)
```

Exit code 0과 JSON의 `ok=true`, `settings.runner_mode=native`, 예상 GPU 수, reviewed native revision과
asset-ready 상태를 확인한다. Qualified activation이 아니면
`fixed_test_set_inference_ready=false`와 빈 inference F0 목록이 정상이다. 이 one-shot이 실패하면
systemd를 enable/start하지 않는다.

`--allow-unverified-gpu-runtime`은 검증 완료 표시가 아니라 아직 GPU matrix가 열려 있다는 위험
확인이다. Production factory는 fully-qualified read-only activation에서만 Sample inference
dependency를 주입한다. 현재 제공된 실제 증적이 없으므로 설치 bundle의 activation은 disabled이고
Agent는 `fixed_test_set_inference_ready=false`를 광고한다. 따라서 native core 학습 후보를 시험할
수 있게 되더라도 실제 Sample Job은 아직 합격 대상으로 간주하지 않는다.

위 `--no-start` 검사 전체가 통과한 뒤에만 service를 별도 단계로 시작하고 확인한다.

```bash
(
  set -Eeuo pipefail
  sudo systemctl daemon-reload
  sudo systemctl enable rvc-orchestrator-worker.service
  sudo systemctl restart rvc-orchestrator-worker.service
  sudo systemctl is-active rvc-orchestrator-worker.service
  sudo /opt/rvc-orchestrator/worker/bin/worker-compose ps
  sudo /opt/rvc-orchestrator/worker/bin/worker-compose logs --tail=200 worker
  sudo /opt/rvc-orchestrator/worker/bin/worker-compose exec -T worker \
    python -m rvc_worker --check | python3 -m json.tool
)
```

Manager의 `학습 서버` 화면에서 같은 Worker ID가 `online`, 최근 heartbeat, 예상 GPU inventory와
현재 Job 없음으로 보이는지 확인한다. Worker를 한 번 재부팅한 뒤에도 새 identity를 만들지 않고
같은 ID로 다시 online이 되어야 등록 smoke가 PASS다. Token이나 전체 `worker.env`를 증적에 남기지
않는다.

### 설치 단계 판정표

각 호스트 작업이 끝나면 다음 표에서 수행한 행만 판정한다. 위 단계의 성공이 아래 단계를 자동으로
성공시키지 않는다.

| 판정 이름 | PASS 조건 | dev.20 기본 예상 |
|---|---|---|
| `BUNDLE-INTEGRITY` | 외부 `.sha256`, 내부 `SHA256SUMS`, manifest 검증 성공 | PASS 가능 |
| `MANAGER-CONFIG` | `--no-start` 설치와 `manager-compose config --quiet` 성공 | PASS 가능 |
| `MANAGER-SMOKE` | 포함 image load identity, Compose 상태와 `/readyz` 정상 | PASS 가능; clean Ubuntu/TLS 인수는 별도 |
| `TLS-PRODUCTION` | operator scheme, Secure cookie/HSTS와 외부 TLS 실제 검증 | 코드 PASS, clean browser 증거 대기 |
| `WORKER-CONFIG` | fake/no-start 설치, UID/GID·mode·Compose config 정상 | PASS 가능 |
| `WORKER-NATIVE` | runtime 포함 bundle, 실제 NVIDIA preflight, Manager 등록·학습 성공 | dev.20 Worker는 BLOCKED |
| `AIRGAP-PRODUCTION` | exact committed source/image closure와 no-pull/no-network 증거 | Manager closure는 존재; clean-host/no-network/Worker gate는 BLOCKED |

`MANAGER-SMOKE`만 성공하고 Worker가 없는 경우에는 Job이 `queued`에 머무르는 것이 정상이다.
`WORKER-CONFIG` 성공을 실제 Worker 설치나 RVC 학습 성공으로 기록하지 않는다.

## 7. 설치 후 운영 명령

### 상태와 로그

```bash
sudo /opt/rvc-orchestrator/manager/bin/manager-compose ps -a
sudo /opt/rvc-orchestrator/manager/bin/manager-compose logs --tail=200 api rq-worker

sudo /opt/rvc-orchestrator/worker/bin/worker-compose ps
sudo /opt/rvc-orchestrator/worker/bin/worker-compose logs --tail=200 worker
```

### Manager backup

기본 backup은 cross-store 일관성을 위해 proxy/web/API/RQ/MLflow를 잠시 중지하는 maintenance
작업이다. 먼저 active Job을 drain하고 Dataset/Artifact upload가 `pending|finalizing`에 남지 않은
maintenance window에서 실행한다. 스크립트가 active upload를 발견하면 실패하며, 종료 trap이
서비스 재시작을 시도하더라도 실행 후 `/readyz`를 다시 확인해야 한다.

```bash
sudo /opt/rvc-orchestrator/manager/bin/backup
```

성공하면 `BACKUP_PATH=/var/backups/...`를 출력한다. backup은 PostgreSQL과 object data를 담지만
`/etc/rvc-orchestrator/manager`의 설정/secret은 포함하지 않는다. 이 경로는 별도의 암호화된
secret/config backup 정책으로 보호한다.

### Upgrade

먼저 Manager backup을 만들고 새 archive/checksum/image를 검증한다. 새로 압축 해제한 bundle에서
다음처럼 release를 설치하되 시작은 분리한다.

```bash
(
  set -Eeuo pipefail
  sudo ./upgrade.sh . --no-start --public-scheme https
  sudo /opt/rvc-orchestrator/manager/bin/manager-compose config --quiet
  sudo systemctl daemon-reload
  sudo systemctl restart rvc-orchestrator-manager.service
  sudo /opt/rvc-orchestrator/manager/bin/manager-compose ps -a
  curl --fail --silent --show-error https://manager.example.com/readyz \
    | python3 -m json.tool
)
```

`dev.8` 이하에서 `dev.9`로 Manager를 올리면 기존 JWT에는 새 token-version claim이 없으므로
기존 브라우저 세션은 의도적으로 만료된다. Migration과 readiness가 정상인 것을 확인한 뒤 모든
사용자가 다시 로그인하도록 안내한다.

dev.9에서 dev.10 telemetry schema로 올릴 때는 먼저 active Job을 drain하고 Worker pending spool이
가능한 범위에서 전송됐는지 확인한 뒤 Manager backup을 만든다. `c7b1e4d9a260`은 기존 ingest row의
fingerprint를 추정 backfill하지 않으므로 NULL fingerprint를 가진 과거 idempotency key replay는
fail-closed한다. `ca8d3e7f4b10`은 terminal count를 nullable로 추가해 historical attempt를
보존하지만 watermark 없는 terminal에는 late telemetry를 허용하지 않는다. Manager migration과
readiness를 확인한 뒤 dev.10 Worker를 재시작한다. 구 Worker가 terminal count 없이 종료한 active
Job의 pending batch를 정상 자동 복구로 가정하지 않는다.

dev.10에서 dev.11로 올릴 때 database revision은 `ca8d3e7f4b10` 그대로다. Manager를 먼저
`--public-scheme https --no-start`로 설치해 기존 env에 trusted scheme을 명시하고 Compose config/
readiness를 확인한다. 이후 dev.11 Worker를 재시작하면 Job 시작 직후와 60초 cadence의
system metric, GPU query availability와 typed telemetry persistence 실패가 적용된다. Active Job
중간에 Worker를 교체하지 말고 drain한 뒤 올린다.

dev.11에서 dev.12로 올릴 때도 database revision은 `ca8d3e7f4b10` 그대로다. 하지만
실행 권한과 storage policy가 바뀌므로 단순 tag 교체로 취급하지 않는다. Active Job과
`pending|finalizing` upload를 drain하고 Manager/config-secret backup을 만든 뒤 다음을
확인한다.

1. dev.12 API/Web/MLflow image를 build/load하고 architecture·user·version label을 검증한다.
2. Host source secret을 `root:root 0600`으로 유지한 채 upgrade한다. App UID로
   `chown`하지 않는다.
3. Restart시 `manager-secrets-init`이 네 runtime volume을 새 generation으로 투영하고
   `minio-init`이 Manager/MLflow user의 broad built-in policy를 제거하는지 확인한다.
4. Init/migration exit 0, container UID/secret mode, `/readyz`, login, 기존 Dataset download와
   MLflow projection을 확인한다. 403을 `readwrite` 재첨부로 우회하지 않는다.

dev.12 Worker archive의 runtime/native/GPU/Sample gate는 dev.11처럼 모두 false며 image도 없다.
따라서 Manager hardening upgrade를 dev.12 partial Worker 재시작이나 native capability 승격과
결부시키지 않는다. Fake/no-start CONFIG-ONLY Worker는 아래 명령으로 provenance·구성만
갱신하고 service를 시작하지 않는다.

dev.12~dev.19에서 dev.20으로 올릴 때는 active Job과 `pending|finalizing` upload를 drain하고 backup을
완료한 뒤 Manager를 먼저 올린다. dev.16 이하에서 시작하면 `d8f2a6c4b901`의 Dataset nullable
integrated loudness migration도 순서대로 적용되며 과거 Dataset은 추정값 없이 historical `null`을
유지한다. 새 head `f5d1c8a9b240`은 maintenance parent-lock 함수를 추가하며, 이전
`e4c7b9d2f610`은 historical JobAttempt를 보존한 채 nullable reviewed RVC commit/provenance
snapshot과 model registry·entry·operation 원장을 추가한다. Migration 뒤 기존
Fake 또는 provenance NULL attempt가 후보가 되지 않는 것이 정상이며 값을 임의 backfill하지 않는다.
새 bundle을 `--no-start`로 설치한 다음 `RELEASE_SHA256SUMS`가 `root:root 444`인지와 exact inventory,
migration head, `/readyz`, 기존 Dataset/Experiment 조회와 빈 registry version 0을 확인하고 service를
시작한다. Worker dev.20도 여전히 partial이며 runtime/native/GPU/profile/Sample gate가 모두 false이므로
CONFIG-ONLY 범위를 넘지 않는다.

Upgrade는 반드시 **새 dev.20 bundle 안의** `upgrade.sh`로 실행한다. Script는 현재 설치 version보다
strict SemVer상 큰 target만 허용하며, 같은 version이나 낮은 version이면 `refusing non-forward
release transition`으로 종료한다. Target Compose는 pending environment와 target release로 먼저
렌더링되므로 여기서 실패하면 기존 `current` symlink와 env byte가 그대로 남아야 한다. 과거
dev.14 이하 bundle의 `upgrade.sh`를 직접 실행하면 새 guard 자체가 없으므로 사용하지 않는다.
낮은 Manager version으로 돌아갈 때는 새 설치나 upgrade가 아니라 아래의 installed guarded
`rollback` 명령만 사용한다. Worker downgrade는 자동화하지 않는다.

Worker upgrade는 기존 `worker.env`의 runner mode를 보존한다. 5절에서 만든 fake/no-start
CONFIG-ONLY 설치를 다시 검사할 때는 개발 mode 확인 옵션도 반복해야 한다.

```bash
(
  set -Eeuo pipefail
  sudo ./upgrade.sh . \
    --runner-mode fake \
    --allow-fake-dev \
    --skip-gpu-check \
    --no-start
  sudo /opt/rvc-orchestrator/worker/bin/worker-compose config --quiet
)
```

이 구성에서는 service를 시작하거나 restart하지 않는다. 기존 native Worker를 새 runtime 포함
bundle로 올리는 경우에만 `--runner-mode native --no-start`로 설치하고 image/runtime manifest,
GPU preflight와 Compose config를 확인한 뒤 service를 restart한다. Fake 설치를 native로, profile을
native로 바꾸는 in-place upgrade는 설치기가 의도적으로 거부하며 clean host 설치 또는 별도 설정
migration 절차가 필요하다.

dev.20 Worker upgrade는 `--ca-bundle-file`을 생략하면 기존 custom CA byte와
활성 fixed path를 보존한다. 새 CA를 전달하면 5.3절 조건으로 먼저 staging/prevalidate하고 target
Compose/activation 실패 시 이전 byte/environment/current를 복구해야 한다. CA 제거를 위해 env/file
한쪽만 수동 변경하지 않는다.

### Rollback과 restore

자동 rollback script는 **Manager에만** 있다.

동일한 reviewed schema marker와 실제 Alembic revision set이 호환되는 설치된 release에만 일반
rollback을 사용한다. 실행 전에는 운영 절차에 따라 별도 backup을 먼저 만든다. Partial 번들이면
대상 version의 정확한 application image도 로컬에 있어야 한다.

```bash
(
  set -Eeuo pipefail
  ROLLBACK_VERSION=REPLACE_WITH_REVIEWED_COMPATIBLE_VERSION
  test "$ROLLBACK_VERSION" != REPLACE_WITH_REVIEWED_COMPATIBLE_VERSION
  sudo /opt/rvc-orchestrator/manager/bin/rollback --to-version "$ROLLBACK_VERSION"
  sudo /opt/rvc-orchestrator/manager/bin/manager-compose ps -a
  curl --fail --silent --show-error https://manager.example.com/readyz \
    | python3 -m json.tool
)
```

Script는 release/checksum/image identity를 검증하지만 일반 same-marker rollback에서 backup을
자동 생성하지 않으며 database downgrade나 restore도 수행하지 않는다. 현재 dev.20과 dev.19의
marker는 `f5d1c8a9b240`, dev.18은 `e4c7b9d2f610`, dev.17은 `d8f2a6c4b901`, dev.12/11은
`ca8d3e7f4b10`이므로 dev.20에서 dev.18 이하로
돌아가는 일반 rollback은 fail-closed한다. Schema-mismatch override는 복제 VM의 복구 훈련에서만
다음처럼 명시적으로
사용하며, 이 경로에서만 script가 mandatory pre-rollback backup을 먼저 만든다.

```bash
sudo /opt/rvc-orchestrator/manager/bin/rollback \
  --to-version 0.1.0-dev.12 \
  --allow-schema-mismatch \
  --confirm-schema-mismatch-risk I_UNDERSTAND_NO_DATABASE_DOWNGRADE
```

dev.12→dev.11은 schema marker가 같아도 **보안 상태가 같지 않다**. dev.11은 역할별
runtime secret projection과 MLflow `10002:10002`·read-only 경계가 없고, 구 `minio-init`은 두
service user에 built-in `readwrite`를 다시 첨부한다. Readiness가 성공해도
secret/MLflow/MinIO-policy 인수는 FAIL로 기록하고 production 정상 상태로 표현하지 않는다.
격리된 복제 VM에서만 실행하고 원인 해결 뒤 dev.12로 재-upgrade해 exact policy와
runtime projection을 다시 적용한다. 남은 runtime secret volume을 rollback 중 수동 수정·
삭제하지 않는다.

dev.20에서 dev.18 이하로의 rollback은 `f5d1c8a9b240` schema나 maintenance 최소권한
credential/ACL을 자동 downgrade하지 않는다. 대상 release의 schema compatibility와 API가 추가
nullable column/table을
안전하게 무시하는지, registry 승인 이력 보존·복구 정책이 무엇인지 복제 VM에서 먼저 검토해야 하며,
검토되지 않은 production rollback은 수행하지 않는다.

Worker에는 자동 rollback script가 없으므로 이전 bundle 재설치는 별도 변경 절차와 runtime/
데이터 호환 검토가 필요하다.

실행을 포함한 upgrade에서 target service 시작이 실패하면 설치기는 database migration의
무분별한 역행을 피하기 위해 새 target env/current를 일관되게 유지한 채 nonzero로 종료한다.
이 상태를 성공이나 자동 rollback으로 간주하지 말고 service log, migration과 target readiness를
진단한다. Target activation **전** Compose 검증/stop이 실패한 경우에만 이전 env/current와 이전
service를 보존·복구한다.

Restore는 database와 bucket 내용을 교체하는 파괴적 작업이다. production이 아닌 복제 VM에서
먼저 검증하고 `docs/OPERATIONS_GUIDE.md`의 절차를 따른다.

### Uninstall

`uninstall.sh`는 설치 경로의 `bin`에 복사되지 않는다. 각 호스트에서 압축 해제한
해당 component bundle 디렉터리로 이동해 실행한다.

```bash
# Manager host, extracted Manager bundle directory
sudo ./uninstall.sh
sudo systemctl is-enabled rvc-orchestrator-manager.service || true
sudo systemctl is-active rvc-orchestrator-manager.service || true
sudo /opt/rvc-orchestrator/manager/bin/manager-compose ps -a

# Worker host, extracted Worker bundle directory
sudo ./uninstall.sh
sudo systemctl is-enabled rvc-orchestrator-worker.service || true
sudo systemctl is-active rvc-orchestrator-worker.service || true
sudo /opt/rvc-orchestrator/worker/bin/worker-compose ps
```

두 `./uninstall.sh`는 서로 다른 호스트/디렉터리에서 실행하는 별도 예시다. 기대 상태는
unit `disabled`/`inactive`와 실행 container 없음이다. dev.20 script는 systemd disable 또는
Compose down 중 하나라도 실패하거나 필수 wrapper가 없으면 성공 문구를 출력하지 않고 exit 1로
끝낸다. 그래도 명령이 exit 0을 반환한 뒤 실제 상태가 바뀌었는지는 아래 post-check로 반드시
확인한다.

Uninstall은 release, config, root source secret, Worker token/profile/custom CA/job data,
PostgreSQL/Redis/MinIO/작업 volume과 파생 runtime secret volume을 삭제하지 않는다. `docker volume prune`, `/etc`
일괄 삭제나 token 출력을 수행하지 않는다. 데이터 영구 삭제는 backup·retention·audit 승인을
거친 별도 decommission 절차로만 수행하며 이 문서에서 자동화하지 않는다.

## 8. 자주 발생하는 문제

| 증상 | 확인할 원인 |
|---|---|
| Manager에서 `pull access denied` 또는 image not found | dev.20은 pull하지 않아야 한다. archive/manifest/loaded identity와 `RVC_IMAGE_PULL_POLICY=never`를 확인하고 별도 build/pull로 우회하지 않음 |
| Worker image not found | dev.20 Worker partial의 의도된 상태다. service를 시작하지 말고 runtime 포함 후속 bundle을 기다림 |
| Manager installer가 HTTPS endpoint를 요구 | `S3_PRESIGN_ENDPOINT_URL`을 Worker/browser가 접근 가능한 HTTPS 주소로 설정 |
| `/readyz`가 `rq_worker=stale/unavailable` | `rq-worker` 실행/Redis 연결/heartbeat 로그 확인; Compose `healthy` 표시는 없음 |
| 브라우저 upload가 CORS/서명 오류 | `CORS_ORIGINS`, object TLS proxy의 Host/path/query/method/body 보존, 인증서 trust 확인 |
| production start가 `PUBLIC_SCHEME` 오류로 중단 | `manager.env`에 정확히 한 번 `PUBLIC_SCHEME=https`; 외부 TLS 종단과 browser-facing URL 확인 |
| HTTPS인데 session cookie `Secure`/HSTS가 없음 | dev.20 trusted scheme/image가 실제 실행 중인지 확인하고 production TLS FAIL로 기록 |
| native Worker 설치 거부 | partial bundle의 의도된 보호 동작; runtime 포함 bundle 필요 |
| Fake Worker 등록 거부 | production Manager의 의도된 보호 동작; `ALLOW_FAKE_WORKERS`를 바꾸지 않음 |
| dev.20 Worker에서 `--ca-bundle-file`이 unknown option | 잘못된/구 bundle을 사용 중임; dev.20 외부 hash와 내부 ledger를 다시 검증해 새 설치 |
| Custom CA install/start 거부 | source와 installed file의 root owner, regular non-symlink, mode `0444|0644`, 1..1 MiB, certificate-only ASCII PEM 및 fixed env path를 확인; 검증 비활성화 금지 |
| Custom CA인데 hostname mismatch | CA 신뢰와 hostname 검증은 별개다. Manager/Object certificate SAN과 URL hostname을 고치며 IP/별칭으로 우회하지 않음 |
| profile Worker 시작 실패 | generic image에는 `/opt/rvc-webui`가 없음; 검증된 repository/profile 포함 custom runtime 필요 |
| systemd는 active지만 화면/API 장애 | oneshot 상태만 보지 말고 Compose `ps`, `/readyz`, service logs 확인 |
| Worker telemetry `413` | 단일 status/log/metric body가 `WORKER_TELEMETRY_JSON_MAX_BYTES` 기본 2 MiB를 넘음; 상한 우회 대신 batch/record와 spool 설정 점검 |
| telemetry가 terminal 뒤 `409` | exact Worker/lease/attempt와 exclusive watermark 미만 sequence인지 확인; legacy/watermark 없는 terminal 또는 상한 이상은 의도된 거부 |
| GPU 수는 0인데 원인 불명 | `system.gpu.telemetry_available`: `1`이면 성공한 0-GPU 관측, `0`이면 nvidia-smi/query 검증 실패 |
| Manager 전체 장애 뒤 old spool 잔여 | terminal status가 커밋되기 전 lease가 회수됐으면 자동 수용 범위가 아님; pending/dead-letter와 status/lease event를 보존해 운영 조사하고 새 attempt에 합치지 않음 |

설치 시험의 단계별 합격 기준과 제출할 증적은 `docs/TEST_GUIDE.md`를 따른다.

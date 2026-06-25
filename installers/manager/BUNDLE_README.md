# RVC Training Orchestrator Manager {{VERSION}}

이 디렉터리는 중앙 Manager 설치 bundle이다. 명령은 Ubuntu 22.04/24.04
`x86_64` 호스트와 Docker Engine, Compose v2, `systemd`, `sudo`, `python3`, GNU
coreutils가 준비됐다고 가정한다. 기존 production 호스트가 아닌 폐기 가능한
VM에서 먼저 `--no-start` 검증을 수행한다.
상세 합격 기준은 `TESTING.md`, 작성 가능한 결과 양식은 `TEST_RESULT_TEMPLATE.md`에 있다.

## 1. 설치 전 무결성과 배포 범위

Archive와 함께 받은 외부 `.sha256`은 **압축을 풀기 전** 전달 디렉터리에서
먼저 확인한다. Sidecar만 신뢰하지 말고, 전달자의 인증된 release 채널에서
받은 archive SHA-256과 sidecar의 값을 먼저 대조한다. 두 값의 출처를 같은 전송물로
대체하지 않는다.

```bash
set -Eeuo pipefail
sha256sum -c rvc-manager-{{VERSION}}-linux-amd64.tar.gz.sha256
test ! -e rvc-manager-{{VERSION}}-linux-amd64
tar -xzf rvc-manager-{{VERSION}}-linux-amd64.tar.gz
cd rvc-manager-{{VERSION}}-linux-amd64
```

압축을 푼 bundle 루트에서 내부 exact inventory와 manifest를 확인한다.

```bash
set -Eeuo pipefail
sha256sum -c SHA256SUMS
python3 common/image_bundle.py verify-ledger --root . --ledger-name SHA256SUMS
GIT_COMMIT=$(awk -F= '$1 == "GIT_COMMIT" {print $2; exit}' manifest.env)
python3 common/image_bundle.py verify-bundle \
  --root . \
  --component manager \
  --version {{VERSION}} \
  --source-commit "$GIT_COMMIT"
grep -E '^(VERSION|PLATFORM|GIT_COMMIT|SELF_CONTAINED|API_IMAGE|WEB_IMAGE|MLFLOW_IMAGE|POSTGRES_IMAGE|REDIS_IMAGE|MINIO_IMAGE|MINIO_CLIENT_IMAGE|NGINX_IMAGE)=' \
  manifest.env
```

모든 명령의 exit code가 0이어야 한다. `SELF_CONTAINED=false`면 bundle에 image가
없으므로 `manifest.env`의 exact application/dependency image를 검증된 별도 경로로
준비하기 전에는 서비스를 시작하지 않는다. `GIT_COMMIT=uncommitted`인 bundle도
production source provenance나 rollback 기준이 아니다.
`SHA256SUMS`가 없거나 실패하면 source tree라고 가정하거나 ledger를 다시 만들지 말고
bundle을 폐기한다.

## 2. Manager CONFIG-ONLY 설치

아래 URL은 예시이다. 실제 후보에서는 Worker와 browser가 신뢰하고 접근할 수
있는 object HTTPS origin으로 바꾸되, CONFIG-ONLY 시험에서는 서비스를 시작하지
않는다.

```bash
set -Eeuo pipefail
sudo ./preflight.sh
sudo ./install.sh \
  --no-start \
  --public-scheme https \
  --s3-presign-endpoint-url https://objects.example.com
```

설치된 release, exact ledger, root-owned 설정과 Compose render를 secret 내용을 출력하지
않고 확인한다.

```bash
set -Eeuo pipefail
MANAGER_RELEASE=$(sudo readlink -f /opt/rvc-orchestrator/manager/current)
case "$MANAGER_RELEASE" in
  /opt/rvc-orchestrator/manager/releases/*) ;;
  *) echo "Manager current resolves outside releases" >&2; exit 1 ;;
esac
printf '%s\n' "$MANAGER_RELEASE"
sudo stat -c '%U:%G %a %n' \
  "$MANAGER_RELEASE/RELEASE_SHA256SUMS" \
  /etc/rvc-orchestrator/manager \
  /etc/rvc-orchestrator/manager/manager.env \
  /etc/rvc-orchestrator/manager/secrets
test "$(sudo stat -c '%u:%g %a' "$MANAGER_RELEASE/RELEASE_SHA256SUMS")" = '0:0 444'
test "$(sudo stat -c '%u:%g %a' /etc/rvc-orchestrator/manager)" = '0:0 700'
test "$(sudo stat -c '%u:%g %a' /etc/rvc-orchestrator/manager/manager.env)" = '0:0 600'
test "$(sudo stat -c '%u:%g %a' /etc/rvc-orchestrator/manager/secrets)" = '0:0 700'
EXPECTED_SECRET_NAMES=$'jwt_secret\nmaintenance_postgres_password\nmaintenance_redis_password\nmaintenance_s3_access_key\nmaintenance_s3_secret_key\nminio_app_access_key\nminio_app_secret_key\nminio_root_password\nminio_root_user\nmlflow_postgres_password\nmlflow_s3_access_key\nmlflow_s3_secret_key\npostgres_password\nredis_password\nworker_bootstrap_token\nworker_token_pepper'
ACTUAL_SECRET_NAMES=$(sudo find /etc/rvc-orchestrator/manager/secrets \
  -mindepth 1 -maxdepth 1 -printf '%f\n')
ACTUAL_SECRET_NAMES=$(printf '%s\n' "$ACTUAL_SECRET_NAMES" | LC_ALL=C sort)
test "$ACTUAL_SECRET_NAMES" = "$EXPECTED_SECRET_NAMES"
for secret_name in $ACTUAL_SECRET_NAMES; do
  secret_path=/etc/rvc-orchestrator/manager/secrets/$secret_name
  sudo test -f "$secret_path"
  sudo test ! -L "$secret_path"
  test "$(sudo stat -c '%u:%g %a' "$secret_path")" = '0:0 600'
done
unset EXPECTED_SECRET_NAMES ACTUAL_SECRET_NAMES secret_name secret_path
sudo python3 /opt/rvc-orchestrator/manager/lib/image_bundle.py \
  verify-ledger \
  --root "$MANAGER_RELEASE" \
  --ledger-name RELEASE_SHA256SUMS
sudo /opt/rvc-orchestrator/manager/bin/manager-compose config --quiet
sudo awk -F= '
  $1 == "ORCHESTRATOR_VERSION" ||
  $1 == "ENVIRONMENT" ||
  $1 == "PUBLIC_SCHEME" ||
  $1 == "API_IMAGE" ||
  $1 == "WEB_IMAGE" ||
  $1 == "MLFLOW_IMAGE" ||
  $1 == "RVC_IMAGE_PULL_POLICY" {print}
' /etc/rvc-orchestrator/manager/manager.env
MANAGER_CIDS=$(sudo /opt/rvc-orchestrator/manager/bin/manager-compose ps -a --quiet)
test -z "$MANAGER_CIDS"
unset MANAGER_CIDS
```

기대 결과:

- `current`는 `/opt/rvc-orchestrator/manager/releases/{{VERSION}}`를 가리킴
- `RELEASE_SHA256SUMS`는 `root:root 444`이고 exact ledger 검증 PASS
- Manager config/secrets directory는 `root:root 700`, `manager.env`는 `root:root 600`
- `ORCHESTRATOR_VERSION={{VERSION}}`, `ENVIRONMENT=production`, `PUBLIC_SCHEME=https`
- Compose config exit code 0, 실행 container 없음

`manager.env` 전체, secret 파일, container/image의 전체 inspect JSON은 증적에 남기지 않는다.
네 `maintenance_*` credential은 API용 PostgreSQL·Redis·Manager S3 credential과 서로 다른
값이어야 한다. 설치기는 source secret의 내용은 출력하지 않고 collision을 runtime projection
전에 fail-closed한다. RQ는 전용 네 파일만 받으며 API와 MLflow projection은 이를 받지 않는다.

## 3. 실제 시작 전 중단 gate

CONFIG-ONLY PASS는 Manager runtime PASS가 아니다. 다음이 모두 준비되기 전에는
`systemctl enable/restart`를 실행하지 않는다.

- `SELF_CONTAINED=true`의 exact image closure 또는 manifest와 일치하는 별도 image 전달
- `/etc/rvc-orchestrator/manager/manager.env`의 실제 DNS, CORS, loopback bind와 HTTPS
  object endpoint
- 외부 TLS proxy의 유효한 인증서, Host/path/query/method 보존, Secure cookie와 HSTS
- `TESTING.md`의 CONFIG-ONLY 인수와 설치 환경의 별도 runtime/readiness 인수

## 4. 설치 후 운영 명령

실제 시작이 승인된 후에만 다음 명령을 사용한다.

```bash
sudo systemctl daemon-reload
sudo systemctl enable rvc-orchestrator-manager.service
sudo systemctl restart rvc-orchestrator-manager.service
sudo /opt/rvc-orchestrator/manager/bin/manager-compose ps -a
sudo /opt/rvc-orchestrator/manager/bin/manager-compose logs --tail=200 api rq-worker
```

`manager-secrets-init`, `api-migrate`, `maintenance-db-authz`, `minio-init`이 모두 exit 0인 뒤에만
RQ가 시작해야 한다. Installed wrapper의 전체 `start`/`restart`는 이 one-shot 경계를 다시
적용하는 `up --force-recreate`로 수행되며 service 이름을 붙인 부분 restart는 거부된다.

`/healthz`는 liveness, `/readyz`는 PostgreSQL·Redis/RQ·설정된 MLflow policy를 포함한
readiness다. `/readyz`가 ready인 후 최초 관리자 비밀번호는 bootstrap 하한보다 강한 사용자
lifecycle 정책과 일관되게 16~1,024자로 정하고 root 소유 mode `0600` 파일에 준비한다.

```bash
sudo install -o root -g root -m 0600 /dev/null /root/rvc-admin-password
sudoedit /root/rvc-admin-password
sudo /opt/rvc-orchestrator/manager/bin/bootstrap-admin \
  --email admin@example.com \
  --password-file /root/rvc-admin-password
```

비밀번호 파일 내용을 출력하지 않고 bootstrap 후 조직의 secret 보관·폐기
정책을 따른다. Upgrade, rollback, restore 전에는 active Job과 upload를 drain하고
backup을 만든다. Upgrade는 target bundle의 `sudo ./upgrade.sh . ...`만 사용하며 같은/낮은
SemVer는 거부된다. 낮은 Manager version은 installed guarded rollback으로만 전환한다.
기본 uninstall은 config, secret, release와 data volume을 보존하지만 systemd/Compose stop
일부 실패 시 exit 1이다. Exit 0 뒤에도 inactive/no-container 상태를 확인한다.

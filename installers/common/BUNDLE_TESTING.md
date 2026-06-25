# {{COMPONENT}} bundle {{VERSION}} 검증

이 문서는 압축 해제한 bundle의 무결성과 component별 `--no-start` 설치 경계를
검증한다. 실제 clean Ubuntu runtime, 외부 TLS/browser, NVIDIA GPU 학습 인수를
대신하지 않는다. 기존 production 설치가 없는 폐기 가능한 Ubuntu 22.04/24.04
`x86_64` VM에서 실행한다.

시험을 시작하기 전에 bundle root의 `TEST_RESULT_TEMPLATE.md`를 별도 파일로 복사해
명령, exit code, PASS/FAIL/BLOCKED와 redacted 증적을 기록한다.

## 1. Bundle 내부 무결성

외부 archive `.sha256`은 압축 해제 전 `README.md` 절차로 먼저 확인해야 한다.
Sidecar의 hash는 전달자의 인증된 release 채널에서 따로 받은 값과 대조한다.
Bundle 루트에서 다음을 실행한다.

```bash
set -Eeuo pipefail
sha256sum -c SHA256SUMS
python3 common/image_bundle.py verify-ledger --root . --ledger-name SHA256SUMS
GIT_COMMIT=$(awk -F= '$1 == "GIT_COMMIT" {print $2; exit}' manifest.env)
python3 common/image_bundle.py verify-bundle \
  --root . \
  --component {{COMPONENT}} \
  --version {{VERSION}} \
  --source-commit "$GIT_COMMIT"
grep -E '^(BUNDLE_FORMAT_VERSION|COMPONENT|VERSION|PLATFORM|GIT_COMMIT|SELF_CONTAINED)=' \
  manifest.env
python3 -m json.tool images-manifest.json >/dev/null
```

합격 기준:

- 모든 명령 exit code 0
- `COMPONENT={{COMPONENT}}`, `VERSION={{VERSION}}`, `PLATFORM=linux-amd64`
- `SHA256SUMS`를 임의로 재생성하지 않은 exact ledger PASS
- `SELF_CONTAINED=false`면 image/archive inventory가 비어 있음; 이는 partial PASS일 뿐
  production/air-gapped PASS가 아님
- `SELF_CONTAINED=true`면 exact role, archive SHA-256/size, image/config digest,
  `linux/amd64`, application `Config.User`, release label이 모두 존재

@@MANAGER_ONLY_BEGIN@@
## 2. Manager CONFIG-ONLY 설치

Bundle 루트에서 실행한다. 예시 object URL은 실제 service를 시작하지 않는
CONFIG-ONLY 설정용이다.

```bash
set -Eeuo pipefail
sudo ./preflight.sh
sudo ./install.sh \
  --no-start \
  --public-scheme https \
  --s3-presign-endpoint-url https://objects.example.com
```

설치기가 생성한 release와 non-secret 설정만 확인한다.

```bash
set -Eeuo pipefail
MANAGER_COMPOSE=/opt/rvc-orchestrator/manager/bin/manager-compose
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
sudo "$MANAGER_COMPOSE" config --quiet
sudo awk -F= '
  $1 == "ORCHESTRATOR_VERSION" ||
  $1 == "ENVIRONMENT" ||
  $1 == "PUBLIC_SCHEME" ||
  $1 == "API_IMAGE" ||
  $1 == "WEB_IMAGE" ||
  $1 == "MLFLOW_IMAGE" ||
  $1 == "RVC_IMAGE_PULL_POLICY" {print}
' /etc/rvc-orchestrator/manager/manager.env
MANAGER_CIDS=$(sudo "$MANAGER_COMPOSE" ps -a --quiet)
test -z "$MANAGER_CIDS"
unset MANAGER_CIDS
```

기대 결과는 `current` release `{{VERSION}}`, `RELEASE_SHA256SUMS` `root:root 444`,
config/secrets directory `root:root 700`, `manager.env` `root:root 600`,
`ENVIRONMENT=production`, `PUBLIC_SCHEME=https`, Compose config exit 0과 실행 container 0개다.
`maintenance_*` 네 파일은 API credential과 분리된 RQ 전용 source이며 내용이나 hash를 증적에
남기지 않는다. Runtime 인수에서는 `maintenance-db-authz`와 `minio-init` 성공 뒤 RQ가 시작하고,
RQ container에 API용 `postgres_password`, `redis_password`, `minio_app_*`가 없는지 확인한다.

`SELF_CONTAINED=false`이거나 exact image/TLS/DNS가 준비되지 않았으면 여기서 중단한다.
CONFIG-ONLY를 Manager runtime/readiness/TLS PASS로 기록하지 않는다.
@@MANAGER_ONLY_END@@

@@WORKER_ONLY_BEGIN@@
## 2. Partial Worker CONFIG-ONLY 설치

먼저 partial/runtime gate를 확인한다.

```bash
set -Eeuo pipefail
grep -E '^(SELF_CONTAINED|RVC_RUNTIME_INCLUDED|RVC_NATIVE_RUNNER_AVAILABLE|RVC_GPU_SMOKE_VERIFIED|RVC_PROFILE_STAGE_SET_VERIFIED|RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED)=' \
  manifest.env
```

`SELF_CONTAINED=false` 또는 runtime/native gate false인 bundle에서만 아래 CONFIG-ONLY 시험을
실행한다. 실제 Manager credential 대신 폐기할 합성 token 파일을 만든다.

```bash
sudo install -o root -g root -m 0600 /dev/null /root/worker-config-only-token
sudoedit /root/worker-config-only-token
```

파일에 `CONFIG_ONLY_DO_NOT_USE`처럼 무효한 값을 넣고 저장한 뒤 다음을 실행한다.
GPU가 없는 CONFIG-ONLY VM이므로 preflight와 installer **둘 다** `--skip-gpu-check`를
사용한다.
사설 CA를 사용하는 시험이면 CA certificate chain PEM을 root 소유 regular file mode
`0644` 또는 `0444`로 준비하고 install command에
`--ca-bundle-file /root/rvc-worker-custom-ca.pem`을 추가한다. Public CA면 생략한다.

```bash
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
```

설치된 release와 non-secret 설정, mode만 확인한다.

```bash
set -Eeuo pipefail
WORKER_COMPOSE=/opt/rvc-orchestrator/worker/bin/worker-compose
WORKER_RELEASE=$(sudo readlink -f /opt/rvc-orchestrator/worker/current)
case "$WORKER_RELEASE" in
  /opt/rvc-orchestrator/worker/releases/*) ;;
  *) echo "Worker current resolves outside releases" >&2; exit 1 ;;
esac
printf '%s\n' "$WORKER_RELEASE"
sudo stat -c '%U:%G %a %n' \
  "$WORKER_RELEASE/RELEASE_SHA256SUMS"
sudo python3 /opt/rvc-orchestrator/worker/lib/image_bundle.py \
  verify-ledger \
  --root "$WORKER_RELEASE" \
  --ledger-name RELEASE_SHA256SUMS
sudo stat -c '%u:%g %a %n' \
  /var/lib/rvc-orchestrator/worker \
  /etc/rvc-orchestrator/worker/secrets/worker_token \
  /etc/rvc-orchestrator/worker/rvc-profile.yaml
sudo awk -F= '$1 == "WORKER_CA_BUNDLE_HOST_DIR" || $1 == "WORKER_CA_BUNDLE_PATH" {print}' \
  /etc/rvc-orchestrator/worker/worker.env
if sudo test -f /etc/rvc-orchestrator/worker/ca/custom-ca.pem; then
  sudo stat -c '%u:%g %a %n' /etc/rvc-orchestrator/worker/ca/custom-ca.pem
  sudo python3 /opt/rvc-orchestrator/worker/lib/worker_ca.py validate \
    --path /etc/rvc-orchestrator/worker/ca/custom-ca.pem --required-uid 0
fi
sudo "$WORKER_COMPOSE" config --quiet
sudo awk -F= '
  $1 == "ORCHESTRATOR_VERSION" ||
  $1 == "WORKER_IMAGE" ||
  $1 == "RVC_RUNNER_MODE" ||
  $1 == "RVC_IMAGE_PULL_POLICY" ||
  $1 == "RVC_GPU_SMOKE_VERIFIED" ||
  $1 == "RVC_PROFILE_STAGE_SET_VERIFIED" ||
  $1 == "RVC_NATIVE_UNVERIFIED_GPU_ACKNOWLEDGED" ||
  $1 == "SYSTEM_TELEMETRY_INTERVAL_SECONDS" {print}
' /etc/rvc-orchestrator/worker/worker.env
WORKER_CIDS=$(sudo "$WORKER_COMPOSE" ps -a --quiet)
test -z "$WORKER_CIDS"
unset WORKER_CIDS
```

기대 결과는 `current` release `{{VERSION}}`, `RELEASE_SHA256SUMS` `root:root 444`, data
directory `10001:10001 700`, token/profile `10001:10001 600`, `RVC_RUNNER_MODE=fake`,
partial pull policy `missing`, GPU/profile gate false, Compose config exit 0과 실행 container 0개다.
Custom CA를 사용하면 host file은 `root:root 444`, container path는 고정
`/etc/rvc-worker/ca/custom-ca.pem`이어야 하고, 사용하지 않으면 해당 environment 값이 비어 있어야 한다.

이 service를 enable/start하지 않고 합성 token을 실제 Manager/Worker credential로
재사용하지 않는다. 이 결과는 `CONFIG-ONLY`이며 학습 서버 설치 또는 RVC 학습
PASS가 아니다.

같은 partial bundle의 native mode가 exact runtime-missing 오류로 거부되는지 기존 fake 설치와
분리된 빈 경로에서 확인한다. 원래 fake 환경과 inactive/no-container 상태도 함께 재검증한다.

```bash
set -Eeuo pipefail
NEGATIVE_ROOT=$(mktemp -d /tmp/rvc-worker-native-negative.XXXXXX)
NEGATIVE_STDERR=$NEGATIVE_ROOT/stderr
cleanup_negative() {
  rm -f -- "$NEGATIVE_STDERR"
  rmdir -- "$NEGATIVE_ROOT"
}
trap cleanup_negative EXIT
set +e
sudo ./install.sh \
  --manager-url https://manager.example.com \
  --worker-name gpu-native-negative \
  --token-file /root/worker-config-only-token \
  --runner-mode native \
  --allow-unverified-gpu-runtime \
  --install-root "$NEGATIVE_ROOT/install" \
  --config-root "$NEGATIVE_ROOT/config" \
  --data-root "$NEGATIVE_ROOT/data" \
  --systemd-dir "$NEGATIVE_ROOT/systemd" \
  --allow-unsupported-os \
  --skip-daemon-check \
  --skip-gpu-check \
  --no-start 2>"$NEGATIVE_STDERR"
negative_status=$?
set -e
test "$negative_status" -ne 0
grep -Fqx \
  '[rvc-installer] error: native mode requires a Worker bundle with a verified offline RVC runtime' \
  "$NEGATIVE_STDERR"
sudo awk -F= '$1 == "RVC_RUNNER_MODE" {print; count++} END {exit count == 1 ? 0 : 1}' \
  /etc/rvc-orchestrator/worker/worker.env | grep -Fqx 'RVC_RUNNER_MODE=fake'
! sudo systemctl is-active --quiet rvc-orchestrator-worker.service
WORKER_CIDS=$(sudo /opt/rvc-orchestrator/worker/bin/worker-compose ps -a --quiet)
test -z "$WORKER_CIDS"
unset WORKER_CIDS
test ! -e "$NEGATIVE_ROOT/install"
test ! -e "$NEGATIVE_ROOT/config"
test ! -e "$NEGATIVE_ROOT/data"
test ! -e "$NEGATIVE_ROOT/systemd"
```

이 거부는 native runtime 부재를 증명할 뿐 GPU/native PASS가 아니다.
@@WORKER_ONLY_END@@

## 3. 증적과 최종 판정

Secret, token, password, presigned query, 사용자 파일명, 전체 environment 또는 전체
container/image inspect JSON을 남기지 않는다. 실행 명령, exit code, 표시된 allowlist,
bundle/archive SHA-256, OS/architecture/Docker/Compose version과 다음 판정을 남긴다.

- `BUNDLE-INTEGRITY`: 외부 `.sha256`, 내부 `SHA256SUMS`, strict manifest 검증
- `CONFIG-ONLY`: component별 `--no-start`, installed exact ledger, mode, Compose render
- `MANAGER-SMOKE`: 전체 dependency health, readiness, TLS/browser/object data plane
- `WORKER-NATIVE`: 실제 NVIDIA GPU의 v1/v2·F0·index·artifact 학습
- `PRODUCTION-SAMPLE`: 고정 TestSet의 네 inference F0와 no-network 증적

낮은 단계의 PASS를 높은 단계의 PASS로 확대 해석하지 않는다.

Upgrade/lifecycle을 시험할 때는 target bundle의 `upgrade.sh`를 사용한다. Target Compose
prevalidation 실패 시 기존 env/current byte가 유지되고, strict SemVer 역행이 거부돼야 한다.
과거 bundle script를 downgrade 도구로 사용하지 않는다. `uninstall.sh`는 systemd disable 또는
Compose down 일부 실패를 nonzero로 보고해야 하며 token/profile/custom CA/data를 보존해야 한다.
Exit 0 뒤에도 실제 inactive/no-container를 별도로 확인한다.

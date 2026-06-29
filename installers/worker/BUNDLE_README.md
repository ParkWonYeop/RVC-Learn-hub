# RVC Training Orchestrator Worker {{VERSION}}

이 디렉터리는 GPU 학습 Worker 설치 bundle이다. 명령은 Ubuntu 22.04/24.04
`x86_64` 호스트와 Docker Engine, Compose v2, `systemd`, `sudo`, `python3`가
준비됐다고 가정한다. 기존 production 호스트가 아닌 폐기 가능한 VM에서 먼저
bundle 종류를 확인한다.
상세 합격 기준은 `TESTING.md`, 작성 가능한 결과 양식은 `TEST_RESULT_TEMPLATE.md`에 있다.

## 1. 설치 전 무결성과 runtime gate

Archive와 함께 받은 외부 `.sha256`은 **압축을 풀기 전** 전달 디렉터리에서
먼저 확인한다. Sidecar만 신뢰하지 말고, 전달자의 인증된 release 채널에서
받은 archive SHA-256과 sidecar의 값을 먼저 대조한다. 두 값의 출처를 같은 전송물로
대체하지 않는다.

```bash
set -Eeuo pipefail
sha256sum -c rvc-worker-{{VERSION}}-linux-amd64.tar.gz.sha256
test ! -e rvc-worker-{{VERSION}}-linux-amd64
tar -xzf rvc-worker-{{VERSION}}-linux-amd64.tar.gz
cd rvc-worker-{{VERSION}}-linux-amd64
```

압축을 푼 bundle 루트에서 내부 exact inventory와 manifest를 확인한다.

```bash
set -Eeuo pipefail
sha256sum -c SHA256SUMS
python3 common/image_bundle.py verify-ledger --root . --ledger-name SHA256SUMS
GIT_COMMIT=$(awk -F= '$1 == "GIT_COMMIT" {print $2; exit}' manifest.env)
python3 common/image_bundle.py verify-bundle \
  --root . \
  --component worker \
  --version {{VERSION}} \
  --source-commit "$GIT_COMMIT"
grep -E '^(VERSION|PLATFORM|GIT_COMMIT|SELF_CONTAINED|WORKER_IMAGE|RVC_RUNTIME_INCLUDED|RVC_NATIVE_RUNNER_AVAILABLE|RVC_GPU_SMOKE_VERIFIED|RVC_PROFILE_STAGE_SET_VERIFIED|RVC_NATIVE_SAMPLE_INFERENCE_VERIFIED)=' \
  manifest.env
```

모든 명령의 exit code가 0이어야 한다. 실제 native 학습은 최소
`SELF_CONTAINED=true`, `RVC_RUNTIME_INCLUDED=true`, `RVC_NATIVE_RUNNER_AVAILABLE=true`와
검증된 runtime image/source/asset/build manifest가 필요하다. Sample 기능은 GPU/no-network
qualification에 결박된 activation까지 필요하다. 어느 gate든 false인 경우 수동
environment 변경이나 image retag로 우회하지 않는다.
`SHA256SUMS`가 없거나 실패하면 source tree라고 가정하거나 ledger를 다시 만들지 말고
bundle을 폐기한다.

## 2. Partial bundle CONFIG-ONLY 설치

`SELF_CONTAINED=false` 또는 runtime/native gate가 false이면 실제 학습용으로 시작할 수
없다. GPU가 없는 폐기 가능한 VM에서는 아래 CONFIG-ONLY 시험만 수행한다.
실제 Manager bootstrap/JWT/Worker token 대신 폐기할 합성 문자열을 사용한다.

```bash
sudo install -o root -g root -m 0600 /dev/null /root/worker-config-only-token
sudoedit /root/worker-config-only-token
```

파일에 `CONFIG_ONLY_DO_NOT_USE` 같은 무효한 값을 넣은 뒤 다음을 실행한다.
Manager 또는 external object endpoint가 사설 CA를 사용하면 CA certificate chain만 담긴 PEM을
root 소유 mode `0644` 또는 `0444` regular file로 준비하고 아래 명령에
`--ca-bundle-file /root/rvc-worker-custom-ca.pem`을 추가한다. Private key가 들어간 파일,
symlink, 다른 mode와 1 MiB 초과 파일은 거부된다. Public CA만 사용하면 이 option을 생략한다.

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

설치된 release, exact ledger, container UID/GID용 파일 mode와 Compose render를
token 내용을 출력하지 않고 확인한다.

```bash
set -Eeuo pipefail
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
sudo /opt/rvc-orchestrator/worker/bin/worker-compose config --quiet
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
WORKER_CIDS=$(sudo /opt/rvc-orchestrator/worker/bin/worker-compose ps -a --quiet)
test -z "$WORKER_CIDS"
unset WORKER_CIDS
```

기대 결과:

- `current`는 `/opt/rvc-orchestrator/worker/releases/{{VERSION}}`를 가리킴
- `RELEASE_SHA256SUMS`는 `root:root 444`이고 exact ledger 검증 PASS
- data directory는 `10001:10001 700`, token/profile은 `10001:10001 600`
- custom CA를 사용하면 host file은 `root:root 444`, environment path는 고정
  `/etc/rvc-worker/ca/custom-ca.pem`; 사용하지 않으면 environment path는 빈 값
- `RVC_RUNNER_MODE=fake`, partial image pull policy `missing`, GPU/profile gate `false`
- Compose config exit code 0, 실행 container 없음

이 systemd service를 enable/start하지 않는다. 합성 token은 실제 credential로 재사용하지
않고, Manager의 `ALLOW_FAKE_WORKERS=false`를 바꾸지 않는다.

같은 partial bundle이 native mode를 fail-closed하는지 기존 fake 설치와 분리된 빈 경로에서
확인한다. 아래 block은 정확한 runtime-missing 오류만 허용하고 원래 설치가 계속 fake/inactive인지
다시 검사한다.

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

이 결과는 native runtime이 없을 때의 안전한 거부 증거일 뿐 GPU/native PASS가 아니다.

## 3. Runtime 포함 bundle의 native `--no-start` 설치

이 절은 manifest의 runtime/native gate가 true이고 실제 NVIDIA preflight가 통과한 별도
후보에서만 수행한다. `--token-file`에는 JWT나 기존 Worker token이 아니라
Manager 운영자가 보호된 경로로 전달한 **Worker bootstrap token** 파일을 사용한다.
파일은 root 소유 regular non-symlink, mode `0600`이어야 한다.
사설 CA endpoint를 사용하는 경우 위와 같은 CA PEM을 준비하고 install command에
`--ca-bundle-file /root/rvc-worker-custom-ca.pem`을 추가한다. Installer는 release 밖
`/etc/rvc-orchestrator/worker/ca/custom-ca.pem`에 원자 게시하며 upgrade에서 option을 생략하면
기존 CA를 보존한다.

```bash
sudo ./preflight.sh
sudo ./install.sh \
  --manager-url https://manager.example.com \
  --worker-name gpu-01 \
  --token-file /root/worker-bootstrap-token \
  --runner-mode native \
  --no-start

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
  --version {{VERSION}} \
  --source-commit "$INSTALLED_SOURCE_COMMIT"
sudo stat -c '%U:%G %a %n' \
  "$INSTALLED_RELEASE/infra/worker/runtime/runtime-activation.json"
sudo python3 -m json.tool \
  "$INSTALLED_RELEASE/infra/worker/runtime/runtime-activation.json"
sudo "$WORKER_ROOT/bin/worker-compose" config --quiet
set -Eeuo pipefail
sudo "$WORKER_ROOT/bin/worker-compose" run --rm --no-deps worker --check \
  | python3 -m json.tool
```

`RVC_GPU_SMOKE_VERIFIED=false`인 runtime 후보는 위 명령을 의도적으로 거부한다.
`--allow-unverified-gpu-runtime`은 production 확인이 아니므로 승인된 release-engineering
시험 외에 추가하지 않는다. 설치 exit code 0만으로 native 학습을 시작하지
않고 `TESTING.md`의 ledger, image/runtime activation, GPU/no-network 인수를 먼저 끝낸다.

## 4. 실제 시작 gate

아래 조건을 모두 충족한 runtime 후보에서만 Worker를 시작한다.

- `SELF_CONTAINED=true`, runtime/native/GPU/profile gate true
- image `linux/amd64`, user `10001:10001`, expected release label/digest
- Manager/Object HTTPS 인증서 trust와 연결
- Public CA 또는 installer가 고정 read-only mount한 custom CA로 Manager/Object hostname 검증 PASS.
  Host trust store에만 추가한 CA, `verify=false`, `curl -k`는 증거가 아님
- 설치된 token/profile/data/activation 권한과 exact release ledger PASS
- 실제 GPU core matrix; Sample은 qualification과 inference gate까지 true인 경우에만 허용

```bash
sudo systemctl daemon-reload
sudo systemctl enable rvc-orchestrator-worker.service
sudo systemctl restart rvc-orchestrator-worker.service
sudo /opt/rvc-orchestrator/worker/bin/worker-compose ps
sudo /opt/rvc-orchestrator/worker/bin/worker-compose logs --tail=200 worker
```

Worker에는 자동 rollback script가 없다. Upgrade 전에 이전 bundle, runtime compatibility,
credential과 local pending spool을 별도로 검토한다.
Upgrade는 target bundle의 `sudo ./upgrade.sh . ...`만 사용하며 strict SemVer상 같은/낮은
version은 거부한다. Uninstall은 token/profile/custom CA/data를 보존하지만 systemd/Compose stop 일부
실패 시 exit 1이며, exit 0 뒤에도 inactive/no-container 상태를 확인한다.

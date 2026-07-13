# 공급망·SBOM 기준선

Manager/Worker bundle builder는 CycloneDX 1.6 JSON inventory와 별도 third-party license
declaration report를 `supply-chain/`에 생성하고 내부/외부 SHA-256으로 보호한다.

```bash
python3 tools/generate_supply_chain_report.py \
  --component manager --version 0.1.0-dev.20 \
  --output-dir /tmp/rvc-manager-supply-chain
python3 tools/generate_supply_chain_report.py \
  --component worker --version 0.1.0-dev.20 \
  --output-dir /tmp/rvc-worker-supply-chain
```

입력은 Manager API exact runtime lock, MLflow의 `infra/mlflow/requirements.lock` exact overlay,
Worker Agent/Fake exact runtime lock, Web npm lock의 package version/license/integrity와 모든
Dockerfile/환경의 기본 container image reference다. 같은 Python package가 여러 lock에 있으면
component는 한 번만 만들고 모든 source lock 경로를 property/notice에 보존한다. Python license 표현은
현재 lock version과 함께 `supply-chain/python-runtime-licenses.json`에서 관리한다. version을
올리면 새 배포판 metadata와 upstream license file을 확인한 뒤 catalog를 명시적으로
갱신해야 하며, 누락 entry는 report 생성을 실패시킨다. npm entry도 version, license 또는
integrity가 없으면 실패한다.

## report가 증명하는 것

- bundle을 만들 때 선언된 exact dependency version inventory
- npm registry artifact의 lockfile SHA-256/384/512 integrity
- package metadata/package-lock에서 얻은 declared license expression
- source lock 문서 자체의 SHA-256
- API/Web/MLflow/Worker Dockerfile과 runtime 환경에 쓰인 container image reference 전체,
  source 위치와 immutable digest 존재 여부

MLflow overlay는 boto3/botocore 및 transitive helper와 `psycopg2-binary==2.9.10`을 exact version으로
고정하고 Dockerfile이 `--no-deps --only-binary=:all:`로 그 lock만 설치한다. License report의
container record는 source/reference/digest 상태를 보여 주지만 사람이 검토하기 전까지
`container-license-not-reviewed` notice를 유지한다.

## report가 아직 증명하지 않는 것

현재 report는 의도적으로 `SBOM_STATUS=partial-release-gates-open`이며 다음 값을 숨기지
않고 SBOM property로 기록한다.

- Python distribution hash는 API/MLflow/Agent image lock에 아직 없다.
- tag 기반 기본 container image는 immutable digest가 아니다.
- Native Worker `Dockerfile.rvc`의 base는 release 입력이 없을 때
  `offline-rvc-base-image-must-be-provided` sentinel로 남으며 검증된 base 증거가 아니다.
- vulnerability, container, secret, SAST scan을 실행하지 않았다.
- declared license는 법률 검토나 재배포 허가가 아니다.
- 실제 RVC runtime의 wheel/source/asset 상세는 offline build manifest에서 검증하지만
  아직 하나의 release SBOM으로 합쳐지지 않는다.

실제 RVC runtime builder는 reviewed source archive를 검증·추출한 뒤 native stage가 읽을
`infer`, `configs`, pretrained/HuBERT/RMVPE, `runtime/crepe/full.pth`와 mute asset의 path,
size, SHA-256, mode를
`projection-manifest.json`에 고정한다. 이 manifest의 SHA-256은 source tree lock,
runtime build manifest, image label과 Worker bundle manifest가 모두 같은 값이어야 한다.
Worker는 시작·claim 시 현재 파일 전체를 다시 검증하고, private projection을 만들 때도
manifest에 기록된 regular file만 `O_NOFOLLOW` file descriptor로 열어 바로 그 byte의
streaming SHA-256을 확인한다. 이는 build 뒤 source 교체와 검사/복사 사이 TOCTOU를
fail-closed하지만 image archive 자체의 서명·배포 digest나 자산 재배포 권리를 증명하지는
않는다.

Runtime image build 자체도 clean 40-hex committed orchestrator source와 release source closure를
요구한다. Image에 넣는 Worker, contracts와 runtime helper는 working tree 재귀 복사가 아니라 exact
commit의 Git archive에서 추출하며, build는 preloaded reviewed base를 쓰는 amd64 Docker daemon에서
`--platform linux/amd64`, `--network=none`, `--pull=false`로만 수행한다. 이 경계는 ignored host cache와
dirty source가 commit label 아래 섞이는 것을 막지만 base digest 선정, 재현 build 또는 GPU 검증을
대신하지 않는다.

Bundle format 2의 `images-manifest.json`은 self-contained Manager의 정확한 8개 역할과 Worker
runtime 1개를 source/runtime reference, Docker image/config digest, `linux/amd64`, archive
SHA-256/size, 실제 `Config.User` 및 application OCI release label에 결박한다. Docker-save의 config
member byte를 path의 SHA-256과 다시 대조하고 Manager API `10001:10001`, Web `nextjs`, MLflow
`10002:10002`, Worker runtime `10001:10001`을 강제한다. 설치기는 archive 내부 RepoTag/Config를
load 전에 검사하고 load 뒤와 start, Manager rollback 때 같은 identity를 재검증한다. 이 closure는 설치
입력의 폐쇄성을 증명하지만 base image 재현 빌드, signature, vulnerability scan 또는 라이선스
승인을 대신하지 않는다. dev.20 Manager 후보는 committed source `298ee1e…`와 정확히 8개
`linux/amd64` image를 기록한 `self_contained=true` archive이며 strict archive/loaded identity와
release-image Compose smoke를 통과했다. 반면 dev.20 Worker는 같은 commit을 기록하지만
`self_contained=false`, 빈 image inventory와 닫힌 runtime gate를 유지한다. Manager의 local amd64
emulation PASS도 clean Ubuntu, scan·서명·license 또는 Worker GPU release attestation은 아니다.

dev.13이 역사적으로 도입하고 dev.16~dev.18이 이어가는 archive `SHA256SUMS`는 나열된 파일만 검사하는
목록이 아니라 추가·누락·unsafe 파일을
거부하는 exact inventory다. 압축 해제 뒤
`python3 common/image_bundle.py verify-ledger --root . --ledger-name SHA256SUMS`로 검증하고,
installer가 원자 게시한 release에서는 mode `0444` `RELEASE_SHA256SUMS`를 같은 방식으로 검사한다.
Compose start/restart와 Manager rollback도 이 installed ledger와 manifest/environment binding을
다시 확인한다. 이 file closure는 report·manifest byte의 설치 후 변조를 탐지하지만 source
서명, 재현 빌드, vulnerability scan 또는 법률 승인을 추가로 증명하지 않는다.

dev.18 installer는 extracted bundle의 `SHA256SUMS`가 없으면 manifest까지 함께 제거해도
fail-closed한다. Ledger 생략은 필수 source 경로가 있고 physical Git top-level이 정확히 같은 실제
개발 source root에서 caller가 source mode를 명시한 경우에만 허용한다. Self-contained builder는
Git clean/commit 검사 뒤 `git check-ignore --no-index` 기반 release source closure를 실행해 필요한
BFF route 같은 source가 broad ignore 규칙에 가려진 상태를 거부한다. dev.14 이하 archive는 이
검사를 소급해 갖지 못하므로 새 설치에 사용하지 않는다.

dev.14의 `host-access` network, bundled proxy foreground command/fallback과 전체 Compose smoke,
dev.15의 source/image-user/installer activation 보정, dev.16의 MLflow overlay lock과
release-readiness 도구, dev.17의 Worker custom CA projection/context 및 bundle-local native
negative runbook, dev.18의 model registry API/BFF/UI는 application/infra byte로 이 exact ledger에
포함돼야 한다. 이 변경은 dependency lock이나
빈 image closure를 self-contained 증적으로 승격하지 않으며, local arm64 Compose PASS도
linux/amd64 image provenance·취약점·라이선스·실제 GPU/TLS release gate를 열지 않는다.

Worker runtime qualification은 SBOM과 별도 증적이다. Exact 49-case report tar.gz, raw
qualification, runtime image/build/asset identity를 검증한 뒤 builder가 작은 activation projection을
만든다. 내부 `SHA256SUMS`, image manifest와 설치/start 검증이 qualification/evidence byte까지
보존하지만 제3자 서명이나 시험 실행의 진실성, 취약점 없음 또는 법률 승인을 대신하지 않는다.

따라서 현재 파일을 완전한 release attestation, 취약점 없음 또는 재배포 권리 확인으로
표현하면 안 된다. v1.0 전에는 Python `--require-hashes`, 모든 image digest, real-RVC
runtime SBOM merge, OS package inventory, 취약점/secret/container scan과 사람의 license
검토를 추가하고 `partial-release-gates-open`을 검증 절차를 통해서만 변경한다.

[CycloneDX 1.6 JSON schema](https://cyclonedx.org/schema/bom-1.6.schema.json)와
[SPDX license expression](https://spdx.github.io/spdx-spec/v3.0.1/annexes/spdx-license-expressions/)
규칙을 사용한다. 복수 license 선택은 `OR`, 모두 적용은 `AND`로 구분하며 알 수 없는
조건을 임의의 SPDX ID로 바꾸지 않는다.

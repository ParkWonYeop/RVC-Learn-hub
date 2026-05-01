# Worker runtime qualification과 Sample 활성화

마지막 갱신: 2026-07-12

이 문서는 실제 NVIDIA GPU/no-network 시험 결과를 Worker 설치 bundle의 운영 capability로
연결하는 절차를 정의한다. 코드 fixture 통과나 사람이 `true`로 바꾼 환경변수는 qualification이
아니다. 현재 저장소와 partial 설치 bundle에는 승인된 실제 증적이 없으므로 activation은 꺼져 있다.

## 신뢰 사슬

활성화 경계는 다음 순서로만 열린다.

```text
외부 installer SHA-256
→ 내부 SHA256SUMS
→ images-manifest.json의 exact linux/amd64 runtime image ID
→ runtime build/asset manifest
→ 49개 필수 case report를 담은 evidence tar.gz
→ strict qualification.json
→ builder가 생성한 runtime-activation.json
→ 설치 host의 image/evidence 재검증
→ /run/rvc-release/runtime-activation.json 고정 read-only mount
→ Worker factory dependency binding과 Agent capability
```

`worker.env`, YAML, CLI로 activation 경로, image digest 또는 Sample verified flag를 지정할 수
없다. Worker는 `/run/rvc-release/runtime-activation.json`만 읽는다. 파일이 없거나 정확한 disabled
template이면 Sample capability는 비활성화된다. 파일이 존재하지만 schema, permission, digest 또는
asset hash가 다르면 Worker 시작을 실패시킨다.

## 필수 49개 case

`infra/worker/runtime/qualification.py`가 case ID의 정확한 집합을 코드로 고정한다.

- core 8개: `v1|v2 × 40k|48k × f0-off|f0-on`
- training F0 5개: `pm`, `harvest`, `dio`, `rmvpe`, `rmvpe-gpu`
- Sample 32개: `v1|v2 × 40k|48k × index-off|index-on × pm|harvest|crepe|rmvpe`
- 운영 4개: cancellation, restart-recovery, telemetry-spool, no-public-egress

각 case는 `passed` 결과, 고정 `reports/<case-id>.json` 경로와 실제 report SHA-256을 가진다.
evidence archive에는 이 49개 regular file만 있어야 한다. 누락·추가·중복·symlink·빈 report,
경로 탈출, report/archive size 또는 hash 불일치는 모두 거부된다. 이 도구는 시험을 대신 실행하지
않으며, release reviewer가 제공한 결과 byte를 정확한 runtime과 결박한다.

## qualification 입력

최상위 JSON은 다음 exact field만 허용한다.

```json
{
  "format_version": 1,
  "kind": "rvc-native-runtime-qualification",
  "runtime": {},
  "cases": [],
  "evidence_archive": {},
  "review": {}
}
```

`runtime`은 exact runtime image digest, release/orchestrator/RVC commit, digest-pinned base,
source/wheelhouse/asset/projection manifest SHA-256, fairseq commit과 고정
Torch/Torchvision/Torchaudio/CUDA/cuDNN 버전을 포함한다. `evidence_archive`는 safe `.tar.gz`
basename, size와 SHA-256을 포함한다. `review`에는 strict UTC timestamp와 reviewer ID를 기록한다.
중복/미지 field와 반복 placeholder hash는 거부된다. 전체 schema와 case ID는 verifier 및
`tests/infra/test_runtime_qualification.py`를 기준으로 한다.

## projection을 단독 검증하는 방법

release 입력을 준비한 뒤 다음 명령으로 검증과 projection 생성을 함께 수행한다.

```bash
python3 infra/worker/runtime/qualification.py project \
  --qualification /offline/qualification/runtime-qualification.json \
  --evidence-archive /offline/qualification/runtime-evidence.tar.gz \
  --runtime-build-manifest /offline/runtime/rvc-runtime-build.env \
  --asset-manifest /offline/assets/assets-manifest.json \
  --runtime-image-digest sha256:<64hex> \
  --output /offline/qualification/runtime-activation.json
```

출력 경로가 이미 존재하거나 symlink이면 덮어쓰지 않는다. 성공 출력은 mode `0444`이며 세 gate가
모두 true이고 inference F0 목록은 정확히 `pm, harvest, crepe, rmvpe`다. 비활성 template은 다음처럼
생성할 수 있다.

```bash
python3 infra/worker/runtime/qualification.py disabled \
  --output /tmp/runtime-activation.json
```

## qualified Worker bundle 생성

실제 qualification을 bundle에 반영하려면 기존 runtime 네 입력과 함께 증적 두 입력을 모두
제공한다.

```bash
installers/worker/build-bundle.sh \
  --version <version> \
  --self-contained \
  --include-rvc-runtime-image rvc-orchestrator-worker:<version> \
  --rvc-runtime-assets /offline/assets \
  --rvc-runtime-asset-manifest /offline/assets/assets-manifest.json \
  --rvc-runtime-build-manifest /offline/runtime/rvc-runtime-build.env \
  --rvc-runtime-qualification /offline/qualification/runtime-qualification.json \
  --rvc-runtime-qualification-evidence /offline/qualification/runtime-evidence.tar.gz
```

qualified bundle은 40-hex committed clean source, exact self-contained runtime image와 전체 증적을
요구한다. builder는 사용자 제공 activation file을 받지 않고 검증 결과로 직접 생성한다. 입력 두
개 중 하나만 주거나 `--self-contained`가 없거나 runtime/image/asset identity가 다르면 실패한다.

설치와 Compose start 시에는 activation, qualification JSON, evidence archive, asset manifest,
image manifest와 실제 loaded image ID를 다시 대조한다. 설치 과정은 Python tar 추출기가 mode를
정규화한 경우에도 release-owned activation을 `0444`로 다시 고정한다. Compose는 literal relative
host path를 read-only로 mount하며 Docker socket은 Worker에 제공하지 않는다.

## Manager 활성화

Worker가 qualified capability를 광고해도 Manager 설정 두 개를 별도로 승인해야 Sample Job이
배정된다.

```dotenv
AUTO_SAMPLE_JOBS_ENABLED=true
SAMPLE_APPROVED_RUNTIME_BUNDLES=sha256:<runtime-image-64hex>@<asset-manifest-64hex>
```

이 값은 실제 qualification 검토가 끝난 release에서만 설정한다. 두 값 중 하나가 없거나 Worker가
광고한 pair와 다르면 Manager는 Sample Job을 배정하지 않는다. `--allow-unverified-gpu-runtime`은
미검증 core 후보를 명시적으로 시험하는 옵션일 뿐 Sample activation을 열지 않는다.

## 현재 판정

- verifier, builder projection, installer/read-only mount, production factory와 capability 연결은
  자동 fixture로 검증됐다.
- 실제 CREPE weight 출처·재배포 승인, reviewed amd64 base digest, NVIDIA GPU 49-case 실행,
  public egress 차단 증적과 vulnerability/container/license 검토는 아직 없다.
- 따라서 현재 배포 기준은 disabled activation, 빈 inference F0 capability,
  `AUTO_SAMPLE_JOBS_ENABLED=false`이며 production Sample은 계속 `BLOCKED`다.


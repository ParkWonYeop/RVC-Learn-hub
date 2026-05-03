# RVC Orchestrator Web

Next.js 16 App Router 기반 중앙 운영 대시보드다. 브라우저는 Manager JWT를 직접
다루지 않으며 Web의 `/session/*`, `/bff/*` route handler와 Server Component만
Manager에 Bearer 요청을 보낸다.

## 로컬 실행

```bash
npm install
API_INTERNAL_URL=http://127.0.0.1:8000 npm run dev
```

Node.js 20.9 이상이 필요하다.

## 서버 환경 변수

- `API_INTERNAL_URL`: Web server에서 접근 가능한 Manager origin. 기본
  `http://127.0.0.1:8000`.
- `PUBLIC_SCHEME`: 외부 사용자가 접속하는 신뢰된 공개 scheme인 `http|https`. 설치
  Compose가 명시하며 production은 `https`만 허용한다. Web은 클라이언트가 보낸
  `X-Forwarded-Proto`로 Secure cookie 또는 same-origin protocol을 결정하지 않는다.
- `SESSION_COOKIE_SECURE`: `PUBLIC_SCHEME`이 없는 로컬/legacy 실행에서만 쓰는
  `true|false` fallback이다. production 배포의 공개 scheme 제어 수단으로 사용하지 않는다.
- `DASHBOARD_DEMO_MODE`: 정확히 `true`일 때만 Fake fixture를 표시한다. 기본값은
  false이며 실제 API의 빈 값 대신 예제 숫자를 채우지 않는다.
- `DATASET_UPLOAD_ALLOWED_ORIGINS`: Manager가 반환한 Dataset upload target 중 Web BFF가
  허용할 origin의 comma-separated allowlist다. browser가 접근하는 MinIO/S3 presign
  origin을 정확히 지정한다. 현재 Compose는 `S3_PRESIGN_ENDPOINT_URL`을 전달한다.

로그인은 `/session/login`, 로그아웃은 `/session/logout` BFF를 사용한다. 경로를
`/api/*` 아래에 두지 않는 이유는 설치 Nginx가 `/api/` 전체를 FastAPI로 직접
라우팅하기 때문이다. 세션 cookie는 `HttpOnly`, `SameSite=Strict`, `Path=/`이며
HTTPS 요청에서는 `Secure`다. JWT를 localStorage/sessionStorage 또는 browser JSON
응답에 저장하지 않는다.

Job 관측 BFF는 browser fetch metadata와 Origin/forwarded Host를 검증하고 Job/Artifact
ID, query key, 값 길이·형식을 allowlist로 제한한다. JSON 응답과 SSE에는 `no-store`,
SSE에는 추가로 `X-Accel-Buffering: no`를 적용한다. browser 또는 route가 stream을
취소하면 upstream reader와 fetch signal도 함께 종료한다. EventSource 재연결 시에는
최신 `Last-Event-ID`만 Manager로 전달해 오래된 `after` query와 충돌하지 않게 한다.

Experiment/Job 생성 BFF도 같은 Origin/forwarded Host/Fetch Metadata 경계를 사용한다.
브라우저가 Manager path나 header를 선택할 수 없으며 Experiment와 전체 JobConfig body는
exact-key schema와 streaming byte 상한을 통과해야 한다. 응답은 public field로 다시
투영하고 `409`, `422`, `429`와 검증된 `Retry-After`만 보존한다.
MLflow fail-closed `503`이 `ledger_committed=true`를 명시하면 safe resource ID만 별도
투영해 UI가 이미 생성된 Experiment/Job을 중복 제출하지 않는다.

## 현재 기능 경계

- Worker, Dataset, Experiment, Job 목록은 Server Component가 Manager에서 200개 단위로
  끝까지 읽는다. total/offset/limit, 진행 여부, total 안정성과 ID 중복을 검증하며 자원별
  10,000개 상한을 넘으면 일부 결과를 전체처럼 표시하지 않고 명시적 제한 상태를 보인다.
- Job 목록은 Manager의 `status`, `experiment_id` 필터를 사용하며 상한 안에서 완전히
  검증한 조건 결과를 작업명·실험명·Worker·F0 기준으로 browser에서 검색한다.
- Job 상세는 상태·설정·오류·시각과 실제 log/metric/artifact read API를 표시한다.
  로그는 tail/cursor/attempt와 SSE 연결 상태를 제공하고 level·메시지는 현재 조회분에서
  검색한다. 메트릭은 최신 200개를 `tail=true`로 가져오고 15초마다 이전 요청과 겹치지
  않게 갱신하며, attempt/key/epoch/step 필터, 표와 key별 간단 그래프를 제공한다.
  `system.gpu.telemetry_available`은 GPU가 실제로 0개인 경우와 수집 실패를 구분한다.
- Overview, Job 목록과 상세의 실행 엔진은 API의 exact `current_attempt_engine_mode`만 사용한다.
  Attempt 전에는 `실행 전`, real은 `RVC WebUI`, Fake는 `FAKE · 운영 결과 아님` badge와 접근 가능한
  경고를 표시한다. JobConfig backend를 fallback하거나 Worker가 광고한 engine capability를 현재
  Job의 실행 결과로 추정하지 않는다.
- Artifact에는 type, 크기, SHA-256, attempt를 표시한다. 다운로드 버튼은 same-origin
  BFF에서 권한을 재검사한 뒤 Manager가 발급한 만료 redirect 또는 인증 stream을
  전달하며 storage URI와 JWT는 browser JSON에 포함하지 않는다.
- Job 취소와 실패 작업 재시도는 각각 Manager의 `cancel`, `retry` endpoint에 연결되어
  있으며 현재 상태가 허용하는 경우에만 UI가 활성화된다. Manager가 최종 상태 전이를
  다시 검증한다.
- Worker 전체 목록은 API와 동일하게 admin 역할만 볼 수 있다.
- loading, error, empty, unauthorized/session-expired 상태를 제공한다.
- Dataset은 browser chunk SHA-256, 멱등 init, local/S3 raw PUT, finalize와 품질 보고서,
  상세/삭제 흐름을 제공한다. 업로드 descriptor는 BFF가 method/origin/header를 검증하고
  browser memory에서 한 번만 사용한다. 외부 target에는 `XMLHttpRequest` progress를
  제공하며 credential 전송을 끈다. 같은 origin target은 JWT cookie를 절대 동봉하지
  않도록 `fetch(credentials: "omit")`를 사용하므로 세부 byte progress 대신 단계 상태를
  표시한다.
- Dataset 목록/상세는 Manager가 확정한 BS.1770-4 integrated LUFS와 algorithm/scope/block/gate
  metadata만 투영한다. 값이 없을 때는 migration 전 기존 행, 짧은 음원, 절대 gate 미만,
  지원하지 않는 channel layout/sample rate를 구분하며 file LUFS 평균을 UI에서 재계산하지 않는다.
- Dataset list/detail BFF는 API 응답을 public field로 다시 투영해 storage URI를 숨긴다.
  품질 보고서의 duplicate/rejected/skipped/decoder pending과 authoritative
  clipping/silence/RMS/LUFS aggregate를 표시한다. Historical null이나 decoder 대기 값을 0으로
  추정하지 않는다.
- ready이면서 `is_usable=true`인 Dataset으로 Experiment를 생성할 수 있다. Experiment
  상세에서는 v1/v2, 40k/48k, use_f0와 학습 F0 5종의 Cartesian 조합을 최대 16개까지
  preview한다. epoch/batch/checkpoint/GPU IDs/index/VRAM/tag/priority도 immutable JobConfig에
  포함한다.
- Job 이름은 조건과 설정에서 안전하고 결정적으로 생성한다. 제출 직전 기존 Experiment
  Job 이름을 최대 200개 단위로 모두 조회해 중복 요청을 막고, 단건 API를 순차 호출해
  부분 성공, 409/422, 429 `Retry-After`와 미제출 항목을 구분한다. 전송 오류가 나도 이미
  확정한 success/conflict/error 행은 보존하고, 응답이 유실된 현재 POST와 실제 미제출
  후보를 서로 다르게 표시한다.
- Experiment create에서 정상 2xx 또는 `ledger_committed=true`를 확인하면 navigation이
  늦어져도 form을 terminal submitted 상태로 잠근다. 다만 HTTP 응답 자체가 유실되면 현재
  API에는 Experiment idempotency key가 없어 commit 여부를 자동 판별할 수 없으므로,
  form을 `uncertain` 상태로 잠그고 사용자가 Experiment 목록을 확인한 뒤 페이지를 다시
  열어야 한다. 새 페이지/다른 client의 재제출까지 막는 것은 API idempotency가 필요하다.
- 고정 TestSet 모델이 아직 없으므로 생성 UI는 `auto_inference_samples.enabled=false`,
  `test_set_id=null`, `collect_samples=false`만 전송한다. 이미 원장에 검증된 Sample이 있는 경우의
  player와 동일 TestSet item current-attempt A/B 비교는 제공하지만 새 production Sample 자동 생성
  gate는 실제 GPU/no-network qualification 전까지 닫혀 있다.

## 검증

```bash
npm test
npm run lint
npm run build
```

Vitest 회귀 테스트는 API projection의 미제공 값 보존, 세션 route의 same-origin 및
HttpOnly cookie 규칙, Job mutation allowlist와 Demo mode write 차단, 관측 BFF의
path/query/origin 방어, JWT 비노출, SSE 취소 전파, download downgrade 차단과 Job 목록
API filter 전달을 확인한다. Dataset suite는 private field 제거, same-origin mutation,
upload origin/header/downgrade 방어, 429 `Retry-After`, finalize timeout 경계, 삭제 충돌과
incremental SHA-256을 확인한다.
Experiment suite는 Origin/Host/Fetch Metadata, 고정 path/query, arbitrary field/header와
body byte 상한, HttpOnly JWT 비노출, public response projection, 409/422/429 전달을
검증한다. Job matrix suite는 결정적 고유 이름, 16개 조합 상한, no-F0 계약, GPU/tag
파싱과 sample 강제 비활성화, 부분 성공 뒤 후속 POST 응답 유실 상태 보존을 확인한다.
Dashboard data suite는 다중 page 완전 수집, 전역 Experiment run count, 상세 Job pagination,
상한 초과 명시 상태와 비진행/변조 envelope fail-closed를 확인한다.

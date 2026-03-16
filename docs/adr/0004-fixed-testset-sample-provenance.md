# ADR-0004: 고정 TestSet과 자동 Sample의 불변 provenance

- 상태: Accepted, central/Worker sample path implemented; release runtime/GPU matrix pending
- 날짜: 2026-07-11
- 관련 요구사항: `API-003`, `RVC-005`, `RVC-012`, `UI-005`, `UI-006`

## 배경

같은 Dataset을 서로 다른 RVC 조건으로 학습한 결과를 귀로 비교하려면 입력 음원과 추론
설정이 Run마다 같아야 한다. 단순히 `sample.wav` Artifact만 저장하면 어떤 입력, 모델,
index와 F0 설정으로 만들었는지 증명할 수 없고 TestSet이 나중에 바뀌면 비교도 재현되지
않는다. 고정 음원은 사용자 Dataset과 별개의 민감 데이터이며 사용·재배포 권리도 따로
확인해야 한다.

## 결정

### 1. TestSet은 수정 대신 revision을 만든다

`TestSet`은 이름, revision, 상태, manifest SHA-256과 생성자를 가진다. `TestSetItem`은
안전한 item ID/표시명, canonical WAV object key, byte size, SHA-256, sample rate, channel,
duration, 정렬 순서와 license/provenance reference를 가진다. ready TestSet의 item이나
순서를 제자리에서 바꾸지 않는다. 변경은 새 revision을 만들고 기존 Job이 참조한 revision은
보존한다.

ready가 되려면 모든 item이 bounded upload/finalize, RIFF/WAVE signature, 실제 PCM decode,
size/SHA-256과 duration 상한을 통과해야 한다. 기본 세트도 저장소에 임의 음원을 넣지 않고
직접 사용·재배포 권리를 검토한 manifest와 함께 관리한다.

### 2. Preset과 Job snapshot을 분리한다

`Preset`은 inference F0(`pm|harvest|crepe|rmvpe`), transpose, index rate, protect,
RMS mix, filter radius와 resample 값을 담는 재사용 가능한 사용자 설정이다. Preset 수정은
향후 Job을 위한 새 revision일 뿐 이미 생성된 Job을 바꾸지 않는다.

Job 생성 시 `test_set_id/revision`, TestSet manifest SHA-256, resolved item 목록과 Preset
값을 canonical snapshot/hash로 Job 원장에 고정한다. Worker는 현재 Preset row를 다시 읽어
실행하지 않는다. 학습용 F0와 inference F0 enum은 계속 분리한다.

### 3. Worker 전송은 현재 lease에만 허용한다

Job claim에는 내부 storage URI나 presigned query를 넣지 않고 item별 Manager-relative
download path, filename, size, SHA-256과 MIME만 넣는다. Worker가 bearer와 현재
lease/attempt header로 GET할 때만 Manager가 짧은 S3 307 또는 bounded local stream을
만든다. 외부 object request에는 Worker credential을 전달하지 않는다.

Worker는 `O_NOFOLLOW`, mode `0600`, size/SHA-256, PCM WAV 검증과 원자 rename을 거쳐
`inputs/test_set/<item-id>.wav`에 게시한다. TestSet 일부만 받았거나 manifest와 다른 경우
sample stage는 fail-closed한다.

### 4. Sample은 Artifact에 provenance를 더한 별도 원장 row다

출력 WAV byte는 기존 검증형 Artifact upload/finalize를 재사용한다. canonical Artifact가
게시된 뒤에만 `Sample` row를 만들며 다음을 기록한다.

- Job/attempt, TestSet revision/item과 Sample Artifact ID
- input/model/index SHA-256
- inference F0와 전체 resolved inference config hash
- output size/SHA-256/sample rate/channel/duration
- peak, clipping, silence와 RMS의 고정 PCM metric
- RVC commit, approved runtime image/asset manifest digest
- native inference manifest/request SHA-256와 model/index/output 역할

`(attempt_id, test_set_item_id, inference_config_hash)`는 유일하며 멱등 재전송이 새 Sample을
만들지 않는다. sample generation이 활성화된 Job은 snapshot의 모든 item에 검증된 Sample
row가 있어야 `completed`가 된다. index를 비활성화한 Job은 `index_rate=0`만 허용하거나
명시적 no-index snapshot을 기록한다. 여러 item이 동일 PCM을 만들면 하나의 content-addressed
Artifact를 공유할 수 있지만 논리 Sample row와 input identity는 합치지 않는다. Manager가
canonical WAV를 다시 읽어 계산한 `pcm-normalized-v2` 지표를 authoritative evidence로 삼으며,
단일 출력과 attempt 전체 byte/duration 상한을 등록과 completion에서 다시 검증한다.

### 5. 비교와 다운로드도 권한 경계를 재사용한다

Sample 목록/다운로드는 Job owner/admin 권한을 적용하고 storage URI는 응답하지 않는다.
대시보드는 같은 TestSet item과 revision끼리만 A/B 열을 맞추며 model/index/config hash가
다르면 그 차이를 표시한다. browser audio player는 same-origin BFF를 통해 짧은 redirect
또는 인증 stream만 사용한다.

## 기각한 대안

- Worker image에 익명 sample WAV를 직접 내장: revision·license·삭제 정책과 중앙 비교가
  불명확해진다.
- TestSet 경로를 JobConfig 문자열로 전달: Worker filesystem을 신뢰하고 path escape와
  비교 불일치를 만든다.
- Sample을 일반 Artifact metadata JSON만으로 표현: 관계/유일성/completion gate와
  TestSet item 기준 query를 DB constraint로 보장할 수 없다.
- ready TestSet을 제자리 수정: 기존 Run의 입력 provenance와 재현성을 파괴한다.

## 구현 순서와 출시 gate

1. **완료** — TestSet/Preset와 Sample identity graph, upload data plane, 등록 교차검증
2. **완료** — Job snapshot/manifest, claim과 lease-bound TestSet download/materialization
3. **부분 완료** — pinned RVC Pipeline PM/Harvest/RMVPE, 검증 Artifact 게시; CREPE offline asset 대기
4. **완료** — Sample completion/list/download, Range player와 동일 TestSet item Experiment A/B 비교
5. 권리 검토된 실제 TestSet, GPU inference matrix와 browser audio E2E

마지막 단계 전에는 샘플 기능을 production-ready로 표시하지 않는다. Manager 배포 기본
`AUTO_SAMPLE_JOBS_ENABLED=false`를 유지한다. TestSet staging 전용 RQ cleanup, local PUT
generation/write-token fence와 finalize heartbeat는 구현했지만 실제 S3의 전역 grace보다 긴
in-flight PUT과 다중 replica 장애 주입은 운영 gate로 남는다.

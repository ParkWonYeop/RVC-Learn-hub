# ADR-0002: canonical Dataset 준비 책임

- 상태: Accepted
- 날짜: 2026-07-11

## 맥락

원문 흐름은 Manager가 업로드를 flatten하고 품질 보고서를 만들지만 Worker 의사코드도 다시 validate/flatten한다. 여러 Worker가 동일 Dataset을 반복 준비하면 결과가 달라지거나 CPU/storage를 낭비할 수 있다.

## 결정

- Manager ingestion task가 안전한 압축 해제, 실제 audio decode, 품질 검사, 파일명 정규화와 canonical flat Dataset 생성을 한 번 수행한다.
- 결과는 immutable manifest, 개별 checksum, 전체 manifest hash와 quality report로 저장한다.
- Worker는 다운로드 뒤 checksum, manifest hash와 audio 접근 가능 여부를 재검증한다.
- canonical 결과가 없거나 hash가 다른 Dataset은 Worker가 학습하지 않는다. 개발용 명시적 fallback 외에는 Worker가 자체 flat 결과를 원장으로 올리지 않는다.

## 결과

동일 Dataset 기반 실험의 재현성과 캐시 효율이 높아진다. Manager 내부 ingestion worker에 audio 처리 도구가 필요하며, GPU Worker의 `preparing_flat_dataset` 상태는 검증/로컬 materialize substage로 해석한다.


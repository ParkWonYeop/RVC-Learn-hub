# ADR-0001: 원격 Worker Job claim과 lease

- 상태: Accepted
- 날짜: 2026-07-11

## 맥락

원문은 Redis+RQ queue와 Worker의 HTTP `next-job` polling을 함께 제시한다. 일반 RQ Worker는 Redis에 직접 연결해 Python 함수를 실행하므로, 서버별 token으로 HTTPS API만 사용하는 물리 GPU Worker 경계와 충돌한다.

## 결정

- PostgreSQL을 Job 상태 원장으로 둔다.
- 원격 Worker는 `/api/v1` Worker API에서 capability에 맞는 Job을 atomic claim한다.
- claim은 `attempt_id`, 예측 불가능한 `lease_id`, `lease_expires_at`을 반환한다.
- PostgreSQL에서는 후보를 transaction 안에서 잠그며 운영 DB에서 `FOR UPDATE SKIP LOCKED` 또는 동등한 원자 조건 갱신을 사용한다.
- heartbeat가 lease를 갱신하고 취소 요청을 반환한다.
- 모든 Worker write는 Worker, attempt, lease 소유권과 만료를 확인한다.
- Redis/RQ는 Dataset staging orphan 정리 같은 중앙 내부 작업에만 사용한다. 현재 MLflow는
  별도 PostgreSQL outbox projector이며 Dataset 검증/finalize는 아직 inline이다.

## 결과

Redis를 GPU 망에 노출하지 않고 인증·감사·버전 계약을 API에 집중할 수 있다. 대신 중앙 API가 scheduling과 lease reconciliation을 명시적으로 구현해야 한다.

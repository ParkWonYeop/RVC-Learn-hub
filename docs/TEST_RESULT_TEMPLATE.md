# 사용자 테스트 결과 양식

이 파일을 복사해 한 번의 시험 실행마다 하나씩 작성한다. Source checkout에서는 설치와 명령을
`docs/INSTALLATION_GUIDE.md`, 단계별 합격 조건을 `docs/TEST_GUIDE.md`에서 확인한다. 압축 해제한
bundle에서는 같은 디렉터리의 `README.md`와 `TESTING.md`를 따른다.

비밀번호, JWT/session cookie, Worker bootstrap/issued token, access key, presigned URL query,
사용자 음성 파일명과 로컬 절대 경로는 기록하지 않는다. 필요한 경우 `[REDACTED]`로 바꾼다.

## 1. 시험 식별 정보

| 항목 | 기록 |
|---|---|
| 시험 ID | |
| 시험자 | |
| 시작/종료 시각과 timezone | |
| 결과 문서 작성 시각(UTC) | |
| source revision 또는 bundle version | |
| source working tree clean/dirty | |
| Manager archive SHA-256 | |
| Worker archive SHA-256 | |
| checksum 신뢰 출처(공식 배포 채널/담당자/서명 위치) | |
| `SOURCE-MIXED` 여부와 이유 | |

Checksum 신뢰 출처에는 archive와 함께 받은 `.sha256` 파일 자체가 아니라, 고정 SHA-256을 독립적으로
확인한 승인된 배포 공지·서명된 release metadata·조직 담당자 전달 위치를 적는다.

## 2. 시험 환경

| 항목 | Manager | Worker |
|---|---|---|
| OS / version | | |
| kernel / architecture | | |
| CPU / memory / free disk | | |
| Docker Engine / Compose | | |
| Docker daemon ID / root | | |
| GPU model / count / VRAM | 해당 없음 | |
| NVIDIA driver / toolkit | 해당 없음 | |
| Manager 공개 URL | | |
| Object 공개 URL | | |
| TLS 종단 위치 / 인증서 issuer | | |

전체 environment, `docker inspect` JSON 또는 secret 파일은 첨부하지 않는다. 문서에서 지정한
allowlist 형식의 version, image ID, architecture, user와 release label만 기록한다.

## 3. 실행 결과 요약

허용 상태는 `PASS`, `FAIL`, `BLOCKED`, `CONFIG-ONLY`, `NATIVE-CANDIDATE-UNVERIFIED`다.
낮은 단계의 PASS가 높은 단계의 PASS를 뜻하지 않는다.

| ID | 시험 | 상태 | 실행 시각 | exit code | redacted 증적 경로 | 비고 |
|---|---|---|---|---:|---|---|
| T0 | source 전체 검사 | | | | | |
| T1 | localhost Fake protocol | | | | | |
| T2-A | migration/Compose | | | | | |
| T2-B | Docker 보안 smoke | | | | | |
| T2-MAINT | Maintenance DB/Redis/S3 최소권한·heartbeat | | | | | |
| T2-C | Manager bundle 무결성 | | | | | |
| T2-D | Worker bundle 무결성 | | | | | |
| T3-CONFIG | Manager `--no-start` 설치/Compose render | | | | | |
| T3 | Manager clean-host 설치 | | | | | |
| T3-TLS | 외부 TLS/browser | | | | | |
| T3-REGISTRY | Experiment 비교·model registry lifecycle/동시성 | | | | | |
| T4 | Worker partial 구성/보호 gate | | | | | |
| T5-CORE | native GPU core matrix | | | | | |
| T5-SAMPLE | 49-case Sample/no-network | | | | | |
| T6 | 복구/rollback drill | | | | | |

## 4. 명령별 상세 기록

각 명령을 실행한 순서대로 행을 추가한다. 예상한 negative test는 nonzero exit가 정상일 수 있으므로
기대 결과와 실제 결과를 함께 적는다.

| 순서 | Test ID | 실행 명령 또는 문서 절 | 기대 결과 | 실제 결과 / exit code | 판정 |
|---:|---|---|---|---|---|
| 1 | | | | | |

## 5. 기능 확인

준비되지 않은 항목은 체크하지 말고 `BLOCKED` 사유를 6절에 적는다.

### Manager

- [ ] 외부 archive checksum, 내부 `SHA256SUMS`, strict ledger/manifest가 모두 통과했다.
- [ ] `--no-start` 설치 뒤 installed `RELEASE_SHA256SUMS`와 Compose render가 통과했다.
- [ ] `/healthz`가 liveness를 반환했다.
- [ ] `/readyz`가 dependency readiness를 반환했다.
- [ ] Migration→DB authz→RQ 순서, maintenance PostgreSQL self-verify, Redis ACL과 staging
  delete-only MinIO policy의 allow/deny matrix를 확인했다.
- [ ] 장기 cleanup 중 DB CAS heartbeat가 유지되고 ownership 유실·poison execution material이
  fail-closed하는지 확인했다.
- [ ] 외부 HTTPS login에서 Secure cookie와 HSTS를 확인했다.
- [ ] 관리자 bootstrap 후 login/logout과 관리자 사용자 lifecycle을 확인했다.
- [ ] Dataset upload/finalize/품질 조회/삭제 보호를 확인했다.
- [ ] Experiment/Job 생성, 상태, log/metric과 Artifact download를 확인했다.
- [ ] 동일 Experiment의 real/Fake 실행 표시와 metric/artifact/sample 비교를 확인했다.
- [ ] 검증된 real current attempt만 model candidate로 등록되고 Fake·변조 artifact는 거부됐다.
- [ ] candidate→approved→revoked, champion 교체·rollback과 owner/admin 경계를 확인했다.
- [ ] mutation 응답 유실 시 새 key로 재전송하지 않고 전체 registry를 다시 읽어 결과를 확정했다.
- [ ] backup archive를 만들고 checksum을 확인했다.

### Worker

- [ ] 외부 archive checksum, 내부 `SHA256SUMS`, strict ledger/manifest가 모두 통과했다.
- [ ] Partial bundle의 `fake --no-start` 구성 권한과 Compose render가 통과했다.
- [ ] Partial bundle의 native 설치가 exact fail-closed 오류로 거부됐다.
- [ ] Runtime 포함 후보의 image/runtime/source/asset identity가 일치했다.
- [ ] Worker가 같은 identity로 등록되고 heartbeat/GPU가 Manager에 표시됐다.
- [ ] v1/v2 core 학습 matrix에서 stage 재실행 없이 artifact가 게시됐다.
- [ ] Qualification이 결박된 후보에서만 Sample capability와 49-case/no-network가 통과했다.

## 6. 실패, 차단과 우회 기록

| Test ID | 분류 | 증상 또는 차단 사유 | 첫 실패 지점 | 사용한 우회 옵션 | 재현 방법 | 후속 조치 |
|---|---|---|---|---|---|---|
| | | | | | | |

`--skip-*`, `--allow-*`, 임시 image retag, 수동 gate 변경을 사용했다면 반드시 기록한다. 그런 결과를
clean-host, GPU 검증 또는 production PASS로 승격하지 않는다.

## 7. 증적 목록과 redaction 확인

| 증적 | 파일/디렉터리 | SHA-256 또는 설명 | redaction 확인 |
|---|---|---|---|
| 명령 log | | | [ ] |
| screenshot | | | [ ] |
| model registry public projection/audit allowlist | | | [ ] |
| bundle checksum/manifest allowlist | | | [ ] |
| image identity allowlist | | | [ ] |
| GPU/runtime qualification | | | [ ] |
| backup/restore 결과 | | | [ ] |

- [ ] 비밀번호, token, cookie, access key가 없다.
- [ ] URL query와 presigned credential이 없다.
- [ ] 사용자 파일명, 원본 음성/모델 byte와 절대 경로가 없다.
- [ ] 전체 environment 또는 전체 container/image inspect dump가 없다.
- [ ] 실패 log도 같은 기준으로 redaction했다.

## 8. 최종 판정

| 판정 축 | 최종 상태 | 근거 Test ID | 설명 |
|---|---|---|---|
| `BUNDLE-INTEGRITY` | | | |
| `EXECUTABLE-SOURCE` | | | |
| `FAKE-PROTOCOL` | | | |
| `MANAGER-CONFIG` | | | |
| `MANAGER-SMOKE` | | | |
| `TLS-PRODUCTION` | | | |
| `MODEL-GOVERNANCE` | | | |
| `WORKER-CONFIG` | | | |
| `WORKER-NATIVE` | | | |
| `PRODUCTION-SAMPLE` | | | |
| `AIRGAP-PRODUCTION` | | | |

전체 결론:

- [ ] 개발 기능 검증에 사용할 수 있다.
- [ ] Manager 운영 후보로 추가 검증할 수 있다.
- [ ] Worker native 후보로 추가 검증할 수 있다.
- [ ] production/air-gapped release로 승인할 수 있다.

남은 위험과 다음 시험:

<!-- 구체적인 BLOCKED/FAIL 항목, 재시험 조건과 담당자를 기록한다. -->

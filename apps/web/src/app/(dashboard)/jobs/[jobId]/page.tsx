import Link from "next/link";
import { notFound } from "next/navigation";
import {
  EngineModeBadge,
  FakeEngineResultWarning,
} from "@/components/engine-mode-badge";
import { PageHeader } from "@/components/page-header";
import { StatusPill } from "@/components/status-pill";
import { loadJobDetail } from "@/lib/server/dashboard-data";
import { JobActions } from "./job-actions";
import { JobObservability } from "./job-observability";
import { JobSamples } from "./job-samples";

export const metadata = { title: "학습 작업 상세" };

export default async function JobDetailPage({
  params,
}: {
  params: Promise<{ jobId: string }>;
}) {
  const { jobId } = await params;
  const detail = await loadJobDetail(jobId);
  if (!detail) notFound();

  const { summary, config } = detail;
  const progress = progressPercent(summary.currentEpoch, summary.totalEpoch);

  return (
    <>
      <Link className="detail-back-link" href="/jobs">
        ← 학습 작업 목록
      </Link>
      <PageHeader
        eyebrow="TRAINING / JOB DETAIL"
        title={summary.name}
        description={
          detail.demo
            ? "Demo fixture의 목록 요약입니다. 상세 설정과 변경 작업은 제공하지 않습니다."
            : "Manager JobRead가 제공하는 상태와 설정입니다. 제공되지 않은 관측값은 추정하지 않습니다."
        }
        actions={
          <JobActions
            cancelRequested={detail.cancelRequestedAt !== null}
            demo={detail.demo}
            jobId={summary.id}
            status={summary.status}
          />
        }
      />

      {detail.demo ? (
        <div className="detail-notice" role="note">
          <strong>Demo fixture</strong>
          <span>이 화면의 요약값은 운영 API 결과가 아니며 상세 필드는 채우지 않습니다.</span>
        </div>
      ) : null}

      <FakeEngineResultWarning mode={summary.engineMode} />

      <div className="job-detail-grid">
        <section className="panel job-status-panel" aria-labelledby="job-status-heading">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">CURRENT STATE</p>
              <h2 id="job-status-heading">작업 상태</h2>
            </div>
            <div className="job-state-badges">
              <EngineModeBadge mode={summary.engineMode} />
              <StatusPill status={summary.status} />
            </div>
          </div>
          <div className="job-status-body">
            <div className="job-progress-heading">
              <div>
                <span>학습 epoch</span>
                <strong>
                  {summary.currentEpoch === null
                    ? "미제공"
                    : `${summary.currentEpoch} / ${summary.totalEpoch}`}
                </strong>
              </div>
              <b>{progress === null ? "—" : `${progress}%`}</b>
            </div>
            <div className="job-progress-track" aria-label="학습 진행률">
              <span style={{ width: `${progress ?? 0}%` }} />
            </div>
            <dl className="detail-definition-grid">
              <DetailValue label="실험" value={summary.experiment} />
              <DetailValue label="Worker" value={detail.workerId} mono />
              <DetailValue label="시도 횟수" value={numberValue(detail.attemptCount)} />
              <DetailValue label="현재 attempt" value={detail.currentAttemptId} mono />
              <DetailValue label="우선순위" value={numberValue(detail.priority)} />
              <DetailValue label="경과 시간" value={summary.duration} />
            </dl>
          </div>
        </section>

        <section className="panel job-identifiers-panel" aria-labelledby="job-ids-heading">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">IDENTIFIERS</p>
              <h2 id="job-ids-heading">리소스 식별자</h2>
            </div>
          </div>
          <dl className="identifier-list">
            <DetailValue label="Job ID" value={summary.id} mono />
            <DetailValue label="Experiment ID" value={detail.experimentId} mono />
            <DetailValue label="Dataset ID" value={detail.datasetId} mono />
            <DetailValue label="Worker ID" value={detail.workerId} mono />
          </dl>
        </section>

        <section className="panel job-config-panel" aria-labelledby="job-config-heading">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">IMMUTABLE CONFIG</p>
              <h2 id="job-config-heading">학습 설정</h2>
            </div>
            <span className="config-source">JobRead.config</span>
          </div>
          {config ? (
            <dl className="config-definition-grid">
              <DetailValue label="RVC 버전" value={summary.version} />
              <DetailValue label="샘플레이트" value={summary.sampleRate} />
              <DetailValue label="F0 사용" value={yesNo(config.useF0)} />
              <DetailValue label="학습 F0 방식" value={summary.f0Method} mono />
              <DetailValue label="전체 epoch" value={String(summary.totalEpoch)} />
              <DetailValue label="GPU당 배치" value={String(config.batchSizePerGpu)} />
              <DetailValue label="GPU ID" value={config.gpuIds.join(", ")} mono />
              <DetailValue label="저장 주기" value={`${config.saveEveryEpoch} epoch`} />
              <DetailValue label="Index 생성" value={yesNo(config.buildIndex)} />
              <DetailValue label="샘플 자동 생성" value={yesNo(config.autoSamples)} />
              <DetailValue label="최소 VRAM" value={`${config.minVramGb} GB`} />
              <DetailValue
                label="선호 Worker 태그"
                value={
                  config.preferredWorkerTags.length > 0
                    ? config.preferredWorkerTags.join(", ")
                    : "지정 없음"
                }
              />
            </dl>
          ) : (
            <div className="detail-inline-empty">
              Demo fixture에는 Manager JobRead 상세 설정이 없습니다.
            </div>
          )}
        </section>

        <section className="panel job-timeline-panel" aria-labelledby="job-timeline-heading">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">TIMESTAMPS</p>
              <h2 id="job-timeline-heading">작업 시각</h2>
            </div>
          </div>
          <dl className="timeline-list">
            <DetailValue label="생성" value={formatTimestamp(detail.createdAt)} />
            <DetailValue label="시작" value={formatTimestamp(detail.startedAt)} />
            <DetailValue label="완료" value={formatTimestamp(detail.completedAt)} />
            <DetailValue label="최근 갱신" value={formatTimestamp(detail.updatedAt)} />
            <DetailValue
              label="취소 요청"
              value={formatTimestamp(detail.cancelRequestedAt)}
            />
          </dl>
        </section>
      </div>

      {detail.errorCode !== null || detail.errorMessage !== null ? (
        <section className="job-error-panel" aria-labelledby="job-error-heading">
          <div>
            <p className="panel-kicker">MANAGER ERROR</p>
            <h2 id="job-error-heading">작업 오류</h2>
          </div>
          <code>{detail.errorCode ?? "오류 코드 미제공"}</code>
          <p>{detail.errorMessage ?? "오류 메시지 미제공"}</p>
        </section>
      ) : null}

      <JobSamples
        autoSamplesEnabled={config?.autoSamples ?? false}
        enabled={!detail.demo}
        jobId={summary.id}
      />

      <JobObservability
        currentAttemptId={detail.currentAttemptId}
        enabled={!detail.demo}
        jobId={summary.id}
      />
    </>
  );
}

function DetailValue({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string | null;
  mono?: boolean;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={mono ? "detail-mono" : undefined}>{value ?? "미제공"}</dd>
    </div>
  );
}

function progressPercent(current: number | null, total: number): number | null {
  if (current === null || total <= 0) return null;
  return Math.min(100, Math.max(0, Math.round((current / total) * 100)));
}

function numberValue(value: number | null): string | null {
  return value === null ? null : String(value);
}

function yesNo(value: boolean): string {
  return value ? "사용" : "사용 안 함";
}

function formatTimestamp(value: string | null): string | null {
  if (value === null) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `${new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "Asia/Seoul",
  }).format(date)} KST`;
}

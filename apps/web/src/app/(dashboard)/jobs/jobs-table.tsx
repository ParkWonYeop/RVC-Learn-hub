"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { EngineModeBadge } from "@/components/engine-mode-badge";
import { StatusPill } from "@/components/status-pill";
import type { JobSummary } from "@/lib/types";

export function JobsTable({ jobs, total }: { jobs: JobSummary[]; total: number }) {
  const [query, setQuery] = useState("");
  const visibleJobs = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("ko-KR");
    if (!normalized) return jobs;
    return jobs.filter((job) =>
      [job.name, job.experiment, job.worker, job.f0Method, job.version, job.sampleRate, job.engineMode]
        .filter((value): value is string => value !== null)
        .some((value) => value.toLocaleLowerCase("ko-KR").includes(normalized)),
    );
  }, [jobs, query]);

  return (
    <>
      <div className="jobs-client-search">
        <label>
          <span>현재 조회 결과 검색</span>
          <input
            aria-controls="jobs-result-table"
            maxLength={200}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="작업명, 실험명, Worker, F0 검색"
            type="search"
            value={query}
          />
        </label>
        <p aria-live="polite">
          {query ? `${visibleJobs.length}개 일치` : `${jobs.length}개 표시`} · API 조건 전체 {total}개
          {total > jobs.length ? " · 검색은 현재 불러온 결과에 한정" : ""}
        </p>
      </div>
      {visibleJobs.length === 0 ? (
        <div className="observability-empty" role="status">
          {query ? "현재 조회 결과에서 검색어와 일치하는 작업이 없습니다." : "조건에 맞는 학습 작업이 없습니다."}
        </div>
      ) : (
        <div className="table-wrap jobs-table-wrap" id="jobs-result-table">
          <table>
            <thead>
              <tr>
                <th>작업 / 실험</th>
                <th>상태</th>
                <th>설정</th>
                <th>Worker</th>
                <th>진행률</th>
                <th>결과물</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {visibleJobs.map((job) => {
                const progress = progressPercent(job.currentEpoch, job.totalEpoch);
                return (
                  <tr key={job.id}>
                    <td>
                      <Link className="table-primary" href={`/jobs/${encodeURIComponent(job.id)}`}>
                        {job.name}
                      </Link>
                      <span className="table-secondary">{job.experiment}</span>
                    </td>
                    <td><StatusPill status={job.status} /></td>
                    <td>
                      <div className="job-setting-stack">
                        <div className="config-chips">
                          <span>{job.version}</span>
                          <span>{job.sampleRate}</span>
                          <span>{job.f0Method}</span>
                        </div>
                        <EngineModeBadge mode={job.engineMode} />
                      </div>
                    </td>
                    <td className="mono-cell">{job.worker ?? "미배정"}</td>
                    <td>
                      <div className="progress-label">
                        <span>
                          {job.currentEpoch === null ? "epoch 미제공" : `${job.currentEpoch} epoch`}
                        </span>
                        <b>{progress === null ? "—" : `${progress}%`}</b>
                      </div>
                      <div className="progress-track">
                        <span style={{ width: `${progress ?? 0}%` }} />
                      </div>
                    </td>
                    <td>
                      <div className="artifact-flags" aria-label="결과물 metadata 상태">
                        <ArtifactFlag label="M" ready={job.hasModel} />
                        <ArtifactFlag label="I" ready={job.hasIndex} />
                        <ArtifactFlag label="S" ready={job.hasSamples} />
                      </div>
                    </td>
                    <td>
                      <Link
                        className="row-action"
                        aria-label={`${job.name} 상세 보기`}
                        href={`/jobs/${encodeURIComponent(job.id)}`}
                      >
                        →
                      </Link>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}

function ArtifactFlag({ label, ready }: { label: string; ready: boolean | null }) {
  return (
    <span className={ready === true ? "ready" : ready === null ? "unknown" : ""}>
      {ready === null ? "?" : label}
    </span>
  );
}

function progressPercent(current: number | null, total: number): number | null {
  if (current === null || total <= 0) return null;
  return Math.min(100, Math.max(0, Math.round((current / total) * 100)));
}

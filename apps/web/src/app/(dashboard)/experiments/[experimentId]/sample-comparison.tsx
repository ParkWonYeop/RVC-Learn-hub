"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  fetchJobSamples,
  SampleReadError,
} from "@/lib/client/samples";
import { pairSamples } from "@/lib/client/sample-comparison";
import type { JobStatus, SampleListView, SampleView } from "@/lib/types";

type CompareJob = { id: string; name: string; status: JobStatus };
type CompareState = {
  status: "idle" | "loading" | "ready" | "error";
  left: SampleListView | null;
  right: SampleListView | null;
  errorStatus: number | null;
};

export function SampleComparison({
  demo,
  jobs,
}: {
  demo: boolean;
  jobs: CompareJob[];
}) {
  const eligible = useMemo(
    () => jobs.filter((job) => job.status === "completed"),
    [jobs],
  );
  const [leftJobId, setLeftJobId] = useState(eligible[0]?.id ?? "");
  const [rightJobId, setRightJobId] = useState(eligible[1]?.id ?? "");
  const [state, setState] = useState<CompareState>({
    status: eligible.length >= 2 ? "loading" : "idle",
    left: null,
    right: null,
    errorStatus: null,
  });
  const loadGeneration = useRef(0);

  useEffect(() => {
    if (demo || !leftJobId || !rightJobId || leftJobId === rightJobId) return;
    const controller = new AbortController();
    const generation = ++loadGeneration.current;
    void Promise.all([
      fetchJobSamples(leftJobId, controller.signal),
      fetchJobSamples(rightJobId, controller.signal),
    ])
      .then(([left, right]) => {
        if (controller.signal.aborted || loadGeneration.current !== generation) return;
        setState({ status: "ready", left, right, errorStatus: null });
      })
      .catch((error: unknown) => {
        if (
          isAbortError(error) ||
          controller.signal.aborted ||
          loadGeneration.current !== generation
        ) {
          return;
        }
        setState((current) => ({
          ...current,
          status: "error",
          errorStatus: error instanceof SampleReadError ? error.status : 502,
        }));
      });
    return () => controller.abort();
  }, [demo, leftJobId, rightJobId]);

  const comparison = useMemo(
    () => pairSamples(state.left, state.right),
    [state.left, state.right],
  );
  const leftJob = jobs.find((job) => job.id === leftJobId);
  const rightJob = jobs.find((job) => job.id === rightJobId);

  return (
    <section className="sample-compare-section" aria-labelledby="sample-compare-heading">
      <div className="section-heading">
        <div>
          <p className="panel-kicker">A/B · SAME TESTSET ITEM</p>
          <h2 id="sample-compare-heading">Job Sample 비교</h2>
        </div>
        <span>등록된 current-attempt Sample만 읽는 비교 화면</span>
      </div>
      <div className="panel sample-compare-panel">
        {demo ? (
          <div className="sample-compare-empty" role="status">
            Demo fixture에서는 Sample BFF를 호출하지 않습니다.
          </div>
        ) : eligible.length < 2 ? (
          <div className="sample-compare-empty" role="status">
            Sample 비교에는 완료된 Job이 두 개 이상 필요합니다.
          </div>
        ) : (
          <>
            <div className="sample-compare-controls">
              <JobSelect
                label="Job A"
                jobs={eligible}
                value={leftJobId}
                onChange={(value) => {
                  loadGeneration.current += 1;
                  setState({
                    status: value === rightJobId ? "idle" : "loading",
                    left: null,
                    right: null,
                    errorStatus: null,
                  });
                  setLeftJobId(value);
                }}
              />
              <JobSelect
                label="Job B"
                jobs={eligible}
                value={rightJobId}
                onChange={(value) => {
                  loadGeneration.current += 1;
                  setState({
                    status: value === leftJobId ? "idle" : "loading",
                    left: null,
                    right: null,
                    errorStatus: null,
                  });
                  setRightJobId(value);
                }}
              />
            </div>
            {leftJobId === rightJobId ? (
              <div className="sample-compare-warning" role="alert">
                서로 다른 두 Job을 선택해 주세요.
              </div>
            ) : null}
            {state.status === "loading" ? (
              <div className="sample-compare-empty" role="status">
                두 Job의 Sample ledger를 불러오는 중입니다.
              </div>
            ) : null}
            {state.status === "error" ? (
              <div className="sample-compare-warning" role="alert">
                {state.errorStatus === 401 ? (
                  <Link href="/session/expired">세션이 만료되었습니다. 다시 로그인해 주세요.</Link>
                ) : state.errorStatus === 403 ? (
                  "한쪽 Job의 Sample 조회 권한이 없습니다."
                ) : (
                  "Sample 비교 데이터를 불러오지 못했습니다."
                )}
              </div>
            ) : null}
            {state.status === "ready" && comparison.invalidLedger ? (
              <div className="sample-compare-warning" role="alert">
                한쪽 Sample ledger에 중복 TestSet item ID가 있어 비교를 중단했습니다.
              </div>
            ) : null}
            {state.status === "ready" && !comparison.invalidLedger && comparison.pairs.length === 0 ? (
              <div className="sample-compare-empty" role="status">
                동일한 TestSet item ID로 등록된 공통 Sample이 없습니다.
              </div>
            ) : null}
            {state.status === "ready" && !comparison.invalidLedger && comparison.pairs.length > 0 ? (
              <div className="sample-pair-list">
                {comparison.pairs.map(([left, right]) => (
                  <SamplePair
                    key={left.testSetItemId}
                    left={left}
                    leftName={leftJob?.name ?? "Job A"}
                    right={right}
                    rightName={rightJob?.name ?? "Job B"}
                  />
                ))}
              </div>
            ) : null}
          </>
        )}
      </div>
    </section>
  );
}

function JobSelect({
  label,
  jobs,
  value,
  onChange,
}: {
  label: string;
  jobs: CompareJob[];
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <label>
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {jobs.map((job) => (
          <option key={job.id} value={job.id}>
            {job.name}
          </option>
        ))}
      </select>
    </label>
  );
}

function SamplePair({
  left,
  leftName,
  right,
  rightName,
}: {
  left: SampleView;
  leftName: string;
  right: SampleView;
  rightName: string;
}) {
  return (
    <article className="sample-pair">
      <header>
        <span>TESTSET ITEM</span>
        <code>{left.testSetItemId}</code>
      </header>
      <div className="sample-pair-columns">
        <PairSide label="A" name={leftName} sample={left} />
        <PairSide label="B" name={rightName} sample={right} />
      </div>
    </article>
  );
}

function PairSide({
  label,
  name,
  sample,
}: {
  label: string;
  name: string;
  sample: SampleView;
}) {
  return (
    <div className="sample-pair-side">
      <strong>{label} · {name}</strong>
      <audio
        aria-label={`${name}의 TestSet item ${sample.testSetItemId} 변환 음성`}
        controls
        preload="none"
        src={`/bff/samples/${encodeURIComponent(sample.id)}/download`}
      />
      <dl>
        <div><dt>RMS</dt><dd>{sample.metrics.managerComputed.rms.toPrecision(5)}</dd></div>
        <div><dt>Peak</dt><dd>{sample.metrics.managerComputed.peakAmplitude.toPrecision(5)}</dd></div>
        <div><dt>Silence</dt><dd>{(sample.metrics.managerComputed.silenceRatio * 100).toFixed(2)}%</dd></div>
        <div><dt>Model</dt><dd><code>{shortHash(sample.modelSha256)}</code></dd></div>
        <div><dt>Config</dt><dd><code>{shortHash(sample.inferenceConfigSha256)}</code></dd></div>
        <div><dt>Runtime</dt><dd><code>{shortHash(sample.runtimeImageDigest)}</code></dd></div>
      </dl>
    </div>
  );
}

function shortHash(value: string): string {
  return value.length > 18 ? `${value.slice(0, 14)}…` : value;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

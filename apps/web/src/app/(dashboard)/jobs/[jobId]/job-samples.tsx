"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import {
  fetchJobSamples,
  SampleReadError,
} from "@/lib/client/samples";
import type { SampleListView, SampleMetricValues, SampleView } from "@/lib/types";

type SampleState = {
  status: "loading" | "ready" | "error";
  data: SampleListView | null;
  error: SampleReadError | null;
};

export function JobSamples({
  autoSamplesEnabled,
  enabled,
  jobId,
}: {
  autoSamplesEnabled: boolean;
  enabled: boolean;
  jobId: string;
}) {
  const [state, setState] = useState<SampleState>({
    status: "loading",
    data: null,
    error: null,
  });

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setState((current) => ({
        status: "loading",
        data: current.status === "loading" ? null : current.data,
        error: null,
      }));
      try {
        const data = await fetchJobSamples(jobId, signal);
        setState({ status: "ready", data, error: null });
      } catch (error) {
        if (isAbortError(error)) return;
        setState((current) => ({
          status: "error",
          data: current.status === "loading" ? null : current.data,
          error: error instanceof SampleReadError ? error : new SampleReadError(502),
        }));
      }
    },
    [jobId],
  );

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [enabled, load]);

  return (
    <section className="sample-section" aria-labelledby="sample-section-heading">
      <div className="section-heading sample-section-heading">
        <div>
          <p className="panel-kicker">FIXED TESTSET / VOICE SAMPLES</p>
          <h2 id="sample-section-heading">변환 샘플 음성</h2>
        </div>
        <span>HttpOnly same-origin BFF · canonical WAV 재검증</span>
      </div>

      {!enabled ? (
        <div className="panel sample-state-panel" role="status">
          Demo fixture에서는 Sample API와 음성 다운로드를 호출하지 않습니다.
        </div>
      ) : null}

      {enabled && state.status === "loading" && state.data === null ? (
        <div className="panel sample-state-panel" role="status" aria-live="polite">
          등록된 Sample과 PCM provenance를 불러오는 중입니다.
        </div>
      ) : null}

      {enabled && state.status === "error" && state.error ? (
        <SampleError error={state.error} onRetry={() => void load()} />
      ) : null}

      {enabled && state.data?.items.length === 0 ? (
        <div className="panel sample-state-panel" role="status">
          <strong>현재 attempt에 등록된 Sample이 없습니다.</strong>
          <span>
            {autoSamplesEnabled
              ? "작업 완료 전이거나 Sample ledger가 아직 완성되지 않았습니다."
              : "이 Job snapshot은 auto_inference_samples.enabled=false입니다."}
          </span>
        </div>
      ) : null}

      {state.data && state.data.items.length > 0 ? (
        <div className="sample-card-list">
          {state.data.items.map((sample, index) => (
            <SampleCard key={sample.id} sample={sample} position={index + 1} />
          ))}
        </div>
      ) : null}
    </section>
  );
}

function SampleError({
  error,
  onRetry,
}: {
  error: SampleReadError;
  onRetry: () => void;
}) {
  const unauthorized = error.status === 401;
  const forbidden = error.status === 403;
  return (
    <div className="panel sample-state-panel sample-state-error" role="alert">
      <strong>
        {unauthorized
          ? "세션이 만료되었습니다."
          : forbidden
            ? "Sample 조회 권한이 없습니다."
            : "Sample을 불러오지 못했습니다."}
      </strong>
      <span>
        {unauthorized || forbidden
          ? "음성 URL이나 Manager 오류 본문은 브라우저에 전달되지 않았습니다."
          : "Manager 연결 상태를 확인한 뒤 다시 시도해 주세요."}
      </span>
      <div className="sample-state-actions">
        {unauthorized ? (
          <Link className="button button-secondary" href="/session/expired">
            로그인 화면으로 이동
          </Link>
        ) : null}
        {!unauthorized ? (
          <button className="button button-secondary" onClick={onRetry} type="button">
            다시 시도
          </button>
        ) : null}
      </div>
    </div>
  );
}

function SampleCard({ sample, position }: { sample: SampleView; position: number }) {
  const headingId = `sample-${sample.id}-heading`;
  const downloadPath = `/bff/samples/${encodeURIComponent(sample.id)}/download`;
  return (
    <article className="panel sample-card" aria-labelledby={headingId}>
      <header className="sample-card-header">
        <div>
          <span>SAMPLE {String(position).padStart(2, "0")}</span>
          <h3 id={headingId}>TestSet item {shortIdentifier(sample.testSetItemId)}</h3>
          <small>{formatTimestamp(sample.createdAt)} · attempt {shortIdentifier(sample.attemptId)}</small>
        </div>
        <span className="sample-authority-badge">Manager PCM authoritative</span>
      </header>

      <div className="sample-audio-block">
        <audio
          aria-label={`TestSet item ${sample.testSetItemId} 변환 음성`}
          controls
          preload="none"
          src={downloadPath}
        >
          브라우저가 WAV 재생을 지원하지 않습니다.
        </audio>
        <a className="sample-open-link" href={downloadPath} target="_blank" rel="noreferrer">
          BFF에서 음성 열기
        </a>
      </div>

      <dl className="sample-output-grid">
        <Value label="길이" value={`${sample.outputDurationSeconds.toFixed(3)}초`} />
        <Value label="샘플레이트" value={`${sample.outputSampleRateHz.toLocaleString()} Hz`} />
        <Value label="채널" value={String(sample.outputChannels)} />
        <Value label="크기" value={formatBytes(sample.outputSizeBytes)} />
        <Value label="Inference F0" value={sample.inferenceF0Method} mono />
        <Value label="Index" value={sample.indexSha256 ? "retrieval 사용" : "no-index"} />
      </dl>

      <div className="sample-metric-comparison" aria-label="PCM 메트릭 비교">
        <MetricColumn
          label="Manager computed · authoritative"
          values={sample.metrics.managerComputed}
        />
        <MetricColumn label="Worker reported" values={sample.metrics.workerReported} />
      </div>

      <details className="sample-provenance">
        <summary>item / model / config / runtime provenance</summary>
        <dl>
          <HashValue label="TestSet ID" value={sample.testSetId} />
          <HashValue label="TestSet item ID" value={sample.testSetItemId} />
          <HashValue label="Input SHA-256" value={sample.inputSha256} />
          <HashValue label="Output SHA-256" value={sample.outputSha256} />
          <HashValue label="Model SHA-256" value={sample.modelSha256} />
          <HashValue label="Index SHA-256" value={sample.indexSha256 ?? "none"} />
          <HashValue label="Inference config SHA-256" value={sample.inferenceConfigSha256} />
          <HashValue
            label="Native inference manifest SHA-256"
            value={sample.nativeInferenceManifestSha256}
          />
          <HashValue
            label="Native inference request SHA-256"
            value={sample.nativeInferenceRequestSha256}
          />
          <HashValue label="RVC commit" value={sample.rvcCommitHash} />
          <HashValue label="Runtime image digest" value={sample.runtimeImageDigest} />
          <HashValue
            label="Runtime asset manifest SHA-256"
            value={sample.runtimeAssetManifestSha256}
          />
          <Value label="Metric algorithm" value={sample.metrics.algorithm} mono />
          <Value
            label="Thresholds"
            value={`clip ${sample.metrics.clippingThreshold} · silence ${sample.metrics.silenceThreshold}`}
            mono
          />
        </dl>
      </details>
    </article>
  );
}

function MetricColumn({ label, values }: { label: string; values: SampleMetricValues }) {
  return (
    <div>
      <strong>{label}</strong>
      <dl>
        <Value label="Peak" value={formatMetric(values.peakAmplitude)} />
        <Value label="RMS" value={formatMetric(values.rms)} />
        <Value label="Clipping" value={formatRatio(values.clippingRatio)} />
        <Value label="Silence" value={formatRatio(values.silenceRatio)} />
      </dl>
    </div>
  );
}

function HashValue({ label, value }: { label: string; value: string }) {
  return <Value label={label} value={value} mono />;
}

function Value({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={mono ? "sample-mono" : undefined}>{value}</dd>
    </div>
  );
}

function formatMetric(value: number): string {
  return new Intl.NumberFormat("en-US", { maximumSignificantDigits: 7 }).format(value);
}

function formatRatio(value: number): string {
  return `${(value * 100).toFixed(3)}%`;
}

function formatBytes(value: number): string {
  if (value < 1_024) return `${value} B`;
  if (value < 1_024 ** 2) return `${(value / 1_024).toFixed(1)} KiB`;
  return `${(value / 1_024 ** 2).toFixed(2)} MiB`;
}

function shortIdentifier(value: string): string {
  return value.length > 18 ? `${value.slice(0, 14)}…` : value;
}

function formatTimestamp(value: string): string {
  return new Intl.DateTimeFormat("ko-KR", {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: "Asia/Seoul",
  }).format(new Date(value));
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

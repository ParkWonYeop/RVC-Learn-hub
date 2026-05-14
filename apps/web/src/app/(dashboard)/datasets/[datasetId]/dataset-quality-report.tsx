import type { DatasetSummary } from "@/lib/types";

export function DatasetQualityReport({ dataset }: { dataset: DatasetSummary }) {
  const pcm = dataset.pcmQuality;
  const pcmUnavailable = pcmUnavailableLabel(dataset.status);
  const analysisPending = dataset.status === "upload_pending" || dataset.status === "processing";
  return (
    <div className="dataset-detail-grid">
      <section className="panel dataset-overview-panel" aria-labelledby="dataset-overview-heading">
        <div className="panel-header">
          <div>
            <p className="panel-kicker">VERIFIED SOURCE</p>
            <h2 id="dataset-overview-heading">원본과 준비 상태</h2>
          </div>
          <span className={`dataset-state dataset-state-${dataset.status}`}>
            {statusLabel(dataset.status)}
          </span>
        </div>
        <dl className="dataset-definition-grid">
          <Detail label="Dataset ID" value={dataset.id} mono />
          <Detail label="원본 파일" value={dataset.originalFilename} />
          <Detail label="MIME" value={dataset.originalMimeType} mono />
          <Detail label="원본 크기" value={formatBytes(dataset.originalSizeBytes)} />
          <Detail label="flat 크기" value={formatBytes(dataset.preparedFlatSizeBytes)} />
          <Detail label="학습 사용 가능" value={dataset.isUsable ? "예" : "아니요"} />
          <Detail label="확인된 길이" value={dataset.durationMinutes === null ? null : `${dataset.durationMinutes}분`} />
          <Detail label="파일 수" value={numberValue(dataset.fileCount)} />
          <Detail label="샘플레이트" value={dataset.sampleRate} />
          <Detail label="최근 갱신" value={dataset.updatedAt} />
        </dl>
      </section>

      <section className="panel dataset-quality-panel" aria-labelledby="dataset-quality-heading">
        <div className="panel-header">
          <div>
            <p className="panel-kicker">QUALITY REPORT</p>
            <h2 id="dataset-quality-heading">검사 결과</h2>
          </div>
          <span className="config-source">typed DatasetRead</span>
        </div>
        <div className="dataset-quality-metrics">
          <QualityMetric label="소스 항목" value={numberValue(dataset.sourceFileEntries)} />
          <QualityMetric label="포함" value={numberValue(dataset.fileCount)} />
          <QualityMetric label="중복" value={numberValue(dataset.duplicateCount)} tone={(dataset.duplicateCount ?? 0) > 0 ? "warn" : "ok"} />
          <QualityMetric label="손상/거부" value={numberValue(dataset.rejectedCount)} tone={(dataset.rejectedCount ?? 0) > 0 ? "danger" : "ok"} />
          <QualityMetric label="건너뜀" value={numberValue(dataset.skippedCount)} />
          <QualityMetric label="Decoder 대기" value={String(dataset.decoderPendingCount)} tone={dataset.decoderPendingCount > 0 ? "warn" : "ok"} />
        </div>
        <div className="pcm-quality-grid">
          <QualityMetric label="Clipping samples" value={pcm ? ratio(pcm.clippingRatio) : pcmUnavailable} />
          <QualityMetric label="Silent samples" value={pcm ? ratio(pcm.silenceRatio) : pcmUnavailable} />
          <QualityMetric label="RMS amplitude" value={pcm ? ratio(pcm.rmsRatio) : pcmUnavailable} />
          <QualityMetric
            label="Integrated loudness"
            value={pcm ? loudnessLabel(pcm.loudness) : pcmUnavailable}
          />
        </div>
        <p
          className="quality-api-note"
          role={analysisPending || dataset.status === "decoder_pending" ? "status" : "note"}
          aria-live={analysisPending || dataset.status === "decoder_pending" ? "polite" : undefined}
        >
          {pcm
            ? `${pcm.algorithm} · 검증 PCM ${pcm.validatedFileCount}개 · interleaved sample ${pcm.sampleCount.toLocaleString("ko-KR")}개를 sample-count 가중 집계했습니다. Silence threshold는 ${pcm.silenceThresholdDbfs} dBFS입니다.${loudnessExplanation(pcm.loudness)}${dataset.decoderPendingCount > 0 ? ` Decoder 대기 ${dataset.decoderPendingCount}개는 이 집계에서 제외됩니다.` : ""}`
            : pcmQualityExplanation(dataset.status)}
        </p>
      </section>

      <section className="panel dataset-checksum-panel" aria-labelledby="dataset-checksum-heading">
        <div className="panel-header">
          <div>
            <p className="panel-kicker">INTEGRITY</p>
            <h2 id="dataset-checksum-heading">검증 checksum</h2>
          </div>
        </div>
        <dl className="checksum-list">
          <Detail label="원본 SHA-256" value={dataset.originalSha256} mono />
          <Detail label="prepared_flat SHA-256" value={dataset.preparedFlatSha256} mono />
          <Detail label="manifest SHA-256" value={dataset.manifestSha256} mono />
          <Detail label="quality report SHA-256" value={dataset.qualityReportSha256} mono />
        </dl>
      </section>

    </div>
  );
}

function Detail({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string | null;
  mono?: boolean;
}) {
  return <div><dt>{label}</dt><dd className={mono ? "detail-mono" : undefined}>{value ?? "미제공"}</dd></div>;
}

function QualityMetric({
  label,
  value,
  tone = "neutral",
}: {
  label: string;
  value: string | null;
  tone?: "neutral" | "ok" | "warn" | "danger";
}) {
  return <div className={`quality-metric quality-metric-${tone}`}><span>{label}</span><strong>{value ?? "미제공"}</strong></div>;
}

function ratio(value: number | null): string | null {
  return value === null ? null : `${(value * 100).toFixed(3)}%`;
}

function loudnessLabel(
  loudness: NonNullable<DatasetSummary["pcmQuality"]>["loudness"],
): string {
  if (loudness === null) return "기존 행—LUFS 미측정";
  if (loudness.integratedLufs !== null) return `${loudness.integratedLufs.toFixed(2)} LUFS`;
  const reasons = {
    below_absolute_gate: "-70 LUFS 절대 gate 미만",
    insufficient_duration: "400 ms 완전 block 없음",
    unsupported_channel_layout: "채널 layout 미지원",
    unsupported_sample_rate: "sample rate 미지원",
  } as const;
  return loudness.unavailableReason === null ? "미제공" : reasons[loudness.unavailableReason];
}

function loudnessExplanation(
  loudness: NonNullable<DatasetSummary["pcmQuality"]>["loudness"],
): string {
  if (loudness === null) {
    return " 이 행은 LUFS migration 전에 확정되어 loudness를 재구성하지 않았습니다.";
  }
  return ` LUFS는 ${loudness.algorithm}, ${loudness.scope}로 계산했으며 ${loudness.blockDurationMs} ms block ${loudness.blockCount.toLocaleString("ko-KR")}개 중 gate 통과 ${loudness.gatedBlockCount.toLocaleString("ko-KR")}개를 사용했습니다.`;
}

function pcmUnavailableLabel(status: DatasetSummary["status"]): string {
  if (status === "upload_pending" || status === "processing") return "분석 중";
  if (status === "decoder_pending") return "Decoder 대기";
  if (status === "legacy_imported") return "기존 행—재업로드 전 집계 없음";
  return "미제공";
}

function pcmQualityExplanation(status: DatasetSummary["status"]): string {
  if (status === "upload_pending" || status === "processing") {
    return "Manager가 PCM sample-count 집계를 계산하고 있습니다.";
  }
  if (status === "decoder_pending") {
    return "검증된 PCM WAV가 없어 집계값을 만들지 않았습니다. non-WAV decoder 완료 전에는 값을 추정하지 않습니다.";
  }
  if (status === "legacy_imported") {
    return "기존 행—재업로드 전 집계 없음. exact sample count가 없어 과거 파일별 값을 재구성하지 않습니다.";
  }
  return "검증된 PCM sample aggregate가 없어 값을 추정하지 않습니다.";
}

function numberValue(value: number | null): string | null {
  return value === null ? null : String(value);
}

function formatBytes(value: number | null): string | null {
  if (value === null) return null;
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(2)} GiB`;
}

function statusLabel(status: DatasetSummary["status"]): string {
  const labels: Record<DatasetSummary["status"], string> = {
    legacy_imported: "기존 등록",
    upload_pending: "업로드 대기",
    processing: "처리 중",
    ready: "학습 가능",
    decoder_pending: "Decoder 대기",
    failed: "처리 실패",
    deleting: "삭제 중",
    delete_failed: "삭제 실패",
  };
  return labels[status];
}

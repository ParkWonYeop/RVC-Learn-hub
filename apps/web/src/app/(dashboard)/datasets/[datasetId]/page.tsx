import Link from "next/link";
import { notFound } from "next/navigation";
import { PageHeader } from "@/components/page-header";
import { dashboardDemoMode } from "@/lib/server/auth";
import { loadDatasetDetail } from "@/lib/server/dashboard-data";
import { DatasetDeleteButton } from "./dataset-delete-button";
import { DatasetQualityReport } from "./dataset-quality-report";

export const metadata = { title: "데이터셋 상세" };

export default async function DatasetDetailPage({
  params,
}: {
  params: Promise<{ datasetId: string }>;
}) {
  const { datasetId } = await params;
  const dataset = await loadDatasetDetail(datasetId);
  if (!dataset) notFound();
  const demo = dashboardDemoMode();

  return (
    <>
      <Link className="detail-back-link" href="/datasets">
        ← Dataset 목록
      </Link>
      <PageHeader
        eyebrow="DATA / DATASET DETAIL"
        title={dataset.name}
        description="Manager가 검증한 metadata와 sample-count 가중 PCM 품질 집계입니다. 제공되지 않은 값은 추정하지 않습니다."
        actions={
          <DatasetDeleteButton
            datasetId={dataset.id}
            datasetName={dataset.name}
            demo={demo}
          />
        }
      />
      {dataset.status === "decoder_pending" ? (
        <div className="detail-notice detail-notice-warning" role="note">
          <strong>Decoder 대기</strong>
          <span>
            {dataset.decoderPendingCount}개 non-WAV 파일은 격리 decoder가 준비되기 전까지 학습 입력으로 사용할 수 없습니다.
          </span>
        </div>
      ) : dataset.failureCode ? (
        <div className="detail-notice detail-notice-error" role="alert">
          <strong>{dataset.status === "delete_failed" ? "삭제 실패" : "Dataset 처리 실패"}</strong>
          <span>{dataset.failureCode} · {dataset.retryable ? "재시도 가능" : "원본을 확인해야 함"}</span>
        </div>
      ) : null}
      <DatasetQualityReport dataset={dataset} />
    </>
  );
}

"use client";

import Link from "next/link";
import { FormEvent, useMemo, useRef, useState } from "react";
import { EmptyState } from "@/components/empty-state";
import { ListLimitNotice } from "@/components/list-limit-notice";
import { PageHeader } from "@/components/page-header";
import { projectDataset } from "@/lib/api-projections";
import type {
  ApiDataset,
  ApiDatasetUploadInitRequest,
  ApiDatasetUploadInitResponse,
  ApiList,
} from "@/lib/api-types";
import { uploadDatasetObject } from "@/lib/client/dataset-upload";
import { sha256Blob } from "@/lib/client/sha256";
import type { DatasetStatus, DatasetSummary, ListLimitation } from "@/lib/types";

type UploadPhase =
  | "idle"
  | "hashing"
  | "initializing"
  | "uploading"
  | "finalizing"
  | "success"
  | "cancelled"
  | "error";

interface UploadState {
  phase: UploadPhase;
  progress: number | null;
  message: string;
  retryAfterSeconds: number | null;
}

const initialUploadState: UploadState = {
  phase: "idle",
  progress: null,
  message: "ZIP 또는 지원 audio 파일을 선택해 주세요.",
  retryAfterSeconds: null,
};
const MAX_DATASET_BYTES = 5 * 1024 ** 3;
const DATASET_PAGE_SIZE = 200;
const DATASET_COLLECTION_LIMIT = 10_000;

export function DatasetDashboard({
  demo,
  initialDatasets,
  initialTotal,
  initialLimitation,
}: {
  demo: boolean;
  initialDatasets: DatasetSummary[];
  initialTotal: number;
  initialLimitation?: ListLimitation;
}) {
  const [datasets, setDatasets] = useState(initialDatasets);
  const [total, setTotal] = useState(initialTotal);
  const [limitation, setLimitation] = useState(initialLimitation);
  const [query, setQuery] = useState("");
  const [showUpload, setShowUpload] = useState(false);
  const [datasetName, setDatasetName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [uploadState, setUploadState] = useState(initialUploadState);
  const [inputGeneration, setInputGeneration] = useState(0);
  const idempotencyKey = useRef<string | null>(null);
  const abortController = useRef<AbortController | null>(null);

  const visibleDatasets = useMemo(() => {
    const normalized = query.trim().toLocaleLowerCase("ko-KR");
    if (!normalized) return datasets;
    return datasets.filter((dataset) =>
      [dataset.name, dataset.originalFilename, dataset.status]
        .filter((value): value is string => typeof value === "string")
        .some((value) => value.toLocaleLowerCase("ko-KR").includes(normalized)),
    );
  }, [datasets, query]);
  const ready = limitation ? null : datasets.filter((dataset) => dataset.isUsable).length;
  const knownDurations = datasets.flatMap((dataset) =>
    dataset.durationMinutes === null ? [] : [dataset.durationMinutes],
  );
  const durationMinutes = knownDurations.length > 0
    ? knownDurations.reduce((sum, duration) => sum + duration, 0)
    : null;

  async function submitUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (demo || !file || isBusy(uploadState.phase)) return;
    const name = datasetName.trim();
    const contentType = supportedContentType(file);
    if (!name || name.length > 128) {
      setUploadError("Dataset 이름은 1~128자로 입력해 주세요.");
      return;
    }
    if (!contentType) {
      setUploadError("ZIP, WAV, FLAC, MP3, M4A, OGG, AAC 파일만 업로드할 수 있습니다.");
      return;
    }
    if (file.size < 1 || file.size > MAX_DATASET_BYTES) {
      setUploadError("파일은 1 byte 이상 5 GiB 이하여야 합니다.");
      return;
    }

    const controller = new AbortController();
    abortController.current = controller;
    const key = idempotencyKey.current ?? crypto.randomUUID();
    idempotencyKey.current = key;
    try {
      setUploadState({
        phase: "hashing",
        progress: 0,
        message: "브라우저에서 SHA-256을 계산하고 있습니다.",
        retryAfterSeconds: null,
      });
      const sha256 = await sha256Blob(file, {
        signal: controller.signal,
        onProgress: (processed, size) => {
          setUploadState({
            phase: "hashing",
            progress: size === 0 ? 0 : Math.round((processed / size) * 100),
            message: "브라우저에서 SHA-256을 계산하고 있습니다.",
            retryAfterSeconds: null,
          });
        },
      });
      setUploadState({
        phase: "initializing",
        progress: null,
        message: "Manager에 멱등 업로드 세션을 요청하고 있습니다.",
        retryAfterSeconds: null,
      });
      const payload: ApiDatasetUploadInitRequest = {
        name,
        filename: file.name,
        content_type: contentType,
        size_bytes: file.size,
        sha256,
        idempotency_key: key,
      };
      const initialized = await datasetJsonRequest<ApiDatasetUploadInitResponse>(
        "/bff/datasets/uploads/init",
        { body: JSON.stringify(payload), method: "POST", signal: controller.signal },
      );
      if (initialized.status === "completed" && initialized.dataset) {
        await finishUpload(initialized.dataset);
        return;
      }
      if (initialized.status !== "pending") {
        throw new UploadRequestError(
          initialized.failure_code
            ? `기존 업로드 세션을 계속할 수 없습니다 (${initialized.failure_code}).`
            : `기존 업로드 세션 상태가 ${initialized.status}입니다.`,
          initialized.retry_after_seconds,
        );
      }
      setUploadState({
        phase: "uploading",
        progress: 0,
        message: "승인된 Object Storage 대상으로 직접 전송하고 있습니다.",
        retryAfterSeconds: null,
      });
      await uploadDatasetObject(initialized, file, {
        signal: controller.signal,
        onProgress: ({ loaded, total, lengthComputable }) => {
          setUploadState({
            phase: "uploading",
            progress: lengthComputable && total > 0 ? Math.round((loaded / total) * 100) : null,
            message: lengthComputable
              ? `${formatBytes(loaded)} / ${formatBytes(total)} 전송`
              : "세션 cookie 없이 안전하게 전송 중입니다. 이 target은 세부 progress를 제공하지 않습니다.",
            retryAfterSeconds: null,
          });
        },
      });
      setUploadState({
        phase: "finalizing",
        progress: null,
        message: "Manager가 크기·SHA-256을 재검증하고 flat Dataset을 준비하고 있습니다.",
        retryAfterSeconds: null,
      });
      const finalized = await datasetJsonRequest<ApiDataset>(
        `/bff/datasets/uploads/${encodeURIComponent(initialized.upload_session_id)}/finalize`,
        { method: "POST", signal: controller.signal },
      );
      await finishUpload(finalized);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        setUploadState({
          phase: "cancelled",
          progress: null,
          message: "브라우저 작업을 중단했습니다. 이미 시작된 Manager finalize는 서버에서 계속될 수 있습니다.",
          retryAfterSeconds: null,
        });
      } else {
        const retryAfterSeconds = error instanceof UploadRequestError
          ? error.retryAfterSeconds
          : null;
        setUploadState({
          phase: "error",
          progress: null,
          message: error instanceof Error ? error.message : "Dataset 업로드에 실패했습니다.",
          retryAfterSeconds,
        });
      }
    } finally {
      abortController.current = null;
    }
  }

  async function finishUpload(dataset: ApiDataset) {
    const successMessage = dataset.status === "decoder_pending"
      ? "업로드는 완료됐지만 non-WAV decoder가 준비될 때까지 학습에 사용할 수 없습니다."
      : "검증과 canonical flat Dataset 준비가 완료되었습니다.";
    setUploadState({
      phase: "success",
      progress: 100,
      message: successMessage,
      retryAfterSeconds: null,
    });
    try {
      await refreshDatasets();
    } catch {
      setUploadState({
        phase: "success",
        progress: 100,
        message: `${successMessage} 목록 새로고침은 실패했으므로 페이지를 다시 열어 주세요.`,
        retryAfterSeconds: null,
      });
    }
  }

  async function refreshDatasets() {
    const response = await fetchCompleteDatasetList();
    setDatasets(response.limitation ? [] : response.items.map(projectDataset));
    setTotal(response.total);
    setLimitation(response.limitation);
  }

  function setUploadError(message: string) {
    setUploadState({ phase: "error", progress: null, message, retryAfterSeconds: null });
  }

  function newUploadSession() {
    abortController.current?.abort();
    idempotencyKey.current = null;
    setFile(null);
    setDatasetName("");
    setInputGeneration((value) => value + 1);
    setUploadState(initialUploadState);
  }

  return (
    <>
      <PageHeader
        eyebrow="DATA / DATASETS"
        title="데이터셋"
        description="원본을 안전하게 업로드하고 canonical flat 준비 상태와 품질 보고서를 확인합니다."
        actions={
          <button
            className="button button-primary"
            disabled={demo}
            onClick={() => setShowUpload((visible) => !visible)}
            title={demo ? "Demo fixture는 읽기 전용입니다." : undefined}
            type="button"
          >
            {showUpload ? "업로드 닫기" : "데이터셋 업로드"}
          </button>
        }
      />
      {demo ? (
        <div className="detail-notice" role="note">
          <strong>Demo fixture</strong>
          <span>예시 데이터이며 업로드·삭제 요청은 차단됩니다.</span>
        </div>
      ) : null}
      {limitation ? <ListLimitNotice limitation={limitation} /> : null}
      {showUpload && !demo ? (
        <section className="panel dataset-upload-panel" aria-labelledby="dataset-upload-heading">
          <div className="panel-header">
            <div>
              <p className="panel-kicker">BOUNDED DIRECT UPLOAD</p>
              <h2 id="dataset-upload-heading">새 Dataset 업로드</h2>
            </div>
            <span className="upload-security-label">JWT는 upload target에 전송하지 않음</span>
          </div>
          <form className="dataset-upload-form" onSubmit={submitUpload}>
            <label>
              <span>Dataset 이름</span>
              <input
                disabled={isBusy(uploadState.phase)}
                maxLength={128}
                onChange={(event) => setDatasetName(event.target.value)}
                placeholder="예: speaker-a-clean-v1"
                required
                value={datasetName}
              />
            </label>
            <label>
              <span>원본 파일</span>
              <input
                accept=".zip,.wav,.flac,.mp3,.m4a,.ogg,.aac,application/zip,audio/*"
                disabled={isBusy(uploadState.phase)}
                key={inputGeneration}
                onChange={(event) => {
                  const selected = event.target.files?.[0] ?? null;
                  setFile(selected);
                  idempotencyKey.current = null;
                  setUploadState(initialUploadState);
                  if (selected && !datasetName) setDatasetName(filenameStem(selected.name));
                }}
                required
                type="file"
              />
            </label>
            <div className={`upload-state upload-state-${uploadState.phase}`} aria-live="polite">
              <div>
                <strong>{uploadPhaseLabel(uploadState.phase)}</strong>
                <span>{uploadState.message}</span>
              </div>
              {uploadState.progress !== null ? (
                <div className="upload-progress" aria-label={`업로드 단계 진행률 ${uploadState.progress}%`}>
                  <span style={{ width: `${uploadState.progress}%` }} />
                </div>
              ) : isBusy(uploadState.phase) ? (
                <div className="upload-progress upload-progress-indeterminate" aria-label="처리 중">
                  <span />
                </div>
              ) : null}
              {uploadState.retryAfterSeconds !== null ? (
                <small>Manager 권고: {uploadState.retryAfterSeconds}초 후 재시도</small>
              ) : null}
            </div>
            <div className="dataset-upload-actions">
              <button
                className="button button-primary"
                disabled={!file || isBusy(uploadState.phase)}
                type="submit"
              >
                {uploadState.phase === "error" || uploadState.phase === "cancelled"
                  ? "동일 요청 재시도"
                  : "검증 후 업로드"}
              </button>
              {isBusy(uploadState.phase) && uploadState.phase !== "finalizing" ? (
                <button
                  className="button button-secondary"
                  onClick={() => abortController.current?.abort()}
                  type="button"
                >
                  중단
                </button>
              ) : null}
              {["success", "error", "cancelled"].includes(uploadState.phase) ? (
                <button className="button button-ghost" onClick={newUploadSession} type="button">
                  새 세션
                </button>
              ) : null}
            </div>
          </form>
        </section>
      ) : null}

      <section className="dataset-summary-row">
        <div><span>전체 데이터셋</span><strong>{total}</strong></div>
        <div><span>학습 가능</span><strong>{ready ?? "—"}</strong></div>
        <div><span>확인된 음성 길이</span><strong>{durationMinutes === null ? "—" : `${durationMinutes.toFixed(1)}분`}</strong></div>
        <div><span>Decoder 대기</span><strong>{limitation ? "—" : datasets.filter((item) => item.decoderPendingCount > 0).length}</strong></div>
      </section>
      <section className="panel">
        <div className="panel-header">
          <div>
            <p className="panel-kicker">CANONICAL INPUTS</p>
            <h2>Dataset 목록</h2>
          </div>
          <input
            aria-label="Dataset 검색"
            className="compact-search"
            onChange={(event) => setQuery(event.target.value)}
            placeholder="이름·파일·상태 검색"
            type="search"
            value={query}
          />
        </div>
        {visibleDatasets.length === 0 ? (
          <EmptyState
            title={limitation
              ? "Dataset 전체 목록을 안전하게 표시할 수 없습니다"
              : datasets.length === 0
                ? "등록된 데이터셋이 없습니다"
                : "검색 결과가 없습니다"}
            description={limitation
              ? "부분 결과는 숨겼습니다. 목록을 상한 이하로 줄인 뒤 다시 열어 주세요."
              : datasets.length === 0
                ? "위의 업로드 버튼으로 첫 Dataset을 등록할 수 있습니다."
              : "검색어를 바꾸거나 지워 주세요."}
          />
        ) : (
          <div className="dataset-list">
            {visibleDatasets.map((dataset) => (
              <article className="dataset-row" key={dataset.id}>
                <div className="dataset-file-mark" aria-hidden="true">
                  {fileMark(dataset.originalFilename)}
                </div>
                <div className="dataset-main">
                  <div>
                    <h3><Link href={`/datasets/${encodeURIComponent(dataset.id)}`}>{dataset.name}</Link></h3>
                    <span>
                      {dataset.originalFilename ?? "원본 파일명 미제공"} · {dataset.fileCount ?? "—"} files ·{` `}
                      {dataset.sampleRate ?? "sample rate 미제공"} · {dataset.createdAt}
                    </span>
                  </div>
                  <div className="dataset-list-metrics">
                    <div className="dataset-meta">
                      <span>길이 <b>{numberLabel(dataset.durationMinutes, "분")}</b></span>
                      <span>중복 <b>{nullableCountLabel(dataset.duplicateCount)}</b></span>
                      <span>손상 <b>{nullableCountLabel(dataset.rejectedCount)}</b></span>
                    </div>
                    <DatasetPcmListSummary dataset={dataset} />
                  </div>
                </div>
                <div className="quality-score">
                  <span>ISSUES</span>
                  <strong>{datasetIssueCount(dataset) ?? "—"}</strong>
                </div>
                <span className={`dataset-state dataset-state-${dataset.status}`}>
                  {datasetStatusLabel(dataset.status)}
                </span>
                <Link className="row-action" aria-label={`${dataset.name} 상세 보기`} href={`/datasets/${encodeURIComponent(dataset.id)}`}>
                  →
                </Link>
              </article>
            ))}
          </div>
        )}
      </section>
    </>
  );
}

function DatasetPcmListSummary({ dataset }: { dataset: DatasetSummary }) {
  const pcm = dataset.pcmQuality;
  const unavailable = dataset.status === "upload_pending" || dataset.status === "processing"
    ? "분석 중"
    : dataset.status === "decoder_pending"
      ? "Decoder 대기"
      : dataset.status === "legacy_imported"
        ? "기존 행—재업로드 전 집계 없음"
        : "미제공";
  const announcesState = dataset.status === "upload_pending" ||
    dataset.status === "processing" || dataset.status === "decoder_pending";
  return (
    <div
      className="dataset-pcm-inline"
      role="group"
      aria-label={`${dataset.name} PCM 품질 집계`}
      aria-live={announcesState ? "polite" : undefined}
    >
      <span>Clip <b>{pcm ? ratioLabel(pcm.clippingRatio) : unavailable}</b></span>
      <span>Silence <b>{pcm ? ratioLabel(pcm.silenceRatio) : unavailable}</b></span>
      <span>RMS <b>{pcm ? ratioLabel(pcm.rmsRatio) : unavailable}</b></span>
      <span>LUFS <b>{pcm ? listLoudnessLabel(pcm.loudness) : unavailable}</b></span>
    </div>
  );
}

function nullableCountLabel(value: number | null): string {
  return value === null ? "—" : String(value);
}

function datasetIssueCount(dataset: DatasetSummary): number | null {
  if (dataset.duplicateCount === null || dataset.rejectedCount === null) return null;
  return dataset.duplicateCount + dataset.rejectedCount + dataset.decoderPendingCount;
}

function ratioLabel(value: number): string {
  return `${(value * 100).toFixed(2)}%`;
}

function listLoudnessLabel(
  loudness: NonNullable<DatasetSummary["pcmQuality"]>["loudness"],
): string {
  if (loudness?.integratedLufs !== null && loudness?.integratedLufs !== undefined) {
    return loudness.integratedLufs.toFixed(1);
  }
  if (loudness === null) return "기존 행";
  return "측정 불가";
}

async function fetchCompleteDatasetList(): Promise<{
  items: ApiDataset[];
  total: number;
  limitation?: ListLimitation;
}> {
  const items: ApiDataset[] = [];
  const ids = new Set<string>();
  let expectedTotal: number | null = null;
  let offset = 0;
  while (true) {
    const page = await datasetJsonRequest<ApiList<ApiDataset>>(
      `/bff/datasets?limit=${DATASET_PAGE_SIZE}&offset=${offset}`,
    );
    if (
      !Array.isArray(page.items) ||
      !Number.isSafeInteger(page.total) ||
      page.total < 0 ||
      page.offset !== offset ||
      page.limit !== DATASET_PAGE_SIZE ||
      page.items.length > DATASET_PAGE_SIZE ||
      offset + page.items.length > page.total
    ) {
      throw new UploadRequestError("Dataset pagination 응답이 올바르지 않습니다.", null);
    }
    if (expectedTotal === null) {
      expectedTotal = page.total;
      if (expectedTotal > DATASET_COLLECTION_LIMIT) {
        return {
          items: [],
          total: expectedTotal,
          limitation: {
            reason: "item_limit_exceeded",
            maximum: DATASET_COLLECTION_LIMIT,
            total: expectedTotal,
            resource: "datasets",
          },
        };
      }
    } else if (page.total !== expectedTotal) {
      throw new UploadRequestError(
        "목록 조회 중 Dataset 수가 변경됐습니다. 페이지를 다시 열어 주세요.",
        null,
      );
    }
    for (const item of page.items) {
      if (
        !item ||
        typeof item.id !== "string" ||
        !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(item.id) ||
        ids.has(item.id)
      ) {
        throw new UploadRequestError("Dataset pagination에 잘못된 중복 항목이 있습니다.", null);
      }
      ids.add(item.id);
      items.push(item);
    }
    if (items.length === expectedTotal) return { items, total: expectedTotal };
    if (page.items.length === 0) {
      throw new UploadRequestError("Dataset pagination이 진행되지 않았습니다.", null);
    }
    offset += page.items.length;
  }
}

async function datasetJsonRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    cache: "no-store",
    credentials: "same-origin",
    headers: init.body === undefined ? init.headers : { "Content-Type": "application/json" },
  });
  if (!response.ok) {
    let code = "request_failed";
    try {
      const payload = await response.json() as { error?: unknown };
      if (typeof payload.error === "string") code = payload.error;
    } catch {
      // BFF error bodies are intentionally minimal; a missing body stays generic.
    }
    const retryHeader = response.headers.get("retry-after");
    const retryAfter = retryHeader && /^[0-9]+$/.test(retryHeader) ? Number(retryHeader) : null;
    throw new UploadRequestError(datasetErrorMessage(code, response.status), retryAfter);
  }
  return await response.json() as T;
}

class UploadRequestError extends Error {
  constructor(message: string, readonly retryAfterSeconds: number | null) {
    super(message);
    this.name = "UploadRequestError";
  }
}

function datasetErrorMessage(code: string, status: number): string {
  const messages: Record<string, string> = {
    conflict: "동일한 요청이 처리 중이거나 Dataset 상태가 변경되었습니다.",
    invalid_dataset: "파일 크기, SHA-256 또는 Dataset 내용 검증에 실패했습니다.",
    invalid_request: "업로드 요청 형식이 올바르지 않습니다.",
    invalid_upload_target: "Manager가 허용되지 않은 업로드 대상을 반환했습니다.",
    manager_unavailable: "Manager 또는 Object Storage를 사용할 수 없습니다.",
    payload_too_large: "Manager의 Dataset 크기 제한을 초과했습니다.",
    rate_limited: "요청이 너무 많아 Manager가 일시적으로 제한했습니다.",
    session_expired: "로그인 세션이 만료되었습니다. 다시 로그인해 주세요.",
  };
  return messages[code] ?? `Dataset 요청을 처리하지 못했습니다 (HTTP ${status}).`;
}

function supportedContentType(file: File): string | null {
  const extension = file.name.split(".").pop()?.toLowerCase();
  const defaults: Record<string, string> = {
    zip: "application/zip",
    wav: "audio/wav",
    flac: "audio/flac",
    mp3: "audio/mpeg",
    m4a: "audio/mp4",
    ogg: "audio/ogg",
    aac: "audio/aac",
  };
  if (!extension || !defaults[extension]) return null;
  const allowed: Record<string, string[]> = {
    zip: ["application/zip", "application/x-zip-compressed"],
    wav: ["audio/wav", "audio/x-wav", "audio/wave"],
    flac: ["audio/flac", "audio/x-flac"],
    mp3: ["audio/mpeg"],
    m4a: ["audio/mp4", "audio/x-m4a"],
    ogg: ["audio/ogg", "application/ogg"],
    aac: ["audio/aac", "audio/x-aac"],
  };
  return !file.type || allowed[extension]?.includes(file.type.toLowerCase())
    ? defaults[extension]
    : null;
}

function filenameStem(filename: string): string {
  const stem = filename.replace(/\.[^.]+$/, "").trim();
  return stem.slice(0, 128);
}

function isBusy(phase: UploadPhase): boolean {
  return ["hashing", "initializing", "uploading", "finalizing"].includes(phase);
}

function uploadPhaseLabel(phase: UploadPhase): string {
  const labels: Record<UploadPhase, string> = {
    idle: "업로드 준비",
    hashing: "SHA-256 계산",
    initializing: "세션 생성",
    uploading: "Object 전송",
    finalizing: "검증·평탄화",
    success: "완료",
    cancelled: "중단됨",
    error: "오류",
  };
  return labels[phase];
}

function datasetStatusLabel(status: DatasetStatus): string {
  const labels: Record<DatasetStatus, string> = {
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

function fileMark(filename: string | null): string {
  if (!filename) return "DATA";
  const extension = filename.split(".").pop();
  return extension && extension.length <= 4 ? extension.toUpperCase() : "DATA";
}

function numberLabel(value: number | null, suffix: string): string {
  return value === null ? "—" : `${value}${suffix}`;
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(2)} GiB`;
}

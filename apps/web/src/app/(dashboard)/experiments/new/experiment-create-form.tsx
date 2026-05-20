"use client";

import { useRouter } from "next/navigation";
import { FormEvent, useRef, useState } from "react";
import type { ApiExperiment, ApiExperimentCreateRequest } from "@/lib/api-types";
import {
  experimentSubmissionLocked,
  finishExperimentSubmission,
  type ExperimentSubmissionPhase,
} from "@/lib/client/experiment-submission";

interface ReadyDatasetOption {
  id: string;
  name: string;
  fileCount: number | null;
  durationMinutes: number | null;
}

export function ExperimentCreateForm({
  datasets,
  demo,
}: {
  datasets: ReadyDatasetOption[];
  demo: boolean;
}) {
  const router = useRouter();
  const [name, setName] = useState("");
  const [datasetId, setDatasetId] = useState(datasets[0]?.id ?? "");
  const [description, setDescription] = useState("");
  const [submitPhase, setSubmitPhase] = useState<ExperimentSubmissionPhase>("idle");
  const [message, setMessage] = useState("");
  const [retryAfter, setRetryAfter] = useState<number | null>(null);
  const submissionLock = useRef(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submissionLock.current || experimentSubmissionLocked(submitPhase, demo)) return;
    const trimmedName = name.trim();
    if (
      trimmedName.length < 1 ||
      trimmedName.length > 128 ||
      trimmedName === "." ||
      trimmedName === ".." ||
      /[\\/\u0000-\u001f\u007f]/.test(trimmedName)
    ) {
      setMessage("Experiment 이름은 경로 문자를 제외한 1~128자로 입력해 주세요.");
      return;
    }
    if (!datasets.some((dataset) => dataset.id === datasetId)) {
      setMessage("학습 가능한 Dataset을 선택해 주세요.");
      return;
    }
    if (description.length > 8_192) {
      setMessage("설명은 8192자 이하여야 합니다.");
      return;
    }

    submissionLock.current = true;
    setSubmitPhase("pending");
    setMessage("Manager가 Dataset 준비 상태와 소유권을 다시 확인하고 있습니다.");
    setRetryAfter(null);
    const payload: ApiExperimentCreateRequest = {
      name: trimmedName,
      dataset_id: datasetId,
      description: description.trim() || null,
    };
    let commitConfirmed = false;
    let responseUncertain = false;
    try {
      const response = await fetch("/bff/experiments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (response.status === 401) {
        router.push("/session/expired");
        return;
      }
      const deferredId = response.status === 503
        ? await projectionDeferredResourceId(response, "experiment")
        : null;
      if (deferredId) {
        commitConfirmed = true;
        setMessage("Experiment 원장은 생성됐고 MLflow 투영이 지연 중입니다. 중복 생성하지 않고 상세로 이동합니다.");
        router.push(`/experiments/${encodeURIComponent(deferredId)}`);
        router.refresh();
        return;
      }
      if (!response.ok) {
        const wait = retryAfterSeconds(response);
        setRetryAfter(wait);
        setMessage(experimentErrorMessage(response.status, wait));
        return;
      }
      commitConfirmed = true;
      const created = await response.json() as Partial<ApiExperiment>;
      if (typeof created.id !== "string" || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(created.id)) {
        setMessage("Experiment는 생성됐지만 응답을 확인할 수 없습니다. 목록을 새로고침해 주세요.");
        return;
      }
      setMessage("Experiment를 만들었습니다. Job 조건 화면으로 이동합니다.");
      router.push(`/experiments/${encodeURIComponent(created.id)}`);
      router.refresh();
    } catch {
      responseUncertain = !commitConfirmed;
      setMessage(
        commitConfirmed
          ? "Manager가 원장 commit을 확인했지만 응답 본문을 읽지 못했습니다. 중복 제출하지 말고 Experiment 목록을 확인해 주세요."
          : "Web BFF 응답이 유실됐습니다. Manager에서 commit됐을 가능성이 있으므로 재제출 전 Experiment 목록을 확인해 주세요.",
      );
    } finally {
      if (!commitConfirmed && !responseUncertain) submissionLock.current = false;
      setSubmitPhase(finishExperimentSubmission(commitConfirmed, responseUncertain));
    }
  }

  const selected = datasets.find((dataset) => dataset.id === datasetId);
  const locked = experimentSubmissionLocked(submitPhase, demo);
  return (
    <section className="panel experiment-create-panel" aria-labelledby="create-experiment-heading">
      <div className="panel-header">
        <div>
          <p className="panel-kicker">IMMUTABLE DATASET BINDING</p>
          <h2 id="create-experiment-heading">기본 정보</h2>
        </div>
        <span className="upload-security-label">JWT는 HttpOnly BFF 내부에서만 사용</span>
      </div>
      <form className="experiment-create-form" onSubmit={submit}>
        <label>
          <span>Experiment 이름</span>
          <input
            autoComplete="off"
            disabled={locked}
            maxLength={128}
            onChange={(event) => setName(event.target.value)}
            placeholder="예: speaker-a-comparison-001"
            required
            value={name}
          />
        </label>
        <label>
          <span>학습 Dataset</span>
          <select
            disabled={locked}
            onChange={(event) => setDatasetId(event.target.value)}
            value={datasetId}
          >
            {datasets.map((dataset) => (
              <option key={dataset.id} value={dataset.id}>{dataset.name}</option>
            ))}
          </select>
        </label>
        <label className="form-field-wide">
          <span>설명 (선택)</span>
          <textarea
            disabled={locked}
            maxLength={8_192}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="이 Experiment에서 비교할 조건과 목적을 적어 주세요."
            rows={5}
            value={description}
          />
        </label>
        <div className="experiment-dataset-preview form-field-wide">
          <span>선택 Dataset</span>
          <strong>{selected?.name ?? "선택 필요"}</strong>
          <small>
            {selected?.fileCount ?? "—"} files · {selected?.durationMinutes === null || selected?.durationMinutes === undefined
              ? "길이 미제공"
              : `${selected.durationMinutes.toFixed(1)}분`} · ready / usable
          </small>
        </div>
        <div className="form-submit-row form-field-wide">
          <p className={message ? "form-status-message" : "form-status-message form-status-placeholder"} aria-live="polite">
            {message || "생성 뒤 Dataset은 이 Experiment에 고정됩니다."}
            {retryAfter !== null ? ` (${retryAfter}초 후 재시도 권고)` : ""}
          </p>
          <button className="button button-primary" disabled={locked} type="submit">
            {submitPhase === "submitted"
              ? "생성 확인됨 · 이동 중…"
              : submitPhase === "uncertain"
                ? "응답 유실 · 목록 확인 필요"
              : submitPhase === "pending"
                ? "검증 중…"
                : "Experiment 만들기"}
          </button>
        </div>
      </form>
    </section>
  );
}

function retryAfterSeconds(response: Response): number | null {
  const value = response.headers.get("retry-after");
  return value && /^(0|[1-9][0-9]{0,5})$/.test(value) ? Number(value) : null;
}

async function projectionDeferredResourceId(
  response: Response,
  resourceType: "experiment",
): Promise<string | null> {
  try {
    const value = await response.json() as Record<string, unknown>;
    return value.error === "projection_deferred" &&
      value.ledger_committed === true &&
      value.resource_type === resourceType &&
      typeof value.resource_id === "string" &&
      /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(value.resource_id)
      ? value.resource_id
      : null;
  } catch {
    return null;
  }
}

function experimentErrorMessage(status: number, retryAfter: number | null): string {
  if (status === 409) return "Dataset이 더 이상 학습 가능 상태가 아니거나 생성 조건이 충돌했습니다.";
  if (status === 422) return "Manager가 Experiment 입력을 거부했습니다. 이름과 Dataset을 확인해 주세요.";
  if (status === 429) return retryAfter === null
    ? "요청이 너무 많습니다. 잠시 후 다시 시도해 주세요."
    : `요청이 너무 많습니다. ${retryAfter}초 후 다시 시도해 주세요.`;
  if (status === 403 || status === 404) return "Dataset을 찾을 수 없거나 사용할 권한이 없습니다.";
  return "Experiment 생성 요청을 처리하지 못했습니다.";
}

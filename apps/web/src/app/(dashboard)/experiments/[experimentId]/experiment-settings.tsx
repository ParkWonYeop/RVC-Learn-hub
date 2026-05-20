"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useRef, useState } from "react";
import {
  experimentDeleteConfirmationMatches,
  experimentMutationErrorCode,
  experimentMutationErrorMessage,
  experimentMutationLocked,
  normalizeExperimentDescription,
  parseExperimentMutationResult,
  isExpectedExperimentUpdate,
  validExperimentDescription,
  type ExperimentMutationPhase,
} from "@/lib/client/experiment-mutation";

interface Feedback {
  tone: "status" | "error";
  message: string;
}

export function ExperimentSettings({
  experimentId,
  experimentName,
  datasetId,
  initialDescription,
  initialRowVersion,
  knownJobCount,
  demo,
}: {
  experimentId: string;
  experimentName: string;
  datasetId: string;
  initialDescription: string | null;
  initialRowVersion: number;
  knownJobCount: number;
  demo: boolean;
}) {
  const router = useRouter();
  const mutationLock = useRef(false);
  const [description, setDescription] = useState(initialDescription ?? "");
  const [persistedDescription, setPersistedDescription] = useState(initialDescription);
  const [rowVersion, setRowVersion] = useState(initialRowVersion);
  const [phase, setPhase] = useState<ExperimentMutationPhase>("idle");
  const [feedback, setFeedback] = useState<Feedback | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [confirmation, setConfirmation] = useState("");

  const normalizedDescription = normalizeExperimentDescription(description);
  const dirty = normalizedDescription !== persistedDescription;
  const locked = experimentMutationLocked(phase, demo);
  const confirmationMatches = experimentDeleteConfirmationMatches(experimentName, confirmation);

  async function saveDescription(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (mutationLock.current || locked || !dirty) return;
    if (!validExperimentDescription(description)) {
      setFeedback({
        tone: "error",
        message: "설명은 8192자 이하이며 binary 제어 문자를 포함할 수 없습니다.",
      });
      return;
    }
    mutationLock.current = true;
    setPhase("saving");
    setFeedback({ tone: "status", message: "최신 row version을 확인하며 설명을 저장하고 있습니다." });
    try {
      const response = await fetch(`/bff/experiments/${encodeURIComponent(experimentId)}`, {
        body: JSON.stringify({
          expected_row_version: rowVersion,
          description: normalizedDescription,
        }),
        cache: "no-store",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        method: "PATCH",
      });
      if (response.status === 401) {
        setPhase("forbidden");
        router.push("/session/expired");
        return;
      }
      const body = await readJson(response);
      if (!response.ok) {
        const code = experimentMutationErrorCode(body);
        const terminalFailure = code === "stale_experiment" || isPermissionFailure(response.status);
        mutationLock.current = terminalFailure;
        setPhase(code === "stale_experiment" ? "stale" : isPermissionFailure(response.status) ? "forbidden" : "idle");
        setFeedback({
          tone: "error",
          message: experimentMutationErrorMessage("save", response.status, code),
        });
        return;
      }
      const result = parseExperimentMutationResult(body, {
        id: experimentId,
        name: experimentName,
        datasetId,
      });
      if (!isExpectedExperimentUpdate(result, rowVersion, normalizedDescription)) {
        setPhase("uncertain");
        setFeedback({
          tone: "error",
          message: "저장 응답을 안전하게 확인할 수 없습니다. 중복 요청 전에 최신 페이지를 다시 불러오세요.",
        });
        return;
      }
      mutationLock.current = false;
      setDescription(result.description ?? "");
      setPersistedDescription(result.description);
      setRowVersion(result.rowVersion);
      setPhase("idle");
      setFeedback({ tone: "status", message: "Experiment 설명을 저장했습니다." });
      router.refresh();
    } catch {
      setPhase("uncertain");
      setFeedback({
        tone: "error",
        message: "응답이 유실되어 저장 여부를 확정할 수 없습니다. 재요청 전에 최신 페이지를 다시 불러오세요.",
      });
    }
  }

  async function removeExperiment(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (mutationLock.current || locked || !confirmationMatches) return;
    mutationLock.current = true;
    setPhase("deleting");
    setFeedback({ tone: "status", message: "참조 Job과 MLflow projection을 다시 확인하고 있습니다." });
    try {
      const response = await fetch(
        `/bff/experiments/${encodeURIComponent(experimentId)}?expected_row_version=${rowVersion}`,
        {
          cache: "no-store",
          credentials: "same-origin",
          method: "DELETE",
        },
      );
      if (response.status === 401) {
        setPhase("forbidden");
        router.push("/session/expired");
        return;
      }
      if (response.status === 204) {
        setFeedback({ tone: "status", message: "Experiment를 삭제했습니다. 목록으로 이동합니다." });
        router.replace("/experiments");
        router.refresh();
        return;
      }
      const body = await readJson(response);
      const code = experimentMutationErrorCode(body);
      const terminalFailure = code === "stale_experiment" || isPermissionFailure(response.status);
      mutationLock.current = terminalFailure;
      setConfirmation("");
      setPhase(code === "stale_experiment" ? "stale" : isPermissionFailure(response.status) ? "forbidden" : "idle");
      setFeedback({
        tone: "error",
        message: experimentMutationErrorMessage("delete", response.status, code),
      });
    } catch {
      setPhase("uncertain");
      setFeedback({
        tone: "error",
        message: "응답이 유실되어 삭제 여부를 확정할 수 없습니다. 다시 삭제하지 말고 목록에서 존재 여부를 확인하세요.",
      });
    }
  }

  return (
    <section className="panel experiment-settings-panel" aria-labelledby="experiment-settings-heading">
      <div className="panel-header">
        <div>
          <p className="panel-kicker">IMMUTABLE IDENTITY / VERSIONED DESCRIPTION</p>
          <h2 id="experiment-settings-heading">Experiment 설정</h2>
        </div>
        <span className="upload-security-label">name·Dataset 고정 · HttpOnly BFF</span>
      </div>

      <form className="experiment-settings-form" aria-busy={phase === "saving"} onSubmit={saveDescription}>
        <div className="experiment-immutable-fields" aria-label="변경할 수 없는 Experiment 속성">
          <div><span>이름</span><strong>{experimentName}</strong><small>생성 후 변경 불가</small></div>
          <div><span>Dataset ID</span><strong>{datasetId}</strong><small>Job provenance에 고정</small></div>
        </div>
        <label htmlFor="experiment-description">
          <span>설명</span>
          <textarea
            aria-describedby="experiment-description-help"
            disabled={locked}
            id="experiment-description"
            maxLength={8_192}
            onChange={(event) => setDescription(event.target.value)}
            rows={5}
            value={description}
          />
        </label>
        <div className="experiment-settings-actions">
          <small id="experiment-description-help">
            설명만 수정할 수 있습니다. 저장 시 현재 row version {rowVersion}을 다시 검증합니다.
          </small>
          <button className="button button-primary" disabled={locked || !dirty} type="submit">
            {phase === "saving" ? "저장 중…" : "설명 저장"}
          </button>
        </div>
      </form>

      <div className="experiment-danger-zone">
        <div>
          <strong>Experiment 삭제</strong>
          <p>
            Job 또는 MLflow projection/outbox가 있으면 Manager가 삭제를 거부합니다.
            {knownJobCount > 0 ? ` 현재 조회된 Job은 ${knownJobCount}개입니다.` : ""}
          </p>
        </div>
        <button
          aria-controls="experiment-delete-confirmation"
          aria-expanded={deleteOpen}
          className="button button-danger"
          disabled={locked}
          onClick={() => {
            setDeleteOpen((current) => !current);
            setConfirmation("");
          }}
          type="button"
        >
          {deleteOpen ? "삭제 확인 닫기" : "삭제 확인 열기"}
        </button>
      </div>

      {deleteOpen ? (
        <form
          className="experiment-delete-confirmation"
          id="experiment-delete-confirmation"
          aria-busy={phase === "deleting"}
          onSubmit={removeExperiment}
        >
          <label htmlFor="experiment-delete-name">
            <span>삭제하려면 Experiment 이름을 정확히 입력하세요: <strong>{experimentName}</strong></span>
            <input
              autoComplete="off"
              disabled={locked}
              id="experiment-delete-name"
              onChange={(event) => setConfirmation(event.target.value)}
              value={confirmation}
            />
          </label>
          <button className="button button-danger" disabled={locked || !confirmationMatches} type="submit">
            {phase === "deleting" ? "삭제 조건 확인 중…" : "Experiment 영구 삭제"}
          </button>
        </form>
      ) : null}

      {feedback ? (
        <div
          className={`experiment-settings-feedback ${feedback.tone === "error" ? "experiment-settings-feedback-error" : ""}`}
          role={feedback.tone === "error" ? "alert" : "status"}
          aria-live="polite"
        >
          <span>{feedback.message}</span>
          {phase === "stale" || phase === "uncertain" ? (
            <button className="button button-secondary" onClick={() => window.location.reload()} type="button">
              최신 페이지 다시 불러오기
            </button>
          ) : null}
          {phase === "forbidden" ? (
            <Link className="button button-secondary" href="/experiments">Experiment 목록</Link>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

function isPermissionFailure(status: number): boolean {
  return status === 403 || status === 404;
}

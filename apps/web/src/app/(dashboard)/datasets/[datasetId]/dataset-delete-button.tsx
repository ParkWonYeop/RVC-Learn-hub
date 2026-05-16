"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

export function DatasetDeleteButton({
  datasetId,
  datasetName,
  demo,
}: {
  datasetId: string;
  datasetName: string;
  demo: boolean;
}) {
  const router = useRouter();
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  async function removeDataset() {
    if (demo || pending) return;
    if (!window.confirm(`“${datasetName}” Dataset과 canonical object를 삭제할까요?`)) return;
    setPending(true);
    setMessage(null);
    try {
      const response = await fetch(`/bff/datasets/${encodeURIComponent(datasetId)}`, {
        cache: "no-store",
        credentials: "same-origin",
        method: "DELETE",
      });
      if (response.status === 204) {
        router.push("/datasets");
        router.refresh();
        return;
      }
      let code = "request_failed";
      try {
        const body = await response.json() as { error?: unknown };
        if (typeof body.error === "string") code = body.error;
      } catch {
        // Minimal BFF failures can have no readable JSON body.
      }
      setMessage(deleteErrorMessage(code, response.status));
    } catch {
      setMessage("삭제 요청을 Manager에 전달하지 못했습니다.");
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="dataset-delete-action">
      <button
        className="button button-danger"
        disabled={demo || pending}
        onClick={removeDataset}
        title={demo ? "Demo fixture는 삭제할 수 없습니다." : undefined}
        type="button"
      >
        {pending ? "삭제 확인 중…" : "Dataset 삭제"}
      </button>
      {message ? <span role="alert">{message}</span> : null}
    </div>
  );
}

function deleteErrorMessage(code: string, status: number): string {
  if (code === "conflict" || status === 409) {
    return "Experiment/Job에서 참조 중이거나 upload/finalize가 활성 상태라 삭제할 수 없습니다.";
  }
  if (code === "not_found" || status === 404) {
    return "Dataset을 찾을 수 없거나 삭제 권한이 없습니다.";
  }
  if (code === "rate_limited" || status === 429) {
    return "삭제 요청이 제한되었습니다. 잠시 후 다시 시도해 주세요.";
  }
  return `Dataset 삭제에 실패했습니다 (HTTP ${status}).`;
}

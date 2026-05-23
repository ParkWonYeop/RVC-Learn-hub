"use client";

import { useActionState } from "react";
import type { FormEvent } from "react";
import type { JobStatus } from "@/lib/types";
import { runJobAction, type JobActionState } from "./actions";

const initialState: JobActionState = { status: "idle", message: "" };
const terminalStatuses = new Set<JobStatus>(["completed", "failed", "cancelled"]);

export function JobActions({
  jobId,
  status,
  cancelRequested,
  demo,
}: {
  jobId: string;
  status: JobStatus;
  cancelRequested: boolean;
  demo: boolean;
}) {
  const [state, formAction, pending] = useActionState(runJobAction, initialState);
  const cancelDisabled =
    demo || pending || cancelRequested || terminalStatuses.has(status);
  const retryDisabled = demo || pending || status !== "failed";

  function confirmCancellation(event: FormEvent<HTMLFormElement>) {
    const submitter = (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null;
    if (
      submitter?.value === "cancel" &&
      !window.confirm("이 학습 작업의 취소를 요청하시겠습니까?")
    ) {
      event.preventDefault();
    }
  }

  return (
    <form className="job-action-form" action={formAction} onSubmit={confirmCancellation}>
      <input name="jobId" type="hidden" value={jobId} />
      <div className="job-action-buttons">
        <button
          className="button button-secondary"
          disabled={cancelDisabled}
          name="operation"
          type="submit"
          value="cancel"
          title={cancelTitle(status, cancelRequested, demo)}
        >
          {pending ? "처리 중…" : cancelRequested ? "취소 요청됨" : "작업 취소"}
        </button>
        <button
          className="button button-primary"
          disabled={retryDisabled}
          name="operation"
          type="submit"
          value="retry"
          title={retryTitle(status, demo)}
        >
          실패 작업 재시도
        </button>
      </div>
      <p
        className={`job-action-message job-action-message-${state.status}`}
        aria-live="polite"
      >
        {state.message}
      </p>
    </form>
  );
}

function cancelTitle(status: JobStatus, requested: boolean, demo: boolean): string {
  if (demo) return "Demo fixture에서는 변경할 수 없습니다.";
  if (requested) return "Manager가 이미 취소 요청을 접수했습니다.";
  if (terminalStatuses.has(status)) return "종료된 작업은 취소할 수 없습니다.";
  return "Manager cancel API를 호출합니다.";
}

function retryTitle(status: JobStatus, demo: boolean): string {
  if (demo) return "Demo fixture에서는 변경할 수 없습니다.";
  if (status !== "failed") return "실패 상태의 작업만 재시도할 수 있습니다.";
  return "Manager retry API를 호출합니다.";
}

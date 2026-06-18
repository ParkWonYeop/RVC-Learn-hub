export type ExperimentSubmissionPhase = "idle" | "pending" | "submitted" | "uncertain";

export function finishExperimentSubmission(
  commitConfirmed: boolean,
  responseUncertain = false,
): ExperimentSubmissionPhase {
  if (commitConfirmed) return "submitted";
  return responseUncertain ? "uncertain" : "idle";
}

export function experimentSubmissionLocked(
  phase: ExperimentSubmissionPhase,
  demo: boolean,
): boolean {
  return demo || phase !== "idle";
}

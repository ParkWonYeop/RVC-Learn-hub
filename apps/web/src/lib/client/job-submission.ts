export type SubmissionPhase =
  | "checking"
  | "creating"
  | "success"
  | "conflict"
  | "error"
  | "blocked";

export interface SubmissionOutcome {
  phase: SubmissionPhase;
  message: string;
  jobId?: string;
}

const terminalPhases = new Set<SubmissionPhase>(["success", "conflict", "error"]);

export function initializeSubmissionOutcomes(
  keys: readonly string[],
): Record<string, SubmissionOutcome> {
  return Object.fromEntries(
    keys.map((key) => [key, { phase: "checking", message: "기존 이름 확인 중" }]),
  );
}

export function withSubmissionOutcome(
  current: Readonly<Record<string, SubmissionOutcome>>,
  key: string,
  outcome: SubmissionOutcome,
): Record<string, SubmissionOutcome> {
  return { ...current, [key]: outcome };
}

export function blockUnsettledSubmissionOutcomes(
  current: Readonly<Record<string, SubmissionOutcome>>,
  allKeys: readonly string[],
  settledKeys: ReadonlySet<string>,
  message: string,
): Record<string, SubmissionOutcome> {
  const next = { ...current };
  for (const key of allKeys) {
    const outcome = next[key];
    if (settledKeys.has(key) || (outcome && terminalPhases.has(outcome.phase))) continue;
    next[key] = { phase: "blocked", message };
  }
  return next;
}

export function recoverSubmissionTransportFailure(
  current: Readonly<Record<string, SubmissionOutcome>>,
  allKeys: readonly string[],
  settledKeys: ReadonlySet<string>,
  inFlightKey: string | null,
): Record<string, SubmissionOutcome> {
  let next = { ...current };
  const settledAfterRecovery = new Set(settledKeys);
  if (inFlightKey && !settledAfterRecovery.has(inFlightKey)) {
    const outcome = next[inFlightKey];
    if (!outcome || !terminalPhases.has(outcome.phase)) {
      next[inFlightKey] = {
        phase: "error",
        message: "POST 응답 유실 · 원장 생성 여부를 Job 목록에서 확인 필요",
      };
    }
    settledAfterRecovery.add(inFlightKey);
  }
  next = blockUnsettledSubmissionOutcomes(
    next,
    allKeys,
    settledAfterRecovery,
    inFlightKey
      ? "앞선 POST 응답 유실로 제출하지 않음"
      : "기존 이름 조회를 완료하지 못해 제출하지 않음",
  );
  return next;
}

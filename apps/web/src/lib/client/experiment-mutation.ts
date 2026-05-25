export type ExperimentMutationPhase =
  | "idle"
  | "saving"
  | "deleting"
  | "stale"
  | "uncertain"
  | "forbidden";

export type ExperimentMutationAction = "save" | "delete";

export interface ExperimentMutationResult {
  rowVersion: number;
  description: string | null;
}

const errorCodes = new Set([
  "conflict",
  "demo_mode_read_only",
  "experiment_became_referenced",
  "experiment_has_jobs",
  "experiment_has_mlflow_projection",
  "forbidden",
  "invalid_experiment",
  "invalid_request",
  "invalid_upstream_response",
  "manager_unavailable",
  "not_found",
  "payload_too_large",
  "rate_limited",
  "session_expired",
  "session_required",
  "stale_experiment",
]);

export function experimentMutationLocked(
  phase: ExperimentMutationPhase,
  demo: boolean,
): boolean {
  return demo || phase !== "idle";
}

export function normalizeExperimentDescription(value: string): string | null {
  return value.length === 0 ? null : value;
}

export function validExperimentDescription(value: string): boolean {
  return value.length <= 8_192 && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(value);
}

export function experimentMutationErrorCode(value: unknown): string {
  if (!isRecord(value) || Object.keys(value).length !== 1 || typeof value.error !== "string") {
    return "unknown";
  }
  return errorCodes.has(value.error) ? value.error : "unknown";
}

export function isExpectedExperimentUpdate(
  result: ExperimentMutationResult | null,
  previousRowVersion: number,
  requestedDescription: string | null,
): result is ExperimentMutationResult {
  return result !== null &&
    result.description === requestedDescription &&
    result.rowVersion === previousRowVersion + 1;
}

export function experimentDeleteConfirmationMatches(
  expectedName: string,
  enteredName: string,
): boolean {
  return enteredName === expectedName;
}

export function parseExperimentMutationResult(
  value: unknown,
  expected: { id: string; name: string; datasetId: string },
): ExperimentMutationResult | null {
  if (!isRecord(value)) return null;
  const keys = Object.keys(value);
  const publicKeys = [
    "id",
    "row_version",
    "name",
    "dataset_id",
    "description",
    "created_at",
    "updated_at",
  ];
  if (keys.length !== publicKeys.length || keys.some((key) => !publicKeys.includes(key))) {
    return null;
  }
  if (
    value.id !== expected.id ||
    value.name !== expected.name ||
    value.dataset_id !== expected.datasetId ||
    !integerInRange(value.row_version, 1, 2_147_483_647) ||
    !validNullableDescription(value.description) ||
    !safeDate(value.created_at) ||
    !safeDate(value.updated_at)
  ) {
    return null;
  }
  return {
    rowVersion: value.row_version as number,
    description: value.description as string | null,
  };
}

export function experimentMutationErrorMessage(
  action: ExperimentMutationAction,
  status: number,
  code: string,
): string {
  if (code === "stale_experiment") {
    return "다른 요청이 이 Experiment를 먼저 변경했습니다. 최신 내용을 불러온 뒤 다시 시도해 주세요.";
  }
  if (code === "experiment_has_jobs" || code === "experiment_became_referenced") {
    return "이 Experiment를 참조하는 Job이 있어 삭제할 수 없습니다. Job 원장을 먼저 확인해 주세요.";
  }
  if (code === "experiment_has_mlflow_projection") {
    return "MLflow projection 또는 outbox 기록이 있는 Experiment는 삭제할 수 없습니다.";
  }
  if (status === 401 || code === "session_expired" || code === "session_required") {
    return "인증 세션이 만료되었습니다. 다시 로그인해 주세요.";
  }
  if (status === 403 || status === 404 || code === "forbidden" || code === "not_found") {
    return "Experiment를 찾을 수 없거나 변경할 권한이 없습니다.";
  }
  if (status === 409 && code === "demo_mode_read_only") {
    return "Demo fixture는 변경하거나 삭제할 수 없습니다.";
  }
  if (status === 409) {
    return action === "delete"
      ? "Experiment 삭제 조건이 충돌했습니다. 최신 원장 상태를 확인해 주세요."
      : "Experiment 수정 조건이 충돌했습니다. 최신 내용을 확인해 주세요.";
  }
  if (status === 413 || code === "payload_too_large") {
    return "설명이 허용된 요청 크기를 초과했습니다.";
  }
  if (status === 422 || code === "invalid_experiment" || code === "invalid_request") {
    return "Manager가 Experiment 변경 요청을 거부했습니다. 입력값을 확인해 주세요.";
  }
  if (status === 429 || code === "rate_limited") {
    return "변경 요청이 제한되었습니다. 잠시 후 다시 시도해 주세요.";
  }
  if (status === 502 || status === 503 || code === "manager_unavailable") {
    return "Manager에 변경 요청을 전달할 수 없습니다. 서비스 상태를 확인해 주세요.";
  }
  return action === "delete"
    ? `Experiment 삭제에 실패했습니다 (HTTP ${status}).`
    : `Experiment 설명 저장에 실패했습니다 (HTTP ${status}).`;
}

function validNullableDescription(value: unknown): boolean {
  return value === null || (typeof value === "string" && validExperimentDescription(value));
}

function integerInRange(value: unknown, minimum: number, maximum: number): boolean {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

function safeDate(value: unknown): boolean {
  return typeof value === "string" && value.length <= 64 && Number.isFinite(Date.parse(value));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

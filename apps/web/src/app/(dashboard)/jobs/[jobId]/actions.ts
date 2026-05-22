"use server";

import { revalidatePath } from "next/cache";
import type { ApiJob } from "@/lib/api-types";
import {
  authenticatedManagerMutation,
  dashboardDemoMode,
} from "@/lib/server/auth";
import { ManagerApiError } from "@/lib/server/manager-api";

export type JobActionState = {
  status: "idle" | "success" | "error";
  message: string;
};

const safeJobId = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;

export async function runJobAction(
  _previousState: JobActionState,
  formData: FormData,
): Promise<JobActionState> {
  const jobId = formValue(formData, "jobId");
  const operation = formValue(formData, "operation");
  if (!jobId || !safeJobId.test(jobId)) {
    return { status: "error", message: "올바르지 않은 작업 ID입니다." };
  }
  if (operation !== "cancel" && operation !== "retry") {
    return { status: "error", message: "지원하지 않는 작업 요청입니다." };
  }
  if (dashboardDemoMode()) {
    return { status: "error", message: "Demo fixture에서는 작업 상태를 변경할 수 없습니다." };
  }

  try {
    await authenticatedManagerMutation<ApiJob>(
      `/api/v1/jobs/${encodeURIComponent(jobId)}/${operation}`,
    );
  } catch (error) {
    if (error instanceof ManagerApiError) {
      return { status: "error", message: actionErrorMessage(error) };
    }
    throw error;
  }

  revalidatePath("/jobs");
  revalidatePath(`/jobs/${jobId}`);
  return {
    status: "success",
    message:
      operation === "cancel"
        ? "Manager가 취소 요청을 접수했습니다."
        : "실패한 작업을 대기열에 다시 등록했습니다.",
  };
}

function formValue(formData: FormData, key: string): string | null {
  const value = formData.get(key);
  return typeof value === "string" ? value : null;
}

function actionErrorMessage(error: ManagerApiError): string {
  if (error.status === 404) {
    return "작업을 찾을 수 없거나 접근 권한이 없습니다.";
  }
  if (error.status === 409) {
    return "현재 작업 상태에서는 이 요청을 실행할 수 없습니다. 최신 상태를 확인해 주세요.";
  }
  return error.message;
}

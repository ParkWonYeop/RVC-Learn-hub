import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  class ManagerApiError extends Error {
    constructor(
      message: string,
      readonly status: number,
      readonly requestId: string,
    ) {
      super(message);
    }
  }
  return {
    ManagerApiError,
    authenticatedManagerMutation: vi.fn(),
    dashboardDemoMode: vi.fn(),
    revalidatePath: vi.fn(),
  };
});

vi.mock("@/lib/server/auth", () => ({
  authenticatedManagerMutation: mocks.authenticatedManagerMutation,
  dashboardDemoMode: mocks.dashboardDemoMode,
}));
vi.mock("@/lib/server/manager-api", () => ({
  ManagerApiError: mocks.ManagerApiError,
}));
vi.mock("next/cache", () => ({ revalidatePath: mocks.revalidatePath }));

import { runJobAction, type JobActionState } from "@/app/(dashboard)/jobs/[jobId]/actions";

const initialState: JobActionState = { status: "idle", message: "" };

describe("Job state-changing server action", () => {
  beforeEach(() => {
    mocks.authenticatedManagerMutation.mockReset();
    mocks.dashboardDemoMode.mockReset().mockReturnValue(false);
    mocks.revalidatePath.mockReset();
  });

  it("rejects a tampered Job ID without contacting Manager", async () => {
    const result = await runJobAction(initialState, form("../other", "cancel"));

    expect(result.status).toBe("error");
    expect(mocks.authenticatedManagerMutation).not.toHaveBeenCalled();
  });

  it("permits only the cancel and retry operations", async () => {
    const result = await runJobAction(initialState, form("job-1", "delete"));

    expect(result.status).toBe("error");
    expect(mocks.authenticatedManagerMutation).not.toHaveBeenCalled();
  });

  it("does not mutate live state while Demo fixtures are enabled", async () => {
    mocks.dashboardDemoMode.mockReturnValue(true);

    const result = await runJobAction(initialState, form("job-1", "cancel"));

    expect(result.message).toContain("Demo fixture");
    expect(mocks.authenticatedManagerMutation).not.toHaveBeenCalled();
  });

  it.each(["cancel", "retry"] as const)(
    "calls the actual Manager %s endpoint and revalidates both views",
    async (operation) => {
      mocks.authenticatedManagerMutation.mockResolvedValue({ id: "job-1" });

      const result = await runJobAction(initialState, form("job-1", operation));

      expect(result.status).toBe("success");
      expect(mocks.authenticatedManagerMutation).toHaveBeenCalledWith(
        `/api/v1/jobs/job-1/${operation}`,
      );
      expect(mocks.revalidatePath).toHaveBeenCalledWith("/jobs");
      expect(mocks.revalidatePath).toHaveBeenCalledWith("/jobs/job-1");
    },
  );

  it("turns a Manager state conflict into a non-sensitive UI message", async () => {
    mocks.authenticatedManagerMutation.mockRejectedValue(
      new mocks.ManagerApiError("internal detail", 409, "request-1"),
    );

    const result = await runJobAction(initialState, form("job-1", "retry"));

    expect(result.status).toBe("error");
    expect(result.message).toContain("현재 작업 상태");
    expect(result.message).not.toContain("internal detail");
  });
});

function form(jobId: string, operation: string): FormData {
  const data = new FormData();
  data.set("jobId", jobId);
  data.set("operation", operation);
  return data;
}

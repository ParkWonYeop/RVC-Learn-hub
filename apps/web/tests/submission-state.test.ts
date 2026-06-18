import { describe, expect, it } from "vitest";
import {
  initializeSubmissionOutcomes,
  recoverSubmissionTransportFailure,
  withSubmissionOutcome,
} from "@/lib/client/job-submission";
import {
  experimentSubmissionLocked,
  finishExperimentSubmission,
} from "@/lib/client/experiment-submission";

describe("creation submission state", () => {
  it("preserves a first POST success when the following POST loses its response", () => {
    const keys = ["job-first", "job-in-flight", "job-not-started", "job-conflict"];
    let outcomes = initializeSubmissionOutcomes(keys);
    outcomes = withSubmissionOutcome(outcomes, "job-first", {
      phase: "success",
      message: "queued Job 생성 완료",
      jobId: "created-1",
    });
    outcomes = withSubmissionOutcome(outcomes, "job-in-flight", {
      phase: "creating",
      message: "Manager 검증 및 생성 중",
    });
    outcomes = withSubmissionOutcome(outcomes, "job-conflict", {
      phase: "conflict",
      message: "기존 이름",
    });

    const recovered = recoverSubmissionTransportFailure(
      outcomes,
      keys,
      new Set(["job-first", "job-conflict"]),
      "job-in-flight",
    );

    expect(recovered["job-first"]).toEqual({
      phase: "success",
      message: "queued Job 생성 완료",
      jobId: "created-1",
    });
    expect(recovered["job-conflict"]?.phase).toBe("conflict");
    expect(recovered["job-in-flight"]).toMatchObject({
      phase: "error",
      message: expect.stringContaining("응답 유실"),
    });
    expect(recovered["job-not-started"]?.phase).toBe("blocked");
  });

  it.each([
    ["normal 201", true],
    ["ledger_committed 503", true],
  ])("keeps Experiment submission terminal after %s", (_case, commitConfirmed) => {
    const phase = finishExperimentSubmission(commitConfirmed);

    expect(phase).toBe("submitted");
    expect(experimentSubmissionLocked(phase, false)).toBe(true);
  });

  it("releases the Experiment form only when no commit was confirmed", () => {
    expect(finishExperimentSubmission(false, false)).toBe("idle");
    expect(experimentSubmissionLocked("idle", false)).toBe(false);
  });

  it("keeps an unknown response locked until the user checks the Experiment list", () => {
    expect(finishExperimentSubmission(false, true)).toBe("uncertain");
    expect(experimentSubmissionLocked("uncertain", false)).toBe(true);
  });
});

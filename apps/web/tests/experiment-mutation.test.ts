import { describe, expect, it } from "vitest";
import {
  experimentDeleteConfirmationMatches,
  experimentMutationErrorCode,
  experimentMutationErrorMessage,
  experimentMutationLocked,
  isExpectedExperimentUpdate,
  normalizeExperimentDescription,
  parseExperimentMutationResult,
  validExperimentDescription,
} from "@/lib/client/experiment-mutation";

describe("Experiment mutation client state", () => {
  it("locks every in-flight, stale, uncertain and forbidden phase", () => {
    expect(experimentMutationLocked("idle", false)).toBe(false);
    expect(experimentMutationLocked("saving", false)).toBe(true);
    expect(experimentMutationLocked("deleting", false)).toBe(true);
    expect(experimentMutationLocked("stale", false)).toBe(true);
    expect(experimentMutationLocked("uncertain", false)).toBe(true);
    expect(experimentMutationLocked("forbidden", false)).toBe(true);
    expect(experimentMutationLocked("idle", true)).toBe(true);
  });

  it("normalizes only an exactly empty description and rejects binary controls", () => {
    expect(normalizeExperimentDescription("")).toBeNull();
    expect(normalizeExperimentDescription("   \n")).toBe("   \n");
    expect(normalizeExperimentDescription(" comparison \n")).toBe(" comparison \n");
    expect(validExperimentDescription("line one\nline two\tvalue")).toBe(true);
    expect(validExperimentDescription("bad\u0000value")).toBe(false);
    expect(validExperimentDescription("x".repeat(8_193))).toBe(false);
  });

  it("accepts only the exact public mutation response and expected immutable identity", () => {
    const response = {
      id: "experiment-1",
      row_version: 3,
      name: "speaker-a",
      dataset_id: "dataset-1",
      description: "updated",
      created_at: "2026-07-11T12:00:00Z",
      updated_at: "2026-07-11T12:01:00Z",
    };
    const expected = { id: "experiment-1", name: "speaker-a", datasetId: "dataset-1" };

    const parsed = parseExperimentMutationResult(response, expected);
    expect(parsed).toEqual({
      rowVersion: 3,
      description: "updated",
    });
    expect(isExpectedExperimentUpdate(parsed, 2, "updated")).toBe(true);
    expect(isExpectedExperimentUpdate(parsed, 3, "updated")).toBe(false);
    expect(isExpectedExperimentUpdate(parsed, 1, "updated")).toBe(false);
    expect(isExpectedExperimentUpdate(parsed, 2, "different")).toBe(false);
    expect(parseExperimentMutationResult({ ...response, storage_uri: "s3://private" }, expected)).toBeNull();
    expect(parseExperimentMutationResult({ ...response, dataset_id: "other" }, expected)).toBeNull();
    expect(parseExperimentMutationResult({ ...response, row_version: 0 }, expected)).toBeNull();
  });

  it("requires a byte-for-byte Experiment name confirmation before delete", () => {
    expect(experimentDeleteConfirmationMatches("speaker-a", "speaker-a")).toBe(true);
    expect(experimentDeleteConfirmationMatches("speaker-a", "Speaker-a")).toBe(false);
    expect(experimentDeleteConfirmationMatches("speaker-a", "speaker-a ")).toBe(false);
  });

  it("accepts only exact known BFF error envelopes", () => {
    expect(experimentMutationErrorCode({ error: "stale_experiment" })).toBe("stale_experiment");
    expect(experimentMutationErrorCode({ error: "stale_experiment", detail: "private" })).toBe("unknown");
    expect(experimentMutationErrorCode({ error: "private_internal_code" })).toBe("unknown");
  });

  it("distinguishes stale, Job reference, MLflow and permission UX", () => {
    expect(experimentMutationErrorMessage("save", 409, "stale_experiment")).toContain("최신 내용");
    expect(experimentMutationErrorMessage("delete", 409, "experiment_has_jobs")).toContain("Job");
    expect(experimentMutationErrorMessage("delete", 409, "experiment_has_mlflow_projection")).toContain("MLflow");
    expect(experimentMutationErrorMessage("save", 403, "forbidden")).toContain("권한");
  });
});

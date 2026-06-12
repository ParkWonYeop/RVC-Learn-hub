import { beforeEach, describe, expect, it, vi } from "vitest";

const auth = vi.hoisted(() => ({
  authenticatedManagerRequest: vi.fn(),
  dashboardDemoMode: vi.fn(),
}));

vi.mock("@/lib/server/auth", () => auth);

import {
  DASHBOARD_COLLECTION_LIMIT,
  DashboardPaginationError,
  loadDatasetsData,
  loadExperimentsData,
  loadExperimentWorkspace,
  loadJobListData,
} from "@/lib/server/dashboard-data";

describe("Job list API filters", () => {
  beforeEach(() => {
    auth.authenticatedManagerRequest.mockReset();
    auth.dashboardDemoMode.mockReset().mockReturnValue(false);
  });

  it("forwards only the selected status and experiment to Manager", async () => {
    auth.authenticatedManagerRequest.mockResolvedValue({
      items: [],
      total: 0,
      offset: 0,
      limit: 200,
    });

    const result = await loadJobListData({ status: "failed", experimentId: "experiment-1" });

    expect(result.jobs).toEqual({ items: [], total: 0 });
    expect(auth.authenticatedManagerRequest).toHaveBeenCalledWith(
      "/api/v1/jobs?limit=200&offset=0&status=failed&experiment_id=experiment-1",
    );
    expect(auth.authenticatedManagerRequest).toHaveBeenCalledWith(
      "/api/v1/experiments?limit=200&offset=0",
    );
  });

  it("applies the same filters locally without API writes in Demo mode", async () => {
    auth.dashboardDemoMode.mockReturnValue(true);

    const result = await loadJobListData({ status: "completed", experimentId: "exp-002" });

    expect(result.jobs.items.map((job) => job.id)).toEqual(["job-003"]);
    expect(auth.authenticatedManagerRequest).not.toHaveBeenCalled();
  });

  it("loads an Experiment workspace and requires both ready and is_usable", async () => {
    auth.authenticatedManagerRequest
      .mockResolvedValueOnce({
        id: "experiment-1",
        row_version: 2,
        name: "speaker-a",
        dataset_id: "dataset-1",
        description: null,
      })
      .mockResolvedValueOnce({
        id: "dataset-1",
        name: "speaker-a-data",
        status: "decoder_pending",
        is_usable: true,
        file_count: 3,
        duration_sec: 90,
      })
      .mockResolvedValueOnce({
        items: [{ id: "job-1", job_name: "speaker-a-v2", status: "queued", created_at: "2026-07-11T12:00:00Z" }],
        total: 1,
        offset: 0,
        limit: 200,
      });

    const result = await loadExperimentWorkspace("experiment-1");

    expect(result?.dataset).toMatchObject({ status: "decoder_pending", isUsable: false });
    expect(result?.rowVersion).toBe(2);
    expect(result?.jobs).toEqual([
      { id: "job-1", name: "speaker-a-v2", status: "queued", createdAt: "2026-07-11T12:00:00Z" },
    ]);
    expect(auth.authenticatedManagerRequest).toHaveBeenNthCalledWith(
      1,
      "/api/v1/experiments/experiment-1",
    );
    expect(auth.authenticatedManagerRequest).toHaveBeenNthCalledWith(
      2,
      "/api/v1/datasets/dataset-1",
    );
    expect(auth.authenticatedManagerRequest).toHaveBeenNthCalledWith(
      3,
      "/api/v1/jobs?limit=200&offset=0&experiment_id=experiment-1",
    );
  });

  it("fails closed before loading related resources when Experiment row version is invalid", async () => {
    auth.authenticatedManagerRequest.mockResolvedValueOnce({
      id: "experiment-1",
      row_version: 0,
      name: "speaker-a",
      dataset_id: "dataset-1",
      description: null,
    });

    await expect(loadExperimentWorkspace("experiment-1")).rejects.toThrow(
      "invalid Experiment row version",
    );
    expect(auth.authenticatedManagerRequest).toHaveBeenCalledTimes(1);
  });

  it("loads every Dataset, Experiment and Job page before computing run counts", async () => {
    auth.authenticatedManagerRequest.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/v1/experiments?")) {
        return page(path, [
          { id: "experiment-1", name: "exp-one", dataset_id: "dataset-1", updated_at: "2026-07-11T12:00:00Z" },
          { id: "experiment-2", name: "exp-two", dataset_id: "dataset-2", updated_at: "2026-07-11T12:00:00Z" },
        ]);
      }
      if (path.startsWith("/api/v1/datasets?")) {
        return page(path, [
          { id: "dataset-1", name: "data-one" },
          { id: "dataset-2", name: "data-two" },
        ]);
      }
      if (path.startsWith("/api/v1/jobs?")) {
        return page(path, [
          { id: "job-1", experiment_id: "experiment-1", status: "completed" },
          { id: "job-2", experiment_id: "experiment-1", status: "queued" },
        ]);
      }
      throw new Error(`unexpected path: ${path}`);
    });

    const result = await loadExperimentsData();

    expect(result.items).toHaveLength(2);
    expect(result.items[0]).toMatchObject({
      id: "experiment-1",
      datasetName: "data-one",
      runCount: 2,
      completedCount: 1,
    });
    expect(auth.authenticatedManagerRequest).toHaveBeenCalledWith(
      "/api/v1/jobs?limit=200&offset=1",
    );
  });

  it("loads every Experiment detail Job page", async () => {
    auth.authenticatedManagerRequest.mockImplementation(async (path: string) => {
      if (path === "/api/v1/experiments/experiment-1") {
        return {
          id: "experiment-1",
          row_version: 4,
          name: "speaker-a",
          dataset_id: "dataset-1",
          description: null,
        };
      }
      if (path === "/api/v1/datasets/dataset-1") {
        return {
          id: "dataset-1",
          name: "speaker-a-data",
          status: "ready",
          is_usable: true,
          file_count: 3,
          duration_sec: 90,
        };
      }
      if (path.startsWith("/api/v1/jobs?")) {
        return page(path, [
          { id: "job-1", job_name: "first", status: "queued", created_at: "2026-07-11T12:00:00Z" },
          { id: "job-2", job_name: "second", status: "failed", created_at: "2026-07-11T12:01:00Z" },
        ]);
      }
      throw new Error(`unexpected path: ${path}`);
    });

    const result = await loadExperimentWorkspace("experiment-1");

    expect(result?.jobs.map((job) => job.id)).toEqual(["job-1", "job-2"]);
    expect(result?.rowVersion).toBe(4);
    expect(auth.authenticatedManagerRequest).toHaveBeenCalledWith(
      "/api/v1/jobs?limit=200&offset=1&experiment_id=experiment-1",
    );
  });

  it("returns an explicit fail-closed limitation instead of a partial collection", async () => {
    auth.authenticatedManagerRequest.mockResolvedValue({
      items: [{ id: "dataset-1" }],
      total: DASHBOARD_COLLECTION_LIMIT + 1,
      offset: 0,
      limit: 200,
    });

    const result = await loadDatasetsData();

    expect(result.items).toEqual([]);
    expect(result.limitation).toEqual({
      reason: "item_limit_exceeded",
      maximum: DASHBOARD_COLLECTION_LIMIT,
      total: DASHBOARD_COLLECTION_LIMIT + 1,
      resource: "datasets",
    });
  });

  it("fails closed when pagination does not advance", async () => {
    auth.authenticatedManagerRequest.mockResolvedValue({
      items: [],
      total: 2,
      offset: 0,
      limit: 200,
    });

    await expect(loadDatasetsData()).rejects.toBeInstanceOf(DashboardPaginationError);
  });
});

function page(path: string, items: Array<Record<string, unknown>>) {
  const offset = Number(new URL(path, "https://manager.test").searchParams.get("offset") ?? "0");
  return {
    items: items.slice(offset, offset + 1),
    total: items.length,
    offset,
    limit: 200,
  };
}

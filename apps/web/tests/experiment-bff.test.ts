import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { buildJobMatrix } from "@/lib/client/job-matrix";

const manager = vi.hoisted(() => ({ managerRawRequest: vi.fn() }));
vi.mock("@/lib/server/manager-api", () => manager);

import {
  DELETE as deleteExperiment,
  GET as getExperiment,
  PATCH as updateExperiment,
} from "@/app/bff/experiments/[experimentId]/route";
import { GET as getExperimentJobs } from "@/app/bff/experiments/[experimentId]/jobs/route";
import { GET as listExperiments, POST as createExperiment } from "@/app/bff/experiments/route";
import { POST as createJob } from "@/app/bff/jobs/route";

const TOKEN = "private.jwt.only.in.httponly.cookie";
const experimentContext = { params: Promise.resolve({ experimentId: "experiment-1" }) };

describe("Experiment and Job creation BFF", () => {
  beforeEach(() => {
    manager.managerRawRequest.mockReset();
    vi.stubEnv("DASHBOARD_DEMO_MODE", "false");
  });

  afterEach(() => vi.unstubAllEnvs());

  it("creates an Experiment with the JWT only in the server-side request", async () => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(Response.json({ ...experimentFixture(), created_by: "private-owner" }, { status: 201 })),
    );
    const payload = { name: "speaker-a", dataset_id: "dataset-1", description: "compare" };
    const response = await createExperiment(mutationRequest("https://manager.test/bff/experiments", payload, {
      browserAuthorization: "Bearer browser-injected",
    }));
    const body = await response.json();

    expect(response.status).toBe(201);
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(body.created_by).toBeUndefined();
    expect(JSON.stringify(body)).not.toContain(TOKEN);
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      "/api/v1/experiments",
      expect.objectContaining({ body: payload, method: "POST", token: TOKEN }),
    );
    expect(manager.managerRawRequest.mock.calls[0]?.[1]).not.toHaveProperty("headers");
  });

  it("rejects Origin/Host mismatch, arbitrary fields and oversized bodies before forwarding", async () => {
    const wrongHost = await createExperiment(mutationRequest(
      "https://manager.test/bff/experiments",
      { name: "speaker-a", dataset_id: "dataset-1", description: null },
      { forwardedHost: "other.test" },
    ));
    const injected = await createExperiment(mutationRequest(
      "https://manager.test/bff/experiments",
      { name: "speaker-a", dataset_id: "dataset-1", description: null, manager_path: "/api/v1/users" },
    ));
    const oversized = await createExperiment(mutationRequest(
      "https://manager.test/bff/experiments",
      { name: "speaker-a", dataset_id: "dataset-1", description: null },
      { contentLength: "999999" },
    ));
    const oversizedStream = await createExperiment(mutationRequest(
      "https://manager.test/bff/experiments",
      { name: "speaker-a", dataset_id: "dataset-1", description: "x".repeat(13_000) },
    ));

    expect(wrongHost.status).toBe(403);
    expect(injected.status).toBe(400);
    expect(oversized.status).toBe(413);
    expect(oversizedStream.status).toBe(413);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it("allowlists Experiment list/detail paths and strips private response fields", async () => {
    manager.managerRawRequest
      .mockResolvedValueOnce(rawResponse(Response.json({ items: [{ ...experimentFixture(), secret: "no" }], total: 1, offset: 0, limit: 50 })))
      .mockResolvedValueOnce(rawResponse(Response.json({ ...experimentFixture(), secret: "no" })));

    const listed = await listExperiments(readRequest("https://manager.test/bff/experiments?limit=50"));
    const detailed = await getExperiment(
      readRequest("https://manager.test/bff/experiments/experiment-1"),
      experimentContext,
    );
    const rejected = await listExperiments(readRequest("https://manager.test/bff/experiments?path=/users"));

    expect(listed.status).toBe(200);
    expect(detailed.status).toBe(200);
    expect((await listed.json()).items[0].secret).toBeUndefined();
    expect(await detailed.json()).toEqual(experimentFixture());
    expect(rejected.status).toBe(400);
    expect(manager.managerRawRequest).toHaveBeenNthCalledWith(
      1,
      "/api/v1/experiments?limit=50",
      expect.objectContaining({ method: "GET", token: TOKEN }),
    );
    expect(manager.managerRawRequest).toHaveBeenNthCalledWith(
      2,
      "/api/v1/experiments/experiment-1",
      expect.objectContaining({ method: "GET", token: TOKEN }),
    );
  });

  it("updates only description with row-version CAS and strips private response fields", async () => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json({
      ...experimentFixture(),
      row_version: 8,
      description: "updated comparison",
      created_by: "private-owner",
      name_conflict_key: "private-key",
    })));
    const payload = { expected_row_version: 7, description: "updated comparison" };
    const response = await updateExperiment(
      mutationRequest(
        "https://manager.test/bff/experiments/experiment-1",
        payload,
        { browserAuthorization: "Bearer browser-injected", method: "PATCH" },
      ),
      experimentContext,
    );
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(body).toEqual({ ...experimentFixture(), row_version: 8, description: "updated comparison" });
    expect(body.created_by).toBeUndefined();
    expect(body.name_conflict_key).toBeUndefined();
    expect(JSON.stringify(body)).not.toContain(TOKEN);
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      "/api/v1/experiments/experiment-1",
      expect.objectContaining({ body: payload, method: "PATCH", token: TOKEN }),
    );
    expect(manager.managerRawRequest.mock.calls[0]?.[1]).not.toHaveProperty("headers");
  });

  it("rejects unsafe Experiment update path, query, body shape and row version", async () => {
    const requests = [
      updateExperiment(
        mutationRequest(
          "https://manager.test/bff/experiments/experiment-1?manager_path=/users",
          { expected_row_version: 1, description: null },
          { method: "PATCH" },
        ),
        experimentContext,
      ),
      updateExperiment(
        mutationRequest(
          "https://manager.test/bff/experiments/experiment-1",
          { expected_row_version: 1, description: null, dataset_id: "other" },
          { method: "PATCH" },
        ),
        experimentContext,
      ),
      updateExperiment(
        mutationRequest(
          "https://manager.test/bff/experiments/experiment-1",
          { expected_row_version: 0, description: null },
          { method: "PATCH" },
        ),
        experimentContext,
      ),
      updateExperiment(
        mutationRequest(
          "https://manager.test/bff/experiments/experiment-1",
          { expected_row_version: 1, description: null },
          { forwardedHost: "other.test", method: "PATCH" },
        ),
        experimentContext,
      ),
      updateExperiment(
        mutationRequest(
          "https://manager.test/bff/experiments/bad%2Fpath",
          { expected_row_version: 1, description: null },
          { method: "PATCH" },
        ),
        { params: Promise.resolve({ experimentId: "bad/path" }) },
      ),
    ];

    const responses = await Promise.all(requests);
    expect(responses.map((response) => response.status)).toEqual([400, 400, 400, 403, 400]);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it("deletes through a fixed CAS query without forwarding browser credentials or a body", async () => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(new Response(null, { status: 204 })));
    const response = await deleteExperiment(
      deleteRequest(
        "https://manager.test/bff/experiments/experiment-1?expected_row_version=2147483647",
        { browserAuthorization: "Bearer browser-injected" },
      ),
      experimentContext,
    );

    expect(response.status).toBe(204);
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(response.headers.get("vary")).toBe("Cookie");
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      "/api/v1/experiments/experiment-1?expected_row_version=2147483647",
      expect.objectContaining({ body: undefined, method: "DELETE", token: TOKEN }),
    );
    expect(manager.managerRawRequest.mock.calls[0]?.[1]).not.toHaveProperty("headers");
  });

  it("rejects missing, duplicate, extra or out-of-range delete query and any request body", async () => {
    const urls = [
      "https://manager.test/bff/experiments/experiment-1",
      "https://manager.test/bff/experiments/experiment-1?expected_row_version=1&expected_row_version=2",
      "https://manager.test/bff/experiments/experiment-1?expected_row_version=1&force=true",
      "https://manager.test/bff/experiments/experiment-1?expected_row_version=0",
      "https://manager.test/bff/experiments/experiment-1?expected_row_version=2147483648",
    ];
    const responses = await Promise.all(urls.map((url) =>
      deleteExperiment(deleteRequest(url), experimentContext),
    ));
    const withBody = await deleteExperiment(
      deleteRequest(
        "https://manager.test/bff/experiments/experiment-1?expected_row_version=1",
        { body: { force: true } },
      ),
      experimentContext,
    );
    const wrongOrigin = await deleteExperiment(
      deleteRequest(
        "https://manager.test/bff/experiments/experiment-1?expected_row_version=1",
        { forwardedHost: "other.test" },
      ),
      experimentContext,
    );

    expect([...responses.map((response) => response.status), withBody.status, wrongOrigin.status]).toEqual([
      400, 400, 400, 400, 400, 400, 403,
    ]);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it.each([
    ["experiment changed; refresh and retry", "stale_experiment"],
    ["experiment with jobs cannot be deleted", "experiment_has_jobs"],
    ["experiment with MLflow projection cannot be deleted", "experiment_has_mlflow_projection"],
    ["experiment became referenced and cannot be deleted", "experiment_became_referenced"],
    ["private internal conflict", "conflict"],
  ] as const)("maps a known delete conflict without exposing upstream detail: %s", async (detail, code) => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(Response.json({ detail, private_path: "/secret" }, { status: 409 })),
    );
    const response = await deleteExperiment(
      deleteRequest("https://manager.test/bff/experiments/experiment-1?expected_row_version=7"),
      experimentContext,
    );

    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: code });
  });

  it("maps update row-version conflict to a stale error code", async () => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json(
      { detail: "experiment changed; refresh and retry", current_row_version: 99 },
      { status: 409 },
    )));
    const response = await updateExperiment(
      mutationRequest(
        "https://manager.test/bff/experiments/experiment-1",
        { expected_row_version: 7, description: null },
        { method: "PATCH" },
      ),
      experimentContext,
    );

    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: "stale_experiment" });
  });

  it("lists names only for the fixed Experiment path and rejects query injection", async () => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(Response.json({ items: [jobFixture()], total: 1, offset: 0, limit: 200 })),
    );
    const response = await getExperimentJobs(
      readRequest("https://manager.test/bff/experiments/experiment-1/jobs?offset=0&limit=200"),
      experimentContext,
    );
    const rejected = await getExperimentJobs(
      readRequest("https://manager.test/bff/experiments/experiment-1/jobs?experiment_id=other"),
      experimentContext,
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      items: [{ id: "job-1", job_name: jobFixture().job_name, status: "queued", created_at: jobFixture().created_at }],
      total: 1,
      offset: 0,
      limit: 200,
    });
    expect(rejected.status).toBe(400);
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      "/api/v1/jobs?offset=0&limit=200&experiment_id=experiment-1",
      expect.objectContaining({ method: "GET", token: TOKEN }),
    );
  });

  it("forwards a strict Job config and never enables unsupported auto samples", async () => {
    const config = jobConfig();
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(Response.json({ ...jobFixture(), job_name: config.job_name }, { status: 201 })),
    );
    const response = await createJob(mutationRequest("https://manager.test/bff/jobs", config));
    const body = await response.json();

    expect(response.status).toBe(201);
    expect(body).toMatchObject({ id: "job-1", experiment_id: "experiment-1", dataset_id: "dataset-1" });
    expect(JSON.stringify(body)).not.toContain(TOKEN);
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      "/api/v1/jobs",
      expect.objectContaining({
        body: expect.objectContaining({
          auto_inference_samples: expect.objectContaining({ enabled: false, test_set_id: null }),
          artifacts: expect.objectContaining({ collect_samples: false }),
        }),
        method: "POST",
        token: TOKEN,
      }),
    );
  });

  it("blocks hidden Job fields and sample enablement before contacting Manager", async () => {
    const config = jobConfig();
    const injected = await createJob(mutationRequest("https://manager.test/bff/jobs", {
      ...config,
      target_path: "/api/v1/workers/register",
    }));
    const sampleEnabled = await createJob(mutationRequest("https://manager.test/bff/jobs", {
      ...config,
      auto_inference_samples: {
        ...config.auto_inference_samples,
        enabled: true,
        test_set_id: "future-test-set",
      },
      artifacts: { ...config.artifacts, collect_samples: true },
    }));

    expect(injected.status).toBe(400);
    expect(sampleEnabled.status).toBe(400);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it.each([
    [409, "conflict", null],
    [422, "invalid_job", null],
    [429, "rate_limited", "23"],
  ] as const)("preserves safe Job error status %s and Retry-After", async (status, code, retryAfter) => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(Response.json(
        { detail: "private validation internals" },
        { status, headers: retryAfter ? { "Retry-After": retryAfter } : undefined },
      )),
    );

    const response = await createJob(mutationRequest("https://manager.test/bff/jobs", jobConfig()));

    expect(response.status).toBe(status);
    expect(await response.json()).toEqual({ error: code });
    expect(response.headers.get("retry-after")).toBe(retryAfter);
  });

  it("preserves a safe committed ledger ID when MLflow projection is deferred", async () => {
    manager.managerRawRequest
      .mockResolvedValueOnce(rawResponse(Response.json({
        detail: {
          code: "mlflow_projection_deferred",
          ledger_committed: true,
          resource_type: "experiment",
          resource_id: "experiment-committed",
          internal_uri: "s3://must-not-leak",
        },
      }, { status: 503, headers: { "Retry-After": "5" } })))
      .mockResolvedValueOnce(rawResponse(Response.json({
        detail: {
          code: "mlflow_projection_deferred",
          ledger_committed: true,
          resource_type: "job",
          resource_id: "job-committed",
        },
      }, { status: 503, headers: { "Retry-After": "5" } })));

    const experiment = await createExperiment(mutationRequest(
      "https://manager.test/bff/experiments",
      { name: "speaker-a", dataset_id: "dataset-1", description: null },
    ));
    const job = await createJob(mutationRequest("https://manager.test/bff/jobs", jobConfig()));

    expect(await experiment.json()).toEqual({
      error: "projection_deferred",
      ledger_committed: true,
      resource_type: "experiment",
      resource_id: "experiment-committed",
    });
    expect(await job.json()).toEqual({
      error: "projection_deferred",
      ledger_committed: true,
      resource_type: "job",
      resource_id: "job-committed",
    });
    expect(experiment.headers.get("retry-after")).toBe("5");
    expect(job.headers.get("retry-after")).toBe("5");
  });
});

function jobConfig() {
  const result = buildJobMatrix("experiment-1", "dataset-1", {
    prefix: "speaker-a",
    versions: ["v2"],
    sampleRates: ["40k"],
    useF0: true,
    f0Methods: ["rmvpe_gpu"],
    epochs: 80,
    batchSizePerGpu: 8,
    saveEveryEpoch: 5,
    saveOnlyLatest: false,
    saveEveryWeights: true,
    cacheDatasetInGpu: false,
    gpuIds: [0],
    buildIndex: true,
    minVramGb: 12,
    preferredWorkerTags: ["24gb"],
    priority: 5,
  });
  if (!result.plans[0]) throw new Error("fixture matrix failed");
  return result.plans[0].config;
}

function experimentFixture() {
  return {
    id: "experiment-1",
    row_version: 7,
    name: "speaker-a",
    dataset_id: "dataset-1",
    description: "compare",
    created_at: "2026-07-11T12:00:00Z",
    updated_at: "2026-07-11T12:00:00Z",
  };
}

function jobFixture() {
  return {
    id: "job-1",
    experiment_id: "experiment-1",
    dataset_id: "dataset-1",
    job_name: jobConfig().job_name,
    status: "queued",
    created_at: "2026-07-11T12:10:00Z",
    storage_uri: "s3://private/never-project",
  };
}

function readRequest(url: string): NextRequest {
  return new NextRequest(url, {
    headers: {
      cookie: `rvc_manager_session=${TOKEN}`,
      origin: new URL(url).origin,
      "sec-fetch-site": "same-origin",
    },
  });
}

function mutationRequest(
  url: string,
  body: unknown,
  options: {
    browserAuthorization?: string;
    contentLength?: string;
    forwardedHost?: string;
    method?: "POST" | "PATCH";
  } = {},
): NextRequest {
  return new NextRequest(url, {
    method: options.method ?? "POST",
    headers: {
      cookie: `rvc_manager_session=${TOKEN}`,
      "content-type": "application/json",
      origin: new URL(url).origin,
      "sec-fetch-site": "same-origin",
      ...(options.browserAuthorization ? { authorization: options.browserAuthorization } : {}),
      ...(options.contentLength ? { "content-length": options.contentLength } : {}),
      ...(options.forwardedHost ? { "x-forwarded-host": options.forwardedHost } : {}),
    },
    body: JSON.stringify(body),
  });
}

function deleteRequest(
  url: string,
  options: {
    body?: unknown;
    browserAuthorization?: string;
    forwardedHost?: string;
  } = {},
): NextRequest {
  return new NextRequest(url, {
    method: "DELETE",
    headers: {
      cookie: `rvc_manager_session=${TOKEN}`,
      origin: new URL(url).origin,
      "sec-fetch-site": "same-origin",
      ...(options.body === undefined ? {} : { "content-type": "application/json" }),
      ...(options.browserAuthorization ? { authorization: options.browserAuthorization } : {}),
      ...(options.forwardedHost ? { "x-forwarded-host": options.forwardedHost } : {}),
    },
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
}

function rawResponse(response: Response) {
  return { response, requestId: "request-1" };
}

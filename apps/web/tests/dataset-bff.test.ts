import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const manager = vi.hoisted(() => ({ managerRawRequest: vi.fn() }));
vi.mock("@/lib/server/manager-api", () => manager);

import { DELETE as removeDataset, GET as getDataset } from "@/app/bff/datasets/[datasetId]/route";
import { GET as listDatasets } from "@/app/bff/datasets/route";
import { POST as finalizeUpload } from "@/app/bff/datasets/uploads/[uploadSessionId]/finalize/route";
import { POST as initializeUpload } from "@/app/bff/datasets/uploads/init/route";

const TOKEN = "private.jwt.must.stay.in.the.bff";
const datasetContext = { params: Promise.resolve({ datasetId: "dataset-1" }) };
const uploadContext = { params: Promise.resolve({ uploadSessionId: "upload-1" }) };

describe("Dataset BFF", () => {
  beforeEach(() => {
    manager.managerRawRequest.mockReset();
    vi.stubEnv("DATASET_UPLOAD_ALLOWED_ORIGINS", "https://objects.test");
    vi.stubEnv("DASHBOARD_DEMO_MODE", "false");
  });

  afterEach(() => vi.unstubAllEnvs());

  it("forwards a validated init payload with the JWT only server-side", async () => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json(uploadFixture())));
    const request = mutationRequest(
      "https://manager.test/bff/datasets/uploads/init",
      uploadPayload(),
    );

    const response = await initializeUpload(request);
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(response.headers.get("referrer-policy")).toBe("no-referrer");
    expect(body.upload_url).toContain("https://objects.test/");
    expect(JSON.stringify(body)).not.toContain(TOKEN);
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      "/api/v1/datasets/uploads/init",
      expect.objectContaining({
        body: uploadPayload(),
        method: "POST",
        token: TOKEN,
      }),
    );
  });

  it("rejects cross-site mutation and unknown browser fields before forwarding", async () => {
    const crossSite = await initializeUpload(
      mutationRequest("https://manager.test/bff/datasets/uploads/init", uploadPayload(), {
        origin: "https://evil.test",
        site: "cross-site",
      }),
    );
    const injected = await initializeUpload(
      mutationRequest("https://manager.test/bff/datasets/uploads/init", {
        ...uploadPayload(),
        upload_url: "https://evil.test/collect",
      }),
    );

    expect(crossSite.status).toBe(403);
    expect(injected.status).toBe(400);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it.each([
    ["unapproved origin", { upload_url: "https://evil.test/put?secret=value" }],
    ["HTTPS downgrade", { upload_url: "http://objects.test/put?secret=value" }],
    ["userinfo", { upload_url: "https://user:password@objects.test/put" }],
    ["fragment", { upload_url: "https://objects.test/put#secret" }],
    ["credential header", { upload_headers: { ...uploadFixture().upload_headers, Authorization: "Bearer bad" } }],
  ])("blocks an upstream %s upload target", async (_label, override) => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(Response.json({ ...uploadFixture(), ...override })),
    );

    const response = await initializeUpload(
      mutationRequest("https://manager.test/bff/datasets/uploads/init", uploadPayload()),
    );

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "invalid_upload_target" });
  });

  it("keeps Retry-After while replacing the Manager error body", async () => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(Response.json(
        { detail: "private quota internals" },
        { status: 429, headers: { "Retry-After": "17" } },
      )),
    );

    const response = await initializeUpload(
      mutationRequest("https://manager.test/bff/datasets/uploads/init", uploadPayload()),
    );

    expect(response.status).toBe(429);
    expect(response.headers.get("retry-after")).toBe("17");
    expect(await response.json()).toEqual({ error: "rate_limited" });
  });

  it("projects only public Dataset fields for list and detail", async () => {
    const privateDataset = {
      ...datasetFixture(),
      storage_uri: "s3://private/original",
      flat_storage_uri: "s3://private/flat",
      quality_report_json: {
        skipped: [{ source_path: "nested/private-speaker.wav", reason: "hidden" }],
        duplicates: [{ source_path: "copy.wav", duplicate_of: "prepared_flat/000001.wav" }],
      },
    };
    manager.managerRawRequest
      .mockResolvedValueOnce(rawResponse(Response.json({ items: [privateDataset], total: 1, offset: 0, limit: 50 })))
      .mockResolvedValueOnce(rawResponse(Response.json(privateDataset)));

    const listed = await listDatasets(readRequest("https://manager.test/bff/datasets?limit=50"));
    const detailed = await getDataset(
      readRequest("https://manager.test/bff/datasets/dataset-1"),
      datasetContext,
    );
    const listBody = await listed.json();
    const detailBody = await detailed.json();

    expect(listed.status).toBe(200);
    expect(detailed.status).toBe(200);
    expect(listBody.items[0].storage_uri).toBeUndefined();
    expect(detailBody.flat_storage_uri).toBeUndefined();
    expect(detailBody.quality_report_json).toBeUndefined();
    expect(detailBody.pcm_quality).toEqual(datasetFixture().pcm_quality);
    expect(JSON.stringify(detailBody)).not.toContain("private-speaker.wav");
    expect(JSON.stringify(detailBody)).not.toContain("prepared_flat/000001.wav");
  });

  it.each([
    ["unknown algorithm", { algorithm: "file-average-v0" }],
    ["zero sample count", { sample_count: 0 }],
    ["ratio above one", { clipping_ratio: 1.01 }],
    ["non-finite-compatible null metric", { rms_ratio: null }],
    ["private nested field", { source_path: "private/member.wav" }],
  ])("fails closed for malformed PCM aggregate: %s", async (_label, override) => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json({
      ...datasetFixture(),
      pcm_quality: { ...datasetFixture().pcm_quality, ...override },
    })));

    const response = await getDataset(
      readRequest("https://manager.test/bff/datasets/dataset-1"),
      datasetContext,
    );

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "invalid_upstream_response" });
  });

  it.each([
    ["non-finite-compatible null", { integrated_lufs: null, unavailable_reason: null }],
    ["wrong algorithm", { algorithm: "file-lufs-average-v0" }],
    ["gated blocks above total", { gated_block_count: 8 }],
    ["partial-input leakage", { source_path: "private/member.wav" }],
  ])("fails closed for malformed LUFS aggregate: %s", async (_label, override) => {
    const fixture = datasetFixture();
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json({
      ...fixture,
      pcm_quality: {
        ...fixture.pcm_quality,
        loudness: { ...fixture.pcm_quality.loudness, ...override },
      },
    })));

    const response = await getDataset(
      readRequest("https://manager.test/bff/datasets/dataset-1"),
      datasetContext,
    );

    expect(response.status).toBe(502);
  });

  it("preserves explicit historical LUFS null without reconstructing it", async () => {
    const fixture = datasetFixture();
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json({
      ...fixture,
      pcm_quality: { ...fixture.pcm_quality, loudness: null },
    })));

    const response = await getDataset(
      readRequest("https://manager.test/bff/datasets/dataset-1"),
      datasetContext,
    );

    expect(response.status).toBe(200);
    expect((await response.json()).pcm_quality.loudness).toBeNull();
  });

  it("keeps historical aggregate and count fields explicitly null", async () => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json({
      ...datasetFixture(),
      source_file_entry_count: null,
      skipped_file_count: null,
      rejected_file_count: null,
      duplicate_file_count: null,
      pcm_quality: null,
    })));

    const response = await getDataset(
      readRequest("https://manager.test/bff/datasets/dataset-1"),
      datasetContext,
    );
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body).toMatchObject({
      source_file_entry_count: null,
      skipped_file_count: null,
      rejected_file_count: null,
      duplicate_file_count: null,
      pcm_quality: null,
    });
  });

  it("fails closed for malformed typed count fields", async () => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json({
      ...datasetFixture(),
      rejected_file_count: -1,
    })));

    const response = await getDataset(
      readRequest("https://manager.test/bff/datasets/dataset-1"),
      datasetContext,
    );

    expect(response.status).toBe(502);
  });

  it("forwards finalize without a timeout and preserves a delete conflict", async () => {
    manager.managerRawRequest
      .mockResolvedValueOnce(rawResponse(Response.json(datasetFixture())))
      .mockResolvedValueOnce(rawResponse(Response.json({ detail: "referenced" }, { status: 409 })));

    const finalized = await finalizeUpload(
      mutationRequest("https://manager.test/bff/datasets/uploads/upload-1/finalize"),
      uploadContext,
    );
    const removed = await removeDataset(
      mutationRequest("https://manager.test/bff/datasets/dataset-1", undefined, { method: "DELETE" }),
      datasetContext,
    );

    expect(finalized.status).toBe(200);
    expect(manager.managerRawRequest.mock.calls[0]?.[1]).toMatchObject({
      method: "POST",
      timeoutMs: null,
      token: TOKEN,
    });
    expect(removed.status).toBe(409);
    expect(await removed.json()).toEqual({ error: "conflict" });
  });
});

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
  body?: unknown,
  options: { origin?: string; site?: string; method?: "POST" | "DELETE" } = {},
): NextRequest {
  return new NextRequest(url, {
    method: options.method ?? "POST",
    headers: {
      cookie: `rvc_manager_session=${TOKEN}`,
      ...(body === undefined ? {} : { "content-type": "application/json" }),
      origin: options.origin ?? new URL(url).origin,
      "sec-fetch-site": options.site ?? "same-origin",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function uploadPayload() {
  return {
    name: "speaker-a",
    filename: "speaker.zip",
    content_type: "application/zip",
    size_bytes: 4,
    sha256: "a".repeat(64),
    idempotency_key: "dataset-upload-0001",
  };
}

function uploadFixture() {
  return {
    upload_session_id: "upload-1",
    dataset_id: "dataset-1",
    status: "pending",
    method: "PUT",
    upload_url: "https://objects.test/bucket/key?X-Amz-Signature=temporary",
    upload_headers: {
      "Content-Type": "application/zip",
      "Content-Length": "4",
      "x-amz-meta-sha256": "a".repeat(64),
    },
    expires_at: "2026-07-11T12:30:00Z",
    dataset: null,
    failure_code: null,
    retryable: true,
    retry_after_seconds: null,
  };
}

function datasetFixture() {
  return {
    id: "dataset-1",
    name: "speaker-a",
    status: "ready",
    original_filename: "speaker.zip",
    original_size_bytes: 4,
    original_sha256: "a".repeat(64),
    original_mime_type: "application/zip",
    prepared_flat_size_bytes: 10,
    prepared_flat_sha256: "b".repeat(64),
    manifest_sha256: "c".repeat(64),
    quality_report_sha256: "d".repeat(64),
    duration_sec: 1,
    file_count: 1,
    sample_rate: 48000,
    decoder_pending_count: 0,
    source_file_entry_count: 2,
    skipped_file_count: 0,
    rejected_file_count: 0,
    duplicate_file_count: 1,
    pcm_quality: {
      algorithm: "pcm-sample-weighted-v1",
      validated_file_count: 1,
      sample_count: 48_000,
      clipping_ratio: 0.001,
      silence_ratio: 0.08,
      rms_ratio: 0.18,
      silence_threshold_dbfs: -50,
      loudness: {
        algorithm: "itu-r-bs1770-4-mono-stereo-v1",
        scope: "global-gate-over-per-file-complete-blocks-v1",
        block_duration_ms: 400,
        block_overlap_percent: 75,
        absolute_gate_lufs: -70,
        relative_gate_lu: -10,
        analyzed_file_count: 1,
        block_count: 7,
        gated_block_count: 7,
        integrated_lufs: -23.1,
        unavailable_reason: null,
      },
    },
    is_usable: true,
    failure_code: null,
    retryable: false,
    created_at: "2026-07-11T12:00:00Z",
    updated_at: "2026-07-11T12:10:00Z",
  };
}

function rawResponse(response: Response) {
  return { response, requestId: "request-1" };
}

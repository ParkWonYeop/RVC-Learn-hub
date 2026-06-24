import { NextRequest } from "next/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

const manager = vi.hoisted(() => {
  class ManagerApiError extends Error {
    constructor(
      message: string,
      readonly status: number,
      readonly requestId: string,
    ) {
      super(message);
    }
  }
  return { ManagerApiError, managerRawRequest: vi.fn() };
});

vi.mock("@/lib/server/manager-api", () => manager);

import { GET as listSamples } from "@/app/bff/jobs/[jobId]/samples/route";
import { GET as downloadSample } from "@/app/bff/samples/[sampleId]/download/route";

const TOKEN = "private.jwt.value.never.returned.to.browser";
const JOB_ID = "10000000-0000-4000-8000-000000000001";
const ATTEMPT_ID = "10000000-0000-4000-8000-000000000002";
const SAMPLE_ID = "10000000-0000-4000-8000-000000000003";
const TEST_SET_ID = "10000000-0000-4000-8000-000000000004";
const ITEM_ID = "10000000-0000-4000-8000-000000000005";

describe("Sample BFF projection and audio boundary", () => {
  beforeEach(() => manager.managerRawRequest.mockReset());

  it("projects only bounded Sample fields and drops Artifact/storage secrets", async () => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(
        Response.json({
          items: [sampleFixture()],
          total: 1,
          offset: 0,
          limit: 200,
          storage_uri: "s3://private-bucket/never-forward",
        }),
      ),
    );

    const response = await listSamples(
      browserRequest(`https://manager.test/bff/jobs/${JOB_ID}/samples?limit=200`),
      { params: Promise.resolve({ jobId: JOB_ID }) },
    );
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(body.items[0]).toMatchObject({
      id: SAMPLE_ID,
      jobId: JOB_ID,
      testSetItemId: ITEM_ID,
      inferenceF0Method: "rmvpe",
      nativeInferenceRequestSha256: "8".repeat(64),
    });
    expect(body.items[0]).not.toHaveProperty("artifactId");
    expect(JSON.stringify(body)).not.toContain("storage_uri");
    expect(JSON.stringify(body)).not.toContain("private-bucket");
    expect(JSON.stringify(body)).not.toContain(TOKEN);
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      `/api/v1/jobs/${JOB_ID}/samples?limit=200`,
      expect.objectContaining({ token: TOKEN }),
    );
  });

  it("fails closed when Sample provenance is malformed", async () => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(
        Response.json({
          items: [{ ...sampleFixture(), runtime_image_digest: "latest" }],
          total: 1,
          offset: 0,
          limit: 50,
        }),
      ),
    );

    const response = await listSamples(
      browserRequest(`https://manager.test/bff/jobs/${JOB_ID}/samples`),
      { params: Promise.resolve({ jobId: JOB_ID }) },
    );

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "invalid_upstream_response" });
  });

  it("streams only verified WAV headers and never forwards upstream cookies", async () => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(
        new Response(new Uint8Array([82, 73, 70, 70]), {
          headers: {
            "accept-ranges": "bytes",
            "content-disposition": "attachment; filename=private.wav",
            "content-length": "4",
            "content-type": "audio/wav",
            etag: '"sample-etag"',
            "set-cookie": "storage-secret=never",
            "x-storage-uri": "s3://private/sample.wav",
          },
        }),
      ),
    );

    const response = await downloadSample(
      browserRequest(`https://manager.test/bff/samples/${SAMPLE_ID}/download`),
      { params: Promise.resolve({ sampleId: SAMPLE_ID }) },
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("audio/wav");
    expect(response.headers.get("content-disposition")).toBe(
      'inline; filename="sample.wav"',
    );
    expect(response.headers.get("accept-ranges")).toBe("bytes");
    expect(response.headers.get("etag")).toBe('"sample-etag"');
    expect(response.headers.get("set-cookie")).toBeNull();
    expect(response.headers.get("x-storage-uri")).toBeNull();
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(
      new Uint8Array([82, 73, 70, 70]),
    );
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      `/api/v1/samples/${SAMPLE_ID}/download`,
      expect.objectContaining({
        accept: "audio/wav",
        redirect: "manual",
        timeoutMs: null,
        token: TOKEN,
      }),
    );
  });

  it("forwards one safe byte range and projects a valid 206 response", async () => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(
        new Response(new Uint8Array([82, 73, 70, 70]), {
          status: 206,
          headers: {
            "accept-ranges": "bytes",
            "content-length": "4",
            "content-range": "bytes 0-3/8",
            "content-type": "audio/wav",
            etag: '"sample-etag"',
          },
        }),
      ),
    );

    const response = await downloadSample(
      browserRequest(`https://manager.test/bff/samples/${SAMPLE_ID}/download`, {
        range: "bytes=0-3",
        ifRange: '"sample-etag"',
      }),
      { params: Promise.resolve({ sampleId: SAMPLE_ID }) },
    );

    expect(response.status).toBe(206);
    expect(response.headers.get("content-range")).toBe("bytes 0-3/8");
    expect(response.headers.get("accept-ranges")).toBe("bytes");
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      `/api/v1/samples/${SAMPLE_ID}/download`,
      expect.objectContaining({
        range: "bytes=0-3",
        ifRange: '"sample-etag"',
        token: TOKEN,
      }),
    );
  });

  it("allows a full 200 response when a valid If-Range validator is stale", async () => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(
        new Response(new Uint8Array([82, 73, 70, 70]), {
          headers: {
            "accept-ranges": "bytes",
            "content-length": "4",
            "content-type": "audio/wav",
            etag: '"new-etag"',
          },
        }),
      ),
    );

    const response = await downloadSample(
      browserRequest(`https://manager.test/bff/samples/${SAMPLE_ID}/download`, {
        range: "bytes=0-3",
        ifRange: '"old-etag"',
      }),
      { params: Promise.resolve({ sampleId: SAMPLE_ID }) },
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("content-range")).toBeNull();
    expect(response.headers.get("accept-ranges")).toBe("bytes");
  });

  it("fails closed if Manager ignores an unconditional Range request", async () => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(
        new Response(new Uint8Array([82, 73, 70, 70]), {
          headers: {
            "content-length": "4",
            "content-type": "audio/wav",
          },
        }),
      ),
    );

    const response = await downloadSample(
      browserRequest(`https://manager.test/bff/samples/${SAMPLE_ID}/download`, {
        range: "bytes=0-3",
      }),
      { params: Promise.resolve({ sampleId: SAMPLE_ID }) },
    );

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "range_not_supported" });
  });

  it.each(["bytes=10-5", "bytes=-0", "bytes=0-1,4-5", "items=0-1"])(
    "rejects malformed or ambiguous Range %s before Manager access",
    async (range) => {
      const response = await downloadSample(
        browserRequest(`https://manager.test/bff/samples/${SAMPLE_ID}/download`, {
          range,
        }),
        { params: Promise.resolve({ sampleId: SAMPLE_ID }) },
      );

      expect(response.status).toBe(400);
      expect(manager.managerRawRequest).not.toHaveBeenCalled();
    },
  );

  it("safely projects an unsatisfiable range as 416", async () => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(
        new Response(null, {
          status: 416,
          headers: { "content-range": "bytes */8" },
        }),
      ),
    );

    const response = await downloadSample(
      browserRequest(`https://manager.test/bff/samples/${SAMPLE_ID}/download`, {
        range: "bytes=20-30",
      }),
      { params: Promise.resolve({ sampleId: SAMPLE_ID }) },
    );

    expect(response.status).toBe(416);
    expect(response.headers.get("content-range")).toBe("bytes */8");
    expect(await response.json()).toEqual({ error: "range_not_satisfiable" });
  });

  it("rejects Manager redirects without exposing or following object URLs", async () => {
    const location = "https://objects.example/sample.wav?X-Amz-Signature=secret";
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(new Response(null, { status: 307, headers: { location } })),
    );

    const response = await downloadSample(
      browserRequest(`https://manager.test/bff/samples/${SAMPLE_ID}/download`),
      { params: Promise.resolve({ sampleId: SAMPLE_ID }) },
    );

    expect(response.status).toBe(502);
    expect(response.headers.get("location")).toBeNull();
    expect(JSON.stringify(await response.json())).not.toContain("X-Amz");
    expect(manager.managerRawRequest).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["missing", undefined],
    ["oversized", String(256 * 1024 * 1024 + 1)],
  ])("rejects a %s Sample Content-Length", async (_description, contentLength) => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(
        new Response(new Uint8Array([82, 73, 70, 70]), {
          headers: {
            "content-type": "audio/wav",
            ...(contentLength ? { "content-length": contentLength } : {}),
          },
        }),
      ),
    );

    const response = await downloadSample(
      browserRequest(`https://manager.test/bff/samples/${SAMPLE_ID}/download`),
      { params: Promise.resolve({ sampleId: SAMPLE_ID }) },
    );

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "invalid_upstream_download" });
  });

  it("cancels the Manager WAV stream when the browser closes", async () => {
    let cancelled = false;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array([82, 73, 70, 70]));
      },
      cancel() {
        cancelled = true;
      },
    });
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(
        new Response(body, {
          headers: { "content-length": "4", "content-type": "audio/wav" },
        }),
      ),
    );

    const response = await downloadSample(
      browserRequest(`https://manager.test/bff/samples/${SAMPLE_ID}/download`),
      { params: Promise.resolve({ sampleId: SAMPLE_ID }) },
    );
    const call = manager.managerRawRequest.mock.calls[0]?.[1];

    await response.body?.cancel("audio element removed");

    expect(cancelled).toBe(true);
    expect(call.signal.aborted).toBe(true);
  });
});

function browserRequest(
  url: string,
  options: { range?: string; ifRange?: string } = {},
): NextRequest {
  return new NextRequest(url, {
    headers: {
      cookie: `rvc_manager_session=${TOKEN}`,
      origin: new URL(url).origin,
      "sec-fetch-site": "same-origin",
      ...(options.range ? { range: options.range } : {}),
      ...(options.ifRange ? { "if-range": options.ifRange } : {}),
    },
  });
}

function rawResponse(response: Response) {
  return { response, requestId: "request-1" };
}

function sampleFixture() {
  const metric = {
    peak_amplitude: 0.5,
    rms: 0.125,
    clipping_ratio: 0,
    silence_ratio: 0.25,
  };
  return {
    id: SAMPLE_ID,
    job_id: JOB_ID,
    attempt_id: ATTEMPT_ID,
    test_set_id: TEST_SET_ID,
    test_set_item_id: ITEM_ID,
    artifact_id: "10000000-0000-4000-8000-000000000006",
    input_sha256: "1".repeat(64),
    model_sha256: "2".repeat(64),
    index_sha256: null,
    inference_f0_method: "rmvpe",
    inference_config_sha256: "3".repeat(64),
    native_inference_manifest_sha256: "7".repeat(64),
    native_inference_request_sha256: "8".repeat(64),
    output_size_bytes: 1_644,
    output_sha256: "4".repeat(64),
    output_sample_rate_hz: 40_000,
    output_channels: 1,
    output_duration_seconds: 0.02,
    metrics: {
      algorithm: "pcm-normalized-v2",
      authoritative_source: "manager_computed",
      clipping_threshold: 0.999,
      silence_threshold: 0.0001,
      worker_reported: metric,
      manager_computed: metric,
      worker_reported_duration_seconds: 0.02,
      manager_computed_sample_rate_hz: 40_000,
      manager_computed_channels: 1,
      manager_computed_duration_seconds: 0.02,
    },
    rvc_commit_hash: "5".repeat(40),
    runtime_image_digest: `sha256:${"6".repeat(64)}`,
    runtime_asset_manifest_sha256: "9".repeat(64),
    created_at: "2026-07-11T12:00:00Z",
    storage_uri: "s3://private-bucket/never-forward",
    token: TOKEN,
  };
}

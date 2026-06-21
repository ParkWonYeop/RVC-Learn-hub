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
  return {
    ManagerApiError,
    managerRawRequest: vi.fn(),
  };
});

vi.mock("@/lib/server/manager-api", () => manager);

import { GET as downloadArtifact } from "@/app/bff/artifacts/[artifactId]/download/route";
import { GET as listArtifacts } from "@/app/bff/jobs/[jobId]/artifacts/route";
import { GET as listLogs } from "@/app/bff/jobs/[jobId]/logs/route";
import { GET as streamLogs } from "@/app/bff/jobs/[jobId]/logs/stream/route";
import { GET as listMetrics } from "@/app/bff/jobs/[jobId]/metrics/route";

const TOKEN = "private.jwt.value.never.returned.to.browser";
const context = { params: Promise.resolve({ jobId: "job-1" }) };

describe("observability BFF security and forwarding", () => {
  beforeEach(() => {
    manager.managerRawRequest.mockReset();
  });

  it("forwards an allowlisted log query with the HttpOnly bearer server-side", async () => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(
        Response.json({ items: [], total: 0, limit: 25, has_more: false, next_cursor: null }),
      ),
    );
    const request = browserRequest(
      "https://manager.test/bff/jobs/job-1/logs?tail=true&limit=25",
    );

    const response = await listLogs(request, context);

    const body = await response.json();
    expect(response.status).toBe(200);
    expect(body).toMatchObject({ total: 0, items: [] });
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      "/api/v1/jobs/job-1/logs?tail=true&limit=25",
      expect.objectContaining({ token: TOKEN }),
    );
    expect(JSON.stringify(body)).not.toContain(TOKEN);
  });

  it("forwards a bounded metric tail query without exposing the bearer", async () => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(
        Response.json({ items: [], total: 0, offset: 0, limit: 200 }),
      ),
    );
    const request = browserRequest(
      "https://manager.test/bff/jobs/job-1/metrics?tail=true&limit=200&key=train.loss",
    );

    const response = await listMetrics(request, context);

    const body = await response.json();
    expect(response.status).toBe(200);
    expect(body).toMatchObject({ total: 0, offset: 0, items: [] });
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      "/api/v1/jobs/job-1/metrics?tail=true&limit=200&key=train.loss",
      expect.objectContaining({ token: TOKEN }),
    );
    expect(JSON.stringify(body)).not.toContain(TOKEN);
  });

  it.each([
    [listLogs, "https://manager.test/bff/jobs/job-1/logs?unexpected=value"],
    [listMetrics, "https://manager.test/bff/jobs/job-1/metrics?key=bad%20key"],
    [listMetrics, "https://manager.test/bff/jobs/job-1/metrics?tail=maybe"],
    [listArtifacts, "https://manager.test/bff/jobs/job-1/artifacts?artifact_type=unknown"],
  ])("rejects non-allowlisted or malformed queries", async (handler, url) => {
    const response = await handler(browserRequest(url), context);

    expect(response.status).toBe(400);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it("rejects cross-site reads before forwarding the bearer", async () => {
    const response = await listLogs(
      browserRequest("https://manager.test/bff/jobs/job-1/logs", {
        origin: "https://evil.test",
        site: "cross-site",
      }),
      context,
    );

    expect(response.status).toBe(403);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it("rejects an Origin that does not match the forwarded public host", async () => {
    const response = await listLogs(
      browserRequest("http://web:3000/bff/jobs/job-1/logs", {
        origin: "https://manager.test",
        forwardedHost: "other.test",
        forwardedProtocol: "https",
      }),
      context,
    );

    expect(response.status).toBe(403);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it("rejects unsafe path IDs and duplicate query keys", async () => {
    const unsafePath = await listLogs(
      browserRequest("https://manager.test/bff/jobs/unsafe/logs"),
      { params: Promise.resolve({ jobId: "../unsafe" }) },
    );
    const duplicateQuery = await listLogs(
      browserRequest("https://manager.test/bff/jobs/job-1/logs?limit=1&limit=2"),
      context,
    );

    expect(unsafePath.status).toBe(400);
    expect(duplicateQuery.status).toBe(400);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it("clears the session cookie when Manager reports an expired JWT", async () => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(Response.json({ detail: "expired" }, { status: 401 })),
    );

    const response = await listMetrics(
      browserRequest("https://manager.test/bff/jobs/job-1/metrics"),
      context,
    );

    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({ error: "session_expired" });
    expect(response.headers.get("set-cookie")).toContain("Max-Age=0");
  });

  it.each([
    [403, "forbidden"],
    [404, "not_found"],
  ])("preserves an upstream %s access state without forwarding its body", async (status, code) => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(Response.json({ detail: "private upstream detail" }, { status })),
    );

    const response = await listArtifacts(
      browserRequest("https://manager.test/bff/jobs/job-1/artifacts"),
      context,
    );

    expect(response.status).toBe(status);
    expect(await response.json()).toEqual({ error: code });
  });

  it("proxies SSE without buffering and cancels the upstream on downstream close", async () => {
    let upstreamCancelled = false;
    const upstreamBody = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(": heartbeat\n\n"));
      },
      cancel() {
        upstreamCancelled = true;
      },
    });
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(
        new Response(upstreamBody, {
          headers: { "content-type": "text/event-stream; charset=utf-8" },
        }),
      ),
    );
    const request = browserRequest(
      "https://manager.test/bff/jobs/job-1/logs/stream?after=cursor_1",
      { lastEventId: "cursor_1" },
    );

    const response = await streamLogs(request, context);
    const call = manager.managerRawRequest.mock.calls[0]?.[1];

    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toContain("text/event-stream");
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(response.headers.get("x-accel-buffering")).toBe("no");
    expect(call).toMatchObject({
      accept: "text/event-stream",
      lastEventId: "cursor_1",
      timeoutMs: null,
      token: TOKEN,
    });
    expect(manager.managerRawRequest.mock.calls[0]?.[0]).toBe(
      "/api/v1/jobs/job-1/logs/stream",
    );
    expect(call.signal.aborted).toBe(false);

    await response.body?.cancel("browser disconnected");

    expect(upstreamCancelled).toBe(true);
    expect(call.signal.aborted).toBe(true);
  });

  it("rejects oversized Last-Event-ID without opening an SSE upstream", async () => {
    const response = await streamLogs(
      browserRequest("https://manager.test/bff/jobs/job-1/logs/stream", {
        lastEventId: "x".repeat(513),
      }),
      context,
    );

    expect(response.status).toBe(400);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it("passes through a fresh HTTPS presigned download redirect without exposing JWT", async () => {
    const location = "https://objects.example.test/model.pth?X-Amz-Signature=short-lived";
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(new Response(null, { status: 307, headers: { location } })),
    );
    const request = browserRequest(
      "https://manager.test/bff/artifacts/artifact-1/download",
    );

    const response = await downloadArtifact(request, {
      params: Promise.resolve({ artifactId: "artifact-1" }),
    });

    expect(response.status).toBe(307);
    expect(response.headers.get("location")).toBe(location);
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(response.headers.get("location")).not.toContain(TOKEN);
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      "/api/v1/artifacts/artifact-1/download",
      expect.objectContaining({ redirect: "manual", token: TOKEN }),
    );
  });

  it("blocks an HTTPS-to-HTTP download downgrade", async () => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(
        new Response(null, {
          status: 307,
          headers: { location: "http://objects.example.test/model.pth?signature=secret" },
        }),
      ),
    );

    const response = await downloadArtifact(
      browserRequest("https://manager.test/bff/artifacts/artifact-1/download"),
      { params: Promise.resolve({ artifactId: "artifact-1" }) },
    );

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "invalid_download_redirect" });
  });

  it.each([
    "https://user:password@objects.example.test/model.pth?signature=secret",
    "https://objects.example.test/model.pth?signature=secret#fragment",
  ])("blocks userinfo or fragments in a download redirect", async (location) => {
    manager.managerRawRequest.mockResolvedValue(
      rawResponse(new Response(null, { status: 307, headers: { location } })),
    );

    const response = await downloadArtifact(
      browserRequest("https://manager.test/bff/artifacts/artifact-1/download"),
      { params: Promise.resolve({ artifactId: "artifact-1" }) },
    );

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "invalid_download_redirect" });
  });
});

function browserRequest(
  url: string,
  options: {
    origin?: string;
    site?: string;
    lastEventId?: string;
    forwardedHost?: string;
    forwardedProtocol?: string;
  } = {},
): NextRequest {
  return new NextRequest(url, {
    headers: {
      cookie: `rvc_manager_session=${TOKEN}`,
      origin: options.origin ?? new URL(url).origin,
      "sec-fetch-site": options.site ?? "same-origin",
      ...(options.lastEventId ? { "last-event-id": options.lastEventId } : {}),
      ...(options.forwardedHost ? { "x-forwarded-host": options.forwardedHost } : {}),
      ...(options.forwardedProtocol ? { "x-forwarded-proto": options.forwardedProtocol } : {}),
    },
  });
}

function rawResponse(response: Response) {
  return { response, requestId: "request-1" };
}

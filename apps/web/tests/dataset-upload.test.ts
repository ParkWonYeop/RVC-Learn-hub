import { createHash } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiDatasetUploadInitResponse } from "@/lib/api-types";
import { browserUploadHeaders, uploadDatasetObject } from "@/lib/client/dataset-upload";
import { IncrementalSha256, sha256Blob } from "@/lib/client/sha256";

describe("Dataset browser upload helpers", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("hashes standard SHA-256 vectors incrementally", () => {
    const encoder = new TextEncoder();
    const digest = new IncrementalSha256();
    digest.update(encoder.encode("a"));
    digest.update(encoder.encode("b"));
    digest.update(encoder.encode("c"));

    expect(digest.hexDigest()).toBe(
      "ba7816bf8f01cfea414140de5dae2223" +
      "b00361a396177a9cb410ff61f20015ad",
    );
  });

  it("hashes a Blob in bounded chunks and reports progress", async () => {
    const progress: number[] = [];
    const result = await sha256Blob(new Blob(["abc"]), {
      chunkBytes: 64,
      onProgress: (processed) => progress.push(processed),
    });

    expect(result).toBe(
      "ba7816bf8f01cfea414140de5dae2223" +
      "b00361a396177a9cb410ff61f20015ad",
    );
    expect(progress).toEqual([0, 3]);
  });

  it.each([55, 56, 63, 64, 65, 1024 * 1024 + 3])(
    "matches Node SHA-256 across a %i-byte block boundary fixture",
    (size) => {
      const bytes = Uint8Array.from({ length: size }, (_, index) => index % 251);
      const digest = new IncrementalSha256();
      for (let offset = 0; offset < bytes.length; offset += 137) {
        digest.update(bytes.subarray(offset, Math.min(bytes.length, offset + 137)));
      }
      expect(digest.hexDigest()).toBe(createHash("sha256").update(bytes).digest("hex"));
    },
  );

  it("never lets script set Content-Length and rejects credential headers", () => {
    const headers = browserUploadHeaders({
      "Content-Length": "3",
      "Content-Type": "application/zip",
      "x-amz-meta-sha256": "a".repeat(64),
    });
    expect(headers.has("content-length")).toBe(false);
    expect(headers.get("content-type")).toBe("application/zip");
    expect(() => browserUploadHeaders({ Authorization: "Bearer secret" })).toThrow(
      "disallowed Dataset upload header",
    );
  });

  it("omits the HttpOnly session cookie for a same-origin local target", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("window", { location: { origin: "https://manager.test" } });
    vi.stubGlobal("fetch", fetchMock);
    const target: ApiDatasetUploadInitResponse = {
      upload_session_id: "upload-1",
      dataset_id: "dataset-1",
      status: "pending",
      method: "PUT",
      upload_url: "https://manager.test/api/v1/storage/dataset-uploads/upload-1",
      upload_headers: {
        "Content-Length": "3",
        "Content-Type": "application/zip",
        "X-RVC-Upload-Token": "rvcd_abcdefghijklmnopqrstuvwxyz",
      },
      expires_at: "2026-07-11T12:30:00Z",
      dataset: null,
      failure_code: null,
      retryable: true,
      retry_after_seconds: null,
    };

    await uploadDatasetObject(target, new Blob(["abc"]) as File, {
      onProgress: vi.fn(),
      signal: new AbortController().signal,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      new URL(target.upload_url!),
      expect.objectContaining({
        credentials: "omit",
        method: "PUT",
        redirect: "error",
        referrerPolicy: "no-referrer",
      }),
    );
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(init.headers).has("authorization")).toBe(false);
    expect(new Headers(init.headers).has("content-length")).toBe(false);
  });
});

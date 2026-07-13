import { NextRequest } from "next/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const manager = vi.hoisted(() => ({ managerRawRequest: vi.fn() }));
vi.mock("@/lib/server/manager-api", () => manager);

import { GET as getRegistry } from "@/app/bff/experiments/[experimentId]/model-registry/route";
import { POST as createCandidate } from "@/app/bff/experiments/[experimentId]/model-registry/candidates/route";
import { POST as promoteEntry } from "@/app/bff/experiments/[experimentId]/model-registry/entries/[entryId]/promote/route";
import { POST as revokeEntry } from "@/app/bff/experiments/[experimentId]/model-registry/entries/[entryId]/revoke/route";

const TOKEN = "private.jwt.only.in.httponly.cookie";
const EXPERIMENT_ID = "11111111-1111-4111-8111-111111111111";
const ENTRY_ID = "22222222-2222-4222-8222-222222222222";
const JOB_ID = "33333333-3333-4333-8333-333333333333";
const ATTEMPT_ID = "44444444-4444-4444-8444-444444444444";
const MODEL_ID = "55555555-5555-4555-8555-555555555555";
const INDEX_ID = "66666666-6666-4666-8666-666666666666";
const ACTOR_ID = "77777777-7777-4777-8777-777777777777";

describe("Model Registry BFF", () => {
  beforeEach(() => {
    manager.managerRawRequest.mockReset();
    vi.stubEnv("DASHBOARD_DEMO_MODE", "false");
  });

  afterEach(() => vi.unstubAllEnvs());

  it("reads a fixed paginated Manager path with only the cookie token and strips private fields", async () => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json({
      ...listFixture(),
      storage_uri: "s3://private/registry",
      items: [entryFixture({
        metadata_json: { private: true },
        model: {
          ...artifactFixture("model"),
          canonical_object_key: "private/model.pth",
        },
      })],
    })));

    const response = await getRegistry(
      readRequest(`${registryUrl()}?limit=200&offset=0`),
      experimentContext(),
    );
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(response.headers.get("vary")).toBe("Cookie");
    expect(Object.keys(body)).toEqual([
      "experiment_id",
      "registry_row_version",
      "active_entry_id",
      "can_manage",
      "items",
      "total",
      "offset",
      "limit",
    ]);
    expect(Object.keys(body.items[0])).toEqual([
      "id",
      "row_version",
      "status",
      "is_active",
      "experiment_id",
      "source_job_id",
      "source_job_name",
      "source_attempt_id",
      "source_attempt_number",
      "engine_mode",
      "model",
      "index",
      "job_config_sha256",
      "rvc_commit_hash",
      "runtime_image_digest",
      "runtime_asset_manifest_sha256",
      "created_at",
      "approved_at",
      "revoked_at",
      "revoke_reason",
    ]);
    expect(Object.keys(body.items[0].model)).toEqual(["id", "filename", "size_bytes", "sha256"]);
    expect(JSON.stringify(body)).not.toContain("private");
    expect(JSON.stringify(body)).not.toContain(TOKEN);
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      `/api/v1/experiments/${EXPERIMENT_ID}/model-registry?offset=0&limit=200`,
      expect.objectContaining({ method: "GET", token: TOKEN }),
    );
    expect(manager.managerRawRequest.mock.calls[0]?.[1]).not.toHaveProperty("headers");
  });

  it("registers a provenance-bound candidate with an actor-scoped idempotency key", async () => {
    const payload = candidatePayload();
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json(
      mutationFixture(entryFixture()),
      { status: 201, headers: { "Idempotency-Replayed": "true" } },
    )));

    const response = await createCandidate(
      mutationRequest(`${registryUrl()}/candidates`, payload, "candidate-register-1"),
      experimentContext(),
    );
    const body = await response.json();

    expect(response.status).toBe(201);
    expect(response.headers.get("idempotency-replayed")).toBe("true");
    expect(response.headers.get("cache-control")).toContain("no-store");
    expect(body.entry.status).toBe("candidate");
    expect(body.entry.model.id).toBe(MODEL_ID);
    expect(manager.managerRawRequest).toHaveBeenCalledWith(
      `/api/v1/experiments/${EXPERIMENT_ID}/model-registry/candidates`,
      expect.objectContaining({
        body: payload,
        expectedActorId: ACTOR_ID,
        idempotencyKey: "candidate-register-1",
        method: "POST",
        token: TOKEN,
      }),
    );
  });

  it("promotes and revokes through fixed entry paths with exact CAS bodies", async () => {
    const promotePayload = {
      expected_registry_row_version: 4,
      expected_entry_row_version: 1,
    };
    const approved = entryFixture({
      row_version: 2,
      status: "approved",
      is_active: true,
      approved_at: "2026-07-12T01:10:00Z",
    });
    const revokePayload = {
      expected_registry_row_version: 5,
      expected_entry_row_version: 2,
      reason_code: "operator_request",
    };
    const revoked = entryFixture({
      row_version: 3,
      status: "revoked",
      is_active: false,
      approved_at: "2026-07-12T01:10:00Z",
      revoked_at: "2026-07-12T01:20:00Z",
      revoke_reason: "operator_request",
    });
    manager.managerRawRequest
      .mockResolvedValueOnce(rawResponse(Response.json(
        mutationFixture(approved, { registry_row_version: 5, active_entry_id: ENTRY_ID }),
      )))
      .mockResolvedValueOnce(rawResponse(Response.json(
        mutationFixture(revoked, { registry_row_version: 6, active_entry_id: null }),
      )));

    const promoted = await promoteEntry(
      mutationRequest(`${registryUrl()}/entries/${ENTRY_ID}/promote`, promotePayload, "promote-1"),
      entryContext(),
    );
    const revokedResponse = await revokeEntry(
      mutationRequest(`${registryUrl()}/entries/${ENTRY_ID}/revoke`, revokePayload, "revoke-1"),
      entryContext(),
    );

    expect(promoted.status).toBe(200);
    expect((await promoted.json()).entry.is_active).toBe(true);
    expect(revokedResponse.status).toBe(200);
    expect((await revokedResponse.json()).entry.revoke_reason).toBe("operator_request");
    expect(manager.managerRawRequest).toHaveBeenNthCalledWith(
      1,
      `/api/v1/experiments/${EXPERIMENT_ID}/model-registry/entries/${ENTRY_ID}/promote`,
      expect.objectContaining({
        body: promotePayload,
        expectedActorId: ACTOR_ID,
        idempotencyKey: "promote-1",
        method: "POST",
      }),
    );
    expect(manager.managerRawRequest).toHaveBeenNthCalledWith(
      2,
      `/api/v1/experiments/${EXPERIMENT_ID}/model-registry/entries/${ENTRY_ID}/revoke`,
      expect.objectContaining({
        body: revokePayload,
        expectedActorId: ACTOR_ID,
        idempotencyKey: "revoke-1",
        method: "POST",
      }),
    );
  });

  it("supports the empty registry version zero and its first candidate registration", async () => {
    const empty = {
      ...listFixture(),
      registry_row_version: 0,
      items: [],
      total: 0,
    };
    const firstPayload = { ...candidatePayload(), expected_registry_row_version: 0 };
    manager.managerRawRequest
      .mockResolvedValueOnce(rawResponse(Response.json(empty)))
      .mockResolvedValueOnce(rawResponse(Response.json(
        mutationFixture(entryFixture(), { registry_row_version: 1 }),
        { status: 201 },
      )));

    const emptyResponse = await getRegistry(
      readRequest(`${registryUrl()}?offset=0&limit=200`),
      experimentContext(),
    );
    const createdResponse = await createCandidate(
      mutationRequest(`${registryUrl()}/candidates`, firstPayload, "candidate-first"),
      experimentContext(),
    );

    expect(emptyResponse.status).toBe(200);
    expect(await emptyResponse.json()).toMatchObject({ registry_row_version: 0, items: [], total: 0 });
    expect(createdResponse.status).toBe(201);
    expect(await createdResponse.json()).toMatchObject({ registry_row_version: 1 });
    expect(manager.managerRawRequest).toHaveBeenNthCalledWith(
      2,
      `/api/v1/experiments/${EXPERIMENT_ID}/model-registry/candidates`,
      expect.objectContaining({ body: firstPayload }),
    );
  });

  it("accepts a direct candidate revoke while keeping approved_at null", async () => {
    const payload = {
      expected_registry_row_version: 1,
      expected_entry_row_version: 1,
      reason_code: "quality_rejected",
    };
    const directlyRevoked = entryFixture({
      row_version: 2,
      status: "revoked",
      approved_at: null,
      revoked_at: "2026-07-12T01:05:00Z",
      revoke_reason: "quality_rejected",
    });
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json(
      mutationFixture(directlyRevoked, { registry_row_version: 2 }),
    )));

    const response = await revokeEntry(
      mutationRequest(`${registryUrl()}/entries/${ENTRY_ID}/revoke`, payload, "revoke-candidate"),
      entryContext(),
    );

    expect(response.status).toBe(200);
    expect((await response.json()).entry).toMatchObject({
      status: "revoked",
      approved_at: null,
      revoke_reason: "quality_rejected",
    });
  });

  it("does not accept registry version zero for promote or revoke transitions", async () => {
    const base = {
      expected_registry_row_version: 0,
      expected_entry_row_version: 1,
    };
    const responses = await Promise.all([
      promoteEntry(
        mutationRequest(`${registryUrl()}/entries/${ENTRY_ID}/promote`, base, "promote-zero"),
        entryContext(),
      ),
      revokeEntry(
        mutationRequest(`${registryUrl()}/entries/${ENTRY_ID}/revoke`, {
          ...base,
          reason_code: "operator_request",
        }, "revoke-zero"),
        entryContext(),
      ),
    ]);

    expect(responses.map((response) => response.status)).toEqual([400, 400]);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it("rejects Authorization injection, cross-origin reads, unsafe IDs and non-exact queries", async () => {
    const responses = await Promise.all([
      getRegistry(
        readRequest(`${registryUrl()}?offset=0&limit=200`, { browserAuthorization: "Bearer injected" }),
        experimentContext(),
      ),
      getRegistry(
        readRequest(`${registryUrl()}?offset=0&limit=200`, { forwardedHost: "other.test" }),
        experimentContext(),
      ),
      getRegistry(
        readRequest(`${registryUrl()}?offset=0&offset=1&limit=200`),
        experimentContext(),
      ),
      getRegistry(
        readRequest(`${registryUrl()}?offset=0&limit=200&path=%2Fapi%2Fv1%2Fworkers`),
        experimentContext(),
      ),
      getRegistry(
        readRequest("https://manager.test/bff/experiments/NOT-A-UUID/model-registry"),
        { params: Promise.resolve({ experimentId: "NOT-A-UUID" }) },
      ),
    ]);

    expect(responses.map((response) => response.status)).toEqual([400, 403, 400, 400, 400]);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it("requires same-origin exact bounded mutation bodies, valid idempotency keys and cookie sessions", async () => {
    const good = candidatePayload();
    const oversizedBytes = rawMutationRequest(new Uint8Array(4_097).fill(0x20), "actual-size-1");
    const invalidUtf8 = rawMutationRequest(new Uint8Array([0xff]), "invalid-utf8-1");
    const responses = await Promise.all([
      createCandidate(
        mutationRequest(`${registryUrl()}/candidates`, good, "auth-1", {
          browserAuthorization: "Bearer injected",
        }),
        experimentContext(),
      ),
      createCandidate(
        mutationRequest(`${registryUrl()}/candidates`, good, "origin-1", { forwardedHost: "other.test" }),
        experimentContext(),
      ),
      createCandidate(
        mutationRequest(`${registryUrl()}/candidates`, { ...good, storage_uri: "s3://private" }, "extra-1"),
        experimentContext(),
      ),
      createCandidate(
        mutationRequest(`${registryUrl()}/candidates`, good, "bad key"),
        experimentContext(),
      ),
      createCandidate(
        mutationRequest(`${registryUrl()}/candidates`, good, "size-1", { contentLength: "4097" }),
        experimentContext(),
      ),
      createCandidate(oversizedBytes, experimentContext()),
      createCandidate(invalidUtf8, experimentContext()),
      createCandidate(
        mutationRequest(`${registryUrl()}/candidates?path=/api/v1/users`, good, "query-1"),
        experimentContext(),
      ),
      createCandidate(
        mutationRequest(`${registryUrl()}/candidates`, good, "cookie-1", { cookie: false }),
        experimentContext(),
      ),
      revokeEntry(
        mutationRequest(`${registryUrl()}/entries/${ENTRY_ID}/revoke`, {
          expected_registry_row_version: 5,
          expected_entry_row_version: 2,
          reason_code: "free-form-secret-reason",
        }, "reason-1"),
        entryContext(),
      ),
    ]);

    expect(responses.map((response) => response.status)).toEqual([
      400,
      403,
      400,
      400,
      413,
      413,
      400,
      400,
      401,
      400,
    ]);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it("rejects a missing or malformed expected actor before contacting Manager", async () => {
    const responses = await Promise.all([
      createCandidate(
        mutationRequest(`${registryUrl()}/candidates`, candidatePayload(), "actor-missing", {
          expectedActorId: null,
        }),
        experimentContext(),
      ),
      createCandidate(
        mutationRequest(`${registryUrl()}/candidates`, candidatePayload(), "actor-invalid", {
          expectedActorId: "NOT-A-UUID",
        }),
        experimentContext(),
      ),
    ]);

    expect(responses.map((response) => response.status)).toEqual([400, 400]);
    await expect(Promise.all(responses.map((response) => response.json()))).resolves.toEqual([
      { error: "invalid_expected_actor" },
      { error: "invalid_expected_actor" },
    ]);
    expect(manager.managerRawRequest).not.toHaveBeenCalled();
  });

  it("fails closed on malformed 2xx state, active, provenance and pagination ledgers", async () => {
    const invalidEngine = listFixture();
    invalidEngine.items[0]!.engine_mode = "fake";
    const activeCandidate = listFixture();
    activeCandidate.active_entry_id = ENTRY_ID;
    const invalidDigest = listFixture();
    invalidDigest.items[0]!.runtime_image_digest = "sha256:private";
    const invalidPage = listFixture();
    invalidPage.offset = 1;
    const wrongMutationVersion = mutationFixture(entryFixture(), { registry_row_version: 7 });
    const inactiveActiveMutation = mutationFixture(entryFixture(), { active_entry_id: ENTRY_ID });
    manager.managerRawRequest
      .mockResolvedValueOnce(rawResponse(Response.json(invalidEngine)))
      .mockResolvedValueOnce(rawResponse(Response.json(activeCandidate)))
      .mockResolvedValueOnce(rawResponse(Response.json(invalidDigest)))
      .mockResolvedValueOnce(rawResponse(Response.json(invalidPage)))
      .mockResolvedValueOnce(rawResponse(Response.json(wrongMutationVersion, { status: 201 })))
      .mockResolvedValueOnce(rawResponse(Response.json(inactiveActiveMutation, { status: 201 })));

    const readResponses = [];
    for (let index = 0; index < 4; index += 1) {
      readResponses.push(await getRegistry(
        readRequest(`${registryUrl()}?offset=0&limit=200`),
        experimentContext(),
      ));
    }
    const mutationResponses = [];
    for (let index = 0; index < 2; index += 1) {
      mutationResponses.push(await createCandidate(
        mutationRequest(`${registryUrl()}/candidates`, candidatePayload(), `malformed-${index}`),
        experimentContext(),
      ));
    }

    expect(readResponses.map((response) => response.status)).toEqual([502, 502, 502, 502]);
    expect(mutationResponses.map((response) => response.status)).toEqual([502, 502]);
    for (const response of [...readResponses, ...mutationResponses]) {
      expect(await response.json()).toEqual({ error: "invalid_upstream_response" });
    }
  });

  it.each([
    [400, "invalid_request", null],
    [401, "session_expired", null],
    [403, "forbidden", null],
    [404, "not_found", null],
    [409, "conflict", null],
    [422, "invalid_request", null],
    [429, "rate_limited", "17"],
    [503, "manager_unavailable", "5"],
    [500, "invalid_upstream_response", null],
  ] as const)("maps upstream %s without exposing details", async (status, code, retryAfter) => {
    manager.managerRawRequest.mockResolvedValue(rawResponse(Response.json(
      { detail: "private s3://bucket/key", token: TOKEN },
      { status, headers: retryAfter ? { "Retry-After": retryAfter } : undefined },
    )));

    const response = await getRegistry(
      readRequest(`${registryUrl()}?offset=0&limit=200`),
      experimentContext(),
    );

    expect(response.status).toBe(status === 500 ? 502 : status);
    expect(await response.json()).toEqual({ error: code });
    expect(response.headers.get("retry-after")).toBe(retryAfter);
  });

  it("returns a generic unavailable result on transport failure and does not expose a browser retry target", async () => {
    manager.managerRawRequest.mockRejectedValue(new Error("connect ECONNREFUSED private-host"));

    const response = await createCandidate(
      mutationRequest(`${registryUrl()}/candidates`, candidatePayload(), "transport-1"),
      experimentContext(),
    );

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ error: "manager_unavailable" });
    expect(response.headers.get("location")).toBeNull();
  });
});

function registryUrl(): string {
  return `https://manager.test/bff/experiments/${EXPERIMENT_ID}/model-registry`;
}

function experimentContext() {
  return { params: Promise.resolve({ experimentId: EXPERIMENT_ID }) };
}

function entryContext() {
  return { params: Promise.resolve({ experimentId: EXPERIMENT_ID, entryId: ENTRY_ID }) };
}

function candidatePayload() {
  return {
    expected_registry_row_version: 3,
    source_job_id: JOB_ID,
    source_attempt_id: ATTEMPT_ID,
    model_artifact_id: MODEL_ID,
  };
}

function listFixture() {
  return {
    experiment_id: EXPERIMENT_ID,
    registry_row_version: 3,
    active_entry_id: null as string | null,
    can_manage: true,
    items: [entryFixture()],
    total: 1,
    offset: 0,
    limit: 200,
  };
}

function mutationFixture(
  entry: ReturnType<typeof entryFixture>,
  overrides: Partial<{
    experiment_id: string;
    registry_row_version: number;
    active_entry_id: string | null;
  }> = {},
) {
  return {
    experiment_id: EXPERIMENT_ID,
    registry_row_version: 4,
    active_entry_id: null as string | null,
    entry,
    ...overrides,
  };
}

function entryFixture(overrides: Record<string, unknown> = {}) {
  return {
    id: ENTRY_ID,
    row_version: 1,
    status: "candidate",
    is_active: false,
    experiment_id: EXPERIMENT_ID,
    source_job_id: JOB_ID,
    source_job_name: "speaker-v2-rmvpe",
    source_attempt_id: ATTEMPT_ID,
    source_attempt_number: 1,
    engine_mode: "rvc_webui",
    model: artifactFixture("model"),
    index: artifactFixture("index"),
    job_config_sha256: "c".repeat(64),
    rvc_commit_hash: "d".repeat(40),
    runtime_image_digest: `sha256:${"e".repeat(64)}`,
    runtime_asset_manifest_sha256: "f".repeat(64),
    created_at: "2026-07-12T01:00:00Z",
    approved_at: null,
    revoked_at: null,
    revoke_reason: null,
    ...overrides,
  };
}

function artifactFixture(kind: "model" | "index") {
  return {
    id: kind === "model" ? MODEL_ID : INDEX_ID,
    filename: kind === "model" ? "final-model.pth" : "final.index",
    size_bytes: kind === "model" ? 1_024 : 2_048,
    sha256: (kind === "model" ? "a" : "b").repeat(64),
  };
}

function readRequest(
  url: string,
  options: {
    browserAuthorization?: string;
    cookie?: boolean;
    forwardedHost?: string;
  } = {},
): NextRequest {
  const headers = commonHeaders(options);
  return new NextRequest(url, { headers });
}

function mutationRequest(
  url: string,
  body: unknown,
  key: string,
  options: {
    browserAuthorization?: string;
    contentLength?: string;
    cookie?: boolean;
    expectedActorId?: string | null;
    forwardedHost?: string;
  } = {},
): NextRequest {
  const headers = commonHeaders(options);
  headers.set("Content-Type", "application/json; charset=utf-8");
  headers.set("Idempotency-Key", key);
  const expectedActorId = options.expectedActorId === undefined ? ACTOR_ID : options.expectedActorId;
  if (expectedActorId !== null) headers.set("X-RVC-Expected-Actor-ID", expectedActorId);
  if (options.contentLength) headers.set("Content-Length", options.contentLength);
  return new NextRequest(url, {
    body: JSON.stringify(body),
    headers,
    method: "POST",
  });
}

function rawMutationRequest(body: BodyInit, key: string): NextRequest {
  const headers = commonHeaders({});
  headers.set("Content-Type", "application/json");
  headers.set("Idempotency-Key", key);
  headers.set("X-RVC-Expected-Actor-ID", ACTOR_ID);
  return new NextRequest(`${registryUrl()}/candidates`, {
    body,
    headers,
    method: "POST",
  });
}

function commonHeaders(options: {
  browserAuthorization?: string;
  cookie?: boolean;
  forwardedHost?: string;
}): Headers {
  const headers = new Headers({
    Host: "manager.test",
    Origin: "https://manager.test",
    "Sec-Fetch-Site": "same-origin",
    "X-Forwarded-Host": options.forwardedHost ?? "manager.test",
    "X-Forwarded-Proto": "https",
  });
  if (options.cookie !== false) headers.set("Cookie", `rvc_manager_session=${TOKEN}`);
  if (options.browserAuthorization) headers.set("Authorization", options.browserAuthorization);
  return headers;
}

function rawResponse(response: Response) {
  return { response, requestId: "request-1" };
}

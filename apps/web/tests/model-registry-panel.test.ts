import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import {
  ModelRegistryPanel,
  type RegistryPanelPhase,
} from "@/app/(dashboard)/experiments/[experimentId]/model-registry-panel";
import type {
  ModelRegistryEntry,
  ModelRegistrySnapshot,
} from "@/lib/api-types";

const EXPERIMENT_ID = "11111111-1111-4111-8111-111111111111";

describe("Model Registry panel", () => {
  it("renders the active champion, candidate, inactive approval and terminal revoked history", () => {
    const active = entry("44444444-4444-4444-8444-444444444444", "approved", true, "active-run");
    const candidate = entry("55555555-5555-4555-8555-555555555555", "candidate", false, "candidate-run");
    const approved = entry("66666666-6666-4666-8666-666666666666", "approved", false, "rollback-run");
    const revoked = entry("77777777-7777-4777-8777-777777777777", "revoked", false, "revoked-run", null);
    const html = renderPanel("ready", {
      experiment_id: EXPERIMENT_ID,
      registry_row_version: 9,
      active_entry_id: active.id,
      can_manage: true,
      items: [active, candidate, approved, revoked],
      total: 4,
    });

    expect(html).toContain("자동 선정 없음");
    expect(html).toContain("승인됨 · 현재 Champion");
    expect(html).toContain("승인 대기 후보");
    expect(html).toContain("이전 승인 · Rollback 후보");
    expect(html).toContain("폐기됨 · 재승격 불가");
    expect(html).toContain("품질 기준 미달");
    expect(html).toContain(`sha256:${"e".repeat(64)}`);
    expect(html).toContain("Runtime asset manifest");
    expect(html).toContain("Job config");
    expect(html).toContain("Champion 승인 확인");
    expect(html).toContain("다시 Champion으로 선택");
    expect(html).not.toContain("revoked-run</h4><small>Attempt #2 · version 1</small></div><button");
  });

  it("shows full model/index hashes and the server-side rehash gate before candidate registration", () => {
    const html = renderToStaticMarkup(createElement(ModelRegistryPanel, {
      ...baseProps("ready", emptySnapshot()),
      candidate: {
        experimentId: EXPERIMENT_ID,
        jobId: "22222222-2222-4222-8222-222222222222",
        jobName: "native-run",
        attemptId: "33333333-3333-4333-8333-333333333333",
        attemptNumber: 2,
        model: {
          id: "88888888-8888-4888-8888-888888888888",
          filename: "final-model.pth",
          size_bytes: 1024,
          sha256: "a".repeat(64),
        },
        index: {
          id: "99999999-9999-4999-8999-999999999999",
          filename: "final.index",
          size_bytes: 2048,
          sha256: "b".repeat(64),
        },
      },
    }));

    expect(html).toContain("후보 등록 검증");
    expect(html).toContain("canonical byte");
    expect(html).toContain("runtime image/asset provenance");
    expect(html).toContain("a".repeat(64));
    expect(html).toContain("b".repeat(64));
    expect(html).toContain('tabindex="-1"');
  });

  it("exposes accessible loading, read-error and conservative uncertain states", () => {
    const loading = renderPanel("loading", null);
    const error = renderToStaticMarkup(createElement(ModelRegistryPanel, {
      ...baseProps("error", null),
      errorStatus: 503,
      errorCode: "verification_unavailable",
    }));
    const uncertain = renderPanel("uncertain", emptySnapshot());

    expect(loading).toContain('aria-busy="true"');
    expect(loading).toContain('role="status"');
    expect(error).toContain('role="alert"');
    expect(error).toContain("canonical byte를 현재 재검증할 수 없습니다");
    expect(uncertain).toContain("새 요청으로 반복하지 마세요");
    expect(uncertain).toContain("target 상태가 반영됐는지 먼저 확인");
    expect(uncertain).toContain("원장이 unchanged인 경우에만");
  });
});

function renderPanel(phase: RegistryPanelPhase, snapshot: ModelRegistrySnapshot | null): string {
  return renderToStaticMarkup(createElement(ModelRegistryPanel, baseProps(phase, snapshot)));
}

function baseProps(phase: RegistryPanelPhase, snapshot: ModelRegistrySnapshot | null) {
  return {
    candidate: null,
    errorCode: null,
    errorStatus: null,
    feedback: null,
    locked: phase !== "ready",
    onCancelCandidate: vi.fn(),
    onPromote: vi.fn(),
    onRegister: vi.fn(),
    onReload: vi.fn(),
    onRevoke: vi.fn(),
    pendingAction: null,
    phase,
    snapshot,
  };
}

function emptySnapshot(): ModelRegistrySnapshot {
  return {
    experiment_id: EXPERIMENT_ID,
    registry_row_version: 0,
    active_entry_id: null,
    can_manage: true,
    items: [],
    total: 0,
  };
}

function entry(
  id: string,
  status: "candidate" | "approved" | "revoked",
  active: boolean,
  name: string,
  approvedAt: string | null = status === "candidate" ? null : "2026-07-12T01:00:00Z",
): ModelRegistryEntry {
  const suffix = id.slice(-1);
  return {
    id,
    row_version: 1,
    status,
    is_active: active,
    experiment_id: EXPERIMENT_ID,
    source_job_id: `22222222-2222-4222-8222-22222222222${suffix}`,
    source_job_name: name,
    source_attempt_id: `33333333-3333-4333-8333-33333333333${suffix}`,
    source_attempt_number: 2,
    engine_mode: "rvc_webui",
    model: {
      id: `88888888-8888-4888-8888-88888888888${suffix}`,
      filename: `${name}.pth`,
      size_bytes: 1024,
      sha256: suffix.repeat(64),
    },
    index: {
      id: `99999999-9999-4999-8999-99999999999${suffix}`,
      filename: `${name}.index`,
      size_bytes: 2048,
      sha256: "b".repeat(64),
    },
    job_config_sha256: "c".repeat(64),
    rvc_commit_hash: "d".repeat(40),
    runtime_image_digest: `sha256:${"e".repeat(64)}`,
    runtime_asset_manifest_sha256: "f".repeat(64),
    created_at: "2026-07-12T00:00:00Z",
    approved_at: approvedAt,
    revoked_at: status === "revoked" ? "2026-07-12T02:00:00Z" : null,
    revoke_reason: status === "revoked" ? "quality_rejected" : null,
  };
}

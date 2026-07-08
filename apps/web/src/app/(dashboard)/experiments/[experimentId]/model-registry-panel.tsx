"use client";

import Link from "next/link";
import { useState } from "react";
import { EngineModeBadge } from "@/components/engine-mode-badge";
import type {
  ModelRegistryEntry,
  ModelRegistryRevokeReason,
  ModelRegistrySnapshot,
} from "@/lib/api-types";
import {
  formatRegistryBytes,
  registryErrorMessage,
  registryRevokeReasonLabel,
  type RegistryCandidateSource,
} from "@/lib/client/model-registry";

export type RegistryPanelPhase = "loading" | "ready" | "error" | "stale" | "uncertain";

export interface RegistryPanelFeedback {
  tone: "status" | "error";
  message: string;
}

export function ModelRegistryPanel({
  candidate,
  errorCode,
  errorStatus,
  feedback,
  locked,
  onCancelCandidate,
  onPromote,
  onRegister,
  onReload,
  onRevoke,
  pendingAction,
  phase,
  snapshot,
}: {
  candidate: RegistryCandidateSource | null;
  errorCode: string | null;
  errorStatus: number | null;
  feedback: RegistryPanelFeedback | null;
  locked: boolean;
  onCancelCandidate: () => void;
  onPromote: (entry: ModelRegistryEntry) => void;
  onRegister: (source: RegistryCandidateSource) => void;
  onReload: () => void;
  onRevoke: (entry: ModelRegistryEntry, reason: ModelRegistryRevokeReason) => void;
  pendingAction: string | null;
  phase: RegistryPanelPhase;
  snapshot: ModelRegistrySnapshot | null;
}) {
  const [promoteEntryId, setPromoteEntryId] = useState<string | null>(null);
  const [revokeEntryId, setRevokeEntryId] = useState<string | null>(null);
  const [revokeReason, setRevokeReason] = useState<ModelRegistryRevokeReason>("operator_request");
  const active = snapshot?.items.find((entry) => entry.id === snapshot.active_entry_id) ?? null;
  const candidates = snapshot?.items.filter((entry) => entry.status === "candidate") ?? [];
  const approved = snapshot?.items.filter(
    (entry) => entry.status === "approved" && entry.id !== snapshot.active_entry_id,
  ) ?? [];
  const revoked = snapshot?.items.filter((entry) => entry.status === "revoked") ?? [];

  return (
    <section aria-labelledby="model-registry-heading" className="model-registry-section">
      <div className="section-heading">
        <div>
          <p className="panel-kicker">MODEL GOVERNANCE · EXPLICIT APPROVAL</p>
          <h2 id="model-registry-heading" tabIndex={-1}>Model Registry</h2>
        </div>
        <span>검증된 native 모델의 후보·승인·폐기 원장</span>
      </div>
      <div className="panel model-registry-panel" aria-busy={phase === "loading" || pendingAction !== null}>
        <div className="model-registry-boundary" role="note">
          <strong>자동 선정 없음</strong>
          <span>
            Loss나 Sample 지표가 낮다는 이유만으로 자동 승인하지 않습니다. Manager가 canonical
            model/index byte와 runtime provenance를 다시 검증한 뒤 사용자가 명시적으로 Champion을
            선택합니다.
          </span>
        </div>

        {feedback ? (
          <div
            aria-live="polite"
            className={`model-registry-feedback ${feedback.tone === "error" ? "model-registry-feedback-error" : ""}`}
            role={feedback.tone === "error" ? "alert" : "status"}
          >
            {feedback.message}
          </div>
        ) : null}

        {candidate ? (
          <CandidateConfirmation
            candidate={candidate}
            disabled={locked}
            onCancel={onCancelCandidate}
            onConfirm={() => onRegister(candidate)}
            pending={pendingAction === "register"}
          />
        ) : null}

        {phase === "loading" ? (
          <RegistryNotice>Model Registry의 모든 page와 row version을 확인하는 중입니다.</RegistryNotice>
        ) : phase === "error" ? (
          <RegistryError
            code={errorCode}
            onReload={onReload}
            status={errorStatus ?? 502}
          />
        ) : phase === "stale" || phase === "uncertain" ? (
          <div className="model-registry-terminal-error" role="alert">
            <strong>{phase === "uncertain" ? "변경 결과를 확정할 수 없습니다" : "Registry 원장이 변경되었습니다"}</strong>
            <span>
              {phase === "uncertain"
                ? "같은 작업을 새 요청으로 반복하지 마세요. 페이지를 다시 불러온 뒤 target 상태가 반영됐는지 먼저 확인하고, 원장이 unchanged인 경우에만 새 명시적 작업을 시작하세요."
                : "현재 화면의 row version으로는 추가 작업을 수행할 수 없습니다."}
            </span>
            <button className="button button-secondary" onClick={() => window.location.reload()} type="button">
              최신 페이지 다시 불러오기
            </button>
          </div>
        ) : snapshot ? (
          <div className="model-registry-content">
            {!snapshot.can_manage ? (
              <div className="model-registry-readonly" role="note">
                이 Registry를 볼 수 있지만 후보 등록·승인·폐기 권한은 없습니다.
              </div>
            ) : null}
            <RegistryGroup
              empty="현재 승인된 Champion이 없습니다."
              entries={active ? [active] : []}
              heading="현재 Champion"
              kind="active"
              locked={locked}
              onPromote={onPromote}
              onPromoteSelect={setPromoteEntryId}
              onRevoke={onRevoke}
              onRevokeSelect={(entryId) => {
                setRevokeEntryId(entryId);
                setRevokeReason("operator_request");
              }}
              pendingAction={pendingAction}
              promoteEntryId={promoteEntryId}
              revokeEntryId={revokeEntryId}
              revokeReason={revokeReason}
              setRevokeReason={setRevokeReason}
            />
            <RegistryGroup
              empty="등록된 후보가 없습니다. 비교 표의 검증된 native model에서 후보 등록을 시작할 수 있습니다."
              entries={candidates}
              heading="승인 대기 후보"
              kind="candidate"
              locked={locked}
              onPromote={onPromote}
              onPromoteSelect={setPromoteEntryId}
              onRevoke={onRevoke}
              onRevokeSelect={setRevokeEntryId}
              pendingAction={pendingAction}
              promoteEntryId={promoteEntryId}
              revokeEntryId={revokeEntryId}
              revokeReason={revokeReason}
              setRevokeReason={setRevokeReason}
            />
            <RegistryGroup
              empty="현재 Champion 외의 승인 이력이 없습니다."
              entries={approved}
              heading="이전 승인 · Rollback 후보"
              kind="approved"
              locked={locked}
              onPromote={onPromote}
              onPromoteSelect={setPromoteEntryId}
              onRevoke={onRevoke}
              onRevokeSelect={(entryId) => {
                setRevokeEntryId(entryId);
                setRevokeReason("operator_request");
              }}
              pendingAction={pendingAction}
              promoteEntryId={promoteEntryId}
              revokeEntryId={revokeEntryId}
              revokeReason={revokeReason}
              setRevokeReason={setRevokeReason}
            />
            <RegistryGroup
              empty="폐기된 모델이 없습니다."
              entries={revoked}
              heading="폐기 이력"
              kind="revoked"
              locked={true}
              onPromote={onPromote}
              onPromoteSelect={setPromoteEntryId}
              onRevoke={onRevoke}
              onRevokeSelect={setRevokeEntryId}
              pendingAction={pendingAction}
              promoteEntryId={promoteEntryId}
              revokeEntryId={revokeEntryId}
              revokeReason={revokeReason}
              setRevokeReason={setRevokeReason}
            />
          </div>
        ) : null}
      </div>
    </section>
  );
}

function CandidateConfirmation({
  candidate,
  disabled,
  onCancel,
  onConfirm,
  pending,
}: {
  candidate: RegistryCandidateSource;
  disabled: boolean;
  onCancel: () => void;
  onConfirm: () => void;
  pending: boolean;
}) {
  return (
    <section aria-labelledby="registry-candidate-confirm-heading" className="model-registry-confirmation">
      <h3 id="registry-candidate-confirm-heading" tabIndex={-1}>후보 등록 검증</h3>
      <p>
        화면의 checksum은 선택 확인용입니다. Manager는 현재 browser 값을 신뢰하지 않고 exact
        Job/attempt, canonical byte, reviewed RVC commit과 runtime image/asset provenance를 다시
        검사합니다.
      </p>
      <dl>
        <div><dt>Job</dt><dd>{candidate.jobName}</dd></div>
        <div><dt>Attempt</dt><dd>#{candidate.attemptNumber} · {candidate.attemptId}</dd></div>
        <div><dt>Model</dt><dd>{candidate.model.filename} · {formatRegistryBytes(candidate.model.size_bytes)}</dd></div>
        <div className="model-registry-hash"><dt>Model SHA-256</dt><dd><code>{candidate.model.sha256}</code></dd></div>
        <div><dt>Index</dt><dd>{candidate.index?.filename ?? "없음"}</dd></div>
        <div className="model-registry-hash"><dt>Index SHA-256</dt><dd><code>{candidate.index?.sha256 ?? "해당 없음"}</code></dd></div>
      </dl>
      <div className="model-registry-confirm-actions">
        <button className="button button-ghost" disabled={disabled} onClick={onCancel} type="button">취소</button>
        <button className="button button-primary" disabled={disabled} onClick={onConfirm} type="button">
          {pending ? "Manager 재검증 중…" : "이 model을 후보로 등록"}
        </button>
      </div>
    </section>
  );
}

function RegistryGroup({
  empty,
  entries,
  heading,
  kind,
  locked,
  onPromote,
  onPromoteSelect,
  onRevoke,
  onRevokeSelect,
  pendingAction,
  promoteEntryId,
  revokeEntryId,
  revokeReason,
  setRevokeReason,
}: {
  empty: string;
  entries: ModelRegistryEntry[];
  heading: string;
  kind: "active" | "candidate" | "approved" | "revoked";
  locked: boolean;
  onPromote: (entry: ModelRegistryEntry) => void;
  onPromoteSelect: (entryId: string | null) => void;
  onRevoke: (entry: ModelRegistryEntry, reason: ModelRegistryRevokeReason) => void;
  onRevokeSelect: (entryId: string | null) => void;
  pendingAction: string | null;
  promoteEntryId: string | null;
  revokeEntryId: string | null;
  revokeReason: ModelRegistryRevokeReason;
  setRevokeReason: (reason: ModelRegistryRevokeReason) => void;
}) {
  const id = `model-registry-${kind}-heading`;
  return (
    <section aria-labelledby={id} className={`model-registry-group model-registry-group-${kind}`}>
      <div className="model-registry-group-heading">
        <h3 id={id}>{heading}</h3>
        <span>{entries.length.toLocaleString("ko-KR")}개</span>
      </div>
      {entries.length === 0 ? <p className="model-registry-empty">{empty}</p> : (
        <div className="model-registry-entry-list">
          {entries.map((entry) => (
            <RegistryEntryCard
              entry={entry}
              key={entry.id}
              locked={locked}
              onPromote={onPromote}
              onPromoteSelect={onPromoteSelect}
              onRevoke={onRevoke}
              onRevokeSelect={onRevokeSelect}
              pendingAction={pendingAction}
              promoteOpen={promoteEntryId === entry.id}
              revokeOpen={revokeEntryId === entry.id}
              revokeReason={revokeReason}
              setRevokeReason={setRevokeReason}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function RegistryEntryCard({
  entry,
  locked,
  onPromote,
  onPromoteSelect,
  onRevoke,
  onRevokeSelect,
  pendingAction,
  promoteOpen,
  revokeOpen,
  revokeReason,
  setRevokeReason,
}: {
  entry: ModelRegistryEntry;
  locked: boolean;
  onPromote: (entry: ModelRegistryEntry) => void;
  onPromoteSelect: (entryId: string | null) => void;
  onRevoke: (entry: ModelRegistryEntry, reason: ModelRegistryRevokeReason) => void;
  onRevokeSelect: (entryId: string | null) => void;
  pendingAction: string | null;
  promoteOpen: boolean;
  revokeOpen: boolean;
  revokeReason: ModelRegistryRevokeReason;
  setRevokeReason: (reason: ModelRegistryRevokeReason) => void;
}) {
  const statusLabel = entry.is_active
    ? "승인됨 · 현재 Champion"
    : entry.status === "candidate"
      ? "후보"
      : entry.status === "approved"
        ? "승인됨 · 비활성"
        : "폐기됨 · 재승격 불가";
  return (
    <article className={`model-registry-entry model-registry-entry-${entry.status}`}>
      <header>
        <div>
          <span className={`model-registry-status model-registry-status-${entry.status}`}>{statusLabel}</span>
          <h4>{entry.source_job_name}</h4>
          <small>Attempt #{entry.source_attempt_number} · version {entry.row_version}</small>
        </div>
        <EngineModeBadge mode="rvc_webui" />
      </header>
      <dl className="model-registry-provenance">
        <RegistryValue label="Model" value={`${entry.model.filename} · ${formatRegistryBytes(entry.model.size_bytes)}`} />
        <RegistryHash label="Model SHA-256" value={entry.model.sha256} />
        <RegistryValue label="Index" value={entry.index ? `${entry.index.filename} · ${formatRegistryBytes(entry.index.size_bytes)}` : "없음"} />
        <RegistryHash label="Index SHA-256" value={entry.index?.sha256 ?? "해당 없음"} />
        <RegistryHash label="RVC commit" value={entry.rvc_commit_hash} />
        <RegistryHash label="Runtime image" value={entry.runtime_image_digest} />
        <RegistryHash label="Runtime asset manifest" value={entry.runtime_asset_manifest_sha256} />
        <RegistryHash label="Job config" value={entry.job_config_sha256} />
        <RegistryValue label="등록" value={formatRegistryTimestamp(entry.created_at)} />
        <RegistryValue label="승인" value={formatRegistryTimestamp(entry.approved_at)} />
        <RegistryValue label="폐기" value={formatRegistryTimestamp(entry.revoked_at)} />
        <RegistryValue label="폐기 사유" value={registryRevokeReasonLabel(entry.revoke_reason)} />
      </dl>
      <div className="model-registry-downloads">
        <a className="button button-secondary" href={`/bff/artifacts/${encodeURIComponent(entry.model.id)}/download`}>Model 다운로드</a>
        {entry.index ? <a className="button button-secondary" href={`/bff/artifacts/${encodeURIComponent(entry.index.id)}/download`}>Index 다운로드</a> : null}
      </div>
      {entry.status !== "revoked" ? (
        <div className="model-registry-entry-actions">
          {!entry.is_active ? (
            <button
              aria-expanded={promoteOpen}
              className="button button-primary"
              disabled={locked}
              onClick={() => onPromoteSelect(promoteOpen ? null : entry.id)}
              type="button"
            >
              {entry.status === "candidate" ? "Champion 승인 확인" : "다시 Champion으로 선택"}
            </button>
          ) : null}
          {entry.status === "approved" || entry.status === "candidate" ? (
            <button
              aria-expanded={revokeOpen}
              className="button button-danger"
              disabled={locked}
              onClick={() => onRevokeSelect(revokeOpen ? null : entry.id)}
              type="button"
            >
              폐기 확인
            </button>
          ) : null}
        </div>
      ) : null}
      {promoteOpen && !entry.is_active ? (
        <div className="model-registry-transition-confirm" role="group" aria-label={`${entry.source_job_name} Champion 승인 확인`}>
          <strong>Champion pointer를 이 모델로 변경합니다.</strong>
          <span>기존 Champion은 폐기하지 않고 승인된 rollback 후보로 보존됩니다. 승인 직전에 canonical byte와 runtime provenance를 다시 검증합니다.</span>
          <div>
            <button className="button button-ghost" disabled={locked} onClick={() => onPromoteSelect(null)} type="button">취소</button>
            <button className="button button-primary" disabled={locked} onClick={() => onPromote(entry)} type="button">
              {pendingAction === `promote:${entry.id}` ? "승인 검증 중…" : "명시적으로 Champion 승인"}
            </button>
          </div>
        </div>
      ) : null}
      {revokeOpen && entry.status !== "revoked" ? (
        <fieldset className="model-registry-transition-confirm">
          <legend>폐기 사유와 terminal 전이 확인</legend>
          <p>폐기된 Entry는 다시 승인할 수 없습니다. 현재 Champion이면 active pointer도 함께 비워집니다.</p>
          <label>
            <span>폐기 사유</span>
            <select
              disabled={locked}
              onChange={(event) => setRevokeReason(event.target.value as ModelRegistryRevokeReason)}
              value={revokeReason}
            >
              <option value="quality_rejected">품질 기준 미달</option>
              <option value="security_issue">보안 문제</option>
              <option value="operator_request">운영자 요청</option>
            </select>
          </label>
          <div>
            <button className="button button-ghost" disabled={locked} onClick={() => onRevokeSelect(null)} type="button">취소</button>
            <button className="button button-danger" disabled={locked} onClick={() => onRevoke(entry, revokeReason)} type="button">
              {pendingAction === `revoke:${entry.id}` ? "폐기 검증 중…" : "이 Entry를 영구 폐기"}
            </button>
          </div>
        </fieldset>
      ) : null}
    </article>
  );
}

function RegistryHash({ label, value }: { label: string; value: string }) {
  return <div className="model-registry-hash"><dt>{label}</dt><dd><code>{value}</code></dd></div>;
}

function RegistryValue({ label, value }: { label: string; value: string }) {
  return <div><dt>{label}</dt><dd>{value}</dd></div>;
}

function RegistryNotice({ children }: { children: React.ReactNode }) {
  return <div className="model-registry-notice" role="status">{children}</div>;
}

function RegistryError({
  code,
  onReload,
  status,
}: {
  code: string | null;
  onReload: () => void;
  status: number;
}) {
  return (
    <div className="model-registry-terminal-error" role="alert">
      <strong>Model Registry 조회 실패</strong>
      {status === 401 ? <Link href="/session/expired">{registryErrorMessage(status, code ?? undefined)}</Link> : <span>{registryErrorMessage(status, code ?? undefined)}</span>}
      {![401, 403, 404].includes(status) ? <button className="button button-secondary" onClick={onReload} type="button">원장 다시 조회</button> : null}
    </div>
  );
}

function formatRegistryTimestamp(value: string | null): string {
  if (!value) return "—";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "—";
  return new Intl.DateTimeFormat("ko-KR", { dateStyle: "short", timeStyle: "short" }).format(date);
}

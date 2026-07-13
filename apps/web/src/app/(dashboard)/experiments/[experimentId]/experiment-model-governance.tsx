"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type {
  ModelRegistryEntry,
  ModelRegistryRevokeReason,
  ModelRegistrySnapshot,
} from "@/lib/api-types";
import {
  applyRegistryMutation,
  fetchModelRegistrySnapshot,
  ModelRegistryMutationError,
  ModelRegistryReadError,
  registryErrorMessage,
  registryMutationIsUncertain,
  type RegistryCandidateSource,
} from "@/lib/client/model-registry";
import {
  createRegistryPromoteIntent,
  createRegistryRegisterIntent,
  createRegistryRevokeIntent,
  executeRegistryMutationIntent,
  reconcileRegistryMutation,
  RegistryActorChangedError,
  RegistryActorIdentityError,
  registryIntentDescription,
  type RegistryMutationIntent,
  verifyRegistryIntentActor,
} from "@/lib/client/model-registry-reconciliation";
import type { JobStatus } from "@/lib/types";
import { ExperimentRunComparison } from "./experiment-run-comparison";
import {
  ModelRegistryPanel,
  type RegistryPanelFeedback,
  type RegistryPanelPhase,
} from "./model-registry-panel";

export function ExperimentModelGovernance({
  actorId,
  comparisonAvailable = true,
  experimentId,
  jobs,
}: {
  actorId: string;
  comparisonAvailable?: boolean;
  experimentId: string;
  jobs: Array<{ id: string; name: string; status: JobStatus }>;
}) {
  const [snapshot, setSnapshot] = useState<ModelRegistrySnapshot | null>(null);
  const [phase, setPhase] = useState<RegistryPanelPhase>("loading");
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [errorCode, setErrorCode] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<RegistryPanelFeedback | null>(null);
  const [selectedCandidate, setSelectedCandidate] = useState<RegistryCandidateSource | null>(null);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [uncertainIntent, setUncertainIntent] = useState<RegistryMutationIntent | null>(null);
  const [readRevision, setReadRevision] = useState(0);
  const readGeneration = useRef(0);
  const mutationLock = useRef(false);

  useEffect(() => {
    const controller = new AbortController();
    const generation = ++readGeneration.current;
    void fetchModelRegistrySnapshot(experimentId, controller.signal)
      .then((next) => {
        if (controller.signal.aborted || readGeneration.current !== generation) return;
        mutationLock.current = false;
        setSnapshot(next);
        setPhase("ready");
      })
      .catch((error: unknown) => {
        if (isAbortError(error) || controller.signal.aborted || readGeneration.current !== generation) return;
        const status = error instanceof ModelRegistryReadError ? error.status : 502;
        const code = error instanceof ModelRegistryReadError ? error.code : "request_failed";
        mutationLock.current = true;
        setErrorStatus(status);
        setErrorCode(code);
        setPhase("error");
      });
    return () => controller.abort();
  }, [experimentId, readRevision]);

  const registeredModelArtifactIds = useMemo(
    () => snapshot?.items.map((entry) => entry.model.id) ?? [],
    [snapshot],
  );
  const locked =
    phase !== "ready" ||
    pendingAction !== null ||
    snapshot?.can_manage !== true ||
    snapshot.experiment_id !== experimentId ||
    uncertainIntent !== null && uncertainIntent.experimentId !== experimentId;
  const reconciliationLocked =
    uncertainIntent === null ||
    uncertainIntent.experimentId !== experimentId ||
    uncertainIntent.actorId !== actorId ||
    snapshot?.experiment_id !== experimentId;

  function selectCandidate(source: RegistryCandidateSource) {
    if (locked || mutationLock.current || !snapshot) return;
    const existing = snapshot.items.find((entry) => entry.model.id === source.model.id);
    if (existing) {
      setFeedback({
        tone: "status",
        message: `${source.jobName} model은 이미 ${registryEntryState(existing)} 상태로 등록되어 있습니다.`,
      });
      return;
    }
    setSelectedCandidate(source);
    setFeedback(null);
    requestAnimationFrame(() => {
      document.getElementById("registry-candidate-confirm-heading")?.focus();
    });
  }

  async function register(source: RegistryCandidateSource) {
    if (!beginMutation("register") || !snapshot) return;
    const intent = createRegistryRegisterIntent(snapshot, source, crypto.randomUUID(), actorId);
    try {
      await verifyRegistryIntentActor(intent);
      const result = await executeRegistryMutationIntent(intent);
      finishSuccessfulMutation(
        result.value,
        result.replayed
          ? "동일 후보 등록 요청의 저장된 결과를 확인했습니다. 최신 원장을 다시 읽습니다."
          : `${source.jobName} model을 검증된 Registry 후보로 등록했습니다.`,
      );
      setSelectedCandidate(null);
    } catch (error) {
      handleMutationFailure(error, intent);
    }
  }

  async function promote(entry: ModelRegistryEntry) {
    if (!beginMutation(`promote:${entry.id}`) || !snapshot) return;
    const intent = createRegistryPromoteIntent(snapshot, entry, crypto.randomUUID(), actorId);
    try {
      await verifyRegistryIntentActor(intent);
      const result = await executeRegistryMutationIntent(intent);
      finishSuccessfulMutation(
        result.value,
        result.replayed
          ? "동일 Champion 승인 요청의 저장된 결과를 확인했습니다. 최신 원장을 다시 읽습니다."
          : `${entry.source_job_name} model을 명시적으로 Champion 승인했습니다.`,
      );
    } catch (error) {
      handleMutationFailure(error, intent);
    }
  }

  async function revoke(entry: ModelRegistryEntry, reason: ModelRegistryRevokeReason) {
    if (!beginMutation(`revoke:${entry.id}`) || !snapshot) return;
    const intent = createRegistryRevokeIntent(snapshot, entry, reason, crypto.randomUUID(), actorId);
    try {
      await verifyRegistryIntentActor(intent);
      const result = await executeRegistryMutationIntent(intent);
      finishSuccessfulMutation(
        result.value,
        result.replayed
          ? "동일 폐기 요청의 저장된 결과를 확인했습니다. 최신 원장을 다시 읽습니다."
          : `${entry.source_job_name} Registry Entry를 영구 폐기했습니다.`,
      );
    } catch (error) {
      handleMutationFailure(error, intent);
    }
  }

  function beginMutation(action: string): boolean {
    if (mutationLock.current || locked || !snapshot || snapshot.experiment_id !== experimentId) return false;
    mutationLock.current = true;
    setPendingAction(action);
    setFeedback({ tone: "status", message: "row version과 canonical byte를 다시 검증하고 있습니다." });
    return true;
  }

  function finishSuccessfulMutation(
    response: Parameters<typeof applyRegistryMutation>[1],
    message: string,
  ) {
    if (snapshot) setSnapshot(applyRegistryMutation(snapshot, response));
    setUncertainIntent(null);
    setPendingAction(null);
    setFeedback({ tone: "status", message });
    mutationLock.current = false;
    // Promotion also versions the previous active entry while the compact
    // mutation response returns only the target. Always reload the complete
    // registry before enabling another CAS mutation.
    setPhase("loading");
    setReadRevision((current) => current + 1);
  }

  function handleMutationFailure(error: unknown, intent: RegistryMutationIntent) {
    setPendingAction(null);
    if (error instanceof RegistryActorChangedError) {
      handleActorChanged();
      return;
    }
    if (error instanceof RegistryActorIdentityError) {
      handleActorIdentityFailure(error, "ready");
      return;
    }
    const status = error instanceof ModelRegistryMutationError ? error.status : 502;
    const code = error instanceof ModelRegistryMutationError ? error.code : "request_failed";
    if (status === 401) {
      window.location.assign("/session/expired");
      return;
    }
    setErrorStatus(status);
    setErrorCode(code);
    if (registryMutationIsUncertain(error)) {
      mutationLock.current = true;
      setUncertainIntent(intent);
      setPhase("uncertain");
      setFeedback({
        tone: "error",
        message: "응답을 안전하게 확인하지 못했습니다. 원래 idempotency key와 요청 body를 보존했으며, 새 요청을 만들기 전에 전체 원장을 재확인해야 합니다.",
      });
      return;
    }
    setUncertainIntent(null);
    if (status === 409 || status === 403 || status === 404) {
      mutationLock.current = true;
      setPhase(status === 409 ? "stale" : "error");
      setFeedback({ tone: "error", message: registryErrorMessage(status, code) });
      return;
    }
    mutationLock.current = false;
    setPhase("ready");
    setFeedback({ tone: "error", message: registryErrorMessage(status, code) });
  }

  async function reconcileUncertainMutation() {
    const intent = uncertainIntent;
    if (
      !intent ||
      intent.experimentId !== experimentId ||
      pendingAction !== null ||
      phase !== "uncertain"
    ) return;
    setPendingAction("reconcile");
    setFeedback({ tone: "status", message: "보존한 요청을 재전송하지 않고 전체 Registry 원장을 다시 읽습니다." });
    try {
      await verifyRegistryIntentActor(intent);
      const latest = await fetchModelRegistrySnapshot(experimentId);
      const result = reconcileRegistryMutation(intent, latest);
      setSnapshot(result.snapshot);
      setErrorStatus(null);
      setErrorCode(null);
      setPendingAction(null);
      if (result.state === "applied") {
        if (intent.action === "register") setSelectedCandidate(null);
        setUncertainIntent(null);
        mutationLock.current = false;
        setPhase("ready");
        setFeedback({
          tone: "status",
          message: `${registryIntentDescription(intent)} 결과가 원장에 반영된 것을 확인했습니다. 요청을 다시 보내지 않았습니다.`,
        });
        return;
      }
      if (result.state === "unchanged") {
        mutationLock.current = true;
        setPhase("retryable");
        setFeedback({
          tone: "status",
          message: "전체 Registry 원장이 요청 전과 정확히 같습니다. 보존한 같은 key·같은 body만 명시적으로 재확인할 수 있습니다.",
        });
        return;
      }
      setUncertainIntent(null);
      mutationLock.current = false;
      setPhase("ready");
      setFeedback({
        tone: "error",
        message: "Registry 원장이 다른 변경으로 달라져 이전 요청 의도를 폐기했습니다. 최신 상태를 검토한 뒤 새 작업을 선택하세요.",
      });
    } catch (error) {
      if (error instanceof RegistryActorChangedError) {
        handleActorChanged();
        return;
      }
      if (error instanceof RegistryActorIdentityError) {
        handleActorIdentityFailure(error, "uncertain");
        return;
      }
      const status = error instanceof ModelRegistryReadError ? error.status : 502;
      const code = error instanceof ModelRegistryReadError ? error.code : "request_failed";
      setPendingAction(null);
      setErrorStatus(status);
      setErrorCode(code);
      mutationLock.current = true;
      setPhase("uncertain");
      setFeedback({
        tone: "error",
        message: `원장 재확인에 실패했습니다. 보존한 요청은 전송하지 않았습니다. ${registryErrorMessage(status, code)}`,
      });
    }
  }

  async function retryUnchangedMutation() {
    const intent = uncertainIntent;
    if (
      !intent ||
      intent.experimentId !== experimentId ||
      pendingAction !== null ||
      phase !== "retryable"
    ) return;
    mutationLock.current = true;
    setPendingAction(`retry:${intent.action}`);
    setFeedback({
      tone: "status",
      message: "변경되지 않은 원장에 대해 보존한 같은 idempotency key와 같은 body를 재확인합니다.",
    });
    try {
      await verifyRegistryIntentActor(intent);
      const result = await executeRegistryMutationIntent(intent);
      finishSuccessfulMutation(
        result.value,
        result.replayed
          ? `${registryIntentDescription(intent)}의 저장된 결과를 확인했습니다. 최신 원장을 다시 읽습니다.`
          : `${registryIntentDescription(intent)} 요청을 완료했습니다. 최신 원장을 다시 읽습니다.`,
      );
      if (intent.action === "register") setSelectedCandidate(null);
    } catch (error) {
      if (error instanceof RegistryActorChangedError) {
        handleActorChanged();
        return;
      }
      if (error instanceof RegistryActorIdentityError) {
        handleActorIdentityFailure(error, "retryable");
        return;
      }
      handleMutationFailure(error, intent);
    }
  }

  function abandonUnchangedMutation() {
    if (
      !uncertainIntent ||
      uncertainIntent.experimentId !== experimentId ||
      pendingAction !== null ||
      phase !== "retryable"
    ) return;
    setUncertainIntent(null);
    mutationLock.current = false;
    setPhase("ready");
    setFeedback({
      tone: "status",
      message: "보존했던 요청을 재전송하지 않고 폐기했습니다. 최신 원장을 기준으로 새 작업을 선택할 수 있습니다.",
    });
  }

  function handleActorChanged() {
    setUncertainIntent(null);
    setPendingAction(null);
    mutationLock.current = true;
    setPhase("identity_changed");
    setFeedback({
      tone: "error",
      message: "로그인 사용자가 변경되어 보존한 Registry 요청을 폐기했습니다. 이전 key/body를 새 사용자로 전송하지 않습니다.",
    });
  }

  function handleActorIdentityFailure(
    error: RegistryActorIdentityError,
    returnPhase: "ready" | "retryable" | "uncertain",
  ) {
    setPendingAction(null);
    if (error.status === 401) {
      window.location.assign("/session/expired");
      return;
    }
    mutationLock.current = returnPhase !== "ready";
    setPhase(returnPhase);
    setFeedback({
      tone: "error",
      message: "현재 로그인 사용자를 확인하지 못해 Registry 요청을 전송하지 않았습니다. 연결을 확인한 뒤 다시 시도하세요.",
    });
  }

  return (
    <>
      {comparisonAvailable ? (
        <ExperimentRunComparison
          experimentId={experimentId}
          jobs={jobs}
          onRegisterCandidate={selectCandidate}
          registeredModelArtifactIds={registeredModelArtifactIds}
          registryLocked={locked}
        />
      ) : null}
      <ModelRegistryPanel
        candidate={selectedCandidate}
        errorCode={errorCode}
        errorStatus={errorStatus}
        feedback={feedback}
        locked={locked}
        onCancelCandidate={() => setSelectedCandidate(null)}
        onAbandonUnchanged={abandonUnchangedMutation}
        onPromote={(entry) => void promote(entry)}
        onReconcile={() => void reconcileUncertainMutation()}
        onRegister={(source) => void register(source)}
        onReload={() => {
          mutationLock.current = false;
          setUncertainIntent(null);
          setFeedback(null);
          setErrorStatus(null);
          setErrorCode(null);
          setPhase("loading");
          setReadRevision((current) => current + 1);
        }}
        onReloadPage={() => window.location.reload()}
        onRetryUnchanged={() => void retryUnchangedMutation()}
        onRevoke={(entry, reason) => void revoke(entry, reason)}
        pendingAction={pendingAction}
        phase={phase}
        reconciliationLocked={reconciliationLocked}
        snapshot={snapshot}
      />
    </>
  );
}

function registryEntryState(entry: ModelRegistryEntry): string {
  if (entry.is_active) return "현재 Champion";
  if (entry.status === "candidate") return "승인 대기 후보";
  if (entry.status === "approved") return "비활성 승인";
  return "폐기";
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

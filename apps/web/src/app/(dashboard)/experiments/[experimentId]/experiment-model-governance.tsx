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
  promoteModelRegistryEntry,
  registerModelCandidate,
  registryErrorMessage,
  registryMutationIsUncertain,
  revokeModelRegistryEntry,
  type RegistryCandidateSource,
} from "@/lib/client/model-registry";
import type { JobStatus } from "@/lib/types";
import { ExperimentRunComparison } from "./experiment-run-comparison";
import {
  ModelRegistryPanel,
  type RegistryPanelFeedback,
  type RegistryPanelPhase,
} from "./model-registry-panel";

export function ExperimentModelGovernance({
  comparisonAvailable = true,
  experimentId,
  jobs,
}: {
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
  const locked = phase !== "ready" || pendingAction !== null || snapshot?.can_manage !== true;

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
    const key = crypto.randomUUID();
    try {
      const result = await registerModelCandidate(
        source,
        snapshot.registry_row_version,
        key,
      );
      finishSuccessfulMutation(
        result.value,
        result.replayed
          ? "동일 후보 등록 요청의 저장된 결과를 확인했습니다. 최신 원장을 다시 읽습니다."
          : `${source.jobName} model을 검증된 Registry 후보로 등록했습니다.`,
      );
      setSelectedCandidate(null);
    } catch (error) {
      handleMutationFailure(error);
    }
  }

  async function promote(entry: ModelRegistryEntry) {
    if (!beginMutation(`promote:${entry.id}`) || !snapshot) return;
    const key = crypto.randomUUID();
    try {
      const result = await promoteModelRegistryEntry(
        experimentId,
        entry,
        snapshot.registry_row_version,
        key,
      );
      finishSuccessfulMutation(
        result.value,
        result.replayed
          ? "동일 Champion 승인 요청의 저장된 결과를 확인했습니다. 최신 원장을 다시 읽습니다."
          : `${entry.source_job_name} model을 명시적으로 Champion 승인했습니다.`,
      );
    } catch (error) {
      handleMutationFailure(error);
    }
  }

  async function revoke(entry: ModelRegistryEntry, reason: ModelRegistryRevokeReason) {
    if (!beginMutation(`revoke:${entry.id}`) || !snapshot) return;
    const key = crypto.randomUUID();
    try {
      const result = await revokeModelRegistryEntry(
        experimentId,
        entry,
        snapshot.registry_row_version,
        reason,
        key,
      );
      finishSuccessfulMutation(
        result.value,
        result.replayed
          ? "동일 폐기 요청의 저장된 결과를 확인했습니다. 최신 원장을 다시 읽습니다."
          : `${entry.source_job_name} Registry Entry를 영구 폐기했습니다.`,
      );
    } catch (error) {
      handleMutationFailure(error);
    }
  }

  function beginMutation(action: string): boolean {
    if (mutationLock.current || locked || !snapshot) return false;
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
    setPendingAction(null);
    setFeedback({ tone: "status", message });
    mutationLock.current = false;
    // Promotion also versions the previous active entry while the compact
    // mutation response returns only the target. Always reload the complete
    // registry before enabling another CAS mutation.
    setPhase("loading");
    setReadRevision((current) => current + 1);
  }

  function handleMutationFailure(error: unknown) {
    setPendingAction(null);
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
      setPhase("uncertain");
      setFeedback({
        tone: "error",
        message: "응답을 안전하게 확인하지 못했습니다. 새 idempotency key로 반복하지 말고 최신 원장을 다시 확인하세요.",
      });
      return;
    }
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
        onPromote={(entry) => void promote(entry)}
        onRegister={(source) => void register(source)}
        onReload={() => {
          mutationLock.current = false;
          setFeedback(null);
          setErrorStatus(null);
          setErrorCode(null);
          setPhase("loading");
          setReadRevision((current) => current + 1);
        }}
        onRevoke={(entry, reason) => void revoke(entry, reason)}
        pendingAction={pendingAction}
        phase={phase}
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

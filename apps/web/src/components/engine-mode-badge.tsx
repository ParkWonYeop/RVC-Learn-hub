import type { EngineMode } from "@/lib/types";

const presentations = {
  fake: {
    label: "FAKE · 운영 결과 아님",
    accessibleLabel: "경고: FAKE 실행 · 운영 결과 아님",
  },
  rvc_webui: {
    label: "RVC WebUI",
    accessibleLabel: "실행 엔진: RVC WebUI",
  },
  pending: {
    label: "실행 전",
    accessibleLabel: "실행 엔진: 실행 전",
  },
} as const;

export function EngineModeBadge({ mode }: { mode: EngineMode }) {
  const key = mode ?? "pending";
  const presentation = presentations[key];

  return (
    <span
      aria-label={presentation.accessibleLabel}
      className={`engine-mode-badge engine-mode-${key}`}
      role="status"
    >
      <span aria-hidden="true" className="engine-mode-dot" />
      {presentation.label}
    </span>
  );
}

export function FakeEngineResultWarning({ mode }: { mode: EngineMode }) {
  if (mode !== "fake") return null;

  return (
    <section
      aria-label="Fake 실행 결과 경고"
      className="engine-mode-result-warning"
      role="alert"
    >
      <strong>FAKE 실행 결과</strong>
      <span>
        이 attempt는 시험용 Fake runner가 생성했습니다. 운영 모델이나 실제 RVC 학습 결과로
        사용하면 안 됩니다.
      </span>
    </section>
  );
}

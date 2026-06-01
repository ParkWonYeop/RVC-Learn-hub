import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  EngineModeBadge,
  FakeEngineResultWarning,
} from "@/components/engine-mode-badge";

describe("EngineModeBadge", () => {
  it("renders Fake mode as an accessible warning that cannot look production-ready", () => {
    const html = renderToStaticMarkup(createElement(EngineModeBadge, { mode: "fake" }));

    expect(html).toContain('role="status"');
    expect(html).toContain('aria-label="경고: FAKE 실행 · 운영 결과 아님"');
    expect(html).toContain("FAKE · 운영 결과 아님");
    expect(html).toContain("engine-mode-fake");
  });

  it("renders real and not-yet-run attempts without inventing an engine", () => {
    const real = renderToStaticMarkup(createElement(EngineModeBadge, { mode: "rvc_webui" }));
    const pending = renderToStaticMarkup(createElement(EngineModeBadge, { mode: null }));

    expect(real).toContain("RVC WebUI");
    expect(real).not.toContain("운영 결과 아님");
    expect(pending).toContain("실행 전");
    expect(pending).toContain('aria-label="실행 엔진: 실행 전"');
  });
});

describe("FakeEngineResultWarning", () => {
  it("uses assertive alert semantics only for Fake attempts", () => {
    const fake = renderToStaticMarkup(
      createElement(FakeEngineResultWarning, { mode: "fake" }),
    );
    const real = renderToStaticMarkup(
      createElement(FakeEngineResultWarning, { mode: "rvc_webui" }),
    );

    expect(fake).toContain('role="alert"');
    expect(fake).toContain('aria-label="Fake 실행 결과 경고"');
    expect(fake).toContain("운영 모델이나 실제 RVC 학습 결과로");
    expect(real).toBe("");
  });
});

import { describe, expect, it } from "vitest";
import {
  metricDisplayName,
  metricDisplayValue,
} from "@/lib/client/metric-presentation";

describe("Job metric presentation", () => {
  it("labels canonical GPU and disk telemetry without hiding the GPU index", () => {
    expect(metricDisplayName("system.gpu.2.utilization_percent")).toBe("GPU 2 사용률");
    expect(metricDisplayName("system.gpu.2.vram_used_mb")).toBe("GPU 2 사용 VRAM");
    expect(metricDisplayName("system.gpu.2.temperature_c")).toBe("GPU 2 온도");
    expect(metricDisplayName("system.gpu.telemetry_available")).toBe(
      "GPU 메트릭 수집 상태",
    );
    expect(metricDisplayName("system.disk_free_bytes")).toBe("Worker 남은 디스크");
  });

  it("renders system units and preserves unknown metric keys", () => {
    expect(metricDisplayValue("system.gpu.0.utilization_percent", 37.5)).toBe("37.5%");
    expect(metricDisplayValue("system.gpu.0.vram_total_mb", 24_576)).toBe("24,576 MiB");
    expect(metricDisplayValue("system.gpu.0.temperature_c", 61)).toBe("61 °C");
    expect(metricDisplayValue("system.gpu.telemetry_available", 1)).toBe("수집 가능");
    expect(metricDisplayValue("system.gpu.telemetry_available", 0)).toBe("수집 불가");
    expect(metricDisplayValue("system.disk_free_bytes", 5 * 1_024 ** 3)).toBe("5 GiB");
    expect(metricDisplayName("custom.metric")).toBe("custom.metric");
  });
});

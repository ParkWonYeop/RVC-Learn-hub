const gpuMetricPattern =
  /^system\.gpu\.(\d+)\.(utilization_percent|vram_used_mb|vram_total_mb|temperature_c)$/;

const trainingMetricLabels: Record<string, string> = {
  current_epoch: "현재 Epoch",
  total_epoch: "전체 Epoch",
  step: "학습 Step",
  loss_g_total: "Generator 전체 Loss",
  loss_d_total: "Discriminator 전체 Loss",
  loss_mel: "Mel Loss",
  loss_kl: "KL Loss",
  loss_fm: "Feature matching Loss",
  learning_rate: "학습률",
  grad_norm_g: "Generator gradient norm",
  grad_norm_d: "Discriminator gradient norm",
  "worker.stage_completed": "완료 Stage",
};

const gpuMetricLabels: Record<string, string> = {
  utilization_percent: "사용률",
  vram_used_mb: "사용 VRAM",
  vram_total_mb: "전체 VRAM",
  temperature_c: "온도",
};

export function metricDisplayName(key: string): string {
  if (key === "system.gpu.count") return "GPU 수";
  if (key === "system.gpu.telemetry_available") return "GPU 메트릭 수집 상태";
  if (key === "system.disk_free_bytes") return "Worker 남은 디스크";
  const gpu = gpuMetricPattern.exec(key);
  if (gpu) return `GPU ${gpu[1]} ${gpuMetricLabels[gpu[2]]}`;
  return trainingMetricLabels[key] ?? key;
}

export function metricDisplayValue(key: string, value: number): string {
  if (key === "system.gpu.telemetry_available") {
    return value >= 0.5 ? "수집 가능" : "수집 불가";
  }
  if (key === "system.disk_free_bytes") return formatBytes(value);
  if (key.endsWith(".utilization_percent")) return `${formatNumber(value)}%`;
  if (key.endsWith(".vram_used_mb") || key.endsWith(".vram_total_mb")) {
    return `${formatNumber(value)} MiB`;
  }
  if (key.endsWith(".temperature_c")) return `${formatNumber(value)} °C`;
  return formatNumber(value);
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat("en-US", { maximumSignificantDigits: 8 }).format(value);
}

function formatBytes(value: number): string {
  if (value < 1_024) return `${formatNumber(value)} B`;
  if (value < 1_024 ** 2) return `${formatNumber(value / 1_024)} KiB`;
  if (value < 1_024 ** 3) return `${formatNumber(value / 1_024 ** 2)} MiB`;
  return `${formatNumber(value / 1_024 ** 3)} GiB`;
}

import type { SampleListView, SampleView } from "@/lib/types";

export class SampleReadError extends Error {
  constructor(readonly status: number) {
    super(`Sample BFF request failed with status ${status}`);
    this.name = "SampleReadError";
  }
}

export async function fetchJobSamples(
  jobId: string,
  signal?: AbortSignal,
): Promise<SampleListView> {
  const response = await fetch(
    `/bff/jobs/${encodeURIComponent(jobId)}/samples?limit=200`,
    {
      cache: "no-store",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      signal,
    },
  );
  if (!response.ok) throw new SampleReadError(response.status);
  let value: unknown;
  try {
    value = await response.json();
  } catch {
    throw new SampleReadError(502);
  }
  if (!isSampleList(value, jobId)) throw new SampleReadError(502);
  return value;
}

function isSampleList(value: unknown, jobId: string): value is SampleListView {
  const root = record(value);
  if (
    !root ||
    !Array.isArray(root.items) ||
    !isInteger(root.total, 0) ||
    !isInteger(root.offset, 0) ||
    !isInteger(root.limit, 1) ||
    root.limit > 200 ||
    root.items.length > root.limit
  ) {
    return false;
  }
  const seenItems = new Set<string>();
  return root.items.every((item) => {
    if (!isSample(item, jobId) || seenItems.has(item.testSetItemId)) return false;
    seenItems.add(item.testSetItemId);
    return true;
  });
}

function isSample(value: unknown, jobId: string): value is SampleView {
  const sample = record(value);
  const metrics = record(sample?.metrics);
  const manager = record(metrics?.managerComputed);
  const worker = record(metrics?.workerReported);
  return Boolean(
    sample &&
      sample.jobId === jobId &&
      typeof sample.id === "string" &&
      typeof sample.testSetItemId === "string" &&
      typeof sample.outputSha256 === "string" &&
      typeof sample.outputDurationSeconds === "number" &&
      typeof sample.outputSampleRateHz === "number" &&
      typeof sample.outputChannels === "number" &&
      metrics?.algorithm === "pcm-normalized-v2" &&
      metrics.authoritativeSource === "manager_computed" &&
      manager &&
      worker,
  );
}

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function isInteger(value: unknown, minimum: number): value is number {
  return Number.isSafeInteger(value) && (value as number) >= minimum;
}

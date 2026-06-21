export type PollTask = (signal: AbortSignal, background: boolean) => Promise<void>;

export function startNonOverlappingPolling(
  task: PollTask,
  intervalMs: number,
): () => void {
  if (!Number.isSafeInteger(intervalMs) || intervalMs < 1) {
    throw new Error("poll interval must be a positive safe integer");
  }

  let stopped = false;
  let activeRequest: AbortController | null = null;
  const run = async (background: boolean) => {
    if (stopped || activeRequest !== null) return;
    const controller = new AbortController();
    activeRequest = controller;
    try {
      await task(controller.signal, background);
    } finally {
      if (activeRequest === controller) activeRequest = null;
    }
  };

  void run(false);
  const timer = globalThis.setInterval(() => {
    void run(true);
  }, intervalMs);

  return () => {
    stopped = true;
    globalThis.clearInterval(timer);
    activeRequest?.abort();
  };
}

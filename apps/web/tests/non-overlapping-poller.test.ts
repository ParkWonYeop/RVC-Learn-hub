import { afterEach, describe, expect, it, vi } from "vitest";

import { startNonOverlappingPolling } from "@/lib/client/non-overlapping-poller";

afterEach(() => {
  vi.useRealTimers();
});

describe("non-overlapping poller", () => {
  it("runs immediately and skips ticks while the previous request is active", async () => {
    vi.useFakeTimers();
    let releaseFirst: (() => void) | undefined;
    const firstPending = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    const task = vi
      .fn<(signal: AbortSignal, background: boolean) => Promise<void>>()
      .mockReturnValueOnce(firstPending)
      .mockResolvedValue(undefined);

    const stop = startNonOverlappingPolling(task, 1_000);
    expect(task).toHaveBeenCalledTimes(1);
    expect(task.mock.calls[0]?.[1]).toBe(false);

    await vi.advanceTimersByTimeAsync(3_000);
    expect(task).toHaveBeenCalledTimes(1);

    releaseFirst?.();
    await firstPending;
    await vi.advanceTimersByTimeAsync(1_000);
    expect(task).toHaveBeenCalledTimes(2);
    expect(task.mock.calls[1]?.[1]).toBe(true);
    stop();
  });

  it("aborts the active request and permanently clears its timer", async () => {
    vi.useFakeTimers();
    let observedSignal: AbortSignal | undefined;
    const task = vi.fn((signal: AbortSignal) => {
      observedSignal = signal;
      return new Promise<void>(() => undefined);
    });

    const stop = startNonOverlappingPolling(task, 1_000);
    expect(task).toHaveBeenCalledTimes(1);
    expect(observedSignal?.aborted).toBe(false);

    stop();
    expect(observedSignal?.aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(5_000);
    expect(task).toHaveBeenCalledTimes(1);
  });

  it("rejects unsafe intervals before scheduling work", () => {
    const task = vi.fn(async () => undefined);
    expect(() => startNonOverlappingPolling(task, 0)).toThrow(
      "poll interval must be a positive safe integer",
    );
    expect(task).not.toHaveBeenCalled();
  });
});

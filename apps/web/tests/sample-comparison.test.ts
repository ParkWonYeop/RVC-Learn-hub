import { describe, expect, it } from "vitest";
import { pairSamples } from "@/lib/client/sample-comparison";
import type { SampleListView, SampleView } from "@/lib/types";

describe("Sample A/B pairing", () => {
  it("pairs only the same immutable TestSet item", () => {
    const left = list(sample("left-1", "item-1"), sample("left-2", "item-2"));
    const right = list(sample("right-2", "item-2"), sample("right-3", "item-3"));

    const result = pairSamples(left, right);

    expect(result.invalidLedger).toBe(false);
    expect(result.pairs.map(([a, b]) => [a.id, b.id])).toEqual([
      ["left-2", "right-2"],
    ]);
  });

  it("fails closed instead of overwriting a duplicate item ID", () => {
    const left = list(sample("left-1", "item-1"), sample("left-2", "item-1"));
    const right = list(sample("right-1", "item-1"));

    const result = pairSamples(left, right);

    expect(result.invalidLedger).toBe(true);
    expect(result.pairs).toEqual([]);
  });
});

function list(...items: SampleView[]): SampleListView {
  return { items, total: items.length, offset: 0, limit: 200 };
}

function sample(id: string, testSetItemId: string): SampleView {
  return { id, testSetItemId } as SampleView;
}

import type { SampleListView, SampleView } from "@/lib/types";

export interface SampleComparisonResult {
  pairs: Array<[SampleView, SampleView]>;
  invalidLedger: boolean;
}

export function pairSamples(
  left: SampleListView | null,
  right: SampleListView | null,
): SampleComparisonResult {
  if (!left || !right) return { pairs: [], invalidLedger: false };
  const leftByItem = uniqueSamplesByItem(left.items);
  const rightByItem = uniqueSamplesByItem(right.items);
  if (!leftByItem || !rightByItem) return { pairs: [], invalidLedger: true };
  const pairs = [...leftByItem.values()].flatMap((sample) => {
    const match = rightByItem.get(sample.testSetItemId);
    return match ? [[sample, match] as [SampleView, SampleView]] : [];
  });
  return { pairs, invalidLedger: false };
}

function uniqueSamplesByItem(samples: SampleView[]): Map<string, SampleView> | null {
  const byItem = new Map<string, SampleView>();
  for (const sample of samples) {
    if (byItem.has(sample.testSetItemId)) return null;
    byItem.set(sample.testSetItemId, sample);
  }
  return byItem;
}

import type { ComparisonStatus } from "@/types/reasonableness";

// Two-state collapse of the 7 ComparisonStatus values.
// Attention is encoded by position + size + a single indigo, never by hue.
export type Attention = "calm" | "worth_a_look" | "no_benchmark";

export function attentionOf(status: ComparisonStatus): Attention {
  if (status === "no_benchmark") return "no_benchmark";
  // Under-spending vs peers is NOT alarming for expenses → calm.
  if (status === "in_line" || status === "below" || status === "well_below")
    return "calm";
  return "worth_a_look"; // above | well_above | watch
}

// Plain-language direction + valence label — NO stats vocabulary.
export function directionLabel(status: ComparisonStatus): string {
  switch (status) {
    case "well_above":
      return "higher than most similar businesses";
    case "above":
      return "a bit higher than most similar businesses";
    case "in_line":
      return "right around the middle for similar businesses";
    case "below":
      return "a bit lower than most similar businesses";
    case "well_below":
      return "lower than most similar businesses";
    case "watch":
      return "worth a quick look";
    case "no_benchmark":
      return "no separate industry figure";
  }
}

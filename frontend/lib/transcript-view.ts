// Presentation rules for the transcript, with no runtime imports so node tests can load them.

type Keyed = { kind: string; key: string };

export type ToolRun<T> = { kind: "tool_run"; key: string; tools: T[] };

// Runs of three or more tool calls in a row are shown as one collapsible group so a busy turn
// does not bury the answer. One or two calls stay as single rows. Anything else in between
// (an approval, a message) ends a run.
export const MIN_RUN = 3;

export function groupToolRuns<T extends Keyed>(items: readonly T[]): (T | ToolRun<T>)[] {
  const out: (T | ToolRun<T>)[] = [];
  let run: T[] = [];
  const flush = () => {
    if (run.length >= MIN_RUN) out.push({ kind: "tool_run", key: `run-${run[0].key}`, tools: run });
    else out.push(...run);
    run = [];
  };
  for (const item of items) {
    if (item.kind === "tool") run.push(item);
    else {
      flush();
      out.push(item);
    }
  }
  flush();
  return out;
}

export function toolRunSummary(tools: readonly { state: string }[]): { total: number; running: number; failed: number } {
  return {
    total: tools.length,
    running: tools.filter((tool) => tool.state === "running").length,
    failed: tools.filter((tool) => tool.state === "error").length,
  };
}

// Message text as it should be spoken: code fences and Markdown marks are dropped, links keep
// their words.
export function spokenText(markdown: string): string {
  return markdown
    .replace(/```[\s\S]*?```/g, " code block ")
    .replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/[`*_>#|~]/g, "")
    .replace(/^\s*[-+]\s+/gm, "")
    .replace(/\s+/g, " ")
    .trim();
}

type AnnounceItem = { kind: string; key: string; text?: string; incomplete?: boolean; status?: string };

// What a screen reader should be told, once, about the newest thing that needs attention: an
// approval waiting, a finished answer, a failed run. The transcript itself is not a live
// region (it would re-read every tool tick); this picks the few events worth speaking.
export function announcement(items: readonly AnnounceItem[]): { key: string; text: string } | null {
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.kind === "approval" && item.status === "pending") return { key: item.key, text: "Your agent is waiting for your approval." };
    if (item.kind === "turn_end" && item.status === "failed") return { key: item.key, text: "The run failed." };
    if (item.kind === "assistant" && !item.incomplete && item.text) {
      const text = spokenText(item.text);
      return { key: item.key, text: `Agent replied: ${text.length > 240 ? `${text.slice(0, 240)}…` : text}` };
    }
  }
  return null;
}


// Show the engine's total once, on the last visible reply from the matching turn.
// Tool-only turns keep their usage at the completion event.
export function usageByMessage(items: readonly {
  key: string; kind: string; turnId?: number | null; text?: string; usage?: Record<string, unknown> | null;
}[]) {
  const replies = new Map<number, string>();
  const usage = new Map<string, Record<string, unknown>>();
  for (const item of items) {
    if (item.kind === "assistant" && item.text && item.turnId != null) replies.set(item.turnId, item.key);
    if (item.kind === "turn_end" && item.usage) {
      const key = item.turnId != null ? replies.get(item.turnId) : undefined;
      usage.set(key ?? item.key, item.usage);
    }
  }
  return usage;
}

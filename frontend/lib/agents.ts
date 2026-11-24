import type { Agent, AgentMemory, ApprovalMode, Engine } from "./api";

// Suggestions for the model box; any text the Claude CLI accepts also works.
export const MODEL_SUGGESTIONS = ["default", "sonnet", "opus", "haiku"] as const;

export const APPROVAL_MODES: { value: ApprovalMode; label: string; description: string }[] = [
  {
    value: "ask",
    label: "Ask first",
    description: "Your agent asks before it writes a file or runs a command. Reading is allowed.",
  },
  {
    value: "accept_edits",
    label: "Accept edits",
    description: "File edits go ahead without asking. Commands still ask first.",
  },
  {
    value: "approve_for_me",
    label: "Approve for me",
    description: "Claude, Codex, or Cursor reviews eligible requests. External tools and computer controls still ask you. Other engines use Ask first.",
  },
  {
    value: "full_access",
    label: "Full access",
    description: "Claude or Codex runs commands and edits files in its workspace without asking, so routines can finish with nobody there. Dangerous commands, connected apps and sign-ins still need you.",
  },
  {
    value: "plan",
    label: "Plan only",
    description: "Your agent looks around and writes a plan, but changes nothing.",
  },
];

export function approvalModesForProvider(provider: string | null | undefined) {
  return APPROVAL_MODES.filter((mode) => (mode.value !== "approve_for_me" || provider === "claude" || provider === "codex" || provider === "cursor")
    && (mode.value !== "full_access" || provider === "claude" || provider === "codex"));
}

export function approvalModeLabel(mode: string): string {
  return APPROVAL_MODES.find((m) => m.value === mode)?.label ?? mode;
}

// "default" in the box means the provider's own default, which is stored as no model.
export function modelFromInput(text: string): string | null {
  const model = text.trim();
  return model === "" || model.toLowerCase() === "default" ? null : model;
}

export const STEWARD_MODELS = [
  { id: "deepseek-flash", label: "DeepSeek Flash" },
  { id: "deepseek-v4-pro", label: "DeepSeek V4 Pro" },
] as const;

export function modelLabel(model: string | null): string {
  if (!model) return "default";
  const steward = STEWARD_MODELS.find(item => item.id === model);
  if (steward) return steward.label;
  const gpt = /^gpt-(\d+(?:\.\d+)*)(?:-(sol|astra|terra|luna|mini|nano|pro|codex))?$/i.exec(model);
  if (gpt) return "GPT-" + gpt[1] + (gpt[2] ? " " + gpt[2][0].toUpperCase() + gpt[2].slice(1) : "");
  return model;
}

export function chatModelLabel(engine: Engine | undefined, model: string | null, fallback: string): string {
  const id = model ?? engine?.default_model ?? engine?.models.find(item => item.default)?.id;
  if (!id) return fallback;
  const label = engine?.models.find(item => item.id === id)?.label;
  return label && label !== id ? label : modelLabel(id);
}

// A model catalogue is capability metadata, never a substitute for an unknown saved model.
export function modelChoice(engine: Engine | undefined, model: string | null, inheritedModel?: string | null) {
  const engineDefault = engine?.default_model || engine?.models.find(entry => entry.default)?.id || null;
  const id = model || inheritedModel || engineDefault;
  const metadata = engine?.models.find(entry => entry.id === id);
  const efforts = metadata?.reasoning_effort_configurable !== false ? metadata?.supported_reasoning_efforts ?? [] : [];
  return { id, engineDefault, efforts: efforts.length > 1 ? efforts : [], defaultEffort: metadata?.default_reasoning_effort };
}

// Names by id, for lists that show which agent a conversation uses.
export function agentNames(agents: Agent[] | null): Map<number, string> {
  return new Map((agents ?? []).map((agent) => [agent.id, agent.name]));
}

// The agents a person can start a chat with: archived ones are left out.
export function activeAgents(agents: Agent[] | null): Agent[] {
  return (agents ?? []).filter((agent) => agent.archived_at === null);
}

// How full the notes are against what loads each turn: by lines or by bytes,
// whichever is closer to its limit. "near" from 80%, "over" once anything is cut.
export type MemoryGauge = { percent: number; level: "ok" | "near" | "over"; text: string };

export function memoryGauge(memory: Pick<AgentMemory, "lines" | "bytes" | "max_lines" | "max_bytes" | "truncated" | "loaded_lines" | "expired" | "expired_lines">): MemoryGauge {
  const share = Math.max(memory.lines / memory.max_lines, memory.bytes / memory.max_bytes);
  const percent = Math.min(100, Math.round(share * 100));
  const level = memory.truncated ? "over" : share >= 0.8 ? "near" : "ok";
  const size = `${memory.lines} of ${memory.max_lines} lines · ${formatKB(memory.bytes)} of ${formatKB(memory.max_bytes)}`;
  const past = Math.max(0, memory.lines - (memory.expired_lines ?? 0) - memory.loaded_lines);
  const cut = memory.truncated ? ` · ${past} lines past the limit do not load` : "";
  const expired = memory.expired ? ` · ${memory.expired} expired ${memory.expired === 1 ? "note is" : "notes are"} not loaded` : "";
  return { percent, level, text: size + cut + expired };
}

function formatKB(bytes: number): string {
  return bytes < 1000 ? `${bytes} B` : `${(bytes / 1000).toFixed(bytes < 10_000 ? 1 : 0)} KB`;
}

// One line for what a tidy-up did.
export function tidyLine(r: { organized: number; expired: number; duplicates: number; superseded: number; note: string | null }): string {
  const parts = [
    r.organized ? `${r.organized} filed into topic notes` : "",
    r.expired ? `${r.expired} expired moved to the archive` : "",
    r.duplicates ? `${r.duplicates} repeat${r.duplicates === 1 ? "" : "s"} removed` : "",
    r.superseded ? `${r.superseded} contradicted note${r.superseded === 1 ? "" : "s"} struck through` : "",
  ].filter(Boolean);
  return (parts.length ? `Tidied: ${parts.join(", ")}.` : "Nothing to tidy.") + (r.note ? ` ${r.note}` : "");
}

// The text of an agent file as saved to disk.
export function agentFileText(document: unknown): string {
  return JSON.stringify(document, null, 2) + "\n";
}

// An agent file read back from disk. Throws an Error with a sentence a person
// can act on; the backend checks everything else.
export function parseAgentFile(text: string): Record<string, unknown> {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error("That file is not an agent file (it is not JSON).");
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("That file is not an agent file.");
  }
  return value as Record<string, unknown>;
}

// Whether a parsed agent file carries notes, so the import can offer to skip them.
export function agentFileHasNotes(document: Record<string, unknown>): boolean {
  const notes = document.notes as Record<string, unknown> | undefined;
  const topics = notes?.topics as Record<string, unknown> | undefined;
  return (
    (typeof notes?.["MEMORY.md"] === "string" && (notes["MEMORY.md"] as string).trim() !== "") ||
    (typeof topics === "object" && topics !== null && Object.keys(topics).length > 0)
  );
}

// After an export: which parts had something that looked like a secret replaced.
export function redactionNotice(parts: string[]): string | null {
  if (parts.length === 0) return null;
  return `Something that looked like a secret was replaced in: ${parts.join(", ")}.`;
}

"use client";

import { Check, ChevronRight, Copy, GitBranch, Loader2, Reply, Square, ThumbsDown, ThumbsUp, X } from "lucide-react";
import type { ReactionKind } from "@/lib/reactions";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { canOpenWorkbench } from "@/lib/session";
import { canAlwaysAllow, isScreenInput } from "@/lib/approvals";
import { can } from "@/lib/permissions";
import { isComputerTool } from "@/lib/computers";
import { cn } from "@/lib/cn";
import { failureNextStep, interruptedText, turnUsageText } from "@/lib/everyday";
import { compactedLabel, recalledLabel } from "@/lib/memory";
import type { TranscriptItem } from "@/lib/transcript";
import { useT } from "@/lib/use-i18n";
import { toolRunSummary } from "@/lib/transcript-view";
import { Button } from "@/components/ui/Button";
import { useSession } from "../AuthGate";
import { presentStewardReply, stewardTurnStatus, downloadStewardReply } from "@/lib/steward-preview";
import { Markdown } from "./Markdown";
import { copyToClipboard } from "@/lib/clipboard";
import { MessageAttachments } from "./Attachments";
import { canBranchFrom } from "@/lib/branch-link";
import { formatDateDivider, formatMessageTime } from "@/lib/relative-time";
export { formatDateDivider, formatMessageTime };

type Item<K extends TranscriptItem["kind"]> = Extract<TranscriptItem, { kind: K }>;

// A value is shown exactly as it will be used, so a person approves what runs:
// strings verbatim (a command, file contents), anything else as JSON.
function formatValue(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

const PREVIEW_CHARS = 4000;

// Long tool output is cut for display so one big result cannot swamp the page.
function LongText({ value }: { value: string }) {
  const [all, setAll] = useState(false);
  const cut = !all && value.length > PREVIEW_CHARS;
  return (
    <>
      <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-field bg-code p-2.5 font-mono text-xs text-ink">
        {cut ? value.slice(0, PREVIEW_CHARS) : value}
      </pre>
      {cut && (
        <button onClick={() => setAll(true)} className="mt-1 cursor-pointer text-xs text-muted underline">
          Show all ({value.length.toLocaleString()} characters)
        </button>
      )}
    </>
  );
}

export function DateDivider({ ts }: { ts: string }) {
  const t = useT();
  const label = formatDateDivider(ts, t);
  if (!label) return null;
  return (
    <div className="conversation-day relative my-3 flex items-center justify-center select-none" role="separator" aria-label={label}>
      <div className="absolute inset-0 flex items-center" aria-hidden="true">
        <div className="w-full border-t border-line/60" />
      </div>
      <span className="relative rounded-full border border-line bg-surface px-2.5 py-0.5 text-[11px] font-medium text-muted shadow-2xs">
        {label}
      </span>
    </div>
  );
}

function CopyButton({ text, className }: { text: string; className?: string }) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  const copy = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    const ok = await copyToClipboard(text);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  const label = copied ? t("chat.copied") : t("chat.copy");
  return (
    <button
      type="button"
      onClick={copy}
      aria-label={label}
      title={label}
      className={cn(
        "inline-flex size-7 cursor-pointer items-center justify-center rounded-field text-muted transition-colors hover:bg-hover hover:text-ink touch:size-10",
        copied && "opacity-100 text-success",
        className,
      )}
    >
      {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
    </button>
  );
}

function QuoteButton({ onQuote }: { onQuote: () => void }) {
  const t = useT();
  return (
    <button type="button" onClick={onQuote} aria-label={t("chat.quote")} title={t("chat.quote")}
      className="shrink-0 rounded-md p-1 text-muted hover:text-ink">
      <Reply size={14} aria-hidden="true" />
    </button>
  );
}

export function UserMessage({
  item,
  threadId = null,
  onBranch,
  onQuote,
}: {
  item: Item<"user">;
  threadId?: number | null;
  // Edit this message and resend it in a new branch; resolves false on failure.
  onBranch?: (turnId: number, text: string) => Promise<boolean>;
  onQuote?: () => void;
}) {
  const t = useT();
  const timeStr = formatMessageTime(item.ts, t);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.text);
  const [busy, setBusy] = useState(false);
  const canBranch = onBranch !== undefined && canBranchFrom(item);
  if (editing && canBranch) {
    const submit = async () => {
      if (!draft.trim() || busy) return;
      setBusy(true);
      const ok = await onBranch(item.turnId as number, draft);
      setBusy(false);
      if (ok) setEditing(false);
    };
    return (
      <div className="flex flex-col items-end gap-2">
        <textarea
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          rows={Math.min(8, Math.max(2, draft.split("\n").length))}
          aria-label={t("branch.edit")}
          className="w-full max-w-[88%] rounded-2xl border border-line bg-surface px-4 py-2.5 text-[15px] leading-relaxed text-ink sm:max-w-[80%]"
        />
        <div className="flex gap-2">
          <Button variant="ghost" onClick={() => { setEditing(false); setDraft(item.text); }} disabled={busy}>
            {t("branch.cancel")}
          </Button>
          <Button onClick={submit} disabled={busy || !draft.trim()}>
            {t("branch.send")}
          </Button>
        </div>
      </div>
    );
  }
  return (
    <div className="conversation-user group/msg flex flex-col items-end gap-1">
      <div className="conversation-byline">{item.person || t("search.kindYou")}</div>
      <MessageAttachments threadId={threadId} items={item.attachments ?? []} />
      <div className="flex max-w-[88%] items-end gap-1.5 sm:max-w-[80%]">
        {item.text && <CopyButton text={item.text} />}
        {onQuote && <QuoteButton onQuote={onQuote} />}
        {canBranch && (
          <button
            type="button"
            onClick={() => { setDraft(item.text); setEditing(true); }}
            aria-label={t("branch.edit")}
            title={t("branch.edit")}
            className="mb-1 shrink-0 rounded-md p-1 text-muted/60 opacity-0 transition hover:text-ink focus-visible:opacity-100 group-hover/msg:opacity-100"
          >
            <GitBranch size={14} aria-hidden="true" />
          </button>
        )}
        <p className="min-w-0 whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-bubble px-4 py-2.5 text-[15px] leading-relaxed text-ink">
          {item.text}
        </p>
      </div>
      {timeStr && (
        <time
          dateTime={item.ts ?? undefined}
          title={item.ts ? new Date(item.ts).toLocaleString() : undefined}
          className="select-none px-1 text-[11px] text-muted/70 font-normal"
        >
          {timeStr}
        </time>
      )}
      {item.skills && item.skills.length > 0 && (
        <p className="max-w-[88%] break-words text-xs text-muted sm:max-w-[80%]">
          Skill: <span className="font-mono">{item.skills.map((name) => `/${name}`).join(" ")}</span>
        </p>
      )}
    </div>
  );
}

export function AssistantMessage({
  text,
  steward = false,
  speaker,
  portrait,
  usage,
  incomplete = false,
  live = false,
  ts = null,
  reaction = null,
  onReact,
  onQuote,
  disabled = false,
  threadId = null,
  workspacePath = null,
}: {
  text: string;
  steward?: boolean;
  speaker?: string;
  portrait?: React.ReactNode;
  usage?: Record<string, unknown>;
  incomplete?: boolean;
  live?: boolean;
  ts?: string | null;
  reaction?: ReactionKind | null;
  onReact?: (reaction: ReactionKind) => void;
  onQuote?: () => void;
  disabled?: boolean;
  threadId?: number | null;
  workspacePath?: string | null;
}) {
  const t = useT();
  const timeStr = formatMessageTime(ts, t);
  const reply = steward ? presentStewardReply(text) : { text, markdown: null };
  const usageSummary = turnUsageText(usage, t);
  return (
    <div className="conversation-answer group/msg max-w-full">
      <div className="conversation-byline">{portrait ?? <span aria-hidden="true" className="conversation-speaker-mark" />}{speaker || t("search.kindAgent")}</div>
      <Markdown text={reply.text} threadId={live ? null : threadId} workspacePath={live ? null : workspacePath} />
      {!live && reply.markdown !== null && (
        <button type="button" className="text-sm text-accent underline" onClick={() => downloadStewardReply(text, (speaker || "管家") + "完整回复")}>下载完整回复（.md）</button>
      )}
      {live && <span aria-hidden="true" className="animate-pulse text-muted motion-reduce:animate-none">▍</span>}
      {!live && text && (
        <div className="-ml-1 mt-1 flex flex-wrap items-center gap-1.5">
          <CopyButton text={text} />
          {onQuote && <QuoteButton onQuote={onQuote} />}
          {onReact && (
            <div className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => onReact("thumbs_up")}
                disabled={disabled}
                aria-label={t("chat.reaction.replyThumbsUp")}
                aria-pressed={reaction === "thumbs_up"}
                title={t("chat.reaction.replyThumbsUp")}
                className={cn(
                  "inline-flex size-7 cursor-pointer items-center justify-center rounded-field text-xs transition-colors",
                  reaction === "thumbs_up"
                    ? "bg-success/20 text-success font-medium"
                    : "text-muted hover:bg-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
                )}
              >
                <ThumbsUp size={14} aria-hidden="true" className={reaction === "thumbs_up" ? "fill-current" : undefined} />
              </button>
              <button
                type="button"
                onClick={() => onReact("thumbs_down")}
                disabled={disabled}
                aria-label={t("chat.reaction.replyThumbsDown")}
                aria-pressed={reaction === "thumbs_down"}
                title={t("chat.reaction.replyThumbsDown")}
                className={cn(
                  "inline-flex size-7 cursor-pointer items-center justify-center rounded-field text-xs transition-colors",
                  reaction === "thumbs_down"
                    ? "bg-danger/20 text-danger font-medium"
                    : "text-muted hover:bg-hover hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
                )}
              >
                <ThumbsDown size={14} aria-hidden="true" className={reaction === "thumbs_down" ? "fill-current" : undefined} />
              </button>
            </div>
          )}
          {timeStr && (
            <time
              dateTime={ts ?? undefined}
              title={ts ? new Date(ts).toLocaleString() : undefined}
              className="select-none px-1 text-[11px] text-muted/70 font-normal"
            >
              {timeStr}
            </time>
          )}
          {usageSummary && <span className="text-[11px] text-faint">{usageSummary}</span>}
        </div>
      )}
      {incomplete && <p className="mt-1 text-xs text-muted">This message stopped before it was finished.</p>}
    </div>
  );
}

const STEP_ICON = {
  running: { icon: <Loader2 size={13} className="animate-spin" />, color: "text-warning", label: "Running" },
  done: { icon: <Check size={13} />, color: "text-success", label: "Done" },
  error: { icon: <X size={13} />, color: "text-danger", label: "Failed" },
  // Never finished: the answer was stopped, failed or was interrupted first.
  stopped: { icon: <Square size={11} />, color: "text-faint", label: "Stopped" },
} as const;

export function ToolStep({ item }: { item: Item<"tool"> }) {
  const step = STEP_ICON[item.state];
  const input = formatValue(item.input);
  return (
    <details className="group rounded-field border border-line bg-raised/60 text-xs">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 touch:py-3 [&::-webkit-details-marker]:hidden">
        <span className={cn("inline-flex w-4 shrink-0 justify-center", step.color)} role="img" aria-label={step.label}>
          {step.icon}
        </span>
        {isComputerTool(item.name) && <ComputerBadge />}
        <span className="font-mono text-ink">{item.name}</span>
        <span className="min-w-0 flex-1 truncate text-muted">{item.summary}</span>
        <ChevronRight size={14} aria-hidden="true" className="shrink-0 text-faint transition-transform group-open:rotate-90" />
      </summary>
      <div className="space-y-2 border-t border-line p-3">
        <div>
          <p className="mb-1 text-muted">Input</p>
          <LongText value={input} />
        </div>
        {item.state === "stopped" && <p className="text-muted">This call did not finish: the answer ended first.</p>}
        {(item.state === "done" || item.state === "error") && (
          <div>
            <p className="mb-1 text-muted">
              {item.state === "error" ? "Error" : "Output"}
              {item.truncated && " (cut at the stored limit)"}
            </p>
            {item.output ? <LongText value={item.output} /> : <p className="text-muted">No output.</p>}
          </div>
        )}
      </div>
    </details>
  );
}

// Three or more tool calls in a row, folded into one line while they are done.
export function ToolRun({ tools }: { tools: Item<"tool">[] }) {
  const { total, running, failed } = toolRunSummary(tools);
  const summary = running > 0 ? `Working · ${total} tool calls so far` : `Used ${total} tools`;
  return (
    <details open={running > 0 || failed > 0} className="group rounded-card border border-line bg-raised/40">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs text-muted touch:py-3 [&::-webkit-details-marker]:hidden">
        {running > 0 ? <Loader2 size={13} aria-hidden="true" className="animate-spin text-warning" /> : <Check size={13} aria-hidden="true" className="text-success" />}
        <span className="flex-1 font-medium text-ink">{summary}</span>
        {failed > 0 && <span className="text-danger">{failed} failed</span>}
        <ChevronRight size={14} aria-hidden="true" className="shrink-0 text-faint transition-transform group-open:rotate-90" />
      </summary>
      <div className="space-y-1.5 border-t border-line p-2">
        {tools.map((tool) => (
          <ToolStep key={tool.key} item={tool} />
        ))}
      </div>
    </details>
  );
}

const APPROVAL_LABEL: Record<Item<"approval">["status"], string> = {
  pending: "approval.waiting",
  allowed: "approval.allowed",
  denied: "approval.denied",
  expired: "approval.expired",
  cancelled: "approval.cancelled",
};

// What became of "Always allow this command" on this card: the saved rule
// (null after Undo), or why it could not be saved.
export type Remembered = { ruleId: number | null; error: string | null; taskScope?: boolean };

export function ApprovalCard({
  item,
  threadId,
  answering,
  error,
  onAnswer,
  remembered,
  undoneRules = [],
  onUndo,
}: {
  item: Item<"approval">;
  threadId?: number | null;
  answering: boolean;
  error: string | null;
  onAnswer: (decision: "allow" | "deny", remember?: boolean | "task") => void;
  remembered?: Remembered;
  // Rules removed since the page opened: their cards stop offering Undo.
  undoneRules?: number[];
  // Stop always allowing: removes the saved rule; the next such command asks.
  onUndo?: (ruleId: number) => void;
}) {
  const t = useT();
  const { session } = useSession();
  // A member may be limited to some answers; a device or the desktop app is not.
  const mayAnswer = can(session?.permissions, "approve");
  const mayRemember = can(session?.permissions, "rules");
  const pending = item.status === "pending";
  // A decided card is one line; the input it was asked about is behind the arrow.
  const [open, setOpen] = useState(false);
  const delivery = item.tool === "mcp__team__deliver_project_file";
  const candidate = delivery || canAlwaysAllow(item.tool, item.input);
  const [offer, setOffer] = useState<{ id: string; employee: boolean | null } | null>(null);
  useEffect(() => {
    if (!pending || !candidate || !threadId || !mayRemember) return;
    let active = true;
    api.pendingApprovalChoices(threadId).then(rows => {
      const row = rows.find(entry => entry.id === item.approvalId);
      if (active && row) setOffer({ id: row.id, employee: row.employee_rememberable ?? null });
    }).catch(() => { /* One-time decisions remain available. */ });
    return () => { active = false; };
  }, [pending, candidate, threadId, mayRemember, item.approvalId]);
  const scoped = offer?.id === item.approvalId ? offer : null;
  const employeeGrant = scoped?.employee === true && canOpenWorkbench(session);
  const rememberable = candidate && (employeeGrant || (!delivery && scoped?.employee === null));
  const screenInput = isScreenInput(item.tool);
  const displayTool = item.tool === "Bash" ? t("approvals.command") : item.tool;
  const byRule = item.resolvedBy === "saved_rule";
  const savedRuleId = remembered?.ruleId ?? (byRule ? item.ruleId : null);
  const undone = savedRuleId != null && undoneRules.includes(savedRuleId);
  return (
    <div
      className={cn(
        "space-y-2.5 rounded-card border p-3.5 text-sm",
        pending ? "border-warning/60 bg-warning/10" : "border-line bg-raised/40 py-2.5",
      )}
    >
      {pending ? (
        <p className="text-xs font-medium text-ink">
          {isComputerTool(item.tool) && <ComputerBadge />}
          {isComputerTool(item.tool)
            ? t(screenInput ? "approval.computerRequest" : "approval.isolatedRequest")
            : t("approval.toolRequest", { tool: displayTool })} {t("approval.waitUntilAllowed")}
        </p>
      ) : (
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          title={open ? t("chat.approval.hide") : t("chat.approval.show")}
          className="flex w-full cursor-pointer items-center gap-2 text-left text-xs font-medium text-muted touch:min-h-9"
        >
          <span aria-hidden="true" className={cn("inline-flex shrink-0", item.status === "allowed" ? "text-success" : item.status === "denied" ? "text-danger" : "text-faint")}>
            {item.status === "allowed" ? <Check size={13} /> : <X size={13} />}
          </span>
          <span className="min-w-0 flex-1 truncate">
            {isComputerTool(item.tool) && <ComputerBadge />}
            <span className="font-mono">{displayTool}</span>:{" "}
            {byRule && item.status === "allowed"
              ? t("approval.allowSaved")
              : item.resolvedBy === "computer_read"
                ? t("approval.allowComputerRead")
                : item.resolvedBy === "accept_edits"
                  ? t("approval.acceptedEdits")
                  : item.resolvedBy === "plan_mode"
                    ? t("approval.planRefused")
                    : t(APPROVAL_LABEL[item.status])}
            {item.resolvedPerson && <span className="text-faint"> · by {item.resolvedPerson}</span>}
            {item.summary && <span className="font-mono text-faint"> · {item.summary}</span>}
          </span>
          <ChevronRight size={14} aria-hidden="true" className={cn("shrink-0 text-faint transition-transform", open && "rotate-90")} />
        </button>
      )}
      {(pending || open) && (
        <dl className="space-y-1.5">
          {Object.entries(item.input).map(([key, value]) => (
            <div key={key}>
              <dt className="text-xs text-muted">{key}</dt>
              <dd>
                <LongText value={formatValue(value)} />
              </dd>
            </div>
          ))}
        </dl>
      )}
      {pending && !mayAnswer && (
        <p className="text-xs text-muted">Waiting for someone who may answer commands. Ask an admin if that should be you.</p>
      )}
      {pending && mayAnswer && (
        <>
        {employeeGrant && <p className="text-xs text-muted">{t(delivery ? "approval.deliveryScope" : "approval.employeeScope")}</p>}
        <div className="flex flex-wrap gap-2">
          {employeeGrant && <Button variant="primary" disabled={answering} onClick={() => onAnswer("allow", "task")}>{t("approval.allowTask")}</Button>}
          <Button variant={employeeGrant ? undefined : "primary"} onClick={() => onAnswer("allow")} disabled={answering}>
            <ThumbsUp size={13} className="mr-1.5 inline" aria-hidden="true" />
            {employeeGrant ? t("approval.onceOnly") : screenInput ? t("approval.allowThisTurn") : t("approval.allow")}
          </Button>
          {rememberable && mayRemember && (
            <Button
              onClick={() => onAnswer("allow", true)}
              disabled={answering}
              title={t(employeeGrant ? "approval.employeeScope" : "approval.alwaysAllowHint")}
            >
              {t(employeeGrant ? "approval.rememberEmployee" : "approval.alwaysAllow")}
            </Button>
          )}
          <Button onClick={() => onAnswer("deny")} disabled={answering} title="Deny with thumbs down">
            <ThumbsDown size={13} className="mr-1.5 inline" aria-hidden="true" />
            {t("approval.deny")}
          </Button>
        </div>
        </>
      )}
      {!pending && remembered?.error && <p className="text-xs text-muted">{t("approval.allowedOnce", { error: remembered.error })}</p>}
      {!pending && undone && <p className="text-xs text-muted">{t("approval.noLongerAlwaysAllowed")}</p>}
      {!pending && savedRuleId != null && !undone && onUndo && (
        <p className="flex flex-wrap items-center gap-2 text-xs text-muted">
          {remembered?.ruleId != null ? t(remembered.taskScope ? "approval.rememberedTask" : "approval.alwaysAllowedHere") : t("approval.savedRuleAllows")}
          <button onClick={() => onUndo(savedRuleId)} className="cursor-pointer underline hover:text-ink">
            {remembered?.ruleId != null ? t("common.undo") : t("approval.stopAlwaysAllowing")}
          </button>
        </p>
      )}
      {error && <p className="text-xs text-danger">{error}</p>}
    </div>
  );
}

const END_TEXT: Record<Item<"turn_end">["status"], (item: Item<"turn_end">) => string | null> = {
  completed: () => null,
  failed: (i) => i.error || "The run failed.",
  cancelled: () => "You stopped this answer.",
  interrupted: (i) => interruptedText(i.reason, i.replay),
};

export function TurnEnd({ item, usage, steward = false }: { item: Item<"turn_end">; usage?: Record<string, unknown>; steward?: boolean }) {
  const t = useT();
  const status = steward && item.status !== "completed"
    ? presentStewardReply(stewardTurnStatus(item.status, item.error || item.reason))
    : null;
  const message = status?.text ?? END_TEXT[item.status](item);
  const summary = turnUsageText(usage, t);
  const next = !steward && item.status === "failed" ? failureNextStep(item.errorCode) : null;
  const bad = item.status === "failed";
  return (
    <>
      {message && (
        <div
          role={bad ? "alert" : undefined}
          className={cn("rounded-card border px-3.5 py-2.5 text-sm", bad ? "border-danger/50 bg-danger/10 text-ink" : "border-line text-muted")}
        >
          <p className={cn("whitespace-pre-wrap break-words", bad && "text-danger")}>{message}</p>
          {status?.markdown && <button type="button" className="text-sm text-accent underline" onClick={() => downloadStewardReply(status.markdown!, "管家完整回复")}>下载完整回复（.md）</button>}
          {next && <p className="mt-1 text-xs text-muted">{next}</p>}
        </div>
      )}
      {summary && (
        <p className="text-[11px] text-faint">
          {summary}
        </p>
      )}
    </>
  );
}

export function SessionReset({ item }: { item: Item<"session_reset"> }) {
  return (
    <p className="border-y border-dashed border-line py-2 text-center text-xs text-muted">
      {item.message ||
        "The engine's session for this conversation was reset. The messages above are shown for reference only."}
    </p>
  );
}

// Marks a call that runs in the agent's computer rather than on this machine.
function ComputerBadge() {
  return (
    <span
      className="mr-1 rounded bg-accent/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-accent"
      title="Runs in the agent's computer (an isolated container), not on this machine"
    >
      computer
    </span>
  );
}

// What recall put in front of the person's message. Always shown, so nothing
// the engine was given is hidden (docs/MEMORY.md).
export function RecalledChip({ item }: { item: Item<"recalled"> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="self-end text-xs text-muted">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="cursor-pointer rounded-full border border-line px-2.5 py-0.5 hover:bg-hover"
      >
        🧠 {recalledLabel(item.notes.length, item.conversations.length)}
      </button>
      {open && (
        <ul className="mt-1 max-w-xl space-y-1 rounded-field border border-line bg-raised p-2 text-left">
          <li className="text-muted">Background for the agent, not instructions. It grants no permission.</li>
          {item.notes.map((n, i) => (
            <li key={`n${i}`}>
              <span className="font-mono">memory/{n.name}</span>: {n.snippet}
            </li>
          ))}
          {item.conversations.map((c, i) => (
            <li key={`c${i}`}>
              <span className="font-medium">“{c.title || "untitled"}”</span>
              {c.at ? ` (${c.at.slice(0, 10)})` : ""}, {c.role === "user" ? "you said" : "the agent said"}: {c.snippet}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// The turn moved to the agent's fallback engine: always shown, never silent.
export function EngineSwitched({ item }: { item: Item<"engine_switched"> }) {
  return (
    <div role="status" className="rounded-card border border-warning/50 bg-warning/10 px-3.5 py-2.5 text-xs">
      <p className="font-medium text-ink">
        Switched engine: {item.from} → {item.to}
        {item.model ? ` (${item.model})` : ""}
      </p>
      {item.message && <p className="mt-1 whitespace-pre-wrap break-words text-muted">{item.message}</p>}
    </div>
  );
}

// Earlier messages were folded into a summary for the engine.
export function CompactedMarker({ item }: { item: Item<"compacted"> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-y border-dashed border-line py-2 text-center text-xs text-muted">
      <p>{compactedLabel(item.folded, item.writtenBy)}</p>
      {item.summary && (
        <button onClick={() => setOpen((v) => !v)} aria-expanded={open} className="mt-1 cursor-pointer underline">
          {open ? "Hide the summary" : "Show the summary"}
        </button>
      )}
      {open && <pre className="mt-2 whitespace-pre-wrap break-words text-left font-mono">{item.summary}</pre>}
    </div>
  );
}

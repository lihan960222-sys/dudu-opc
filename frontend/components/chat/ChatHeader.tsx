"use client";

import { ArrowLeft, BarChart3, CalendarDays, ChevronDown, Folder, Link2, PanelLeftOpen, MoreHorizontal, ShieldCheck, Undo2 } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import type { Agent } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useT } from "@/lib/use-i18n";
import { Avatar } from "@/components/ui/Avatar";
import { AgentImage } from "./AgentImage";
import { IconButton } from "@/components/ui/IconButton";
import { Menu, MenuItem } from "@/components/ui/Menu";
import { Select } from "@/components/ui/Select";

// The strip above a conversation: what it is (agent and title), how it runs (engine and model,
// approval mode, computer), and a few quiet icon buttons. Everything else lives in the sidebar
// or opens in a panel. On a phone the secondary buttons fold into one menu.
export function ChatHeader({
  showWorkbench,
  title,
  agent,
  onOpenSidebar,
  agentChoice,
  engine,
  computer,
  approvalMode,
  onApprovals,
  onFiles,
  onUsage,
  onToday,
  onCopyLink,
  onUndo,
  usageHint,
  trailing,
  status,
}: {
  showWorkbench: boolean;
  title: string;
  // The conversation's agent, for the avatar; null for a plain chat.
  agent: Pick<Agent, "id" | "name"> | null;
  // Present when the sidebar is a drawer: the button that opens it.
  onOpenSidebar?: () => void;
  // Only on a new chat: pick the agent it starts with.
  agentChoice?: { agents: Agent[]; value: number | null; onChange: (id: number | null) => void };
  engine?: { label: string; onClick: () => void; disabled: boolean };
  computer?: { text: string; hint: string; onClick: () => void };
  approvalMode?: { label: string; hint: string };
  onApprovals?: () => void;
  onFiles?: () => void;
  onUsage?: () => void;
  onToday?: () => void;
  // Copies a link that opens this chat or room again.
  onCopyLink?: () => void;
  // Opens the "undo this task's file changes" confirmation; absent while a task runs.
  onUndo?: () => void;
  usageHint?: string;
  // The notification bell and anything else that sits at the far right.
  trailing?: ReactNode;
  // Short text such as "Reconnecting…" or the engine's version.
  status?: { text: string; warn?: boolean } | null;
}) {
  const t = useT();
  const visibleAgent = agentChoice ? agentChoice.agents.find(item => item.id === agentChoice.value) : agent;
  type Action = { key: string; label: string; icon: ReactNode; run: () => void; hint?: string };
  const secondary: Action[] = [];
  if (onApprovals) secondary.push({ key: "approvals", label: t("header.approvals"), icon: <ShieldCheck size={18} />, run: onApprovals });
  if (onFiles) secondary.push({ key: "files", label: t("header.files"), icon: <Folder size={18} />, run: onFiles });
  if (onUsage) secondary.push({ key: "usage", label: t("header.usage"), icon: <BarChart3 size={18} />, run: onUsage, hint: usageHint });
  if (onToday) secondary.push({ key: "today", label: t("header.today"), icon: <CalendarDays size={18} />, run: onToday, hint: t("header.todayHint") });

  if (onCopyLink) secondary.push({ key: "copyLink", label: t("header.copyLink"), icon: <Link2 size={18} />, run: onCopyLink });
  if (onUndo) secondary.push({ key: "undo", label: t("header.undo"), icon: <Undo2 size={18} />, run: onUndo });

  return (
    <header className="opc-chat-header">
      <div className="opc-chat-navigation">
        {showWorkbench && (
          <Link href="/" className="opc-workbench-link">
            <ArrowLeft size={16} aria-hidden="true" />{t("portal.workbench")}
          </Link>
        )}
        {onOpenSidebar && (
          <IconButton label={t("shell.openSidebar")} onClick={onOpenSidebar}>
            <PanelLeftOpen size={18} />
          </IconButton>
        )}
      </div>
      <div className="opc-chat-identity flex min-w-0 items-center gap-2.5">
        {visibleAgent
          ? <AgentImage key={visibleAgent.id} agentId={visibleAgent.id} name={visibleAgent.name} />
          : <Avatar agentId={null} />}
        {agentChoice ? (
          <>
            <Select
              value={agentChoice.value ?? ""}
              onChange={(e) => agentChoice.onChange(e.target.value === "" ? null : Number(e.target.value))}
              aria-label={t("header.agentForChat")}
              className="h-8 min-w-0 max-w-48 cursor-pointer truncate rounded-field border border-line bg-raised px-2 text-sm font-medium touch:h-10"
            >
              <option value="">{t("header.noAgent")}</option>
              {agentChoice.agents.map((a) => (
                <option key={a.id} value={a.id}>{a.name}</option>
              ))}
            </Select>
          </>
        ) : (
          <>
            <h1 className="min-w-0 truncate text-[15px] font-semibold">
              {title}
              {agent && agent.name !== title ? <span className="block truncate text-xs font-normal text-muted">{agent.name}</span> : null}
            </h1>
          </>
        )}
      </div>

      {engine && (
        <button type="button" aria-label={`${t("header.engine")} · ${engine.label}`} disabled={engine.disabled} onClick={engine.onClick} title={t("portal.modelSettings")} className="opc-chat-model">
          <span className="truncate">{engine.label}</span><ChevronDown size={14} aria-hidden="true" />
        </button>
      )}
      <div className="opc-chat-tools">
        {status && <span role="status" className={cn("max-w-24 truncate text-xs", status.warn ? "text-warning" : "text-muted")}>{status.text}</span>}
        <div className="hidden items-center lg:flex">
          {secondary.filter(item => item.key === "approvals" || item.key === "usage").map(item => (
            <IconButton key={item.key} label={item.label} title={item.hint} onClick={item.run}>{item.icon}</IconButton>
          ))}
        </div>
        {trailing}
        {secondary.length > 0 || computer || approvalMode ? (
          <Menu
            trigger={
              <IconButton label={t("header.more")}>
                <MoreHorizontal size={19} />
              </IconButton>
            }
          >
            {approvalMode && <p title={approvalMode.hint} className="border-b border-line px-2 py-1.5 text-xs text-muted">{approvalMode.label}</p>}
            {computer && <MenuItem onSelect={computer.onClick}>{computer.text}</MenuItem>}
            {secondary.map((item) => (
              <MenuItem key={item.key} onSelect={item.run} icon={item.icon}>{item.label}</MenuItem>
            ))}
          </Menu>
        ) : null}
      </div>
    </header>
  );
}

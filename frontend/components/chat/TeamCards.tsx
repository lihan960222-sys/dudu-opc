"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { AvatarPicker, FolderField, savePicture } from "../staff/ProfileControls";
import { request } from "@/lib/api";
import { StaffModelFields } from "../staff/ProfileControls";
import { api, ApiError, type Engine } from "@/lib/api";
import { collaboration } from "@/lib/collaboration";
import { can } from "@/lib/permissions";
import type { TeamJobItem, TeamSetupOperation, TeamSetupPlan, TranscriptItem } from "@/lib/transcript";
import { JobCard } from "./TeamPanel";
import { useSession } from "@/components/AuthGate";
import { useT } from "@/lib/use-i18n";
import { AgentPortrait } from "./AgentImage";

export function DelegationCard({ item }: { item: TeamJobItem }) {
  return <JobCard job={{ id: item.jobId, root_id: null, parent_id: null, depth: 0, requester_agent_id: 0, requester_thread_id: null, assignee_agent_id: item.agentId, assignee_thread_id: item.threadId, mode: item.mode, brief: item.brief, status: item.status, created_at: new Date().toISOString(), started_at: null, finished_at: null, result: item.result, outcome: item.outcome }} agents={[]} onChanged={() => {}} />;
}
export function AgentCreatedCard({ item }: { item: Extract<TranscriptItem, { kind: "agent_created" }> }) {
  const t = useT();
  const [state, setState] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [available, setAvailable] = useState(false);
  const { session } = useSession();
  useEffect(() => {
    let live = true;
    collaboration.undoState(item.agentId).then(r => { if (live) setAvailable(r.available); }, () => { if (live) setAvailable(false); });
    return () => { live = false; };
  }, [item.agentId]);
  const undo = async () => {
    setState("busy"); setError(null);
    try { const result = await collaboration.undoCreate(item.agentId); setState(result.removed); }
    catch (err) { setState(""); setError(err instanceof ApiError ? err.message : "Could not undo this specialist."); }
  };
  return <article className="space-y-2 rounded-field border border-line p-3 text-sm"><p><strong>{t("receipt.newEmployee", { name: item.name || t("receipt.unknownEmployee", {id:item.agentId}) })}</strong>{item.title ? ` · ${item.title}` : ""}</p>{session?.scopes.includes("admin") && available && !state && <button type="button" className="text-xs underline" onClick={() => void undo()}>{t("common.undo")}</button>}{state && <p role="status" className="text-xs text-muted">{state === "busy" ? t("receipt.undoing") : t("receipt.undone")}</p>}{error && <p role="alert" className="text-xs text-danger">{error}</p>}</article>;
}

const STALE: Record<string, string> = {
  changed_since: "Something changed since this was applied, so it was left alone.",
  agent_used: "A specialist this added has already been used, so it was kept.",
  agent_busy: "A teammate is working right now. Try again when it is idle.",
};
const receiptFields = new Set(["name", "title", "description", "instructions", "model", "provider", "first_task", "workspace_path"]);

function ProposalField({ label, value, onChange, multiline = false, disabled = false, current }: { label: string; value: string; onChange: (value: string) => void; multiline?: boolean; disabled?: boolean; current?: string | null }) {
  return <label className="block text-xs font-medium">{label}{current !== undefined && <span className="ml-2 font-normal text-muted">Current: {current ?? "Default / empty"}</span>}{multiline
    ? <textarea disabled={disabled} value={value} onChange={event => onChange(event.target.value)} rows={3} className="mt-1 w-full rounded-field border border-line bg-surface px-2 py-1 text-sm disabled:opacity-70" />
    : <input disabled={disabled} value={value} onChange={event => onChange(event.target.value)} className="mt-1 w-full rounded-field border border-line bg-surface px-2 py-1 text-sm disabled:opacity-70" />}</label>;
}

function TeamSetupProposal({ item, onDecided }: { item: Extract<TranscriptItem, { kind: "team_setup" }>; onDecided?:()=>void }) {
  const { session } = useSession();
  const [plan, setPlan] = useState<TeamSetupPlan>(item.plan ?? { operations: [] });
  const [engines, setEngines] = useState<Engine[]>([]);
  useEffect(() => { api.getEngines().then(setEngines).catch(() => setEngines([])); }, []);
  const [pictures,setPictures]=useState<Record<number,string>>({});
  const [savedPlan, setSavedPlan] = useState<TeamSetupPlan>(item.plan ?? { operations: [] });
  const [version, setVersion] = useState(item.version ?? 0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState<string | null>(null);
  const canReview = session !== null && session !== undefined && (session.scopes.includes("admin") || (session.scopes.includes("manage") && can(session.permissions, "agents")));
  const isAdmin = session?.scopes.includes("admin") === true;
  const locked = item.state === "rejected" || message === "approved" || message === "declined";
  const needsAdmin = !isAdmin && plan.operations.some(operation => operation.op === "create" && (operation.share_workspace === true || !!operation.workspace_path));
  const dirty = JSON.stringify(plan) !== JSON.stringify(savedPlan);
  const stale = (err: unknown) => err instanceof ApiError && ["changed_since", "request_not_found", "lead_only"].includes(err.code ?? "");

  useEffect(() => {
    const current = item.plan ?? { operations: [] };
    // Resets the editable copy when a newer version of the plan arrives.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPlan(current); setSavedPlan(current); setVersion(item.version ?? 0);
  }, [item.version, item.plan]);
  const changeOperation = (index: number, update: (operation: TeamSetupOperation) => TeamSetupOperation) => {
    setPlan(current => ({ ...current, operations: current.operations.map((operation, i) => i === index ? update(operation) : operation) }));
    setError(null);
  };
  const save = async () => {
    if (!canReview || busy || locked) return;
    setBusy(true); setError(null); setMessage("");
    try { const result = await collaboration.updateSetupProposal(item.requestId, version, plan); setVersion(result.version); setPlan(result.plan); setSavedPlan(result.plan); setMessage("Saved revision."); }
    catch (err) { setError(stale(err) ? "This proposal changed or closed elsewhere. Refresh the conversation before editing again." : err instanceof ApiError ? err.message : "Could not save this revision."); }
    finally { setBusy(false); }
  };
  const decide = async (decision: "approve" | "decline") => {
    if (!canReview || busy || locked) return;
    setBusy(true); setError(null); setMessage("");
    try {
      if (decision === "approve") {
        let decisionVersion = version;
        if (dirty) {
          const saved = await collaboration.updateSetupProposal(item.requestId, version, plan);
          decisionVersion = saved.version;
          setVersion(saved.version); setPlan(saved.plan); setSavedPlan(saved.plan);
        }
        await collaboration.approveSetupProposal(item.requestId, decisionVersion); setMessage("approved");
        if(Object.keys(pictures).length) {
          try {
            const receipt=await request<{results:{action:string;agent_id:number}[]}>(`/team/setup/${encodeURIComponent(item.requestId)}`);
            for(const [index,data] of Object.entries(pictures)) {
              const row=receipt.results[Number(index)];
              if(row?.action==="created") await savePicture(row.agent_id,data);
            }
          } catch { setError("入职已完成。头像保存失败，可在员工档案中重新上传。"); return; }
        }
        onDecided?.();
      }
      else { await collaboration.declineSetupProposal(item.requestId, version); setMessage("declined"); onDecided?.(); }
    } catch (err) { setError(stale(err) ? "This proposal changed or closed elsewhere. Refresh the conversation before deciding." : err instanceof ApiError ? err.message : "Could not update this proposal."); }
    finally { setBusy(false); }
  };

  return <article className="space-y-3 rounded-field border border-line p-3 text-sm">
    <div><strong>{item.state === "rejected" || message === "declined" ? "已拒绝入职方案" : message === "approved" ? "已确认入职方案" : "待确认的员工方案"}</strong><p className="mt-1 text-xs text-muted">确认岗位、模型和工作目录后即可入职。</p></div>
    {!locked && <ProposalField label="方案摘要" value={plan.summary ?? ""} disabled={!canReview || busy} onChange={summary => setPlan(current => ({ ...current, summary }))} multiline />}
    {!canReview && plan.summary && <p className="text-sm">{plan.summary}</p>}
    {!locked ? <>
      <ol className="space-y-3">{plan.operations.map((operation, index) => <li key={`${operation.op}-${index}`} className="rounded-field border border-line p-3">
        <div className="mb-2 flex items-center justify-between"><strong>{operation.op === "create" ? `新员工 ${index + 1}` : `更新 ${operation.name || `员工 #${operation.agent_id}`}`}</strong>{canReview && plan.operations.length > 1 && <button type="button" className="text-xs underline" disabled={busy} onClick={() => {setPictures(previous=>Object.fromEntries(Object.entries(previous).filter(([i])=>Number(i)!==index).map(([i,v])=>[Number(i)>index?Number(i)-1:Number(i),v])));setPlan(current => ({ ...current, operations: current.operations.filter((_, i) => i !== index) }));}}>移除</button>}</div>
        {operation.op === "create" ? <div className="space-y-3">
          {(["name", "title", "description", "instructions", "first_task"] as const).map(field =>
            <ProposalField key={field} label={({name:"员工名称", title:"岗位", description:"职责简介 · 供管家判断何时派给这名员工", instructions:"工作指令 · 员工执行任务时遵循的规则", first_task:"首项任务草稿"})[field]}
              value={operation[field] ?? ""} disabled={!canReview || busy} multiline={field !== "name" && field !== "title"}
              onChange={value => changeOperation(index, current => current.op === "create" ? { ...current, [field]: value } : current)} />)}
          <StaffModelFields engines={engines} provider={operation.provider ?? "codex"} model={operation.model ?? null}
            disabled={!canReview || busy}
            onChange={(provider, model) => changeOperation(index, current => current.op === "create" ? {...current, provider, model} : current)} />
          {isAdmin&&<div><span className="text-xs text-muted">员工头像 · 确认入职时保存</span>{pictures[index]&&<AgentPortrait src={pictures[index]} agentId={null} size="lg"/>}<AvatarPicker onChange={data=>setPictures(prev=>({...prev,[index]:data}))}/></div>}
          <FolderField value={operation.workspace_path??""} disabled={!isAdmin||busy||operation.share_workspace}
            onChange={value=>changeOperation(index,current=>current.op==="create"?{...current,workspace_path:value||undefined}:current)}/>
          <label className="flex items-center gap-2 text-xs"><input type="checkbox" disabled={!canReview || busy || !!operation.workspace_path || (!isAdmin && !operation.share_workspace)}
            checked={operation.share_workspace === true} onChange={event => changeOperation(index, current => current.op === "create" ? {...current, share_workspace:event.target.checked} : current)} />共享管家的工作目录</label>
        </div>
          : <div className="space-y-2">{Object.entries(operation.fields).map(([field, value]) => <ProposalField key={field} label={field.replaceAll("_", " ")} value={value ?? ""} current={operation.current?.[field]} disabled={!canReview || busy} multiline={field === "instructions" || field === "description"} onChange={next => changeOperation(index, current => current.op === "update" ? { ...current, fields: { ...current.fields, [field]: field === "model" && next === "" ? null : next } } : current)} />)}<p className="text-xs text-muted">Revision {operation.expected_revision}</p></div>}
      </li>)}</ol>
      {needsAdmin && <p className="text-xs text-muted">An admin must approve a plan that shares the lead’s workspace.</p>}
      {canReview && <div className="flex flex-wrap gap-3"><button type="button" className="text-xs underline" disabled={busy || plan.operations.length < 1 || !dirty} onClick={() => void save()}>{busy ? "保存中…" : "保存草稿"}</button><button type="button" className="text-xs underline" disabled={busy || needsAdmin || plan.operations.length < 1} onClick={() => void decide("approve")}>{busy ? "处理中…" : dirty ? "保存并确认" : "确认方案"}</button><button type="button" className="text-xs underline" disabled={busy} onClick={() => void decide("decline")}>拒绝</button></div>}
    </> : <p className="text-xs text-muted">{item.state === "rejected" || message === "declined" ? "方案已关闭。" : "已应用审批通过的方案。"}</p>}
    {message === "Saved revision." && <p role="status" className="text-xs text-muted">已保存第 {version} 版。</p>}{error && <p role="alert" className="text-xs text-danger">{error}</p>}
  </article>;
}

export function TeamSetupCard({ item, onDecided }: { item: Extract<TranscriptItem, { kind: "team_setup" }>; onDecided?:()=>void }) {
  return item.state === "pending" || item.state === "rejected"
    ? <TeamSetupProposal item={item} onDecided={onDecided} />
    : <AppliedTeamSetupCard item={item} />;
}

function AppliedTeamSetupCard({ item }: { item: Extract<TranscriptItem, { kind: "team_setup" }> }) {
  const t = useT();
  const [state, setState] = useState("");
  const [error, setError] = useState<string | null>(null);
  const { session } = useSession();
  const [availability, setAvailability] = useState<{ available: boolean; reason: string | null } | null>(null);
  useEffect(() => {
    if (!session?.scopes.includes("admin")) return;
    let live = true;
    const refresh = () => collaboration.setupReceipt(item.requestId).then(result => {
      if (live) setAvailability({ available: result.undo_available, reason: result.undo_reason });
    }, () => { if (live) setAvailability({ available: false, reason: "Could not check Undo availability." }); });
    void refresh();
    const timer = setInterval(() => { if (!document.hidden) void refresh(); }, 5000);
    window.addEventListener("focus", refresh);
    return () => { live = false; clearInterval(timer); window.removeEventListener("focus", refresh); };
  }, [item.requestId, session]);
  const undo = async () => {
    setState("busy"); setError(null);
    try { await collaboration.undoSetup(item.requestId); setState("undone"); }
    catch (err) { setState(""); setError(err instanceof ApiError ? STALE[err.code ?? ""] ?? err.message : "Could not undo this change."); }
  };
  const canUndo = availability?.available === true && !state && session?.scopes.includes("admin");
  return (
    <article className="space-y-2 rounded-field border border-line p-3 text-sm">
      <p><strong>{t("receipt.applied")}</strong></p>
      <ul className="space-y-2 text-xs text-muted">{item.rows.map((row) => <li key={`${row.action}-${row.agentId}`}>
        {t(`receipt.${row.action}`)} · {row.name || t("receipt.unknownEmployee", {id:row.agentId})}
        {row.changes && <details className="mt-1"><summary className="cursor-pointer underline">{t("receipt.details")}</summary><dl className="mt-2 space-y-2">{row.changes.map(change => <div key={change.field}><dt className="font-medium">{receiptFields.has(change.field) ? t(`receipt.${change.field}`) : change.field.replaceAll("_", " ")}</dt><dd className="whitespace-pre-wrap break-words"><del>{change.before == null ? t("receipt.empty") : String(change.before)}</del>{" → "}{change.after == null ? t("receipt.empty") : String(change.after)}</dd></div>)}</dl><p className="mt-2">{t("agentTeam.teamSavedDefaults")}</p></details>}
        {row.action !== "deleted" && <div className="mt-1 flex flex-wrap gap-3"><button type="button" className="underline" onClick={() => window.dispatchEvent(new CustomEvent("suparagent:open-agent", { detail: { id: row.agentId, edit: false } }))}>{t("receipt.start")}</button>{row.firstTask && <button type="button" className="underline" onClick={() => window.dispatchEvent(new CustomEvent("suparagent:open-agent", { detail: { id: row.agentId, edit: false, firstTask: row.firstTask } }))}>{t("receipt.first")}</button>}{session?.scopes.includes("admin") && <button type="button" className="underline" onClick={() => window.dispatchEvent(new CustomEvent("suparagent:open-agent", { detail: { id: row.agentId, edit: true } }))}>{t("employee.profile")}</button>}</div>}
      </li>)}</ul>
      {item.undoAvailable && <p className="text-xs text-muted">{t("agentTeam.undoCaveat")}</p>}
      {availability && !availability.available && !state && <p className="text-xs text-muted">{availability.reason}</p>}
      {canUndo && <button type="button" className="text-xs underline" onClick={() => void undo()}>{t("common.undo")}</button>}
      {state && <p role="status" className="text-xs text-muted">{state === "busy" ? t("receipt.undoing") : t("receipt.undone")}</p>}
      {error && <p role="alert" className="text-xs text-danger">{error}</p>}
    </article>
  );
}

const NOTICE_LABEL = { incident: "Incident", escalation: "Escalation", refused: "Report not delivered", retried: "Retry" } as const;

// What the lead was told about a teammate (or what it retried). The text is
// quoted data from other agents; it is only ever shown, never acted on here.
export function TeamNoticeCard({ item }: { item: Extract<TranscriptItem, { kind: "team_notice" }> }) {
  return (
    <article className="flex items-start justify-between gap-3 rounded-field border border-line p-3 text-sm">
      <p className="min-w-0 break-words"><strong>{NOTICE_LABEL[item.tone]}</strong>{" · "}{item.text.replace(/^(Incident|Escalation): /, "")}</p>
      {item.threadId !== null && <Link href={`/chat?thread=${item.threadId}`} className="shrink-0 text-xs underline">Open</Link>}
    </article>
  );
}

"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { workspaceHref, type WorkspaceSection } from "@/lib/workspace-navigation";
import { useT } from "@/lib/use-i18n";
import { teamActivityLabel } from "@/lib/team-employee";
import { EmployeeAdvancedTools, EmployeeCreationSources } from "../chat/EmployeeAdvancedTools";
import { employeeChatHref } from "@/lib/sidebar";
import { useCallback, useEffect, useRef, useState } from "react";
import { Activity, ChevronRight, Monitor, Plus } from "lucide-react";
import { api, request, type Agent, type Engine, type Thread } from "@/lib/api";
import {
  collaboration,
  type TeamState,
  type TeamJob,
} from "@/lib/collaboration";
import { planner, type WorkBoard, type WorkItem } from "@/lib/planner-api";
import type { TeamSetupPlan } from "@/lib/transcript";
import { TeamSetupCard } from "../chat/TeamCards";
import { StaffEditor } from "./StaffEditor";
import { SharedLibraryReviews } from "./SharedLibrary";
import { HistoryReset } from "./HistoryReset";
import { useSession } from "../AuthGate";
import { canManageProjectTerminal } from "@/lib/directory-mode";
import { ReportingBoard } from "./ReportingBoard";
import { Face } from "./ProfileControls";
import { FEISHU_RETURN_EMPLOYEE, feishuEntry } from "@/lib/feishu-employee";
import "./workbench.css";

type ManagementSection = Exclude<WorkspaceSection, "chat">;
type Pending = {
  requestId: string;
  version: number;
  plan: TeamSetupPlan;
  threadId: number | null;
  turnId: number | null;
};
type Suggestion = {
  id: number;
  revision: number;
  state: string;
  title: string;
  detail: string;
  task_id: number | null;
  options: { id: string; label: string }[];
};
function Suggestions() {
  const [pendingItems, setPendingItems] = useState<Suggestion[]>([]),
    [laterItems, setLaterItems] = useState<Suggestion[]>([]),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [report, setReport] = useState("");
  const [deferred, setDeferred] = useState(false);
  const items = deferred ? laterItems : pendingItems;
  const generation = useRef<symbol | null>(null);
  const [followup, setFollowup] = useState<number | null>(null);
  const load = useCallback(() => {
    const current = Symbol();
    generation.current = current;
    return Promise.allSettled([
      request<{items: Suggestion[]}>("/steward/suggestions"),
      request<{items: Suggestion[]}>("/steward/suggestions?state=deferred"),
    ]).then(([pending, later]) => {
      if (generation.current !== current) return;
      if (pending.status === "fulfilled") setPendingItems(pending.value.items);
      if (later.status === "fulfilled") setLaterItems(later.value.items);
      setError([pending, later].flatMap(result => result.status === "rejected" ? [String(result.reason)] : []).join("。"));
    });
  }, []);
  useEffect(() => {
    void load();
    return () => { generation.current = null; };
  }, [load]);
  async function decide(s: Suggestion, choice: string, option?: string) {
    setBusy(true);
    setError("");
    try {
      const r = await request<{
        report: string;
        followup_task_id: number | null;
      }>(`/steward/suggestions/${s.id}/decision`, {
        method: "POST",
        body: JSON.stringify({ choice, option, revision: s.revision }),
      });
      setReport(r.report || "已处理");
      setFollowup(r.followup_task_id);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      {(pendingItems.length > 0 || laterItems.length > 0 || deferred) && <div className="dp-suggestions-head">
        <span>{deferred ? "稍后处理" : items.length ? "待确认建议" : ""}</span>
        {(laterItems.length > 0 || deferred) && <button
          className="dp-link"
          onClick={() => setDeferred((d) => !d)}
        >
          {deferred ? "返回待确认建议" : "查看稍后处理的建议"}
        </button>}
      </div>}
      {deferred && items.length === 0 && <small>暂无稍后处理的建议。</small>}
      {items.map((s) => (
        <article className="dp-feature" key={s.id}>
          <div className="dp-legend">
            管家建议
            {s.task_id && (
              <Link href={`/chat?view=planner&task=${s.task_id}`}>
                任务 #{s.task_id}
              </Link>
            )}
          </div>
          <h3>{s.title}</h3>
          <p>{s.detail}</p>
          <div className="dp-actions">
            {s.options.map((o) => (
              <button
                className="dp-button primary"
                disabled={busy}
                key={o.id}
                onClick={() => void decide(s, "approve", o.id)}
              >
                {o.label}
              </button>
            ))}
            {!deferred && (
              <button
                className="dp-link"
                disabled={busy}
                onClick={() => void decide(s, "defer")}
              >
                稍后
              </button>
            )}
            <button
              className="dp-link"
              disabled={busy}
              onClick={() => void decide(s, "dismiss")}
            >
              忽略
            </button>
          </div>
        </article>
      ))}
      {report && (
        <p className="dp-notice dp-wrap" role="status">
          {report}{" "}
          {followup && (
            <Link
              className="dp-link"
              href={`/chat?view=planner&task=${followup}`}
            >
              查看跟进任务 #{followup}
            </Link>
          )}
        </p>
      )}
      {error && (
        <p className="dp-error" role="alert">
          读取或处理管家建议失败。{error}
        </p>
      )}
    </>
  );
}
type Protocol = {
  project: string;
  task_id: number;
  revision: number;
  from: number | null;
  to: number | null;
  objective: string;
  priority: string;
  status: string;
  dependencies: number[];
  deliverables: string[];
  acceptance_criteria: string[];
  version: string;
  thread_id: number | null;
};
function Requirements({
  record,
  onSaved,
}: {
  record: Protocol;
  onSaved: (record: Protocol) => void;
}) {
  const [open, setOpen] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const [priority, setPriority] = useState(record.priority),
    [dependencies, setDependencies] = useState(
      (record.dependencies ?? []).join(", "),
    );
  const [deliverables, setDeliverables] = useState(
      (record.deliverables ?? []).join("\n"),
    ),
    [criteria, setCriteria] = useState(
      (record.acceptance_criteria ?? []).join("\n"),
    );
  async function save() {
    setBusy(true);
    setError("");
    try {
      const ids = dependencies.trim()
        ? dependencies.split(/[,，\s]+/).map(Number)
        : [];
      if (ids.some((id) => !Number.isSafeInteger(id) || id < 1))
        throw Error("依赖项请填写任务编号");
      const lines = (v: string) =>
        v
          .split("\n")
          .map((line) => line.trim())
          .filter(Boolean);
      const path = `/team/tasks/${record.task_id}/protocol`;
      await request(path, {
        method: "PATCH",
        body: JSON.stringify({
          revision: record.revision,
          priority,
          dependencies: ids,
          deliverables: lines(deliverables),
          acceptance_criteria: lines(criteria),
        }),
      });
      onSaved(await request<Protocol>(path));
      setOpen(false);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }
  return open ? (
    <div className="dp-fields">
      <label className="dp-field">
        优先级
        <select
          className="dp-select"
          value={priority}
          onChange={(e) => setPriority(e.target.value)}
        >
          {[
            ["low", "低"],
            ["normal", "普通"],
            ["high", "高"],
            ["urgent", "紧急"],
          ].map(([v, t]) => (
            <option value={v} key={v}>
              {t}
            </option>
          ))}
        </select>
      </label>
      <label className="dp-field">
        依赖任务编号
        <input
          className="dp-input"
          value={dependencies}
          onChange={(e) => setDependencies(e.target.value)}
        />
      </label>
      <label className="dp-field">
        交付物 · 每行一项
        <textarea
          className="dp-textarea"
          value={deliverables}
          onChange={(e) => setDeliverables(e.target.value)}
        />
      </label>
      <label className="dp-field">
        验收标准 · 每行一项
        <textarea
          className="dp-textarea"
          value={criteria}
          onChange={(e) => setCriteria(e.target.value)}
        />
      </label>
      <div className="dp-actions">
        <button
          className="dp-button primary"
          disabled={busy}
          onClick={() => void save()}
        >
          保存要求
        </button>
        <button
          className="dp-link"
          disabled={busy}
          onClick={() => setOpen(false)}
        >
          关闭
        </button>
      </div>
      {error && (
        <p role="alert" className="dp-error">
          {error}
        </p>
      )}
    </div>
  ) : (
    <button className="dp-link" onClick={() => setOpen(true)}>
      编辑任务要求
    </button>
  );
}
function formatFields(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(formatFields).join("、");
  if (value && typeof value === "object")
    return Object.entries(value)
      .map(([k, v]) => k + " · " + formatFields(v))
      .join("\n");
  return value == null ? "" : String(value);
}
function Projects({ staff }: { staff: Agent[] }) {
  const [board, setBoard] = useState<WorkBoard | null>(null),
    [items, setItems] = useState<WorkItem[]>([]),
    [active, setActive] = useState<WorkItem | null>(null),
    [error, setError] = useState("");
  const [projects, setProjects] = useState<Record<number, string>>({}),
    [project, setProject] = useState("");
  const [protocol, setProtocol] = useState<Protocol | null>(null),
    [jobs, setJobs] = useState<TeamJob[]>([]),
    [detailError, setDetailError] = useState("");
  const selectedId = useRef<number | null>(null);
  function selectTask(task: WorkItem | null) {
    if (selectedId.current === (task?.id ?? null)) return;
    selectedId.current = task?.id ?? null;
    setProtocol(null);
    setJobs([]);
    setDetailError("");
    setActive(task);
  }
  useEffect(() => {
    let live = true;
    planner
      .board()
      .then(async (b) => {
        const [rows, index] = await Promise.all([
          planner.items(b.id),
          request<{ task_id: number; project: string }[]>(
            "/staff/projects/index",
          ),
        ]);
        if (live) {
          setProjects(
            Object.fromEntries(index.map((i) => [i.task_id, i.project])),
          );
          setBoard(b);
          setItems(rows);
          selectTask(rows[0] ?? null);
        }
      })
      .catch((e) => {
        if (live) setError(String(e));
      });
    return () => {
      live = false;
    };
  }, []);
  useEffect(() => {
    if (!active) return;
    let live = true;
    const current = () => live && selectedId.current === active.id;
    request<Protocol>(`/team/tasks/${active.id}/protocol`)
      .then((p) => {
        if (current()) setProtocol(p);
      })
      .catch((e) => {
        if (current() && e.status !== 404) setDetailError(String(e));
      });
    request<TeamJob[]>(`/team/jobs?task_id=${active.id}`)
      .then((j) => {
        if (current()) setJobs(j);
      })
      .catch((e) => {
        if (current()) setDetailError(String(e));
      });
    return () => {
      live = false;
    };
  }, [active]);
  return (
    <>
      <section className="dp-boardhead">
        <h2>{board?.name ?? "任务板"}</h2>
        <p>{board ? `共 ${items.length} 项任务` : "正在读取任务…"}</p>
      </section>
      {error && <p role="alert">{error}</p>}
      <div className="dp-tabs" aria-label="项目筛选">
        {[
          "",
          ...Array.from(
            new Set(items.map((i) => projects[i.id] || "未归入项目")),
          ),
        ].map((group) => (
          <button
            key={group}
            aria-pressed={project === group}
            onClick={() => {
              setProject(group);
              selectTask(
                items.find(
                  (i) => !group || (projects[i.id] || "未归入项目") === group,
                ) ?? null,
              );
            }}
          >
            {group || "全部项目"}
          </button>
        ))}
      </div>
      <div className="dp-columns dp-projects">
        <div className="dp-list">
          {board && !items.length && <p className="dp-empty">暂无任务。<Link href="/chat?view=planner" className="dp-link">打开任务板</Link></p>}
          {items
            .filter(
              (i) => !project || (projects[i.id] || "未归入项目") === project,
            )
            .map((i) => (
              <button
                className="dp-listrow"
                key={i.id}
                aria-pressed={active?.id === i.id}
                onClick={() => selectTask(i)}
              >
                <div>
                  <strong>{i.title}</strong>
                  <small>
                    {staff.find((a) => a.id === i.agent_id)?.name ?? "待分配"}
                  </small>
                </div>
                <span className="dp-tag">
                  {board?.columns.find((c) => c.id === i.column_id)?.name}
                </span>
              </button>
            ))}
        </div>
        {active && (
          <aside className="dp-aside dp-task-detail">
            <h3>{active.title}</h3>
            {protocol?.project && <p>{protocol.project}</p>}
            <p className="dp-wrap">{active.description}</p>
            {protocol &&
              [
                ["目标", protocol.objective],
                ["版本", protocol.version],
                ["依赖", protocol.dependencies],
                ["交付物", protocol.deliverables],
                ["验收标准", protocol.acceptance_criteria],
              ].map(
                ([label, value]) =>
                  formatFields(value) && (
                    <p className="dp-wrap" key={String(label)}>
                      <strong>{String(label)} · </strong>
                      {formatFields(value)}
                    </p>
                  ),
              )}
            {protocol?.revision && (
              <Requirements
                key={protocol.task_id + "-" + protocol.revision}
                record={protocol}
                onSaved={(r) =>
                  setProtocol((current) =>
                    current?.task_id === r.task_id ? r : current,
                  )
                }
              />
            )}
            {jobs.length > 0 && (
              <>
                <hr />
                <h3>协作记录</h3>
                {jobs.map((j) => (
                  <p key={j.id} className="dp-wrap">
                    {staff.find((a) => a.id === j.requester_agent_id)?.name ??
                      j.requester_agent_id}{" "}
                    →{" "}
                    {staff.find((a) => a.id === j.assignee_agent_id)?.name ??
                      j.assignee_agent_id}{" "}
                    · {j.mode} · {j.status}
                    <br />
                    {j.brief}
                    {j.assignee_thread_id && <Link className="dp-link" href={`/chat/?thread=${j.assignee_thread_id}`}>查看执行会话</Link>}
                    {j.result && (
                      <>
                        <br />
                        {j.result}
                      </>
                    )}
                  </p>
                ))}
              </>
            )}
            {detailError && <p className="dp-error">{detailError}</p>}
            <Link
              href={`/chat?view=planner&task=${active.id}`}
              className="dp-link"
            >
              打开任务
            </Link>
          </aside>
        )}
      </div>
    </>
  );
}

function loadPortal(page: ManagementSection) {
  const people = page === "people";
  return Promise.allSettled([
    page === "host" ? Promise.resolve<Agent[]>([]) : api.listAgents(people),
    people ? collaboration.team() : Promise.resolve(null),
    people || page === "host" ? api.getEngines() : Promise.resolve<Engine[]>([]),
    people ? request<Pending[]>("/staff/pending") : Promise.resolve<Pending[]>([]),
    page === "activity" ? collaboration.activity() : Promise.resolve([]),
    people ? api.listThreads(false, true) : Promise.resolve<Thread[]>([]),
  ]);
}

export function Workbench({ page, onChanged }: { page: ManagementSection; onChanged: () => void }) {
  const { session } = useSession();
  const router = useRouter();
  const params = useSearchParams();
  const t = useT();
  const entry = (() => {
    let employee = null;
    if (typeof window !== "undefined" && params.has("feishu_oauth")) {
      try { employee = sessionStorage.getItem(FEISHU_RETURN_EMPLOYEE); } catch { /* use lead */ }
    }
    return feishuEntry(params, typeof window === "undefined" ? "" : window.location.hash, employee);
  })();
  const [agents, setAgents] = useState<Agent[]>([]),
    [threads, setThreads] = useState<Thread[] | null>(null),
    [engines, setEngines] = useState<Engine[]>([]),
    [team, setTeam] = useState<TeamState | null>(null);
  const [pending, setPending] = useState<Pending[]>([]),
    [activity, setActivity] = useState<
      Awaited<ReturnType<typeof collaboration.activity>>
    >([]),
    [showArchived, setShowArchived] = useState(false),
    [restoring, setRestoring] = useState(false),
    [rev, setRev] = useState(0),
    [loadedPage, setLoadedPage] = useState<ManagementSection | null>(null),
    [errors, setErrors] = useState<string[]>([]);
  const applyPortal = useCallback(([a, t, e, p, log, chats]: Awaited<ReturnType<typeof loadPortal>>) => {
    if (a.status === "fulfilled") setAgents(a.value);
    if (t.status === "fulfilled") setTeam(t.value);
    if (e.status === "fulfilled") setEngines(e.value);
    if (p.status === "fulfilled") setPending(p.value);
    if (log.status === "fulfilled") setActivity(log.value);
    if (chats.status === "fulfilled") setThreads(chats.value);
    setErrors(
      [a, t, e, p, log, chats].flatMap((r, i) =>
        r.status === "rejected"
          ? [
              ["员工", "团队", "引擎", "待审批档案", "运行记录", "最近对话"][i] +
                " · " +
                String(r.reason),
            ]
          : [],
      ),
    );
    setLoadedPage(page);
  }, [page]);
  const loaded = loadedPage === page;
  const sequence = useRef<symbol | null>(null);
  const refresh = useCallback(() => {
    const requestId = Symbol();
    sequence.current = requestId;
    return loadPortal(page).then(result => {
      if (requestId === sequence.current) applyPortal(result);
    });
  }, [applyPortal, page]);
  useEffect(() => {
    void refresh();
    return () => { sequence.current = null; };
  }, [refresh]);
  const lead = agents.find((a) => a.id === team?.team?.lead_agent_id) ?? null;
  const activeAgents = agents.filter(agent => !agent.archived_at);
  const archived = agents.filter(agent => agent.archived_at);
  const ordered = lead && !lead.archived_at ? [lead, ...activeAgents.filter(agent => agent.id !== lead.id)] : activeAgents;
  const members = team?.agents.filter(a => a.state !== "outside").map(a => a.id) ?? [];
  const initialEmployee = members.includes(Number(entry.employee)) ? entry.employee : lead ? String(lead.id) : "";
  const requested = params.get("employee");
  const named = agents.find(agent => String(agent.id) === requested);
  const pendingProfile = pending.find(item => item.requestId === requested);
  const key = (entry.requested && initialEmployee) || (named && String(named.id)) || pendingProfile?.requestId ||
    (requested === "new" ? "new" : ordered[0] ? String(ordered[0].id) : pending[0]?.requestId ?? "new");
  const chosen = agents.find(agent => String(agent.id) === key) ?? null;
  const proposal = pending.find(item => item.requestId === key);
  const setActive = (id: string, advanced = false, replace = false) =>
    router[replace ? "replace" : "push"](workspaceHref(params.toString(), "people", id, advanced), { scroll: false });
  async function teamChanged() {
    await refresh();
    onChanged();
  }
  async function restore() {
    if (!chosen) return;
    setRestoring(true);
    try {
      await api.updateAgent(chosen.id, { archived: false });
      await teamChanged();
    } catch (error) {
      setErrors([String(error)]);
    } finally {
      setRestoring(false);
    }
  }
  return (
    <div id="workbench">
        <main className="dp-main">
          <header className="dp-pagehead">
            <div>
              <h1>{t(`workspace.${page}`)}</h1>
              {loaded && page === "people" && <p className="dp-headnote">{activeAgents.length} 位员工{pending.length > 0 && ` · ${pending.length} 份待审批档案`}</p>}
            </div>
            {page === "people" && (
              <button
                className="dp-button primary"
                onClick={() => setActive("new")}
              >
                <Plus />
                新建员工
              </button>
            )}
          </header>
          {!!errors.length && (
            <div role="alert" className="dp-error">
              {errors.join("；")}{" "}
              <button className="dp-link" onClick={() => void refresh()}>
                重新读取
              </button>
            </div>
          )}
          {page === "people" && (
            <>
              <ReportingBoard openProfile={id => { setActive(id); void refresh(); }} />
              <div className="dp-directory">
              <nav className="dp-roster" aria-label="员工名册">
                {ordered.map((a) => (
                  <button
                    key={a.id}
                    className="dp-person"
                    aria-pressed={key === String(a.id)}
                    onClick={() => setActive(String(a.id))}
                  >
                    <Face id={a.id} name={a.name} rev={rev} />
                    <span>
                      <strong>{a.name}</strong>
                      <small>
                        {a.id === lead?.id ? "大管家" : a.title || "员工"}
                      </small>
                    </span>
                  </button>
                ))}
                {archived.length > 0 && <button type="button" className="dp-link dp-archived" aria-expanded={showArchived || !!chosen?.archived_at} onClick={() => {
                  if (chosen?.archived_at) setActive("");
                  setShowArchived(!showArchived && !chosen?.archived_at);
                }}>已归档（{archived.length}）</button>}
                {(showArchived || !!chosen?.archived_at) && archived.map(a => (
                  <button key={a.id} className="dp-person" aria-pressed={String(a.id) === key} onClick={() => setActive(String(a.id))}>
                    <Face id={a.id} name={a.name} rev={rev} /><span><strong>{a.name}</strong><small>已归档</small></span>
                  </button>
                ))}
                {pending.map((p) => (
                  <button
                    className="dp-person"
                    aria-pressed={key === p.requestId}
                    key={p.requestId}
                    onClick={() => setActive(p.requestId)}
                  >
                    <Face />
                    <span>
                      <strong>
                        {p.plan.operations[0]?.name || "员工方案"}
                      </strong>
                      <small>待审批</small>
                    </span>
                  </button>
                ))}
              </nav>
              <section className="dp-record" aria-label="员工档案">
              {loaded && key === "new" && <button type="button" className="dp-link dp-import" onClick={() => router.push(workspaceHref(params.toString(), "people", "new") + "&sources=1", { scroll: false })}>从模板或文件创建</button>}
              {key === "new" && params.get("sources") === "1" && <EmployeeCreationSources
                agents={agents} onClose={() => setActive("new", false, true)} onChanged={() => void teamChanged()}
                onCreated={agent => setActive(String(agent.id), false, true)} />}
              {!loaded ? (
                <p role="status">正在读取员工档案…</p>
              ) : proposal ? (
                <TeamSetupCard
                  key={proposal.requestId}
                  onDecided={() => {
                    setActive("", false, true);
                    void teamChanged();
                  }}
                  item={{
                    kind: "team_setup",
                    key: proposal.requestId,
                    requestId: proposal.requestId,
                    plan: proposal.plan,
                    version: proposal.version,
                    turnId: proposal.turnId,
                    state: "pending",
                    rows: [],
                    undoAvailable: false,
                  }}
                />
              ) : chosen?.archived_at ? (
                <div className="dp-fields">
                  <h2>{chosen.name}</h2><p>这位员工已归档。</p>
                  <button className="dp-button" disabled={restoring} onClick={() => void restore()}>恢复员工</button>
                </div>
              ) : (
                <StaffEditor
                  key={key + "-" + rev}
                  agent={chosen}
                  chatHref={chosen ? employeeChatHref(threads, chosen.id, Number(params.get("thread")) || null) : "/chat/"}
                  lead={lead}
                  team={team}
                  onTeamChanged={teamChanged}
                  onOpenAdvanced={chosen ? () => setActive(String(chosen.id), true) : undefined}
                  member={
                    !!team?.agents.some(
                      (a) => a.id === chosen?.id && a.state !== "outside",
                    )
                  }
                  engines={engines}
                  members={members}
                  openFeishu={entry.requested && key === initialEmployee}
                  onSaved={async (id) => {
                    await teamChanged();
                    if (String(id) !== key) setActive(String(id), false, true);
                    setRev((i) => i + 1);
                  }}
                />
              )}
              </section>
              {chosen && !chosen.archived_at && params.get("tools") === "1" && (
                <EmployeeAdvancedTools key={chosen.id} agent={chosen} onChanged={() => void teamChanged()} onClose={() => setActive(String(chosen.id), false, true)} />
              )}
              </div>
            </>
          )}
          {page === "projects" && (
            <>
              <Suggestions />
              {canManageProjectTerminal(session) && <SharedLibraryReviews />}
              <Projects staff={agents} />
            </>
          )}
          {page === "activity" && (
            <div className="dp-list dp-timeline">
              {activity.length === 0 ? (
                <p className="dp-empty"><Activity />暂无团队运行记录。</p>
              ) : (
                activity.map((a) => (
                  <div className="dp-listrow" key={a.id}>
                    <div>
                      <strong>{teamActivityLabel(a.kind, t)}</strong>
                      <small>
                        {agents.find((s) => s.id === a.agent_id)?.name && `${agents.find((s) => s.id === a.agent_id)?.name} · `}
                        {new Date(a.created_at).toLocaleString()}
                      </small>
                    </div>
                    {a.thread_id && (
                      <Link
                        className="dp-link"
                        href={`/chat?thread=${a.thread_id}`}
                      >
                        打开对话 <ChevronRight size={14} />
                      </Link>
                    )}
                  </div>
                ))
              )}
            </div>
          )}
          {page === "host" && (
            <>
              <section className="dp-host-summary">
                <div className="dp-legend">
                  <Monitor />
                  管理地址
                </div>
                <strong className="dp-mono">{typeof window !== "undefined" ? window.location.host : ""}</strong>
              </section>
              <div className="dp-inventory">
              <section>
              <h3>执行引擎</h3>
              {loaded && !engines.length && <p className="dp-empty">暂无已登记的执行引擎。</p>}
              {engines.map((e) => (
                <div className="dp-listrow" key={e.id}>
                  <div>
                    <strong>{e.label}</strong>
                    {e.version && <small>{e.version}</small>}
                  </div>
                  <span className={`dp-status ${e.available ? "" : "pending"}`}>
                    {e.available ? "可用" : "待配置"}
                  </span>
                </div>
              ))}
              </section>
              {canManageProjectTerminal(session) && <HistoryReset />}
              </div>
            </>
          )}
        </main>
    </div>
  );
}

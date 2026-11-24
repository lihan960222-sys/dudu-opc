"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useSession } from "../AuthGate";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  ChevronRight,
  FolderKanban,
  Layers3,
  Monitor,
  Plus,
  UserRound,
  UsersRound,
} from "lucide-react";
import { api, request, type Agent, type Engine } from "@/lib/api";
import {
  collaboration,
  type TeamState,
  type TeamJob,
} from "@/lib/collaboration";
import { planner, type WorkBoard, type WorkItem } from "@/lib/planner-api";
import type { TeamSetupPlan } from "@/lib/transcript";
import { TeamSetupCard } from "../chat/TeamCards";
import { StaffEditor } from "./StaffEditor";
import { Face } from "./ProfileControls";
import { FeishuAccess } from "./FeishuAccess";
import "./workbench.css";

const pages = [
  { id: "people", label: "员工", icon: UsersRound },
  { id: "projects", label: "项目", icon: FolderKanban },
  { id: "activity", label: "运行记录", icon: Activity },
  { id: "host", label: "家庭主机", icon: Monitor },
] as const;
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
  const [items, setItems] = useState<Suggestion[]>([]),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [report, setReport] = useState("");
  const [deferred, setDeferred] = useState(false);
  const generation = useRef(0);
  const [followup, setFollowup] = useState<number | null>(null);
  const load = useCallback(async () => {
    const current = ++generation.current;
    const data = await request<{ items: Suggestion[] }>(
      "/steward/suggestions" + (deferred ? "?state=deferred" : ""),
    );
    if (generation.current === current) setItems(data.items);
  }, [deferred]);
  useEffect(() => {
    void load().catch((e) => setError(String(e)));
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
      <div className="dp-suggestions-head">
        <span>{deferred ? "稍后处理" : items.length ? "待确认建议" : ""}</span>
        <button
          className="dp-link"
          onClick={() => {
            setItems([]);
            setError("");
            setDeferred((d) => !d);
          }}
        >
          {deferred ? "返回待确认建议" : "查看稍后处理的建议"}
        </button>
      </div>
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
  native_handoff?: {
    session_id: string;
    cwd: string;
    resume_command: string;
    last_native_activity?: { count: number } | null;
  } | null;
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
      <section className="dp-feature">
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
      <div className="dp-columns">
        <div className="dp-list">
          {items
            .filter(
              (i) => !project || (projects[i.id] || "未归入项目") === project,
            )
            .map((i) => (
              <button
                className="dp-listrow"
                key={i.id}
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
            {protocol?.native_handoff && (
              <>
                <hr />
                <h3>在 Codex 续接</h3>
                <label className="dp-field">
                  主机目录
                  <input
                    className="dp-input dp-mono"
                    readOnly
                    value={protocol.native_handoff.cwd}
                    onFocus={(e) => e.target.select()}
                  />
                </label>
                <label className="dp-field">
                  在此目录运行
                  <input
                    className="dp-input dp-mono"
                    readOnly
                    value={protocol.native_handoff.resume_command}
                    onFocus={(e) => e.target.select()}
                  />
                </label>
                {protocol.native_handoff.last_native_activity && (
                  <small>
                    上次恢复时，此会话有{" "}
                    {protocol.native_handoff.last_native_activity.count}{" "}
                    轮原生更新。
                  </small>
                )}
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

export function Workbench() {
  const { session } = useSession();
  const router = useRouter();
  const canManage =
    session?.scopes.includes("admin") &&
    (!session.person_id || session.role === "owner");
  useEffect(() => {
    if (!canManage) router.replace("/chat/");
  }, [canManage, router]);
  return canManage ? <ManagementPortal /> : <Link href="/chat/">进入对话</Link>;
}

function loadPortal() {
  return Promise.allSettled([
    api.listAgents(),
    collaboration.team(),
    api.getEngines(),
    request<Pending[]>("/staff/pending"),
    collaboration.activity(),
  ]);
}

function ManagementPortal() {
  const params = useSearchParams();
  const [page, setPage] = useState<string>(() => params.get("section") === "host" ? "host" : "people"),
    [agents, setAgents] = useState<Agent[]>([]),
    [engines, setEngines] = useState<Engine[]>([]),
    [team, setTeam] = useState<TeamState | null>(null);
  const [pending, setPending] = useState<Pending[]>([]),
    [activity, setActivity] = useState<
      Awaited<ReturnType<typeof collaboration.activity>>
    >([]),
    [active, setActive] = useState<string>(""),
    [rev, setRev] = useState(0),
    [loaded, setLoaded] = useState(false),
    [errors, setErrors] = useState<string[]>([]);
  const applyPortal = useCallback(([a, t, e, p, log]: Awaited<ReturnType<typeof loadPortal>>) => {
    if (a.status === "fulfilled") setAgents(a.value);
    if (t.status === "fulfilled") setTeam(t.value);
    if (e.status === "fulfilled") setEngines(e.value);
    if (p.status === "fulfilled") setPending(p.value);
    if (log.status === "fulfilled") setActivity(log.value);
    setErrors(
      [a, t, e, p, log].flatMap((r, i) =>
        r.status === "rejected"
          ? [
              ["员工", "团队", "引擎", "待审批档案", "运行记录"][i] +
                " · " +
                String(r.reason),
            ]
          : [],
      ),
    );
    setLoaded(true);
  }, []);
  const sequence = useRef<symbol | null>(null);
  const refresh = useCallback(() => {
    const requestId = Symbol();
    sequence.current = requestId;
    return loadPortal().then(result => {
      if (requestId === sequence.current) applyPortal(result);
    });
  }, [applyPortal]);
  useEffect(() => {
    void refresh();
    return () => { sequence.current = null; };
  }, [refresh]);
  const lead = agents.find((a) => a.id === team?.team?.lead_agent_id) ?? null;
  const ordered = lead
    ? [lead, ...agents.filter((a) => a.id !== lead.id)]
    : agents;
  const key =
    active ||
    (ordered[0] ? String(ordered[0].id) : (pending[0]?.requestId ?? "new"));
  const chosen = agents.find((a) => String(a.id) === key) ?? null,
    proposal = pending.find((p) => p.requestId === key);
  const label = pages.find((p) => p.id === page)?.label;
  return (
    <div id="workbench">
      <div className="dp-window">
        <aside className="dp-rail">
          <div className="dp-brand">
            <span className="dp-mark">
              <Layers3 />
            </span>
            <span>
              <strong>Dudu</strong>
              <small>OPC 工作台</small>
            </span>
          </div>
          <nav className="dp-navigation">
            {pages.map((p) => (
              <button
                key={p.id}
                aria-current={page === p.id ? "page" : undefined}
                onClick={() => setPage(p.id)}
              >
                <p.icon />
                {p.label}
              </button>
            ))}
          </nav>
          <div className="dp-owner">
            <span className="dp-owner-mark">
              <UserRound />
            </span>
            <div>
              所有者<small>后台管理</small>
            </div>
          </div>
        </aside>
        <main className="dp-main">
          <div className="dp-context">
            <span>
              工作台 <b>/</b> {label}
            </span>
            <nav>
              <Link href="/chat/">对话</Link>
              <Link href="/chat?view=planner">任务板</Link>
              <Link href="/settings/">设置</Link>
            </nav>
          </div>
          <header className="dp-pagehead">
            <div>
              <h1>{label}</h1>
              <p className="dp-headnote">
                {
                  {
                    people: "管理角色、模型与工作目录。",
                    projects: "查看任务归属、协作和验收标准。",
                    activity: "查看团队已记录的操作。",
                    host: "查看主机上的执行引擎与工作目录。",
                  }[page]
                }
              </p>
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
              <Suggestions />
              <div className="dp-roster">
                {ordered.map((a) => (
                  <button
                    key={a.id}
                    className="dp-person"
                    aria-pressed={key === String(a.id)}
                    onClick={() => setActive(String(a.id))}
                  >
                    <Face id={a.id} rev={rev} />
                    <span>
                      <strong>{a.name}</strong>
                      <small>
                        {a.id === lead?.id ? "大管家" : a.title || "员工"}
                      </small>
                    </span>
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
              </div>
              {!loaded ? (
                <p role="status">正在读取员工档案…</p>
              ) : proposal ? (
                <TeamSetupCard
                  key={proposal.requestId}
                  onDecided={() => {
                    setActive("");
                    void refresh();
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
              ) : (
                <StaffEditor
                  key={key + "-" + rev}
                  agent={chosen}
                  lead={lead}
                  member={
                    !!team?.agents.some(
                      (a) => a.id === chosen?.id && a.state !== "outside",
                    )
                  }
                  engines={engines}
                  onSaved={async (id) => {
                    await refresh();
                    setActive(String(id));
                    setRev((i) => i + 1);
                  }}
                />
              )}
            </>
          )}
          {page === "projects" && (
            <>
              <Suggestions />
              <Projects staff={agents} />
            </>
          )}
          {page === "activity" && (
            <div className="dp-list">
              {activity.length === 0 ? (
                <p>暂无团队运行记录。</p>
              ) : (
                activity.map((a) => (
                  <div className="dp-listrow" key={a.id}>
                    <div>
                      <strong>{a.kind}</strong>
                      <small>
                        {agents.find((s) => s.id === a.agent_id)?.name} ·{" "}
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
              <section className="dp-feature">
                <div className="dp-legend">
                  <Monitor />
                  执行设备
                </div>
                <h2>家庭主机</h2>
                <p>
                  管理地址 ·{" "}
                  {typeof window !== "undefined" ? window.location.host : ""}
                </p>
              </section>
              <FeishuAccess team={team?.agents ?? []} />
              <h3>执行引擎</h3>
              {engines.map((e) => (
                <div className="dp-listrow" key={e.id}>
                  <div>
                    <strong>{e.label}</strong>
                    <small>{e.version || e.reason}</small>
                  </div>
                  <span className="dp-tag">
                    {e.available ? "可用" : "待配置"}
                  </span>
                </div>
              ))}
              <h3>员工工作目录</h3>
              {agents.map((a) => (
                <div className="dp-listrow" key={a.id}>
                  <div>
                    <strong>{a.name}</strong>
                    <small className="dp-mono dp-wrap">
                      {a.role_path || a.workspace_path || "使用默认目录"}
                    </small>
                  </div>
                </div>
              ))}
            </>
          )}
        </main>
      </div>
    </div>
  );
}

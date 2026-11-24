"use client";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { Check, FileText, MessageSquare, Pencil, UserRound } from "lucide-react";
import { api, request, type Agent, type Engine } from "@/lib/api";
import { teamActionsFor, type ProfileTeamAction } from "@/lib/staff-team";
import { canOpenWorkbench } from "@/lib/session";
import { useT } from "@/lib/use-i18n";
import { collaboration, type TeamState } from "@/lib/collaboration";
import type { SkillSummary } from "@/lib/skills";
import { StaffModelFields } from "./ProfileControls";
import { DeepSeekKey } from "./DeepSeekKey";
import { FeishuAccess } from "./FeishuAccess";
import { useSession } from "../AuthGate";
import {
  AvatarPicker,
  Face,
  FolderField,
  savePicture,
} from "./ProfileControls";

type Rules = {
  path: string;
  digest: string;
  model: string;
  instructions: string;
};
type Props = {
  agent: Agent | null;
  chatHref: string;
  lead: Agent | null;
  member: boolean;
  engines: Engine[];
  members: number[];
  openFeishu: boolean;
  team?: TeamState | null;
  onTeamChanged?: () => Promise<void>;
  onOpenAdvanced?: () => void;
  onSaved: (id: number) => void;
};
export function StaffEditor({ agent, chatHref, lead, member, engines, members, openFeishu, onSaved, team, onTeamChanged, onOpenAdvanced }: Props) {
  const t = useT();
  const { session } = useSession();
  const ownsHost = session?.kind === "browser" && !!session.session_id && session.scopes.includes("admin")
    && (session.role === "owner" || (!session.role && !session.person_id));
  const isLead = !!agent && agent.id === lead?.id;
  const [name, setName] = useState(agent?.name ?? ""),
    [title, setTitle] = useState(agent?.title ?? ""),
    [summary, setSummary] = useState(agent?.description ?? "");
  const [provider, setProvider] = useState(agent?.provider ?? "codex"),
    [model, setModel] = useState<string | null>(agent?.model ?? null);
  const [effort,setEffort] = useState<string | null>(agent?.effort ?? null);
  const [confirm,setConfirm] = useState<ProfileTeamAction | null>(null);
  const [teamError,setTeamError] = useState("");
  const [path, setPath] = useState(
      agent?.role_path ?? agent?.workspace_path ?? "",
    ),
    [body, setBody] = useState(agent?.instructions ?? "");
  const [rules, setRules] = useState<Rules | null>(null),
    [ruleError, setRuleError] = useState("");
  const [showFeishu, setShowFeishu] = useState(openFeishu && ownsHost);
  const [tab, setTab] = useState(openFeishu && ownsHost ? "tools" : "basic"),
    [avatar, setAvatar] = useState(""),
    [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState(false),
    [notice, setNotice] = useState(""),
    [error, setError] = useState("");
  const [skills, setSkills] = useState<SkillSummary[]>([]),
    [skillError, setSkillError] = useState("");
  const [makeLead, setMakeLead] = useState(false);
  const [teamMember, setTeamMember] = useState(!!lead);
  useEffect(() => {
    if (tab !== "tools" || !agent) return;
    let live = true;
    api.listSkills(agent.id).then(
      (s) => {
        if (live) setSkills(s);
      },
      (e) => {
        if (live) setSkillError(String(e));
      },
    );
    return () => {
      live = false;
    };
  }, [agent, tab]);
  const persisted = useRef<Agent | null>(agent);
  const [loadTick, reload] = useState(0);
  useEffect(() => {
    if (!agent?.role_path) return;
    let live = true;
    request<Rules>(`/staff/${agent.id}/rules`).then(
      (r) => {
        if (live) {
          setRules(r);
          setModel(r.model);
          setBody(r.instructions);
          setRuleError("");
        }
      },
      (err) => {
        if (live) setRuleError(String(err));
      },
    );
    return () => {
      live = false;
    };
  }, [agent?.id, agent?.role_path, loadTick]);
  const role =
    agent && agent.id === lead?.id
      ? "大管家"
      : member
        ? "专业员工"
        : "独立员工";
  const teamAgent = team?.agents.find(person => person.id === agent?.id);
  const actions = teamActionsFor(canOpenWorkbench(session), agent, team);
  const actionKeys: Record<ProfileTeamAction,string> = {delegate:"team.canDelegate", lead:"team.makeLead",
    remove:"team.removeMember", undo:"team.undoCreation", join:"profile.joinTeam"};
  async function changeTeam(action: ProfileTeamAction) {
    if (!agent || !actions.includes(action) || busy) return;
    setBusy(true);
    setTeamError("");
    try {
      if (action === "join") await collaboration.addMember(agent.id);
      if (action === "lead") await collaboration.lead(agent.id);
      if (action === "remove") await collaboration.removeMember(agent.id);
      if (action === "undo") await collaboration.undoCreate(agent.id);
      if (action === "delegate") await collaboration.delegation(agent.id,!teamAgent?.delegation_enabled);
      setConfirm(null);
      await onTeamChanged?.();
    } catch (e) {
      setTeamError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    setBusy(true);
    setError("");
    setNotice("");
    let step = "保存资料";
    try {
      if (!name.trim()) throw Error("请填写员工名称");
      if (!path.trim() && agent?.role_path) throw Error("请填写独立的工作目录");
      if (!agent && !model) throw Error("请为新员工选择默认模型");
      const data = {
        name: name.trim(),
        title: title.trim() || null,
        description: summary.trim() || null,
        provider,
        model, effort,
      };
      const before = persisted.current;
      if (agent?.role_path && !rules) throw Error("请先重新读取工作指令");
      if (
        agent?.role_path &&
        path !== agent.role_path &&
        (body !== rules?.instructions || model !== rules?.model)
      )
        throw Error("请先保存当前指令和模型，再更换工作目录");
      // The file revision is the source of truth for configured roles.
      if (
        rules &&
        before &&
        path === rules.path &&
        (body !== rules.instructions || model !== rules?.model)
      ) {
        if (!model) throw Error("角色目录需要明确的模型 ID");
        step = "保存 AGENTS.md";
        const next = await request<Rules>(`/staff/${before.id}/rules`, {
          method: "PUT",
          body: JSON.stringify({ ...rules, instructions: body, model }),
        });
        setRules(next);
      }
      step = "保存资料";
      const employee = before
        ? await api.updateAgent(before.id, {
            ...data,
            ...(!before.role_path ? { instructions: body } : {}),
          })
        : await api.createAgent({
            ...data,
            instructions: body,
            make_lead: makeLead,
          });
      persisted.current = employee;
      if (
        (path.trim() && path !== employee.role_path) ||
        (!employee.role_path && model && !path.trim())
      ) {
        step = "设置工作目录";
        const updated = await api.configureRoleFolder(
          employee.id,
          path.trim() || null,
        );
        persisted.current = updated;
        if (updated.role_path) {
          setPath(updated.role_path);
          const loaded = await request<Rules>(`/staff/${employee.id}/rules`);
          setRules(loaded);
          setModel(loaded.model);
          setBody(loaded.instructions);
        }
      }
      if (!agent && lead && teamMember) {
        step = "加入团队";
        await collaboration.addMember(employee.id);
      }
      if (avatar) {
        step = "保存头像";
        await savePicture(employee.id, avatar);
      }
      setNotice("已保存");
      onSaved(employee.id);
    } catch (e) {
      setError(`${step}失败。${String(e)}`);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="dp-profile">
      <div className="dp-profilehead">
        <button
          type="button"
          className="dp-avatar-control"
          aria-label="更换头像"
          title="更换头像"
          aria-expanded={picking}
          onClick={() => setPicking(!picking)}
        >
          <Face id={agent?.id} name={name} big preview={avatar} />
          <Pencil aria-hidden="true" />
        </button>
        <div>
          <div className="dp-profilename">
            <h2>{name || "新员工"}</h2>
            <span className={"dp-status" + (agent ? "" : " pending")}>
              {agent ? "在职" : "待入职"}
            </span>
          </div>
          <p>{title || role}</p>
        </div>
        {agent && <Link className="dp-button dp-chat-link" aria-label={`与 ${agent.name} 对话`} href={chatHref}><MessageSquare />对话</Link>}
      </div>
      {picking && <AvatarPicker onChange={setAvatar} />}
      <div className="dp-tabs dp-profiletabs" aria-label="档案栏目">
        {[
          ["basic", "基本资料"],
          ["rules", "AGENTS.md"],
          ["tools", "能力"],
        ].map(([id, label]) => (
          <button key={id} aria-pressed={tab === id} onClick={() => setTab(id)}>
            {label}
          </button>
        ))}
      </div>
      <div className="dp-columns">
        <div className="dp-fields">
          {tab === "basic" ? (
            <>
              <div className="dp-pair">
                <label className="dp-field">
                  <span>员工名称</span>
                  <input
                    className="dp-input"
                    maxLength={80}
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                  />
                </label>
                <label className="dp-field">
                  <span>岗位</span>
                  <input
                    className="dp-input"
                    maxLength={200}
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                  />
                </label>
              </div>
              <label className="dp-field">
                <span>职责简介 · 供管家判断何时派工</span>
                <textarea
                  className="dp-textarea"
                  value={summary}
                  maxLength={4000}
                  onChange={(e) => setSummary(e.target.value)}
                />
              </label>
              <StaffModelFields
                engines={engines}
                provider={provider}
                model={model}
                effort={effort}
                requireModel={!agent || !!agent.role_path}
                disabled={!!agent?.role_path && !rules}
                lockProvider={!!agent?.role_path}
                stewardModels={isLead}
                onChange={(p, m, e) => {
                  setEffort(e ?? null);
                  setProvider(p);
                  setModel(m);
                }}
              />
              {isLead && ownsHost && provider === "codex" && (model === "deepseek-flash" || model === "deepseek-v4-pro") && (
                <DeepSeekKey />
              )}
              {!agent && (
                <label className="dp-field">
                  <span>
                    <input
                      type="checkbox"
                      checked={lead ? teamMember : makeLead}
                      onChange={(e) =>
                        lead
                          ? setTeamMember(e.target.checked)
                          : setMakeLead(e.target.checked)
                      }
                    />{" "}
                    {lead ? `加入 ${lead.name} 的团队` : "设为团队大管家"}
                  </span>
                </label>
              )}
              <FolderField value={path} onChange={setPath} />
            </>
          ) : tab === "rules" ? (
            <>
              <div className="dp-filehead dp-mono">
                <FileText />
                {path
                  ? path.replace(/[\\/]$/, "") + "/AGENTS.md"
                  : "设置工作目录后写入 AGENTS.md"}
              </div>
              <label className="dp-field">
                <span>工作指令 · 执行任务时遵循的规则</span>
                <textarea
                  className="dp-textarea dp-rules dp-mono"
                  disabled={!!agent?.role_path && !rules}
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                />
              </label>
            </>
          ) : (
            <>
              <h3>技能与工具</h3>
              {skills.map((s) => (
                <p key={s.name}>
                  {s.name} · {s.enabled ? "已启用" : "已停用"}
                </p>
              ))}
              {skills.length === 0 && !skillError && (
                <small>暂无已登记技能。</small>
              )}
              {skillError && <small role="alert">{skillError}</small>}
              {agent && (member || isLead) && ownsHost && (
                <>
                  <button type="button" className="dp-link" aria-expanded={showFeishu}
                    onClick={() => setShowFeishu(!showFeishu)}>
                    {showFeishu ? "收起飞书能力" : "配置飞书能力"}
                  </button>
                  {showFeishu && <FeishuAccess employee={agent.id} name={agent.name} isLead={isLead} members={members} />}
                </>
              )}
              <h3>执行权限</h3>
              <p>审批模式 · {t(`profile.approval.${agent?.approval_mode ?? "ask"}`)}</p>
              <p>
                工作记忆 · {agent?.memory_enabled === false ? "关闭" : "开启"}
              </p>
              {agent && onOpenAdvanced && <button type="button" className="dp-link" onClick={onOpenAdvanced}>更多员工设置</button>}
            </>
          )}
          {ruleError && (
            <p role="alert" className="dp-error">
              {ruleError}{" "}
              <button className="dp-link" onClick={() => reload((n) => n + 1)}>
                重新读取
              </button>
            </p>
          )}
        </div>
        <details className="dp-reporting">
          <summary>汇报关系与团队管理</summary>
          <div className="dp-chain">
            <div className="dp-chainrow">
              <span className="dp-face blue">
                <UserRound />
              </span>
              <span>所有者</span>
            </div>
            {lead && (member || agent?.id === lead.id) && (
              <div className="dp-chainrow">
                <Face id={lead.id} name={lead.name} />
                <span>
                  {lead.name}
                  <small>大管家</small>
                </span>
              </div>
            )}
            {agent?.id !== lead?.id && (
              <div className="dp-chainrow">
                <Face id={agent?.id} name={name} />
                <span>
                  {name || "新员工"}
                  <small>{role}</small>
                </span>
              </div>
            )}
          </div>
          {actions.length > 0 && <div className="dp-fields">
            {actions.includes("delegate") && <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={Boolean(teamAgent?.delegation_enabled)} disabled={busy}
                onChange={() => void changeTeam("delegate")} />{t("team.canDelegate")}
            </label>}
            <div className="flex flex-wrap gap-2">{actions.filter(action => action !== "delegate").map(action => (
              <button key={action} type="button" className="dp-button" disabled={busy}
                onClick={() => { setTeamError(""); if (action === "join") void changeTeam(action); else setConfirm(action); }}>
                {t(actionKeys[action])}
              </button>
            ))}</div>
            {confirm && actions.includes(confirm) && <div className="space-y-2 rounded-md border border-line p-3">
              <p>{t(`profile.confirm.${confirm}`, {name: agent?.name ?? "", lead: lead?.name ?? ""})}</p>
              <button type="button" disabled={busy} className="dp-button" onClick={() => setConfirm(null)}>{t("common.cancel")}</button>{" "}
              <button type="button" disabled={busy} className="dp-button" onClick={() => void changeTeam(confirm)}>{t(actionKeys[confirm])}</button>
            </div>}
            {teamError && <p role="alert" className="dp-error">{teamError}</p>}
          </div>}
        </details>
      </div>
      <div className="dp-actions">
        <button
          className="dp-button primary"
          disabled={busy || !name.trim() || (!!agent?.role_path && !rules)}
          onClick={() => void save()}
        >
          <Check />
          {busy ? "保存中…" : agent ? "保存更改" : "确认入职"}
        </button>
        {notice && <small role="status">{notice}</small>}
      </div>
      {error && (
        <p role="alert" className="dp-error">
          {error}
        </p>
      )}
    </div>
  );
}

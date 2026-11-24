"use client";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { Check, FileText, UserRound } from "lucide-react";
import { api, request, type Agent, type Engine } from "@/lib/api";
import { collaboration } from "@/lib/collaboration";
import type { SkillSummary } from "@/lib/skills";
import { StaffModelFields } from "./ProfileControls";
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
  lead: Agent | null;
  member: boolean;
  engines: Engine[];
  onSaved: (id: number) => void;
};
export function StaffEditor({ agent, lead, member, engines, onSaved }: Props) {
  const [name, setName] = useState(agent?.name ?? ""),
    [title, setTitle] = useState(agent?.title ?? ""),
    [summary, setSummary] = useState(agent?.description ?? "");
  const [provider, setProvider] = useState(agent?.provider ?? "codex"),
    [model, setModel] = useState<string | null>(agent?.model ?? null);
  const [path, setPath] = useState(
      agent?.role_path ?? agent?.workspace_path ?? "",
    ),
    [body, setBody] = useState(agent?.instructions ?? "");
  const [rules, setRules] = useState<Rules | null>(null),
    [ruleError, setRuleError] = useState("");
  const [tab, setTab] = useState("basic"),
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
        model,
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
    <div>
      <div className="dp-profilehead">
        <button
          type="button"
          aria-label="更换头像"
          onClick={() => setPicking(!picking)}
        >
          <Face id={agent?.id} big preview={avatar} />
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
        <button
          className="dp-link"
          style={{ marginLeft: "auto" }}
          onClick={() => setPicking(!picking)}
        >
          更换头像
        </button>
      </div>
      {picking && <AvatarPicker onChange={setAvatar} />}
      <div className="dp-tabs">
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
                disabled={!!agent?.role_path && !rules}
                lockProvider={!!agent?.role_path}
                onChange={(p, m) => {
                  setProvider(p);
                  setModel(m);
                }}
              />
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
              <h3>执行权限</h3>
              <p>审批模式 · {agent?.approval_mode ?? "ask"}</p>
              <p>执行引擎 · {provider}</p>
              <p>
                工作记忆 · {agent?.memory_enabled === false ? "关闭" : "开启"}
              </p>
              {agent && (
                <Link href={`/chat?view=agents`} className="dp-link">
                  打开员工高级设置
                </Link>
              )}
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
        <aside className="dp-aside">
          <h3>汇报关系</h3>
          <div className="dp-chain">
            <div className="dp-chainrow">
              <span className="dp-face blue">
                <UserRound />
              </span>
              <span>所有者</span>
            </div>
            {lead && (member || agent?.id === lead.id) && (
              <div className="dp-chainrow">
                <Face id={lead.id} />
                <span>
                  {lead.name}
                  <small>大管家</small>
                </span>
              </div>
            )}
            {agent?.id !== lead?.id && (
              <div className="dp-chainrow">
                <Face id={agent?.id} />
                <span>
                  {name || "新员工"}
                  <small>{role}</small>
                </span>
              </div>
            )}
          </div>
          {agent && (
            <>
              <hr />
              <Link className="dp-link" href={`/chat?agent=${agent.id}`}>
                与 {agent.name} 对话
              </Link>
            </>
          )}
        </aside>
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

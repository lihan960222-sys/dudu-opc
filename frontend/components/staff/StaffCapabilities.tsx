"use client";
import { useEffect, useState } from "react";
import { api, request } from "@/lib/api";
import type { SkillSummary } from "@/lib/skills";

type Catalog = {
  available: { template_id: string; name: string; description: string; enabled: boolean; conflict: boolean }[];
  project_skills?: { name: string; description: string; enabled: boolean; verified_at?: string | null }[];
  skill_receipts?: Record<string, string>;
  tools: { name: string; description: string; source?: string; status?: string; verified_at?: string | null }[];
  last_model?: { engine_id: string; model_vendor: string; requested_model: string; confirmed_model?: string; effort?: string; at: string } | null;
  weekly_knowledge?: { enabled: boolean; dispatch_enabled: boolean; contact: string; next_due: string; task_id: number; blocked_reason?: string } | null;
};

const toolState: Record<string, string> = {
  granted: "已授予员工", disabled: "已停用", needs_test: "连接需重新验证", private: "仅限私人对话",
};

function verification(at?: string | null) {
  return at ? "最近成功 " + new Date(at).toLocaleString("zh-CN") : "近期无成功回执";
}

export function StaffCapabilities({ employee }: { employee: number }) {
  const [state, setState] = useState<{ skills: SkillSummary[]; catalog: Catalog } | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  function refresh() {
    setState(null);
    setError("");
    setAttempt(value => value + 1);
  }
  useEffect(() => {
    let active = true;
    Promise.all([api.listSkills(employee), request<Catalog>(`/agents/${employee}/skills/catalog/available`)]).then(
      ([skills, catalog]) => { if (active) setState({ skills, catalog }); },
      (e) => { if (active) setError(String(e)); },
    );
    return () => { active = false; };
  }, [employee, attempt]);
  if (error) return <div role="alert">能力清单读取失败。 <button type="button" className="dp-link" onClick={refresh}>重新读取</button></div>;
  if (!state) return <small role="status">正在读取能力清单…</small>;
  const available = state.catalog.available.filter(s => !s.enabled && !s.conflict);
  const projectSkills = state.catalog.project_skills ?? [];
  const receipt = state.catalog.last_model;
  const weekly = state.catalog.weekly_knowledge;
  return <>
    {weekly && <section className="dp-cap-section"><h3>每周知识库</h3>
      <p>{weekly.contact} · 工单 #{weekly.task_id}</p>
      <small>{!weekly.enabled ? (weekly.blocked_reason ?? "已停用")
        : !weekly.dispatch_enabled ? "已登记，等待启动每周派工" : "下次派工 " + new Date(weekly.next_due).toLocaleString("zh-CN")}</small>
      {weekly.enabled && <button type="button" className="dp-link" onClick={async () => {
        try { await request(`/staff/weekly-knowledge/${employee}`, {method:"DELETE"}); refresh(); }
        catch (e) { setError(String(e)); }
      }}>停用周计划</button>}
    </section>}
    {receipt && <section className="dp-cap-section"><h3>最近一次项目模型回执</h3>
      <p>请求 {receipt.engine_id} · {receipt.model_vendor} · {receipt.requested_model}{receipt.effort ? " · " + receipt.effort : ""}</p>
      <small>{receipt.confirmed_model ? "引擎返回 " + receipt.confirmed_model : "引擎未返回模型标识"} · {new Date(receipt.at).toLocaleString("zh-CN")}</small>
    </section>}
    <section className="dp-cap-section">
      <h3>岗位技能</h3>
      {state.skills.map(s => <div className="dp-cap-row" key={s.name}>
        <div><strong>{s.name}</strong>{s.description && <small>{s.description}</small>}
          <small>员工技能 · {s.source} · {verification(state.catalog.skill_receipts?.[s.name])}</small></div>
        <span>{s.enabled ? "已启用" : "已停用"}</span>
      </div>)}
      {projectSkills.map(s => <div className="dp-cap-row" key={"project/" + s.name}>
        <div><strong>{s.name}</strong><small>{s.description}</small><small>项目目录 .agents/skills · {verification(s.verified_at)}</small></div>
        <span>{s.enabled ? "本项目可用" : "已停用"}</span></div>)}
      {!state.skills.length && !projectSkills.length && <p>暂无岗位或项目技能。</p>}
      {!!available.length && <details className="dp-cap-catalog">
        <summary>员工可按需启用的技能（{available.length}）</summary>
        {available.map(s => <div className="dp-cap-row" key={s.template_id + "/" + s.name}>
          <div><strong>{s.name}</strong><small>{s.description}</small><small>内置模板 · {s.template_id} · 待启用</small></div>
        </div>)}
      </details>}
    </section>
    <section className="dp-cap-section">
      <h3>已配置工具</h3>
      {state.catalog.tools.map(t => <div key={t.name} className="dp-cap-row"><div><strong>{t.name}</strong><small>{t.description}</small>
        <small>{t.source} · {toolState[t.status ?? "granted"]} · {verification(t.verified_at)}</small></div></div>)}
      {!state.catalog.tools.length && <p>暂无额外配置的工具。</p>}
    </section>
  </>;
}

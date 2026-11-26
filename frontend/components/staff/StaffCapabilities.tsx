"use client";
import { useEffect, useState } from "react";
import { api, request } from "@/lib/api";
import type { SkillSummary } from "@/lib/skills";

type Catalog = {
  available: { template_id: string; name: string; description: string; enabled: boolean; conflict: boolean }[];
  tools: { name: string; description: string }[];
};

export function StaffCapabilities({ employee }: { employee: number }) {
  const [state, setState] = useState<{ skills: SkillSummary[]; catalog: Catalog } | null>(null);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let active = true;
    setState(null);
    setError("");
    Promise.all([api.listSkills(employee), request<Catalog>(`/agents/${employee}/skills/catalog/available`)]).then(
      ([skills, catalog]) => { if (active) setState({ skills, catalog }); },
      (e) => { if (active) setError(String(e)); },
    );
    return () => { active = false; };
  }, [employee, attempt]);
  if (error) return <div role="alert">能力清单读取失败。 <button type="button" className="dp-link" onClick={() => setAttempt(attempt + 1)}>重新读取</button></div>;
  if (!state) return <small role="status">正在读取能力清单…</small>;
  const available = state.catalog.available.filter(s => !s.enabled && !s.conflict);
  return <>
    <section className="dp-cap-section">
      <h3>岗位技能</h3>
      {state.skills.map(s => <div className="dp-cap-row" key={s.name}>
        <div><strong>{s.name}</strong>{s.description && <small>{s.description}</small>}</div>
        <span>{s.enabled ? "已启用" : "已停用"}</span>
      </div>)}
      {!state.skills.length && <p>尚未启用岗位技能。</p>}
      {!!available.length && <details className="dp-cap-catalog">
        <summary>员工可按需启用的技能（{available.length}）</summary>
        {available.map(s => <div className="dp-cap-row" key={s.template_id + "/" + s.name}>
          <div><strong>{s.name}</strong><small>{s.description}</small></div>
        </div>)}
      </details>}
    </section>
    <section className="dp-cap-section">
      <h3>已配置工具</h3>
      {state.catalog.tools.map(t => <div key={t.name} className="dp-cap-row"><div><strong>{t.name}</strong><small>{t.description}</small></div></div>)}
      {!state.catalog.tools.length && <p>暂无额外配置的工具。</p>}
    </section>
  </>;
}

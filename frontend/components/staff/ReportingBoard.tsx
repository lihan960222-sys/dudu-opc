"use client";

import { useEffect, useState } from "react";
import { Network, UserRound } from "lucide-react";
import { api, type Agent } from "@/lib/api";
import { collaboration, type TeamState } from "@/lib/collaboration";
import { useT } from "@/lib/use-i18n";
import { reportingPath } from "@/lib/reporting";
import { Face } from "./ProfileControls";

export function ReportingBoard({ openProfile }: {
  openProfile: (id: string) => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [snapshot, setSnapshot] = useState<{ team: TeamState; staff: Agent[] } | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!open) return;
    let active = true;
    Promise.all([collaboration.team(), api.listAgents()]).then(
      ([team, staff]) => { if (active) setSnapshot({ team, staff }); },
      () => { if (active) setFailed(true); },
    );
    return () => { active = false; };
  }, [open]);

  const members = snapshot?.team.agents.filter(a => !a.archived_at && a.state !== "outside") ?? [];
  const lead = members.find(a => a.id === snapshot?.team.team?.lead_agent_id);
  const employees = members.filter(a => a.id !== lead?.id);
  const memberIds = new Set(members.map(a => a.id));
  const outside = snapshot?.staff.filter(a => !a.archived_at && !memberIds.has(a.id)) ?? [];
  const person = (agent: Agent) => (
    <button type="button" className="dp-org-person" onClick={() => openProfile(String(agent.id))}>
      <Face id={agent.id} name={agent.name} /><span>{agent.name}</span>
    </button>
  );

  const branches = new Map<number, Agent[]>();
  const unresolved: Agent[] = [];
  for (const employee of employees) {
    const chain = reportingPath(members, lead?.id ?? null, employee.id);
    const manager = chain?.at(-2);
    if (!manager) { unresolved.push(employee); continue; }
    const reports = branches.get(manager.id) ?? [];
    reports.push(employee);
    branches.set(manager.id, reports);
  }
  function reports(managerId: number): React.ReactNode {
    const staff = branches.get(managerId);
    return staff && <ul>{staff.map(a => <li key={a.id}>{person(a)}{reports(a.id)}</li>)}</ul>;
  }
  return <details className="dp-org" onToggle={e => {
    setOpen(e.currentTarget.open);
    if (!e.currentTarget.open) { setSnapshot(null); setFailed(false); }
  }}>
    <summary><Network size={17} />{t("org.title")}</summary>
    {open && <div className="dp-org-body">
      {failed && <p role="status">{t("org.failed")}</p>}
      {!snapshot && !failed && <p role="status">{t("org.loading")}</p>}
      {snapshot && <>
        <ul className="dp-org-tree"><li>
          <div className="dp-org-node"><UserRound size={18} />{t("org.owner")}</div>
          <ul><li>
            <div className="dp-org-node">{lead ? <>{person(lead)}<small>{t("org.lead")}</small></> : t("org.noLead")}</div>
            {lead && reports(lead.id)}
          </li></ul>
        </li></ul>
        {unresolved.length > 0 && <div><h3>{t("org.unassigned")}</h3>{unresolved.map(a => <div key={a.id}>{person(a)}</div>)}</div>}
        {outside.length > 0 && <div><h3>{t("org.outside")}</h3>{outside.map(a => <div key={a.id}>{person(a)}</div>)}</div>}
      </>}
    </div>}
  </details>;
}

"use client";

import { useEffect, useState } from "react";
import { Network, UserRound } from "lucide-react";
import { api, type Agent } from "@/lib/api";
import { collaboration, type TeamState } from "@/lib/collaboration";
import { useT } from "@/lib/use-i18n";
import { Face } from "./ProfileControls";

export function ReportingBoard({ currentTeam, openProfile }: {
  currentTeam: TeamState | null;
  openProfile: (id: string) => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [snapshot, setSnapshot] = useState<{ team: TeamState; staff: Agent[] } | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!open) return;
    let active = true;
    let pending = false;
    async function sync() {
      if (document.hidden || pending) return;
      pending = true;
      try {
        const [team, staff] = await Promise.all([collaboration.team(), api.listAgents()]);
        if (active) { setSnapshot({ team, staff }); setFailed(false); }
      } catch {
        if (active) setFailed(true);
      } finally {
        pending = false;
      }
    }
    void sync();
    const timer = window.setInterval(sync, 15000);
    document.addEventListener("visibilitychange", sync);
    window.addEventListener("focus", sync);
    return () => {
      active = false;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", sync);
      window.removeEventListener("focus", sync);
    };
  }, [open, currentTeam]);

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

  return <details className="dp-org" onToggle={e => setOpen(e.currentTarget.open)}>
    <summary><Network size={17} />{t("org.title")}</summary>
    {open && <div className="dp-org-body">
      {failed && <p role="status">{t("org.failed")}</p>}
      {!snapshot && !failed && <p role="status">{t("org.loading")}</p>}
      {snapshot && <>
        <ul className="dp-org-tree"><li>
          <div className="dp-org-node"><UserRound size={18} />{t("org.owner")}</div>
          <ul><li>
            <div className="dp-org-node">{lead ? <>{person(lead)}<small>{t("org.lead")}</small></> : t("org.noLead")}</div>
            {lead && employees.length > 0 && <ul>{employees.map(a => <li key={a.id}>{person(a)}</li>)}</ul>}
          </li></ul>
        </li></ul>
        {!lead && employees.length > 0 && <div><h3>{t("org.pendingLead")}</h3>{employees.map(a => <div key={a.id}>{person(a)}</div>)}</div>}
        {outside.length > 0 && <div><h3>{t("org.outside")}</h3>{outside.map(a => <div key={a.id}>{person(a)}</div>)}</div>}
      </>}
    </div>}
  </details>;
}

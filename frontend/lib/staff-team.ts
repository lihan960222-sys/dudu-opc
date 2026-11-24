import type { Agent } from "./api";
import type { TeamState } from "./collaboration";

export type ProfileTeamAction = "join" | "lead" | "delegate" | "remove" | "undo";

export function teamActionsFor(admin: boolean, agent: Pick<Agent, "id" | "archived_at"> | null,
  team?: TeamState | null): ProfileTeamAction[] {
  if (!admin || !agent || agent.archived_at || !team || team.enabled === false) return [];
  const member = team.agents.find(person => person.id === agent.id && person.state !== "outside");
  if (!member) {
    if (!team.team) return ["lead"];
    return team.team.lead_agent_id === null ? ["lead", "join"] : ["join"];
  }
  const actions: ProfileTeamAction[] = ["delegate"];
  if (!member.is_lead) actions.push("lead", "remove");
  if (member.created_by_agent_id) actions.push("undo");
  return actions;
}

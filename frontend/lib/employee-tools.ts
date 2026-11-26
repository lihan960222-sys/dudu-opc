import type { AgentInput } from "./api";

const fields = ["fallback_provider", "fallback_model", "claude_account", "connector_scopes", "connector_account_scopes"] as const;

export function employeeToolChanges(original: AgentInput, current: AgentInput): Partial<AgentInput> {
  return Object.fromEntries(fields.filter(key => JSON.stringify(current[key]) !== JSON.stringify(original[key]))
    .map(key => [key, current[key]]));
}

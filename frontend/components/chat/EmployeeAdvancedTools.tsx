"use client";

import type { Agent } from "@/lib/api";
import { canOpenWorkbench } from "@/lib/session";
import { useSession } from "@/components/AuthGate";
import { AgentsPanel } from "./AgentsPanel";

export function EmployeeAdvancedTools({ agent, onClose, onChanged }: {
  agent: Agent; onClose: () => void; onChanged: () => void;
}) {
  const { session } = useSession();
  if (!canOpenWorkbench(session)) return null;
  return <AgentsPanel key={agent.id} toolsFor={agent} agents={[agent]} onClose={onClose} onChanged={onChanged} />;
}

export function EmployeeCreationSources({ agents, onClose, onChanged, onCreated }: {
  agents: Agent[]; onClose: () => void; onChanged: () => void; onCreated?: (agent: Agent) => void;
}) {
  const { session } = useSession();
  if (!canOpenWorkbench(session)) return null;
  return <AgentsPanel creationSources agents={agents} onClose={onClose} onChanged={onChanged} onCreated={onCreated} />;
}

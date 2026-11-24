"use client";
import { translateNow, useT } from "@/lib/use-i18n";

import { useEffect, useState } from "react";
import { api, ApiError, type Agent } from "@/lib/api";
import { SettingsCard } from "./SettingsCard";

const TOOLS = [
  ["muapi_data_api_search", "settingsPage.searchDataApis"],
  ["muapi_data_api_inspect", "settingsPage.inspectModelSchema"],
  ["muapi_data_api_estimate", "settingsPage.estimateACall"],
  ["muapi_data_api_call", "settingsPage.makeACappedPaidCall"],
  ["muapi_predict_result", "settingsPage.checkAPredictionResult"],
] as const;

type GrantMap = Record<number, string[]>;
type SpendMap = Record<number, boolean>;

const sorted = (values: string[]) => [...values].sort();

export function IntegrationsSettings() {
  const t = useT();
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [saved, setSaved] = useState<GrantMap>({});
  const [draft, setDraft] = useState<GrantMap>({});
  const [scheduled, setScheduled] = useState<SpendMap>({});
  const [keyDraft, setKeyDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    Promise.all([api.getMuapiIntegration(), api.listAgents(), api.getSpend()]).then(async ([status, people, spend]) => {
      const grantRows = await Promise.all(people.map(async (agent) => {
        try { return [agent.id, (await api.getMuapiAgentGrants(agent.id)).tool_names] as const; }
        catch { return [agent.id, []] as const; }
      }));
      if (!live) return;
      setConfigured(status.configured);
      setAgents(people);
      const next = Object.fromEntries(grantRows) as GrantMap;
      setSaved(next);
      setDraft(next);
      setScheduled(Object.fromEntries(spend.workers.map((worker) => [worker.agent_id, worker.scheduled_paid_use])));
    }).catch((cause: unknown) => {
      if (live) setError(cause instanceof Error ? cause.message : translateNow("settingsPage.couldNotLoadIntegrations"));
    });
    return () => { live = false; };
  }, []);

  const saveKey = async () => {
    if (!keyDraft.trim()) return;
    setBusy(true); setError(null); setMessage(null);
    try {
      await api.saveMuapiIntegration(keyDraft.trim());
      setConfigured(true); setKeyDraft(""); setMessage(t("settingsPage.muapiIsConnectedTheApiKeyStaysEncrypted"));
    } catch (cause) {
      setError(cause instanceof ApiError || cause instanceof Error ? cause.message : t("settingsPage.couldNotConnectMuapi"));
    } finally { setBusy(false); }
  };

  const removeKey = async () => {
    setBusy(true); setError(null); setMessage(null);
    try {
      await api.deleteMuapiIntegration();
      setConfigured(false);
      const empty = Object.fromEntries(agents.map((agent) => [agent.id, []])) as GrantMap;
      setSaved(empty); setDraft(empty);
      setMessage(t("settingsPage.muapiWasDisconnectedAndItsWorkerGrantsWere"));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("settingsPage.couldNotDisconnectMuapi"));
    } finally { setBusy(false); }
  };

  const toggleTool = (agentId: number, tool: string, checked: boolean) => {
    setDraft((current) => {
      const tools = new Set(current[agentId] ?? []);
      if (checked) tools.add(tool); else tools.delete(tool);
      return { ...current, [agentId]: [...tools] };
    });
  };

  const allTools = TOOLS.map(([tool]) => tool as string);
  const setAllTools = (agentIds: number[], checked: boolean) => {
    setDraft((current) => ({ ...current, ...Object.fromEntries(agentIds.map((id) => [id, checked ? [...allTools] : []])) }));
  };
  const everyAgentFull = agents.length > 0 && agents.every((agent) => allTools.every((tool) => (draft[agent.id] ?? []).includes(tool)));
  const dirtyAgents = agents.filter((agent) => JSON.stringify(sorted(draft[agent.id] ?? [])) !== JSON.stringify(sorted(saved[agent.id] ?? [])));

  const saveAll = async () => {
    setBusy(true); setError(null); setMessage(null);
    try {
      const results = await Promise.all(dirtyAgents.map(async (agent) => [agent.id, (await api.setMuapiAgentGrants(agent.id, draft[agent.id] ?? [])).tool_names] as const));
      setSaved((current) => ({ ...current, ...Object.fromEntries(results) }));
      setMessage(t("settingsPage.savedGrants",{count:results.length}));
    } catch (cause) {
      setError(cause instanceof ApiError || cause instanceof Error ? cause.message : t("settingsPage.couldNotSaveMuapiAccess"));
    } finally { setBusy(false); }
  };

  const saveAgent = async (agent: Agent) => {
    setBusy(true); setError(null); setMessage(null);
    try {
      const next = (await api.setMuapiAgentGrants(agent.id, draft[agent.id] ?? [])).tool_names;
      setSaved((current) => ({ ...current, [agent.id]: next }));
      setMessage(t("settingsPage.savedGrant",{name:agent.name}));
    } catch (cause) {
      setError(cause instanceof ApiError || cause instanceof Error ? cause.message : t("settingsPage.couldNotSaveThisAgentSMuapiAccess"));
    } finally { setBusy(false); }
  };

  const toggleScheduled = async (agent: Agent, checked: boolean) => {
    setBusy(true); setError(null); setMessage(null);
    try {
      await api.setWorkerPaidUse(agent.id, checked);
      setScheduled((current) => ({ ...current, [agent.id]: checked }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("settingsPage.couldNotChangeScheduledPaidUse"));
    } finally { setBusy(false); }
  };

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold">{t("settingsPage.integrations")}</h1>
        <p className="mt-1 text-sm text-muted">{t("settingsPage.connectMuapiDirectlyThenChooseWhichAgentsMay")}</p>
      </header>
      <div id="setting-integration-muapi" data-setting-container>
      <SettingsCard title="MuAPI" description={t("settingsPage.thisFirstPartyConnectionCallsMuapiSRest")}>
        {configured === null ? <p className="text-sm text-muted">{t("settingsPage.checkingConnection")}</p> : configured ? (
          <p className="text-sm text-success">{t("settingsPage.connected")}</p>
        ) : (
          <p className="text-sm text-muted">{t("settingsPage.notConnected")}</p>
        )}
        {configured !== null && <div className="space-y-2">
          <label className="block text-sm">{t("settingsPage.muapiApiKey")}
            <input type="password" autoComplete="off" spellCheck={false} value={keyDraft}
              onChange={(event) => setKeyDraft(event.target.value)} placeholder={configured ? t("settingsPage.enterAReplacementKey") : t("settingsPage.pasteYourMuapiApiKey")}
              className="mt-1 w-full rounded-md border border-line bg-transparent px-3 py-2" />
          </label>
          <div className="flex flex-wrap items-center gap-3">
            <button type="button" disabled={busy || !keyDraft.trim()} onClick={() => void saveKey()} className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-hover disabled:opacity-50">{busy ? t("settingsPage.saving") : configured ? t("settingsPage.replaceApiKey") : t("settingsPage.connectMuapi")}</button>
            {configured && <button type="button" disabled={busy} onClick={() => void removeKey()} className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-hover disabled:opacity-50">{t("settingsPage.disconnectMuapi")}</button>}
            <a className="text-xs text-accent underline" href="https://muapi.ai/access-keys" target="_blank" rel="noopener noreferrer">{t("settingsPage.getAnApiKey")}</a>
          </div>
        </div>}
        {error && <p role="alert" className="text-xs text-danger">{error}</p>}
        {message && <p role="status" className="text-xs text-muted">{message}</p>}
      </SettingsCard>
      </div>

      <SettingsCard title={t("settingsPage.agentAccess")} description={t("settingsPage.eachGrantIsExactAndStartsOffPaid")}>
        {!agents.length ? <p className="text-sm text-muted">{t("settingsPage.createAnAgentToConfigureMuapiAccess")}</p> : (
          <>
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <label className="flex items-center gap-2 text-xs font-medium">
              <input type="checkbox" disabled={busy || !configured} checked={everyAgentFull}
                onChange={(event) => setAllTools(agents.map((agent) => agent.id), event.target.checked)} />{t("settingsPage.selectAllToolsForEveryAgent")}
            </label>
            <button type="button" disabled={busy || !configured || dirtyAgents.length === 0} onClick={() => void saveAll()}
              className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-hover disabled:opacity-50">{t("settingsPage.saveAllChanges")}</button>
          </div>
          <ul className="space-y-3">
            {agents.map((agent) => {
              const selected = draft[agent.id] ?? [];
              const isDirty = JSON.stringify(sorted(selected)) !== JSON.stringify(sorted(saved[agent.id] ?? []));
              const hasPaid = (saved[agent.id] ?? []).includes("muapi_data_api_call");
              return <li key={agent.id} className="space-y-2 rounded-md border border-line p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <h3 className="text-sm font-medium">{agent.name}</h3>
                  <label className="flex items-center gap-2 text-xs text-muted">
                    <input type="checkbox" disabled={busy || !configured} checked={allTools.every((tool) => selected.includes(tool))}
                      onChange={(event) => setAllTools([agent.id], event.target.checked)} />{t("settingsPage.selectAll")}
                  </label>
                </div>
                <div className="grid gap-2 sm:grid-cols-2">
                  {TOOLS.map(([tool, label]) => <label key={tool} className="flex items-center gap-2 text-xs">
                    <input type="checkbox" disabled={busy || !configured} checked={selected.includes(tool)}
                      onChange={(event) => toggleTool(agent.id, tool, event.target.checked)} />{t(label)}
                  </label>)}
                </div>
                {hasPaid && <label className="flex items-start gap-2 border-t border-line pt-2 text-xs text-muted">
                  <input type="checkbox" className="mt-0.5" disabled={busy || !configured} checked={scheduled[agent.id] ?? false}
                    onChange={(event) => void toggleScheduled(agent, event.target.checked)} />
                  <span>{t("settingsPage.allowScheduledRunsToSpendWithinTheConfigured")}</span>
                </label>}
                <button type="button" disabled={busy || !configured || !isDirty} onClick={() => void saveAgent(agent)}
                  className="rounded-md border border-line px-3 py-1.5 text-xs font-medium hover:bg-hover disabled:opacity-50">{t("settingsPage.saveAccess")}</button>
              </li>;
            })}
          </ul>
          </>
        )}
      </SettingsCard>
    </div>
  );
}

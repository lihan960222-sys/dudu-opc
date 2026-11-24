"use client";

import Link from "next/link";
import { useId, useState } from "react";
import type { Engine } from "@/lib/api";
import { engineLimitText } from "@/lib/everyday";

import { chatModelLabel, modelLabel, modelChoice, STEWARD_MODELS } from "@/lib/agents";
import { useT } from "@/lib/use-i18n";
import { Select } from "@/components/ui/Select";

const FIELD = "mt-1 w-full rounded-md border border-line bg-transparent px-3 py-1.5 text-sm disabled:opacity-50";

export function EngineModelFields({
  engines,
  provider,
  model,
  effort,
  onChange,
  disabled = false,
  lockProvider = false,
  lockModel = false,
  fixedModelUnavailable = false,
  hasComputer = false,
  showEffort = true,
  preferModelSelect = false,
  defaultLabel,
  leadDefault, inheritedModel, inheritedEffort, requireModel = false, profile = false,
}: {
  engines: Engine[];
  provider: string;
  model: string | null;
  effort?: string | null;
  onChange: (provider: string, model: string | null, effort?: string | null) => void;
  disabled?: boolean;
  lockProvider?: boolean;
  lockModel?: boolean;
  fixedModelUnavailable?: boolean;
  hasComputer?: boolean;
  showEffort?: boolean;
  preferModelSelect?: boolean;
  defaultLabel?: string;
  leadDefault?: string | null;
  inheritedModel?: string | null;
  inheritedEffort?: string | null;
  requireModel?: boolean;
  profile?: boolean;
}) {
  const listId = useId();
  const t = useT();
  const [customProvider, setCustomProvider] = useState<string | null>(null);
  const engine = engines.find((item) => item.id === provider);
  const hasStewardSources = leadDefault !== undefined && (!lockProvider || provider === "codex");
  const inherited = inheritedModel !== undefined ? inheritedModel : leadDefault;
  const { id: active, engineDefault, efforts, defaultEffort } = modelChoice(engine, model, inherited);
  const isDeep = (id: string | null) => STEWARD_MODELS.some(item => item.id === id);
  const deep = provider === "codex" && hasStewardSources && isDeep(active);
  const catalog = (engine?.models ?? []).filter(item => !(provider === "codex" && isDeep(item.id)));
  const custom = engine?.custom_models && (!preferModelSelect || customProvider === provider);
  const known = !model || catalog.some(item => item.id === model);
  const sameRoleModel = !model || model === inherited;
  const fallbackEffort = (sameRoleModel && efforts.includes(inheritedEffort ?? "") && inheritedEffort) || defaultEffort;
  const effortName = (id: string) => ["minimal","low","medium","high","xhigh","max","ultra"].includes(id)
    ? t(`profile.effort.${id}`) : id;
  const nativeModel = engineDefault && !isDeep(engineDefault) ? engineDefault : catalog[0]?.id;
  const defaultName = defaultLabel ?? (inherited
    ? t("portal.agentDefault", { model: chatModelLabel(engine,inherited,inherited) })
    : engineDefault ? t("portal.engineDefault", { model: chatModelLabel(engine,engineDefault,engineDefault) }) : t("onboarding.model.default"));

  function chooseSource(source: string) {
    setCustomProvider(null);
    if (source === "__deepseek") {
      onChange("codex", STEWARD_MODELS[0].id, null);
    } else {
      const next = engines.find(item => item.id === source);
      const explicit = requireModel ? modelChoice(next, null).engineDefault : null;
      onChange(source, source === provider && deep ? nativeModel ?? null : explicit, null);
    }
  }
  const sourceLabel = t(hasStewardSources ? "portal.modelSource" : "portal.engine");
  return (
    <div className={profile ? "dp-model-picker" : "space-y-3"}>
      {!lockModel && (!lockProvider || hasStewardSources) && <div>
        <label htmlFor={listId + "-source"} className="block text-xs text-muted">{sourceLabel}</label>
        <Select id={listId + "-source"} className={FIELD} containerClassName="w-full" aria-label={sourceLabel} value={deep ? "__deepseek" : provider}
          disabled={disabled} onChange={event => chooseSource(event.target.value)}>
          {!engine && <option value={provider}>{provider}</option>}
          {engines.filter(item => !lockProvider || item.id === provider).map(item => (
            <option key={item.id} value={item.id} disabled={(!profile && !item.available) || (hasComputer && !item.capabilities.computers) ||
              (deep && item.id === "codex" && !nativeModel && isDeep(inherited ?? null))}>
              {hasStewardSources && item.id === "codex" ? "OpenAI · Codex" : item.label}
              {!item.available ? " · " + t("portal.notReady") : ""}
            </option>
          ))}
          {hasStewardSources && (provider === "codex" || engines.some(item => item.id === "codex")) && <option value="__deepseek" disabled={hasComputer && !engines.find(item => item.id === "codex")?.capabilities.computers}>DeepSeek</option>}
        </Select>
      </div>}
      {!deep && engine?.limit && (
        <p role="status" className="text-xs text-warning">
          <span className="mr-1 rounded-full border border-warning/60 px-2 py-0.5 font-medium">
            {engineLimitText(engine.limit)}
          </span>
          {engine.limit.message}
        </p>
      )}
      {engine && !engine.available && (
        <p className="text-xs text-warning">
          {engine.reason} <Link href="/settings/engines/" className="underline">{t("portal.settings")}</Link>
        </p>
      )}
      {lockModel ? <div className="text-sm text-ink">
        <span className="mr-2 text-xs text-muted">{t("portal.model")}</span>
        {fixedModelUnavailable ? t("profile.unreadableModel") : active ? chatModelLabel(engine,active,active) : defaultLabel ?? t("onboarding.model.default")}
      </div> : <div>
        <label htmlFor={listId + "-model"} className="block text-xs text-muted">{t(profile ? "profile.roleModel" : "portal.model")}</label>
        {custom && preferModelSelect && !deep && <button type="button" className="text-xs underline" disabled={disabled}
          onClick={() => setCustomProvider(null)}>{t("portal.backToModels")}</button>}
        {custom && !deep ? <>
          <input id={listId + "-model"} className={FIELD} list={listId} value={model ?? ""} maxLength={200}
            placeholder={defaultName} disabled={disabled} autoComplete="off" spellCheck={false}
            onChange={e => onChange(provider,e.target.value || null,null)} />
          <datalist id={listId}>{catalog.map(item => <option key={item.id} value={item.id}>{chatModelLabel(engine,item.id,item.id)}</option>)}</datalist>
        </> : <Select id={listId + "-model"} className={FIELD} value={model ?? ""} disabled={disabled}
          onChange={event => event.target.value === "__custom_model__" ? setCustomProvider(provider) : onChange(provider,event.target.value || null,null)}>
          {(!requireModel || !model) && <option value="" disabled={requireModel}>{requireModel ? t("profile.chooseModel") : defaultName}</option>}
          {!deep && !known && model && <option value={model}>{t("portal.currentModel", { model: modelLabel(model) })}</option>}
          {(deep ? STEWARD_MODELS : catalog).map(item => <option key={item.id} value={item.id}>
            {deep ? item.label : chatModelLabel(engine,item.id,item.id)}
          </option>)}
          {!deep && engine?.custom_models && <option value="__custom_model__">{t("portal.customModel")}</option>}
        </Select>}
      </div>}
      {!fixedModelUnavailable && !deep && showEffort && efforts.length > 0 && <div>
        <label htmlFor={listId + "-effort"} className="block text-xs text-muted">{t("portal.reasoning")}</label>
        <Select id={listId + "-effort"} className={FIELD} containerClassName="w-full" value={effort ?? ""} disabled={disabled}
          onChange={e => onChange(provider,model,e.target.value || null)}>
          <option value="">{fallbackEffort ? t("profile.defaultEffort", { effort: effortName(fallbackEffort) }) : t("profile.default")}</option>
          {effort && !efforts.includes(effort) && <option value={effort} disabled>{effortName(effort)} · {t("header.unavailable")}</option>}
          {efforts.map(id => <option value={id} key={id}>{effortName(id)}</option>)}
        </Select>
      </div>}
      {engine && !deep && !profile && !lockModel && (
        <p className="text-xs text-muted">
          {[engine.capabilities.tools ? t("portal.tools") : t("portal.textOnly"),
            engine.capabilities.computers && t("portal.computers"),
            engine.capabilities.resume && t("portal.resume")].filter(Boolean).join(" · ")}
        </p>
      )}
    </div>
  );
}

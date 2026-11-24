"use client";

import Link from "next/link";
import { useId, useState } from "react";
import type { Engine } from "@/lib/api";
import { cn } from "@/lib/cn";
import { engineLimitText } from "@/lib/everyday";
import { engineGroups } from "@/lib/engines";
import { modelLabel } from "@/lib/agents";
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
  hasComputer = false,
  showEffort = true,
  preferModelSelect = false,
  defaultLabel,
}: {
  engines: Engine[];
  provider: string;
  model: string | null;
  effort?: string | null;
  onChange: (provider: string, model: string | null, effort?: string | null) => void;
  disabled?: boolean;
  lockProvider?: boolean;
  hasComputer?: boolean;
  showEffort?: boolean;
  preferModelSelect?: boolean;
  defaultLabel?: string;
}) {
  const listId = useId();
  const t = useT();
  const [customProvider, setCustomProvider] = useState<string | null>(null);
  const engine = engines.find((item) => item.id === provider);
  const knownModel = model === null || engine?.models.some((item) => item.id === model);
  const defaultName = defaultLabel ?? (engine?.default_model
    ? t("portal.engineDefault", { model: modelLabel(engine.default_model) })
    : t("onboarding.model.default"));

  // Identify current model metadata
  const effectiveModelId = model ?? engine?.default_model ?? engine?.models[0]?.id ?? null;
  const currentModel = engine?.models.find((item) => item.id === effectiveModelId);

  // Check if current model supports configurable reasoning efforts
  const supportsEffort = Boolean(
    currentModel?.supported_reasoning_efforts &&
    currentModel.supported_reasoning_efforts.length > 1 &&
    currentModel.reasoning_effort_configurable !== false
  );

  const activeEffort =
    effort ??
    currentModel?.default_reasoning_effort ??
    (supportsEffort ? currentModel?.supported_reasoning_efforts?.[0] : null);

  const handleProviderChange = (newProvider: string) => {
    const nextEngine = engines.find((item) => item.id === newProvider);
    const nextModel = nextEngine?.default_model ?? nextEngine?.models[0]?.id ?? null;
    const nextModelObj = nextEngine?.models.find((item) => item.id === nextModel);
    const nextEffort = nextModelObj?.default_reasoning_effort ?? null;
    onChange(newProvider, null, nextEffort);
  };

  const handleModelChange = (newModel: string | null) => {
    const nextModelId = newModel ?? engine?.default_model ?? engine?.models[0]?.id ?? null;
    const nextModelObj = engine?.models.find((item) => item.id === nextModelId);
    const nextEffort = nextModelObj?.default_reasoning_effort ?? null;
    onChange(provider, newModel, nextEffort);
  };

  const handleEffortChange = (newEffort: string) => {
    onChange(provider, model, newEffort);
  };

  return (
    <div className="space-y-3">
      <label className="block text-xs text-muted">
        {t("portal.engine")}
        <Select
          className={FIELD}
          aria-label={t("portal.engine")}
          value={provider}
          disabled={disabled || lockProvider}
          onChange={(event) => handleProviderChange(event.target.value)}
        >
          {!engine && <option value={provider}>{provider}</option>}
          {engineGroups(engines).map((group) => (
            <optgroup key={group.label} label={group.label === "Ready" ? t("header.ready") : t("portal.notReady")}>
              {group.engines.map((item) => (
                <option
                  key={item.id}
                  value={item.id}
                  disabled={!item.available || (hasComputer && !item.capabilities.computers)}
                >
                  {item.label}
                  {item.available ? "" : ` · ${item.reason ?? t("portal.notReady")}`}
                  {item.limit ? ` · ${engineLimitText(item.limit)}` : ""}
                  {hasComputer && !item.capabilities.computers ? ` · ${t("portal.unavailableHere")}` : ""}
                </option>
              ))}
            </optgroup>
          ))}
        </Select>
      </label>
      {engine?.limit && (
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
      <label className="block text-xs text-muted">
        {t("portal.model")}
        {preferModelSelect && engine?.custom_models && customProvider === provider && (
          <button type="button" className="ml-2 underline" onClick={() => setCustomProvider(null)}>{t("portal.backToModels")}</button>
        )}
        {engine && (!engine.custom_models || (preferModelSelect && customProvider !== provider)) ? (
          <Select
            className={FIELD}
            aria-label={t("portal.model")}
            value={model ?? ""}
            disabled={disabled}
            onChange={(event) => event.target.value === "__custom_model__" ? setCustomProvider(provider) : handleModelChange(event.target.value || null)}
          >
            <option value="">
              {defaultName}
            </option>
            {!knownModel && model && <option value={model}>{t("portal.currentModel", { model: modelLabel(model) })}{engine.custom_models ? "" : ` · ${t("header.unavailable")}`}</option>}
            {engine.models.map((item) => (
              <option key={item.id} value={item.id}>{item.label}</option>
            ))}
            {engine.custom_models && <option value="__custom_model__">{t("portal.customModel")}</option>}
          </Select>
        ) : (
          <>
            <input
              className={FIELD}
              aria-label={t("portal.model")}
              list={listId}
              value={model ?? ""}
              disabled={disabled}
              placeholder={defaultName}
              maxLength={200}
              autoComplete="off"
              spellCheck={false}
              onChange={(event) => handleModelChange(event.target.value || null)}
            />
            <datalist id={listId}>
              {engine?.models.map((item) => (
                <option key={item.id} value={item.id}>{item.label}</option>
              ))}
            </datalist>
          </>
        )}
      </label>

      {showEffort && supportsEffort && currentModel && currentModel.supported_reasoning_efforts && (
        <div className="pt-1">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted">{t("portal.reasoning")}</span>
            <div className="inline-flex rounded-lg bg-inset p-1 border border-line gap-1">
              {currentModel.supported_reasoning_efforts.map((eff) => {
                const isSelected = activeEffort?.toLowerCase() === eff.toLowerCase();
                return (
                  <button
                    key={eff}
                    type="button"
                    disabled={disabled}
                    onClick={() => handleEffortChange(eff)}
                    className={cn(
                      "px-3 py-1 text-xs rounded-md font-medium capitalize transition-all duration-150 cursor-pointer disabled:cursor-not-allowed disabled:opacity-50",
                      isSelected
                        ? "bg-raised text-ink shadow-sm ring-1 ring-line/50 font-semibold"
                        : "text-muted hover:text-ink hover:bg-hover/50"
                    )}
                  >
                    {eff}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      )}

      {engine && (
        <p className="text-xs text-muted">
          {[engine.capabilities.tools ? t("portal.tools") : t("portal.textOnly"),
            engine.capabilities.computers && t("portal.computers"),
            engine.capabilities.resume && t("portal.resume")].filter(Boolean).join(" · ")}
        </p>
      )}
    </div>
  );
}

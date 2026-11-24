"use client";

import { useT } from "@/lib/use-i18n";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { centsToDollars, dollarsToCents, limitSourceLabel, type SpendOverview } from "@/lib/spend";
import { SettingsCard } from "./SettingsCard";

// The owner's spending limits for paid provider tools, and each worker's month so far. Each limit says where its
// value comes from (settings, the server's environment or the default). If a server refuses a change, the page says so.
export function SpendLimitsCard() {
  const t = useT();
  const [overview, setOverview] = useState<SpendOverview | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [perCall, setPerCall] = useState("");
  const [perRun, setPerRun] = useState("");
  const [perMonth, setPerMonth] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const show = (o: SpendOverview) => {
    setOverview(o);
    setPerCall((o.limits.per_call_cents / 100).toFixed(2));
    setPerRun((o.limits.per_run_cents / 100).toFixed(2));
    setPerMonth((o.limits.per_worker_month_cents / 100).toFixed(2));
  };

  useEffect(() => {
    let live = true;
    api.getSpend().then((o) => { if (live) show(o); }).catch((e) => { if (live) setLoadError(e instanceof Error ? e.message : t("personalSettings.limitLoad")); });
    return () => { live = false; };
  }, [t]);

  const save = async () => {
    const a = dollarsToCents(perCall);
    const b = dollarsToCents(perRun);
    const c = dollarsToCents(perMonth);
    if (a === null || b === null || c === null) {
      setMessage({ kind: "error", text: t("personalSettings.limitInvalid") });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      await api.saveSpendLimits({ per_call_cents: a, per_run_cents: b, per_worker_month_cents: c });
      // Read it back so the source labels show the saved values as the server now reports them.
      show(await api.getSpend());
      setMessage({ kind: "ok", text: t("personalSettings.limitSaved") });
    } catch (e) {
      const status = e instanceof ApiError ? e.status : null;
      const serverMessage = e instanceof Error ? e.message.trim() : "";
      const text = status === 404 ? t("personalSettings.limitRefused")
        : serverMessage || t(status === 409 ? "personalSettings.limitConflict" : "personalSettings.limitSaveError");
      setMessage({ kind: "error", text });
    } finally {
      setBusy(false);
    }
  };

  const field = (label: string, value: string, set: (v: string) => void, source?: string) => (
    <label className="flex items-center justify-between gap-3 text-sm">
      <span>{label}{limitSourceLabel(source) && <span className="ml-2 text-xs text-muted">({t(`personalSettings.source.${source}`)})</span>}</span>
      <span className="flex items-center gap-1">$<input inputMode="decimal" value={value} onChange={(e) => set(e.target.value)} className="w-24 rounded-md border border-line bg-transparent px-2 py-1 text-right" /></span>
    </label>
  );

  return (
    <SettingsCard title={t("personalSettings.limitTitle")} description={t("personalSettings.limitHint")}>
      {loadError && <p role="alert" className="text-xs text-danger">{loadError}</p>}
      {overview && (
        <div className="space-y-3">
          {field(t("personalSettings.call"), perCall, setPerCall, overview.limits.sources?.per_call)}
          {field(t("personalSettings.run"), perRun, setPerRun, overview.limits.sources?.per_run)}
          {field(t("personalSettings.monthly"), perMonth, setPerMonth, overview.limits.sources?.monthly)}
          <div className="flex items-center gap-3">
            <button type="button" disabled={busy} onClick={() => void save()} className="cursor-pointer rounded-md border border-line px-3 py-1 text-xs font-medium hover:bg-hover disabled:opacity-50">
              {busy ? t("personalSettings.saving") : t("personalSettings.saveLimits")}
            </button>
            {message && <p role={message.kind === "error" ? "alert" : "status"} className={`text-xs ${message.kind === "error" ? "text-danger" : "text-muted"}`}>{message.text}</p>}
          </div>
          {overview.workers.some((w) => w.month_cents > 0 || w.scheduled_paid_use) && (
            <ul className="space-y-1 border-t border-line pt-3 text-xs">
              {overview.workers.filter((w) => w.month_cents > 0 || w.scheduled_paid_use).map((w) => (
                <li key={w.agent_id} className="flex justify-between gap-3">
                  <span>{w.name}{w.scheduled_paid_use ? t("personalSettings.scheduled") : ""}</span>
                  <span className="text-muted">{t("personalSettings.thisMonth", { amount: centsToDollars(w.month_cents) })}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </SettingsCard>
  );
}

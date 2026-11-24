"use client";
import { translateNow, useT } from "@/lib/use-i18n";

import { useEffect, useRef, useState } from "react";
import { api, ApiError, type Engine, type GoogleSetupOperation } from "@/lib/api";
import { Button } from "@/components/ui/Button";
import { Banner } from "@/components/ui/Banner";
import { Input } from "@/components/ui/Input";


export function GoogleEngineSetup({ engine, onChanged }: { engine: Engine; onChanged: () => void }) {
  const t = useT();
  const [operation, setOperation] = useState<GoogleSetupOperation | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [callback, setCallback] = useState("");
  const autoOpenedRef = useRef<string | null>(null);
  const changed = useRef(onChanged);
  useEffect(() => { changed.current = onChanged; }, [onChanged]);

  useEffect(() => {
    if (operation?.phase === "running" && operation.authorization_url && autoOpenedRef.current !== operation.authorization_url) {
      autoOpenedRef.current = operation.authorization_url;
      if (window.suparagent?.openGoogleSignIn) {
        window.suparagent.openGoogleSignIn(operation.authorization_url).catch(() => {});
      }
    }
  }, [operation?.phase, operation?.authorization_url]);

  useEffect(() => {
    let cancelled = false;
    api.googleSetupStatus().then((status) => {
      if (!cancelled && status?.phase === "running") setOperation(status);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (operation?.phase !== "running") return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const status = await api.googleSetupStatus();
        if (cancelled) return;
        setOperation(status);
        if (status?.phase !== "running") {
          changed.current();
          return;
        }
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? `${translateNow("settingsPage.couldNotCheckGoogleSetup")} ${err.message}` : translateNow("settingsPage.couldNotCheckGoogleSetup"));
      }
      if (!cancelled) timer = setTimeout(() => void poll(), 1500);
    };
    timer = setTimeout(() => void poll(), 1000);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [operation?.id, operation?.phase]);

  const act = async (work: () => Promise<void>) => {
    setBusy(true); setError(null);
    try { await work(); }
    catch (err) {
      const msg = err instanceof ApiError ? err.message : (err instanceof Error ? err.message : String(err));
      setError(`Could not complete Google setup. ${msg}`);
    }
    finally { setBusy(false); }
  };
  const running = operation?.phase === "running";
  const start = (kind: "install" | "sign_in" | "models") => void act(async () => {
    autoOpenedRef.current = null;
    setCallback("");
    setOperation(await api.startGoogleSetup(kind));
  });

  return <div className="space-y-3">
    <div className="flex flex-wrap gap-2">
      {!engine.installed && <Button type="button" disabled={busy || running} onClick={() => start("install")}>{t("settingsPage.installAntigravity")}</Button>}
      {engine.installed && <Button type="button" disabled={busy || running} onClick={() => start("sign_in")}>{engine.signed_in ? t("settingsPage.switchGoogleAccount") : t("settingsPage.signInWithGoogle")}</Button>}
      {engine.signed_in && <>
        <Button type="button" disabled={busy || running} onClick={() => start("models")}>{t("settingsPage.refreshModels")}</Button>
        <Button type="button" disabled={busy || running} onClick={() => void act(async () => { await api.deleteEngineConfig("antigravity"); setOperation(null); changed.current(); })}>{t("settingsPage.removeGoogleAccount")}</Button>
      </>}
      {running && <Button type="button" disabled={busy} onClick={() => void act(async () => { await api.cancelGoogleSetup(); setOperation(await api.googleSetupStatus()); setCallback(""); changed.current(); })}>{t("settingsPage.cancelSetup")}</Button>}
    </div>
    {running && (
      <div className="space-y-2">
        <p role="status" className="text-xs text-muted">
          {operation.detail || (operation.kind === "install" ? t("settingsPage.downloadingAndVerifyingTheOfficialGoogleRuntimeThis") : operation.kind === "models" ? t("settingsPage.refreshingTheModelsAvailableToYourAccount") : t("settingsPage.waitingForGoogleSignInAndLoadingYour"))}
        </p>
        {operation.progress != null && (
          <div className="w-full bg-line rounded-full h-2 overflow-hidden">
            <div className="bg-accent h-2 rounded-full transition-all duration-200" style={{ width: `${Math.max(4, Math.min(100, operation.progress))}%` }} />
          </div>
        )}
      </div>
    )}
    {running && operation.authorization_url && <div className="space-y-2">
      <Button type="button" disabled={busy} onClick={() => void act(async () => {
        if (window.suparagent?.openGoogleSignIn) {
          await window.suparagent.openGoogleSignIn(operation.authorization_url!);
        } else {
          window.open(operation.authorization_url!, "_blank", "noopener,noreferrer");
        }
      })}>{t("settingsPage.continueToGoogleSignIn")}</Button>
      <details><summary className="cursor-pointer text-xs">{t("settingsPage.signingInFromAnotherComputer")}</summary>
        <p className="mt-2 text-xs text-muted">{t("settingsPage.afterSigningInCopyTheCompleteLocalhostRedirect")}</p>
        <form className="mt-2 flex flex-wrap gap-2" onSubmit={(event) => { event.preventDefault(); void act(async () => { const value = callback; setCallback(""); await api.completeGoogleSetup(operation.id, value); }); }}>
          <Input aria-label={t("settingsPage.googleRedirectUrl")} type="password" autoComplete="off" value={callback} onChange={(event) => setCallback(event.target.value)} className="min-w-0 flex-1" />
          <Button type="submit" disabled={busy || !callback.trim()}>{t("settingsPage.completeSignIn")}</Button>
        </form>
      </details>
    </div>}
    {operation?.phase === "succeeded" && <Banner tone="success">{operation.kind === "install" ? t("settingsPage.antigravityInstalledSignInWithGoogleToContinue") : t("settingsPage.googleAccountModelsRefreshed")}</Banner>}
    {(error || operation?.error) && <Banner tone="danger">{error || operation?.error}</Banner>}
  </div>;
}

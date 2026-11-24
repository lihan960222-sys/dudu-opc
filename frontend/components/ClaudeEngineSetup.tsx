"use client";
import { translateNow, useT } from "@/lib/use-i18n";

import { useEffect, useRef, useState } from "react";
import { api, ApiError, type ClaudeLoginOperation } from "@/lib/api";
import { EngineSignOut } from "./EngineSignOut";
import { trustedLink } from "@/lib/claude-login-link";
import { Button } from "@/components/ui/Button";
import { Banner } from "@/components/ui/Banner";
import { Field, Input } from "@/components/ui/Input";

export function ClaudeEngineSetup({ onChanged, signedIn }: { onChanged: () => void; signedIn: boolean | null }) {
  const t = useT();
  const [flow, setFlow] = useState<ClaudeLoginOperation | null>(null);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [signOutPending, setSignOutPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const changed = useRef(onChanged);
  useEffect(() => { changed.current = onChanged; }, [onChanged]);
  const flowId = flow?.id;
  const phase = flow?.phase;
  useEffect(() => {
    if (!flowId || phase !== "running") return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await api.claudeLoginStatus(flowId);
        if (stopped) return;
        setFlow(next);
        if (next.phase !== "running") { setCode(""); changed.current(); return; }
        timer = setTimeout(() => void poll(), 1500);
      } catch (err) {
        if (!stopped) setError(err instanceof ApiError ? `${translateNow("settingsPage.couldNotCheckClaudeSignIn")} ${err.message}` : translateNow("settingsPage.couldNotCheckClaudeSignIn"));
      }
    };
    timer = setTimeout(() => void poll(), 500);
    return () => { stopped = true; clearTimeout(timer); };
  }, [flowId, phase]);

  const act = async (work: () => Promise<void>) => {
    setBusy(true); setError(null);
    try { await work(); }
    catch (err) { setError(err instanceof ApiError ? `${t("settingsPage.couldNotUpdateClaudeSignIn")} ${err.message}` : t("settingsPage.couldNotUpdateClaudeSignIn")); }
    finally { setBusy(false); }
  };
  const running = flow?.phase === "running";
  const link = trustedLink(flow?.authorization_url);
  return <div className="space-y-2">
    <Button  disabled={busy || running || signOutPending} onClick={() => void act(async () => {
      setCode(""); setFlow(await api.startClaudeEngineLogin());
    })}>{t("settingsPage.signInToClaude")}</Button>
    {signedIn && <EngineSignOut label="Claude" disabled={busy || running} onSignOut={api.logoutClaudeEngine} onChanged={onChanged} onPendingChange={setSignOutPending} />}
    {running && <Button className="ml-2" disabled={busy} onClick={() => void act(async () => {
      await api.cancelClaudeLogin(flow.id); setCode(""); setFlow(null); changed.current();
    })}>{t("settingsPage.cancelClaudeSignIn")}</Button>}
    {running && !link && <p role="status" className="text-xs text-muted">{t("settingsPage.preparingClaudeSignIn")}</p>}
    {running && link && <div className="space-y-2 text-sm">
      <p>{t("settingsPage.open")} <a href={link} onClick={event => {
        if (!window.suparagent?.openEngineSignIn) return;
        event.preventDefault();
        void window.suparagent.openEngineSignIn(link!).catch(() => setError(t("settingsPage.couldNotOpenYourBrowserTrySigningIn")));
      }} target="_blank" rel="noreferrer" className="underline">{t("settingsPage.claudeSignIn")}</a>{t("settingsPage.thenPasteTheCodeShownAfterSigningIn")}</p>
      <form className="flex flex-wrap items-end gap-2" onSubmit={(event) => {
        event.preventDefault();
        if (busy || flow.code_sent) return;
        const pasted = code.trim();
        setCode("");
        void act(async () => {
          await api.completeClaudeLogin(flow.id, pasted);
          setFlow((current) => current?.id === flow.id ? { ...current, code_sent: true } : current);
        });
      }}>
        <Field label={t("settingsPage.claudeAuthorizationCode")}>{(props) => <Input {...props} type="password" autoComplete="off" spellCheck={false} maxLength={1024} value={code}
          disabled={busy || flow.code_sent} onChange={(event) => setCode(event.target.value)} className="max-w-md" />}</Field>
        <Button disabled={busy || flow.code_sent || !code.trim()} type="submit">{t("settingsPage.submitCode")}</Button>
      </form>
      <p className="text-xs text-muted">{t("settingsPage.theCodeIsSentOnceToTheClaude")}</p>
      {flow.code_sent && <p role="status" className="text-xs text-muted">{t("settingsPage.codeSubmittedWaitingForClaudeToConfirmThe")}</p>}
    </div>}
    {phase === "succeeded" && <Banner tone="success">{t("settingsPage.claudeAccountConnected")}</Banner>}
    {(error || flow?.error) && <Banner tone="danger">{error || flow?.error}</Banner>}
  </div>;
}

"use client";
import { translateNow, useT } from "@/lib/use-i18n";

import { useEffect, useRef, useState } from "react";
import { api, ApiError, type CodexLoginOperation, type CodexSignInActions } from "@/lib/api";
import { EngineSignOut } from "./EngineSignOut";
import { Button } from "@/components/ui/Button";
import { Banner } from "@/components/ui/Banner";


const globalAccount: CodexSignInActions = {
  start: api.startCodexLogin, poll: api.codexLoginStatus, cancel: api.cancelCodexLogin, signOut: api.logoutCodexEngine,
};

export function CodexEngineSetup({ onChanged, signedIn, account = globalAccount, initialFlow }: {
  onChanged: () => void; signedIn: boolean | null; account?: CodexSignInActions; initialFlow?: CodexLoginOperation;
}) {
  const t = useT();
  const [flow, setFlow] = useState<CodexLoginOperation | null>(initialFlow ?? null);
  const [busy, setBusy] = useState(false);
  const [signOutPending, setSignOutPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const changed = useRef(onChanged);
  useEffect(() => { changed.current = onChanged; }, [onChanged]);

  const flowId = flow?.id;
  const flowPhase = flow?.phase;
  useEffect(() => {
    if (!flowId || flowPhase !== "running") return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const status = await account.poll(flowId);
        if (cancelled) return;
        setFlow(status);
        if (status.phase !== "running") { changed.current(); return; }
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? `${translateNow("settingsPage.couldNotCheckCodexSignIn")} ${err.message}` : translateNow("settingsPage.couldNotCheckCodexSignIn"));
        return;
      }
      timer = setTimeout(() => void poll(), 1500);
    };
    timer = setTimeout(() => void poll(), 500);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [flowId, flowPhase, account]);

  const act = async (work: () => Promise<void>) => {
    setBusy(true); setError(null);
    try { await work(); }
    catch (err) { setError(err instanceof ApiError ? `${t("settingsPage.couldNotUpdateCodexSignIn")} ${err.message}` : t("settingsPage.couldNotUpdateCodexSignIn")); }
    finally { setBusy(false); }
  };
  const running = flow?.phase === "running";
  return <div className="space-y-2">
    {(!signedIn || account.signOut) && <Button type="button" disabled={busy || running || signOutPending} onClick={() => void act(async () => setFlow(await account.start()))}>{t("settingsPage.signInToCodex")}</Button>}
    {signedIn && account.signOut && <EngineSignOut label="Codex" disabled={busy || running} onSignOut={account.signOut} onChanged={onChanged} onPendingChange={setSignOutPending} />}
    {running && <Button className="ml-2" disabled={busy} onClick={() => void act(async () => {
      await account.cancel(flow.id); setFlow(null); changed.current();
    })}>{t("settingsPage.cancelSignIn")}</Button>}
    {running && !flow.user_code && <p role="status" className="text-xs text-muted">{t("settingsPage.preparingADeviceCode")}</p>}
    {running && flow.authorization_url === "https://auth.openai.com/codex/device" && flow.user_code && <div className="space-y-2 text-sm">
      <p>{t("settingsPage.open")} <a href={flow.authorization_url} onClick={event => {
        if (!window.suparagent?.openEngineSignIn) return;
        event.preventDefault();
        void window.suparagent.openEngineSignIn(flow.authorization_url!).catch(() => setError(t("settingsPage.couldNotOpenYourBrowserTrySigningIn")));
      }} target="_blank" rel="noreferrer" className="underline">{t("settingsPage.chatgptDeviceSignIn")}</a> {t("settingsPage.andEnter")} <code className="select-all font-semibold">{flow.user_code}</code>.</p>
      <p className="text-xs text-muted">{t("settingsPage.enableDeviceCodeLoginInYourChatgptSecurity")}</p>
    </div>}
    {flow?.phase === "succeeded" && <Banner tone="success">{t("settingsPage.codexAccountConnected")}</Banner>}
    {(error || flow?.error) && <Banner tone="danger">{error || flow?.error}</Banner>}
  </div>;
}

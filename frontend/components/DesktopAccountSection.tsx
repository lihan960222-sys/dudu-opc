"use client";
import { translateNow, useT } from "@/lib/use-i18n";

import { useEffect, useState } from "react";
import { ApiError, request } from "@/lib/api";
import type { DesktopAccountStatus } from "./DesktopAccountLogin";
import { DesktopAccountDanger } from "./DesktopAccountDanger";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";

export function DesktopAccountSection() {
  const t = useT();
  const [account, setAccount] = useState<DesktopAccountStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let live = true;
    request<DesktopAccountStatus>("/desktop-account/status").then(value => { if (live) setAccount(value); }).catch(err => {
      if (live && !(err instanceof ApiError && err.status === 404)) setError(err instanceof Error ? `${translateNow("settingsPage.couldNotReadYourAccount")} ${err.message}` : translateNow("settingsPage.couldNotReadYourAccount"));
    });
    return () => { live = false; };
  }, []);
  if (!account && !error) return null;
  return <section className="space-y-3 rounded-lg border border-line p-5"><h3 className="font-semibold">{t("settingsPage.appAccount")}</h3>
    {account && <p className="text-sm">{account.signed_in ? t("settingsPage.signedInAs",{name:account.email ?? ""}) : t("settingsPage.signedOut")}</p>}
    <p className="text-xs text-muted">{t("settingsPage.signingOutLocksThisLocalProfileYourAgents")}</p>
    {error && <Banner tone="danger">{error}</Banner>}
    {account?.signed_in && <Button disabled={busy} onClick={() => {
      setBusy(true); setError(null);
      void request<{ revoked: boolean }>("/desktop-account/logout", { method: "POST" }).then(() => window.location.reload()).catch(err => { setError(err instanceof Error ? `${t("settingsPage.couldNotSignOut")} ${err.message}` : t("settingsPage.couldNotSignOut")); setBusy(false); });
    }}>{busy ? t("settingsPage.signingOut") : t("settingsPage.signOutOfAppAccount")}</Button>}
    {account?.signed_in && <div className="space-y-2"><DesktopAccountDanger kind="reset" onDone={() => window.location.reload()} /><DesktopAccountDanger kind="delete" onDone={() => window.location.reload()} /></div>}
  </section>;
}

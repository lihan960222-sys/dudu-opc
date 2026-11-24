"use client";
import { useT } from "@/lib/use-i18n";

import { useEffect, useState } from "react";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";

type HostedState = { available: boolean; signedIn: boolean; active: boolean };
type Bridge = { hostedWorkspace?(action: "status" | "open"): Promise<HostedState> };

// Desktop app only, and only while the app offers it: the shell answers "unavailable" otherwise,
// so this card never shows in a browser or in a build with the feature off.
export function HostedWorkspaceCard() {
  const t = useT();
  const [state, setState] = useState<HostedState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bridge = (typeof window === "undefined" ? undefined : (window.suparagent as Bridge | undefined));

  useEffect(() => {
    let live = true;
    bridge?.hostedWorkspace?.("status").then(value => { if (live) setState(value); }).catch(() => {});
    return () => { live = false; };
  }, [bridge]);

  if (!state?.available || !bridge?.hostedWorkspace) return null;
  const open = bridge.hostedWorkspace;
  return <section className="mt-6 space-y-3 rounded-lg border border-line p-5">
    <h3 className="font-semibold">{t("settingsPage.hostedWorkspace")}</h3>
    <p className="text-sm">{t("settingsPage.openYourCloudWorkspaceInThisWindowIt")}</p>
    <p className="text-xs text-muted">{t("settingsPage.youSignInToItSeparatelyWithYour")}</p>
    {!state.signedIn && <Banner tone="info">{t("settingsPage.signInToYourAppAccountAboveFirst")}</Banner>}
    {error && <Banner tone="danger">{error}</Banner>}
    <Button disabled={busy || !state.signedIn} onClick={() => {
      setBusy(true); setError(null);
      void open("open").then(setState).catch(err => setError(err instanceof Error ? `${t("settingsPage.couldNotOpenTheHostedWorkspace")} ${err.message}` : t("settingsPage.couldNotOpenTheHostedWorkspace"))).finally(() => setBusy(false));
    }}>{busy ? t("settingsPage.opening") : t("settingsPage.openHostedWorkspace")}</Button>
  </section>;
}

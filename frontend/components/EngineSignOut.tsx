"use client";
import { useT } from "@/lib/use-i18n";

import { useState } from "react";
import { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/Button";
import { Banner } from "@/components/ui/Banner";


export function EngineSignOut({ label, disabled, onSignOut, onChanged, onPendingChange }: {
  label: string;
  disabled: boolean;
  onSignOut: () => Promise<void>;
  onChanged: () => void;
  onPendingChange: (pending: boolean) => void;
}) {
  const t = useT();
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const signOut = async () => {
    setBusy(true);
    setError(null);
    try {
      await onSignOut();
      setConfirming(false);
      onPendingChange(false);
      onChanged();
    } catch (err) {
      // Keep provider and command output out of the settings UI.
      setError(err instanceof ApiError && err.status === 409
        ? t("settingsPage.couldNotConfirmSignOutStopActiveConversations")
        : `Could not sign out of ${label}. Check the engine status and try again.`);
    } finally {
      setBusy(false);
    }
  };

  return <div className="space-y-2">
    {!confirming ? <Button type="button" disabled={disabled || busy} onClick={() => { setError(null); setConfirming(true); onPendingChange(true); }}>{t("settingsPage.signOutOf")} {label}</Button> : <div className="space-y-2 rounded-md border border-warning/50 p-3 text-sm">
      <p>{t("settingsPage.thisSigns")} {label} {t("settingsPage.outOfTheSharedServerCliAccountIt")}</p>
      <div className="flex flex-wrap gap-2">
        <Button type="button" disabled={disabled || busy} onClick={() => void signOut()}>{busy ? t("settingsPage.signingOut") : `Sign out of ${label}`}</Button>
        <Button type="button" disabled={busy} onClick={() => { setConfirming(false); onPendingChange(false); }}>{t("settingsPage.keepSignedIn")}</Button>
      </div>
    </div>}
    {error && <Banner tone="danger">{error}</Banner>}
  </div>;
}

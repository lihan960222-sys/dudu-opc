"use client";
import { useT } from "@/lib/use-i18n";
import { useState } from "react";
import { request } from "@/lib/api";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Field, Input } from "@/components/ui/Input";

type Kind = "reset" | "delete";

const COPY: Record<Kind, { open: string; word: string; path: string; warning: string }> = {
  reset: {
    open: "personalSettings.resetAction",
    word: "RESET",
    path: "/desktop-account/reset-profile",
    warning: "personalSettings.resetWarning",
  },
  delete: {
    open: "personalSettings.deleteAction",
    word: "DELETE",
    path: "/desktop-account/delete-account",
    warning: "personalSettings.deleteWarning",
  },
};

// Explicit, typed confirmation for the two actions that erase local data. Sign-out never does.
export function DesktopAccountDanger({ kind, onDone }: { kind: Kind; onDone: () => void }) {
  const t = useT();
  const copy = COPY[kind];
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setBusy(true); setError(null);
    try {
      await request(copy.path, { method: "POST", body: JSON.stringify({ confirm: copy.word }) });
      onDone();
    } catch (err) {
      setError(err instanceof Error ? `${t("personalSettings.dangerError")} ${err.message}` : t("personalSettings.dangerError"));
      setBusy(false);
    }
  }

  if (!open) return <Button variant="ghost" onClick={() => setOpen(true)}>{t(copy.open)}</Button>;
  return <div className="space-y-3 rounded-md border border-line p-4">
    <Banner tone="warning">{t(copy.warning)}</Banner>
    <Field label={t("personalSettings.confirmWord", { word: copy.word })}>{(props) => <Input {...props} autoComplete="off" value={typed} disabled={busy} onChange={event => setTyped(event.target.value)} />}</Field>
    {error && <Banner tone="danger">{error}</Banner>}
    <div className="flex gap-3">
      <Button variant="danger" disabled={busy || typed !== copy.word} onClick={() => void run()}>{busy ? t("personalSettings.wait") : t(copy.open)}</Button>
      <Button variant="ghost" disabled={busy} onClick={() => { setOpen(false); setTyped(""); setError(null); }}>{t("personalSettings.cancel")}</Button>
    </div>
  </div>;
}

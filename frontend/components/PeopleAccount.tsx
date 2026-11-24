"use client";
import { useT } from "@/lib/use-i18n";

import { useState, type FormEvent } from "react";
import { ApiError } from "@/lib/api";
import { people, type Person } from "@/lib/people";
import { withBase } from "@/lib/paths";
import type { SessionInfo } from "@/lib/session";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Field, Input, Select } from "@/components/ui/Input";

function failure(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

// The signed-in person's own name and password. The owner of an install that predates people has no
// email or password yet: they set both once, confirmed with the owner passphrase, so they can sign in
// as an ordinary member (and hand the workspace over).
export function YourAccount({ session, me, onChanged }: { session: SessionInfo; me: Person | null; onChanged: () => void }) {
  const t = useT();
  const [name, setName] = useState(session.display_name ?? "");
  const [email, setEmail] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  if (session.person_id == null || !me) return null;
  const needsSetup = me.role === "owner" && !me.email;

  const run = async (work: () => Promise<void>, done: string, fallback: string) => {
    setBusy(true);
    setError(null);
    setNote(null);
    try {
      await work();
      setNote(done);
      onChanged();
    } catch (err) {
      setError(failure(err, fallback));
    } finally {
      setBusy(false);
    }
  };

  const saveName = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => { await people.rename(name); }, t("settingsPage.nameSaved"), t("settingsPage.couldNotSaveYourName"));
  };

  const savePassword = (event: FormEvent) => {
    event.preventDefault();
    if (next !== repeat) { setError(t("settingsPage.theTwoPasswordsAreDifferent")); return; }
    void run(async () => {
      await people.changePassword(needsSetup
        ? { new_password: next, email, passphrase }
        : { new_password: next, current_password: current });
      setCurrent(""); setNext(""); setRepeat(""); setPassphrase("");
    }, needsSetup ? t("settingsPage.emailSignInIsSetUpOtherSigned") : t("settingsPage.passwordChangedOtherSignedInBrowsersWereSigned"),
    t("settingsPage.couldNotChangeThePassword"));
  };

  return (
    <div className="space-y-4 rounded-md border border-line p-4" data-testid="your-account">
      <h4 className="text-sm font-medium">{t("settingsPage.yourAccount")}</h4>
      {error && <Banner tone="danger">{error}</Banner>}
      {note && <Banner tone="success">{note}</Banner>}
      <form onSubmit={saveName} className="flex flex-wrap items-end gap-3">
        <Field label={t("settingsPage.yourName")} className="min-w-48 flex-1">
          {(props) => <Input {...props} value={name} onChange={(e) => setName(e.target.value)} maxLength={80} required />}
        </Field>
        <Button type="submit" disabled={busy || !name.trim() || name.trim() === session.display_name}>{t("settingsPage.saveName")}</Button>
      </form>
      <form onSubmit={savePassword} className="space-y-3">
        <p className="text-sm text-muted">
          {needsSetup
            ? t("settingsPage.youSignInWithTheOwnerPassphraseAdd")
            : t("settingsPage.changeYourPasswordYourOtherSignedInBrowsers")}
        </p>
        {needsSetup ? (
          <>
            <Field label={t("settingsPage.email")}>{(props) => <Input {...props} type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" required />}</Field>
            <Field label={t("settingsPage.ownerPassphrase")}>{(props) => <Input {...props} type="password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} autoComplete="current-password" required />}</Field>
          </>
        ) : (
          <Field label={t("settingsPage.currentPassword")}>{(props) => <Input {...props} type="password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" required />}</Field>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label={t("settingsPage.newPassword")} hint="At least 10 characters.">{(props) => <Input {...props} type="password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" required minLength={10} />}</Field>
          <Field label={t("settingsPage.repeatNewPassword")}>{(props) => <Input {...props} type="password" value={repeat} onChange={(e) => setRepeat(e.target.value)} autoComplete="new-password" required />}</Field>
        </div>
        <Button type="submit" disabled={busy}>{needsSetup ? t("settingsPage.setUpEmailSignIn") : t("settingsPage.changePassword")}</Button>
      </form>
    </div>
  );
}

// The owner hands the workspace to another member. The old owner becomes an admin and the owner
// passphrase is replaced, so this signs the owner out; they sign back in with email and password.
export function HandOver({ roster, me }: { roster: Person[]; me: Person | null }) {
  const t = useT();
  const [target, setTarget] = useState("");
  const [passphrase, setPassphrase] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!me || me.role !== "owner") return null;
  const others = roster.filter((p) => !p.is_you && p.role !== "owner");
  if (!me.email) {
    return <p className="text-xs text-muted">{t("settingsPage.toHandThisWorkspaceToSomeoneElseFirst")}</p>;
  }
  if (others.length === 0) return null;
  const chosen = others.find((p) => String(p.id) === target);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!chosen) return;
    if (!confirming) { setConfirming(true); return; }
    setBusy(true);
    setError(null);
    try {
      await people.transfer(chosen.id, passphrase);
      window.location.replace(withBase("/login/"));
    } catch (err) {
      setError(failure(err, t("settingsPage.couldNotHandOverTheWorkspace")));
      setConfirming(false);
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="space-y-3 rounded-md border border-line p-4" data-testid="hand-over">
      <h4 className="text-sm font-medium">{t("settingsPage.handOverThisWorkspace")}</h4>
      <p className="text-xs text-muted">
        {t("settingsPage.theNewOwnerControlsBillingAndPeopleYou")} <span className="font-mono">python -m app.auth.cli set-passphrase</span>.
      </p>
      {error && <Banner tone="danger">{error}</Banner>}
      <Field label={t("settingsPage.newOwner")}>
        {(props) => (
          <Select {...props} value={target} onChange={(e) => { setTarget(e.target.value); setConfirming(false); }}>
            <option value="">{t("settingsPage.chooseAMember")}</option>
            {others.map((p) => <option key={p.id} value={p.id}>{p.display_name}{p.email ? ` (${p.email})` : ""}</option>)}
          </Select>
        )}
      </Field>
      <Field label={t("settingsPage.ownerPassphrase")}>
        {(props) => <Input {...props} type="password" value={passphrase} onChange={(e) => setPassphrase(e.target.value)} autoComplete="current-password" required />}
      </Field>
      <Button type="submit" variant={confirming ? "primary" : undefined} disabled={busy || !chosen || !passphrase}>
        {confirming && chosen ? t("settingsPage.ownerConfirm",{name:chosen.display_name}) : t("settingsPage.handOver")}
      </Button>
    </form>
  );
}

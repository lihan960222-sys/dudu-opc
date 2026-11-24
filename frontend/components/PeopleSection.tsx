"use client";
import { translateNow, useT } from "@/lib/use-i18n";
import { settingsTerm } from "@/lib/locales/settings-pages";

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import { BASE_PATH } from "@/lib/paths";
import {
  AUDIT_LABELS,
  people,
  ROLE_LABELS,
  type AuditRow,
  type Invite,
  type Person,
} from "@/lib/people";
import { inviteLink } from "@/lib/session";
import { useSession } from "./AuthGate";
import { HandOver, YourAccount } from "./PeopleAccount";
import { HostedPeople } from "./HostedPeople";
import { AccessPicker, DEFAULT_ACCESS, type Access } from "./AccessPicker";
import { describePermissions } from "@/lib/permissions";
import { Badge } from "@/components/ui/Badge";
import { Banner } from "@/components/ui/Banner";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { SkeletonList } from "@/components/ui/Skeleton";

function when(iso: string): string {
  return new Date(iso).toLocaleString();
}

function failure(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

// Shown once, right after an invitation is made: the secret is not kept in readable form.
function NewInvite({ invite, onDone }: { invite: Invite & { token: string }; onDone: () => void }) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  const link = inviteLink(window.location.origin, BASE_PATH, invite);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="space-y-3 rounded-md border border-line p-4" aria-live="polite" data-testid="new-invite">
      <p className="text-sm">
        {t("settingsPage.sendThisLinkTo")} <strong>{invite.email}</strong>{t("settingsPage.itWorksOnceOnlyForThatAddressAnd")} {when(invite.expires_at)}{t("settingsPage.itIsShownOnlyNow")}
      </p>
      <p className="break-all rounded bg-inset p-2 font-mono text-xs" data-testid="invite-link">{link}</p>
      <div className="flex gap-2">
        <Button type="button" onClick={copy}>{copied ? t("settingsPage.copied") : t("settingsPage.copyLink")}</Button>
        <Button type="button" variant="ghost" onClick={onDone}>{t("settingsPage.done")}</Button>
      </div>
    </div>
  );
}

// Everyone who works in this workspace. In a hosted workspace the account service owns the list (HostedPeople);
// everywhere else all members see names and roles and admins invite and manage.
export function PeopleSection() {
  const { session } = useSession();
  return session?.kind === "grant" ? <HostedPeople /> : <LocalPeopleSection />;
}

function LocalPeopleSection() {
  const t = useT();
  const { session } = useSession();
  const [roster, setRoster] = useState<Person[] | null>(null);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [audit, setAudit] = useState<AuditRow[] | null>(null);
  const [created, setCreated] = useState<(Invite & { token: string }) | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState<number | null>(null);
  const [email, setEmail] = useState("");
  const [access, setAccess] = useState<Access>(DEFAULT_ACCESS);
  // The person whose access is being edited, and what it is being changed to.
  const [editing, setEditing] = useState<number | null>(null);
  const [draft, setDraft] = useState<Access>(DEFAULT_ACCESS);
  const [showAudit, setShowAudit] = useState(false);

  const admin = session !== null && session.kind !== "desktop" && session.scopes.includes("admin");
  const isOwner = session?.role === "owner" || (session !== null && session.role == null && admin);
  const present = session !== null && session.kind !== "desktop";

  const load = useCallback(async () => {
    try {
      setRoster(await people.list());
      if (admin) setInvites(await people.invites());
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) setUnavailable(true);
      else setError(failure(err, translateNow("settingsPage.couldNotLoadThePeopleInThisWorkspace")));
    }
  }, [admin]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (present) void load();
  }, [present, load]);

  if (!present || unavailable) return null;

  const act = async (work: () => Promise<void>, fallback: string) => {
    setBusy(true);
    setError(null);
    try {
      await work();
      await load();
      if (showAudit) setAudit(await people.audit());
    } catch (err) {
      setError(failure(err, fallback));
    } finally {
      setBusy(false);
    }
  };

  const invite = () => act(async () => {
    const made = await people.invite(email, access.role, access.role === "member" ? access.permissions : undefined);
    setCreated(made as Invite & { token: string });
    setEmail("");
    setAccess(DEFAULT_ACCESS);
  }, t("settingsPage.couldNotCreateTheInvitation"));

  const toggleAudit = async () => {
    const next = !showAudit;
    setShowAudit(next);
    if (next) {
      try { setAudit(await people.audit()); } catch (err) { setError(failure(err, t("settingsPage.couldNotLoadTheHistory"))); }
    }
  };

  const remove = (person: Person) => {
    if (confirming !== person.id) { setConfirming(person.id); return; }
    setConfirming(null);
    void act(() => people.remove(person.id), t("settingsPage.removeFailed",{name:person.display_name}));
  };

  const saveAccess = (person: Person) => act(async () => {
    await people.update(person.id, draft.role === person.role && draft.role !== "member"
      ? { role: draft.role }
      : { role: draft.role, ...(draft.role === "member" ? { permissions: draft.permissions } : {}) });
    setEditing(null);
  }, t("settingsPage.couldNotChangeThatAccess"));

  // An admin changes members and viewers; only the owner touches admins.
  const canManage = (person: Person) => admin && !person.is_you && person.role !== "owner" && (person.role !== "admin" || isOwner);

  return (
    <section className="space-y-4 rounded-lg border border-line p-5" aria-labelledby="people-heading">
      <h3 id="people-heading" className="font-semibold">{t("settingsPage.people")}</h3>
      <p className="text-xs text-muted">
        {t("settingsPage.everyoneHereWorksInTheSameWorkspaceAnd")}
        {session?.display_name ? " " + t("settingsPage.signedInAs",{name:session.display_name}) + (session.role ? ` (${settingsTerm(t,ROLE_LABELS[session.role])})` : "") : ""}
      </p>
      {error && <Banner tone="danger">{error}</Banner>}

      {session && <YourAccount session={session} me={roster?.find((p) => p.is_you) ?? null} onChanged={() => void load()} />}

      {roster === null ? <SkeletonList rows={3} /> : (
        <ul className="divide-y divide-line" data-testid="people-list">
          {roster.map((person) => (
            <li key={person.id} className="flex flex-wrap items-center justify-between gap-2 py-2" data-testid="person-row">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">
                  {person.display_name}
                  {person.is_you && <span className="ml-2 rounded bg-inset px-1.5 py-0.5 text-xs font-normal">{t("settingsPage.you")}</span>}
                </p>
                <p className="text-xs text-muted">
                  {[
                    person.email, // only admins are shown addresses
                    t("settingsPage.joinedDate",{date:when(person.joined_at)}),
                    person.last_login_at ? t("settingsPage.lastLogin",{date:when(person.last_login_at)}) : null,
                  ].filter(Boolean).join(" · ")}
                </p>
              </div>
              <div className="flex items-center gap-2">
                {canManage(person) ? (
                  <Button type="button" disabled={busy} aria-expanded={editing === person.id}
                    aria-label={t("settingsPage.changeAccess",{name:person.display_name,access:describePermissions(person.role,person.permissions).split(", ").map(label => settingsTerm(t,label)).join(" / ")})}
                    onClick={() => {
                      if (editing === person.id) { setEditing(null); return; }
                      setDraft({ role: person.role as Access["role"], permissions: [...person.permissions] });
                      setEditing(person.id);
                    }}>
                    {describePermissions(person.role, person.permissions).split(", ").map(label => settingsTerm(t,label)).join(" / ")}
                  </Button>
                ) : (
                  <Badge>{describePermissions(person.role, person.permissions).split(", ").map(label => settingsTerm(t,label)).join(" / ")}</Badge>
                )}
                {canManage(person) && (
                  <Button type="button" disabled={busy} onClick={() => remove(person)} aria-label={confirming === person.id ? t("settingsPage.confirmRemoveName",{name:person.display_name}) : t("settingsPage.removeName",{name:person.display_name})}>
                    {confirming === person.id ? t("settingsPage.confirmRemove") : t("settingsPage.remove")}
                  </Button>
                )}
              </div>
              {editing === person.id && (
                <div className="w-full space-y-3 rounded-md border border-line p-3" data-testid="access-editor">
                  <AccessPicker value={draft} onChange={setDraft} isOwner={isOwner} disabled={busy} />
                  <div className="flex gap-2">
                    <Button type="button" variant="primary" disabled={busy} onClick={() => void saveAccess(person)}>{t("settingsPage.save")}</Button>
                    <Button type="button" variant="ghost" onClick={() => setEditing(null)}>{t("settingsPage.cancel")}</Button>
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      {admin && (
        <div className="space-y-3">
          <h4 className="text-sm font-medium">{t("settingsPage.inviteSomeone")}</h4>
          {created ? (
            <NewInvite invite={created} onDone={() => setCreated(null)} />
          ) : (
            <form className="space-y-3" onSubmit={(e) => { e.preventDefault(); void invite(); }}>
              <div className="space-y-1">
                <label htmlFor="invite-email" className="block text-sm font-medium">{t("settingsPage.email")}</label>
                <Input id="invite-email" type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                  placeholder="teammate@example.com" required maxLength={254} autoComplete="off" />
              </div>
              <AccessPicker value={access} onChange={setAccess} isOwner={isOwner} disabled={busy} />
              <Button type="submit" disabled={busy || !email.trim()}>{t("settingsPage.createInvitationLink")}</Button>
            </form>
          )}

          {invites.length > 0 && (
            <div>
              <h4 className="mb-1 text-sm font-medium">{t("settingsPage.waitingToJoin")}</h4>
              <ul className="divide-y divide-line" data-testid="invite-list">
                {invites.map((item) => (
                  <li key={item.id} className="flex flex-wrap items-center justify-between gap-2 py-2">
                    <p className="min-w-0 truncate text-sm">
                      {item.email} <span className="text-xs text-muted">· {describePermissions(item.role, item.permissions).split(", ").map(label => settingsTerm(t,label)).join(" / ")} {t("settingsPage.expires")} {when(item.expires_at)}</span>
                    </p>
                    <Button type="button" disabled={busy || (item.role === "admin" && !isOwner)} aria-label={t("settingsPage.revokeName",{name:item.email})}
                      onClick={() => void act(() => people.revokeInvite(item.id), t("settingsPage.couldNotRevokeThatInvitation"))}>
                      {t("settingsPage.revoke")}
                    </Button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <HandOver roster={roster ?? []} me={roster?.find((p) => p.is_you) ?? null} />

          <div>
            <Button type="button" variant="ghost" onClick={toggleAudit} aria-expanded={showAudit}>
              {showAudit ? t("settingsPage.hideHistory") : t("settingsPage.showHistory")}
            </Button>
            {showAudit && (audit === null ? <SkeletonList rows={3} /> : audit.length === 0 ? (
              <p className="mt-2 text-sm text-muted">{t("settingsPage.nothingHasChangedYet")}</p>
            ) : (
              <ul className="mt-2 divide-y divide-line" data-testid="audit-list">
                {audit.map((row) => (
                  <li key={row.id} className="py-1.5 text-sm">
                    <span className="font-medium">{row.actor || t("settingsPage.someone")}</span> {AUDIT_LABELS[row.action] ?? row.action} <span className="font-medium">{row.subject}</span>
                    {row.detail && <span className="text-muted"> ({row.detail})</span>}
                    <span className="block text-xs text-muted">{when(row.at)}</span>
                  </li>
                ))}
              </ul>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

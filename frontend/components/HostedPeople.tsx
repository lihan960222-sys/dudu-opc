"use client";
import { translateNow, useT } from "@/lib/use-i18n";
import { settingsTerm } from "@/lib/locales/settings-pages";

import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Banner } from "@/components/ui/Banner";
import { SkeletonList } from "@/components/ui/Skeleton";
import { activeWorkspace, listWorkspaces, roleLabel, sortRoster, workspaceRoster, type RosterMember, type Workspace } from "@/lib/workspaces";

// People in a hosted workspace. The account service decides who belongs and in what role, so this screen only lists
// them (names and roles, from the account service, not from people who happened to open this computer) and sends
// owners and admins to the page where people are managed.
export function HostedPeople() {
  const t = useT();
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [members, setMembers] = useState<RosterMember[] | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let live = true;
    listWorkspaces()
      .then(async (list) => {
        const current = activeWorkspace(list);
        if (!current) throw new Error(translateNow("settingsPage.noActiveWorkspace"));
        const roster = await workspaceRoster(current.id);
        if (!live) return;
        setWorkspace(current);
        setMembers(sortRoster(roster));
      })
      .catch(() => {
        if (live) setFailed(true);
      });
    return () => {
      live = false;
    };
  }, []);

  const manage = workspace?.role === "owner" || workspace?.role === "admin";
  return (
    <section aria-labelledby="people-heading" className="space-y-3">
      <h2 id="people-heading" className="text-base font-semibold">
        {t("settingsPage.people")}{workspace ? ` in ${workspace.name}` : ""}
      </h2>
      {failed && <Banner tone="danger">{t("settingsPage.couldNotLoadThePeopleInThisWorkspace")}</Banner>}
      {!failed && members === null && <SkeletonList rows={3} />}
      {members && (
        <ul className="divide-y divide-line rounded-md border border-line" data-testid="hosted-people">
          {members.map((m) => (
            <li key={`${m.role}-${m.name}`} className="flex items-center justify-between gap-2 px-3 py-2 text-sm">
              <span className="min-w-0 truncate">
                {m.name}
                {m.me ? t("settingsPage.you") : ""}
              </span>
              <Badge>{settingsTerm(t,roleLabel(m.role))}</Badge>
            </li>
          ))}
        </ul>
      )}
      <p className="text-xs text-muted">
        {manage ? (
          <>
            {t("settingsPage.invitePeopleChangeRolesAndPermissionsOrRemove")}{" "}
            <a className="underline" href="/account/workspaces">
              {t("settingsPage.workspacesPage")}
            </a>
            .
          </>
        ) : (
          t("settingsPage.peopleAreManagedByTheWorkspaceOwnerAnd")
        )}
      </p>
    </section>
  );
}

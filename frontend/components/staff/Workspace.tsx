"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState, useSyncExternalStore } from "react";
import { Activity, FolderKanban, Layers3, ListTodo, LogOut, MessageSquare, Monitor, MoreHorizontal, Settings2, UsersRound } from "lucide-react";
import { canOpenWorkbench } from "@/lib/session";
import { auth } from "@/lib/auth";
import { useT } from "@/lib/use-i18n";
import { workspaceHref, workspaceSection } from "@/lib/workspace-navigation";
import { managedWindowId, requestManagedWindowExit } from "@/lib/managed-window";
import { useSession } from "../AuthGate";
import { ChatApp } from "../chat/ChatApp";
import { Menu, MenuItem, MenuSeparator } from "../ui/Menu";
import { Workbench } from "./Workbench";
import "./workspace.css";

const primary = [
  { id: "chat", icon: MessageSquare },
  { id: "people", icon: UsersRound },
  { id: "projects", icon: FolderKanban },
] as const;

const subscribeToWindow = () => () => {};

export function Workspace() {
  const { session } = useSession();
  return canOpenWorkbench(session) ? <OwnerWorkspace /> : <ChatApp />;
}

function OwnerWorkspace() {
  const { session } = useSession();
  const windowId = useSyncExternalStore(subscribeToWindow, () => managedWindowId(window, session), () => null);
  const search = useSearchParams().toString();
  const router = useRouter();
  const t = useT();
  const section = workspaceSection(search, true);
  const [chatEmployee, setChatEmployee] = useState<number | null>();
  const [profileRevision, setProfileRevision] = useState(0);
  const employee = section === "chat" ? chatEmployee : new URLSearchParams(search).get("employee") ?? chatEmployee;
  const moreActive = section === "activity" || section === "host";
  const chat = workspaceHref(search, "chat");
  return (
    <div className="opc-workspace">
      <nav aria-label={t("workspace.name")} className="opc-workspace-nav">
        <div className="opc-workspace-brand"><Layers3 size={21} aria-hidden="true" /><span>{t("workspace.name")}</span></div>
        {primary.map(({ id, icon: Icon }) => (
          <Link key={id} href={workspaceHref(search, id, employee)} scroll={false} aria-current={section === id ? "page" : undefined}>
            <Icon size={18} aria-hidden="true" /><span>{t(`workspace.${id}`)}</span>
          </Link>
        ))}
        <Menu align="start" trigger={<button type="button" className="opc-workspace-more" aria-current={moreActive ? "page" : undefined}><MoreHorizontal size={18} aria-hidden="true" /><span>{t("workspace.more")}</span></button>}>
          <MenuItem icon={<Activity size={16} />} onSelect={() => router.push(workspaceHref(search, "activity", employee), { scroll: false })}>{t("workspace.activity")}</MenuItem>
          <MenuItem icon={<Monitor size={16} />} onSelect={() => router.push(workspaceHref(search, "host", employee), { scroll: false })}>{t("workspace.host")}</MenuItem>
          <MenuItem icon={<ListTodo size={16} />} onSelect={() => router.push(chat + (chat.includes("?") ? "&" : "?") + "view=planner", { scroll: false })}>{t("workspace.planner")}</MenuItem>
          <MenuItem icon={<Settings2 size={16} />} onSelect={() => router.push("/settings/")}>{t("sidebar.settings")}</MenuItem>
          {windowId && <>
            <MenuSeparator />
            <MenuItem icon={<LogOut size={16} />} onSelect={() => void requestManagedWindowExit(window, session, auth.session)}>{t("workspace.exitDudu")}</MenuItem>
          </>}
        </Menu>
      </nav>
      <div className="opc-workspace-content">
        {(section === "chat" || chatEmployee !== undefined) && <ChatApp
          unified foreground={section === "chat"} onEmployeeChange={setChatEmployee} profileRevision={profileRevision}
        />}
        {section !== "chat" && <Workbench page={section} onChanged={() => setProfileRevision(revision => revision + 1)} />}
      </div>
    </div>
  );
}

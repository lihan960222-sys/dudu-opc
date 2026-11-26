import type { SessionInfo } from "./session";

export function canManageProjectTerminal(session: SessionInfo | null): boolean {
  return session?.kind === "browser" && (session.session_id ?? 0) > 0
    && session.role === "owner" && (session.person_id ?? 0) > 0 && session.scopes.includes("admin");
}

export const directoryModes = {
  plan: {label: "只读", help: "可以查看自己的工作目录和拟定计划。"},
  ask: {label: "编辑前确认", help: "可以查看自己的工作目录，修改文件前需审批。"},
  accept_edits: {label: "允许编辑", help: "可以在自己的工作目录内读取、新建和修改文件。"},
} as const;

export type DirectoryMode = keyof typeof directoryModes;
export function directoryMode(mode?: string | null): DirectoryMode {
  if (mode === "plan") return "plan";
  if (mode === "accept_edits" || mode === "full_access") return "accept_edits";
  return "ask";
}

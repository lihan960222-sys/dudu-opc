export type WorkspaceSection = "chat" | "people" | "projects" | "activity" | "host";

function chatUrl(params: URLSearchParams) {
  const query = params.toString();
  return query ? `/chat/?${query}` : "/chat/";
}

export function isStewardChatEntry(search: string) {
  const params = new URLSearchParams(search);
  if (params.get("section") === "chat") params.delete("section");
  return params.size === 0;
}

export function workspaceSection(search: string, canManage: boolean): WorkspaceSection {
  if (!canManage) return "chat";
  const params = new URLSearchParams(search);
  if (params.has("feishu_oauth") || params.get("capability") === "feishu" || params.get("view") === "agents") return "people";
  const section = params.get("section");
  if (section === "people" || section === "projects" || section === "activity" || section === "host") return section;
  return "chat";
}

export function workspaceHref(search: string, section: WorkspaceSection, employee?: string | number | null, advanced = false) {
  const current = new URLSearchParams(search);
  const next = new URLSearchParams();
  for (const key of ["thread", "agent", "room", "plain"]) {
    const value = current.get(key);
    if (value) next.set(key, value);
  }
  if (section !== "chat") next.set("section", section);
  if (section !== "chat" && employee) {
    next.set("employee", String(employee));
    if (section === "people" && advanced) next.set("tools", "1");
  }
  return chatUrl(next);
}

// The original home URL is also the registered OAuth return address.
export function homeDestination(search: string, hash: string, canManage: boolean) {
  const params = new URLSearchParams(search);
  if (!canManage) {
    for (const key of ["section", "employee", "tools", "capability", "feishu_oauth"]) params.delete(key);
    return chatUrl(params);
  }
  if (hash === "#feishu-access") params.set("capability", "feishu");
  const section = workspaceSection(params.toString(), true);
  if (section !== "chat") params.set("section", section);
  else if (params.has("employee") || !["thread", "agent", "room", "view", "panel"].some(key => params.has(key))) params.set("section", "people");
  return chatUrl(params) + hash;
}

export function createdThreadHref(search: string, id: number) {
  const params = new URLSearchParams(search);
  params.delete("agent");
  params.delete("plain");
  params.set("thread", String(id));
  return chatUrl(params);
}

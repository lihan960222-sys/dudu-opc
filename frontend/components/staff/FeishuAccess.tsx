"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { request } from "@/lib/api";
import type { TeamAgent } from "@/lib/collaboration";

type Source = { chat_id: string; peer_open_id: string; agent_id: number | null };
type Access = {
  revision: string;
  allow_codex_summary: boolean;
  history_sources: Record<string, Source>;
  document_target: { tenant_origin?: string; folder_token?: string };
};
type OAuth = {
  connected: boolean;
  scopes: string[];
  actions: { history: boolean; doc: boolean };
  redirect_uri: string;
};
const oauth = "/feishu/owner-oauth";
const settings = "/staff/feishu";

export function FeishuAccess({ team }: { team: TeamAgent[] }) {
  const [access, setAccess] = useState<Access | null>(null);
  const [auth, setAuth] = useState<OAuth | null>(null);
  const [alias, setAlias] = useState("");
  const [chat, setChat] = useState("");
  const [assignee, setAssignee] = useState("");
  const [folder, setFolder] = useState("");
  const [busy, setBusy] = useState(false);
  const [pendingAnalysis, setPendingAnalysis] = useState<boolean | null>(null);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const accept = useCallback((next: Access) => {
    setAccess(next);
    const target = next.document_target;
    setFolder(target.folder_token ? target.tenant_origin + "/drive/folder/" + target.folder_token : "");
  }, []);
  const loadId = useRef<symbol | null>(null);
  const load = useCallback(() => {
    const current = Symbol();
    loadId.current = current;
    return Promise.allSettled([
      request<Access>(settings + "/access"), request<OAuth>(oauth + "/status")
    ]).then(([policy, connection]) => {
      if (current !== loadId.current) return;
      if (policy.status === "fulfilled") accept(policy.value);
      if (connection.status === "fulfilled") setAuth(connection.value);
      const errors = [policy, connection].flatMap(r => r.status === "rejected" ? [String(r.reason)] : []);
      setError(errors.join("。"));
    });
  }, [accept]);
  useEffect(() => {
    void load();
    return () => { loadId.current = null; };
  }, [load]);

  async function run(work: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await work(); } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  }
  async function connect(action: "doc" | "history" | "both") {
    const next = await request<{authorize_url: string}>(oauth + "/start", {
      method: "POST", body: JSON.stringify({ action })
    });
    const url = new URL(next.authorize_url);
    if (url.origin !== "https://accounts.feishu.cn") throw Error("请检查飞书授权地址。");
    window.location.assign(url.href);
  }
  async function saveContact() {
    if (!access) return;
    const updated = await request<Access>(settings + "/contacts", {method:"POST", body:JSON.stringify({
      revision:access.revision, alias:alias.trim(), chat:chat.trim(), agent_id:assignee ? Number(assignee) : null
    })});
    accept(updated); setAlias(""); setChat(""); setAssignee(""); setNotice("联系人和经办人已保存。");
  }
  async function remove(name: string) {
    if (!access) return;
    accept(await request<Access>(settings + "/contacts/" + encodeURIComponent(name), {
      method:"DELETE", body:JSON.stringify({revision:access.revision})
    }));
    setNotice("已取消这位联系人的读取授权。");
  }
  async function saveFolder() {
    if (!access) return;
    accept(await request<Access>(settings + "/document-target", {method:"PUT",
      body:JSON.stringify({revision:access.revision,folder_url:folder.trim()})}));
    setNotice(folder.trim() ? "文档文件夹已保存。" : "已关闭文档写入。");
  }
  const lead = team.find(person => person.is_lead)?.name ?? "管家";
  return (
    <section className="dp-feishu" aria-labelledby="feishu-access">
      <h2 id="feishu-access">飞书个人授权</h2>
      <p>指定可以查阅的单聊和新文档的存放位置。结果由管家私聊发给你。</p>
      <fieldset disabled={busy} className="dp-fields">
        <legend>连接你的飞书账号</legend>
        <p>{auth?.actions.history ? "私聊已授权" : "私聊待授权"} · {auth?.actions.doc ? "新建文档已授权" : "新建文档待授权"}</p>
        <div className="dp-actions">
          <button className="dp-button" onClick={() => void run(() => connect("history"))}>授权读取私聊</button>
          <button className="dp-button" onClick={() => void run(() => connect("doc"))}>授权新建文档</button>
          {auth && !auth.actions.history && !auth.actions.doc && <button className="dp-link" onClick={() => void run(() => connect("both"))}>同时授权两项</button>}
          {auth?.connected && <button className="dp-link" onClick={() => void run(async () => {
            setAuth(await request<OAuth>(oauth + "/connection", {method:"DELETE"}));
            setNotice("已断开本机的飞书个人授权。");
          })}>断开个人授权</button>}
        </div>
        <details>
          <summary>在飞书开放平台开通权限</summary>
          <p>在应用的权限管理中选择用户身份。读取私聊需要 <code>im:chat:read</code>、<code>im:message:readonly</code> 和 <code>im:message.p2p_msg:get_as_user</code>。新建文档需要 <code>docx:document:create</code> 和 <code>docx:document:write_only</code>。持续使用还需要 <code>offline_access</code>。</p>
          <p>把下面的地址填入应用的「安全设置 → 重定向 URL」，发布新版本，再回这里授权。</p>
          {auth?.redirect_uri && <input aria-label="飞书重定向 URL" readOnly value={auth.redirect_uri} onFocus={e => e.target.select()} />}
        </details>
      </fieldset>
      <fieldset disabled={busy || !access} className="dp-fields">
        <legend>允许查阅的单聊</legend>
        {Object.entries(access?.history_sources ?? {}).map(([name, source]) =>
          <div className="dp-listrow" key={name}><div><strong>{name}</strong><small>经办人 · {source.agent_id ? team.find(a => a.id === source.agent_id)?.name ?? "已离职员工" : lead}</small></div>
            <div className="dp-actions">
              <button className="dp-link" onClick={() => { setAlias(name); setChat(source.chat_id); setAssignee(source.agent_id ? String(source.agent_id) : ""); }}>编辑</button>
              <button className="dp-link" onClick={() => void run(() => remove(name))}>移除</button>
            </div></div>)}
        <div className="dp-pair">
          <label>联系人简称<input value={alias} maxLength={40} onChange={e => setAlias(e.target.value)} placeholder="如小周" /></label>
          <label>经办人<select value={assignee} onChange={e => setAssignee(e.target.value)}><option value="">{lead}</option>
            {team.filter(a => !a.is_lead).map(a => <option key={a.id} value={a.id}>{a.name}</option>)}</select></label>
        </div>
        <label>所选单聊的链接或会话 ID<input value={chat} onChange={e => setChat(e.target.value)} placeholder="粘贴带会话 ID 的链接，或 oc_ 开头的 ID" /></label>
        <button className="dp-button" disabled={!alias.trim() || !chat.trim() || !auth?.actions.history} onClick={() => void run(saveContact)}>核对并保存联系人</button>
        <label className="dp-check">
          <input type="checkbox" checked={pendingAnalysis ?? access?.allow_codex_summary ?? false} onChange={e => {
            const enabled = e.target.checked;
            if (!access) return;
            setPendingAnalysis(enabled);
            void run(async () => {
              accept(await request<Access>(settings + "/analysis", {method:"PUT",
                body:JSON.stringify({revision:access.revision,enabled})}));
              setNotice(enabled ? "已允许指定员工用 OpenAI Codex 分析所选片段。" : "私聊整理已切回本机。");
            }).finally(() => setPendingAnalysis(null));
          }} />
          允许经办人在我要求分析时把所选聊天摘录发给 OpenAI Codex
        </label>
      </fieldset>
      <fieldset disabled={busy || !access} className="dp-fields">
        <legend>新建飞书文档</legend>
        <label>云盘文件夹链接<input value={folder} onChange={e => setFolder(e.target.value)} placeholder="粘贴你有编辑权限的飞书文件夹链接" /></label>
        <div className="dp-actions"><button className="dp-button" onClick={() => void run(saveFolder)}>保存文件夹</button><small>清空后保存可关闭写入。</small></div>
      </fieldset>
      {error && <div role="alert" className="dp-error">{error} <button className="dp-link" onClick={() => void load()}>重新读取</button></div>}
      <p role="status" className="dp-toaster">{notice}</p>
    </section>
  );
}

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { request } from "@/lib/api";
import { assignedContact, FEISHU_RETURN_EMPLOYEE, type FeishuContact } from "@/lib/feishu-employee";

type Access = {
  revision: string;
  allow_codex_summary: boolean;
  history_sources: Record<string, FeishuContact>;
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

export function FeishuAccess({ employee, name, isLead, members }: {
  employee: number; name: string; isLead: boolean; members: number[];
}) {
  const [access, setAccess] = useState<Access | null>(null);
  const [auth, setAuth] = useState<OAuth | null>(null);
  const [alias, setAlias] = useState("");
  const [chat, setChat] = useState("");
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
    try { sessionStorage.setItem(FEISHU_RETURN_EMPLOYEE, String(employee)); } catch { /* return to lead */ }
    window.location.assign(url.href);
  }
  async function saveContact() {
    if (!access) return;
    const existing = Object.hasOwn(access.history_sources, alias.trim()) ? access.history_sources[alias.trim()] : undefined;
    if (existing && !assignedContact(existing, employee, isLead)) throw Error("这个简称已绑定其他岗位，请换一个简称，或先在原岗位移除。");
    const updated = await request<Access>(settings + "/contacts", {method:"POST", body:JSON.stringify({
      revision:access.revision, alias:alias.trim(), chat:chat.trim(), agent_id:isLead ? existing?.agent_id ?? null : employee
    })});
    accept(updated); setAlias(""); setChat(""); setNotice("已保存本岗位的联系人。");
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
  const contacts = Object.entries(access?.history_sources ?? {});
  const assigned = contacts.filter(([,source]) => assignedContact(source, employee, isLead));
  const orphaned = isLead ? contacts.filter(([,source]) => source.agent_id !== null && !members.includes(source.agent_id)) : [];
  return (
    <section className="dp-feishu" aria-labelledby="feishu-access">
      <h3 id="feishu-access">{name}的飞书能力</h3>
      <p>在和管家的私聊中发起查阅或整理。{isLead ? "管家可查阅你已登记的联系人。" : "点名这位员工整理时，只能使用分配给该岗位的联系人。"}</p>
      <fieldset disabled={busy} className="dp-fields">
        <legend>老板的飞书账号</legend>
        <p>授权由本机共用。{auth?.actions.history ? "已授权读取私聊。" : "读取私聊待授权。"}{isLead && (auth?.actions.doc ? "已授权新建文档。" : "新建文档待授权。")}</p>
        <div className="dp-actions">
          <button className="dp-button" onClick={() => void run(() => connect("history"))}>授权读取私聊</button>
          {isLead && <button className="dp-button" onClick={() => void run(() => connect("doc"))}>授权新建文档</button>}
          {isLead && auth && !auth.actions.history && !auth.actions.doc && <button className="dp-link" onClick={() => void run(() => connect("both"))}>同时授权两项</button>}
          {isLead && auth?.connected && <button className="dp-link" onClick={() => void run(async () => {
            setAuth(await request<OAuth>(oauth + "/connection", {method:"DELETE"}));
            setNotice("已断开本机的飞书个人授权。");
          })}>断开本机共享授权</button>}
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
        {assigned.map(([name, source]) =>
          <div className="dp-listrow" key={name}><strong>{name}</strong>
            <div className="dp-actions">
              <button className="dp-link" onClick={() => { setAlias(name); setChat(source.chat_id); }}>编辑</button>
              <button className="dp-link" onClick={() => void run(() => remove(name))}>移除</button>
            </div></div>)}
        <label>联系人简称<input value={alias} maxLength={40} onChange={e => setAlias(e.target.value)} placeholder="如小周" /></label>
        <label>所选单聊的链接或会话 ID<input value={chat} onChange={e => setChat(e.target.value)} placeholder="粘贴带会话 ID 的链接，或 oc_ 开头的 ID" /></label>
        <button className="dp-button" disabled={!alias.trim() || !chat.trim() || !auth?.actions.history} onClick={() => void run(saveContact)}>核对并保存联系人</button>
        {orphaned.length > 0 && <details><summary>经办人已离开团队的联系人</summary>
          {orphaned.map(([alias]) => <div key={alias}>{alias} <button className="dp-link" onClick={() => void run(() => remove(alias))}>移除</button></div>)}
        </details>}
        {isLead && <label className="dp-check">
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
          允许已绑定联系人经办人在我要求时将摘录发送至 OpenAI Codex（共享设置）
        </label>}
      </fieldset>
      {isLead && <fieldset disabled={busy || !access} className="dp-fields">
        <legend>新建飞书文档</legend>
        <label>云盘文件夹链接<input value={folder} onChange={e => setFolder(e.target.value)} placeholder="粘贴你有编辑权限的飞书文件夹链接" /></label>
        <div className="dp-actions"><button className="dp-button" onClick={() => void run(saveFolder)}>保存文件夹</button><small>清空后保存可关闭写入。</small></div>
      </fieldset>}
      {error && <div role="alert" className="dp-error">{error} <button className="dp-link" onClick={() => void load()}>重新读取</button></div>}
      <p role="status" className="dp-toaster">{notice}</p>
    </section>
  );
}

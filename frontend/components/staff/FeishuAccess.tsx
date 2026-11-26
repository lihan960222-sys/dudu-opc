"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { request } from "@/lib/api";
import { assignedContact, FEISHU_RETURN_EMPLOYEE, type FeishuContact } from "@/lib/feishu-employee";

type Access = {
  revision: string;
  allow_codex_summary: boolean;
  employee_summary_vendors?: Record<string, string[]>;
  history_sources: Record<string, FeishuContact>;
  document_target: { tenant_origin?: string; folder_token?: string };
};
type OAuth = {
  connected: boolean;
  scopes: string[];
  actions: { history: boolean; doc: boolean; delete?: boolean; wiki?: boolean; folder?: boolean };
  redirect_uri: string;
  permissions_url?: string;
  consent?: {state: "expired" | "failed" | "processing" | "awaiting_return"; missing_scopes: string[]} | null;
};
const oauth = "/feishu/owner-oauth";
const settings = "/staff/feishu";

export function FeishuAccess({ employee, name, isLead, members }: {
  employee: number; name: string; isLead: boolean; members: number[];
}) {
  const [access, setAccess] = useState<Access | null>(null);
  const [auth, setAuth] = useState<OAuth | null>(null);
  const [authError, setAuthError] = useState("");
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
  const authLoadId = useRef<symbol | null>(null);
  const readAuth = useCallback(() => {
    const current = Symbol();
    authLoadId.current = current;
    return request<OAuth>(oauth + "/status?employee=" + employee).then(next => {
      if (authLoadId.current === current) { setAuth(next); setAuthError(""); }
    }).catch(err => {
      if (authLoadId.current === current) { setAuth(null); setAuthError(String(err)); }
    });
  }, [employee]);
  const loadId = useRef<symbol | null>(null);
  const load = useCallback(() => {
    const current = Symbol();
    loadId.current = current;
    const policy = request<Access>(settings + "/access").then(next => {
      if (loadId.current === current) { accept(next); setError(""); }
    }).catch(err => {
      if (loadId.current === current) setError(String(err));
    });
    return Promise.all([policy, readAuth()]);
  }, [accept, readAuth]);
  useEffect(() => {
    void load();
    const onFocus = () => { void readAuth(); };
    window.addEventListener("focus", onFocus);
    return () => {
      authLoadId.current = null;
      loadId.current = null;
      window.removeEventListener("focus", onFocus);
    };
  }, [readAuth, load]);

  async function run(work: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await work(); } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
    finally { setBusy(false); }
  }
  async function connect(action: "doc" | "history" | "both" | "delete" | "folder" | "wiki") {
    const next = await request<{authorize_url: string}>(oauth + "/start?employee=" + employee, {
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
        {auth ? <p>当前应用：{name}。{auth.actions.history ? "已授权读取私聊。" : "读取私聊待授权。"}{auth.actions.doc ? "已授权文档读写。" : "文档读写待授权。"}</p>
          : <p role="status">{authError ? "暂时无法读取飞书授权状态。" : "正在读取飞书授权状态…"}</p>}
        {auth && <div className="dp-actions">
          <button className="dp-button" onClick={() => void run(() => connect("history"))}>授权读取私聊</button>
          <button className="dp-button" onClick={() => void run(() => connect("doc"))}>授权文档读写</button>
          {!isLead && (auth.actions.wiki ? <span>已授权识别知识库链接</span> :
            <button className="dp-button" onClick={() => void run(() => connect("wiki"))}>授权识别知识库链接</button>)}
          {!isLead && (auth.actions.folder ? <span>已授权核对目标文件夹</span> :
            <button className="dp-button" onClick={() => void run(() => connect("folder"))}>授权核对目标文件夹</button>)}
          {!isLead && (auth.actions.delete ? <span>已授权删除员工创建的指定文档</span> :
            <button className="dp-button" onClick={() => void run(() => connect("delete"))}>授权删除指定文档</button>)}
          {isLead && !auth.actions.history && !auth.actions.doc && <button className="dp-link" onClick={() => void run(() => connect("both"))}>同时授权两项</button>}
          {auth.connected && <button className="dp-link" onClick={() => void run(async () => {
            const next = await request<OAuth>(oauth + "/connection?employee=" + employee, {method:"DELETE"});
            authLoadId.current = null;
            setAuth(next); setAuthError("");
            setNotice("已断开该应用在本机的飞书授权。");
          })}>断开此应用授权</button>}
        </div>}
        {auth?.consent && <p role="status">
          {auth.consent.state === "failed" ? "上次授权未保存，请核对应用权限后重新授权。" :
            auth.consent.state === "expired" ? "上次授权已过期，请重新发起缺少的授权。" :
            auth.consent.state === "processing" ? "已收到飞书回调，正在核对授权。" : "等待飞书回到工作台。在授权页点「同意」后等它自动返回。"}
          {" 缺少 "}{auth.consent.missing_scopes.join("、")}
        </p>}
        <details>
          <summary>在飞书开放平台开通权限</summary>
          {auth?.permissions_url && <a href={auth.permissions_url} target="_blank" rel="noreferrer">打开{name}应用的权限管理</a>}
          <p>在应用的权限管理中选择用户身份。读取私聊需要 <code>im:chat:read</code>、<code>im:message:readonly</code> 和 <code>im:message.p2p_msg:get_as_user</code>。文档读写需要 <code>docx:document:create</code>、<code>docx:document:write_only</code> 和 <code>docx:document:readonly</code>。打开知识库链接还需要 <code>wiki:node:read</code>。核对文档所在文件夹需要 <code>space:document:retrieve</code>。删除指定文档需要单独开通 <code>space:document:delete</code>。持续使用还需要 <code>offline_access</code>。</p>
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
        {auth && !auth.actions.history && <p role="status">
          工作台还没有收到{name}的读取凭据。完成授权后才能核对这个联系人。
          <button type="button" className="dp-link" disabled={busy} onClick={() => void run(() => connect("history"))}>去授权读取私聊</button>
        </p>}
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
        {!isLead && access && <fieldset disabled={busy} className="dp-fields">
          <legend>可整理指定私聊的模型供应商</legend>
          {(["openai","deepseek","anthropic","google"] as const).map(vendor => {
            const selected = access.employee_summary_vendors?.[String(employee)] ?? (access.allow_codex_summary ? ["openai"] : []);
            return <label className="dp-check" key={vendor}>
              <input type="checkbox" checked={selected.includes(vendor)} onChange={e => {
                const vendors = e.target.checked ? [...selected,vendor] : selected.filter(v => v !== vendor);
                void run(async () => {
                  accept(await request<Access>(settings + "/analysis/" + employee,{method:"PUT",
                    body:JSON.stringify({revision:access.revision,vendors})}));
                  setNotice("已保存这名员工的私聊整理授权。");
                });
              }} />
              {{openai:"OpenAI",deepseek:"DeepSeek",anthropic:"Anthropic",google:"Google"}[vendor]}
            </label>;
          })}
        </fieldset>}
      </fieldset>
      {isLead && <fieldset disabled={busy || !access} className="dp-fields">
        <legend>新建飞书文档</legend>
        <label>云盘文件夹链接<input value={folder} onChange={e => setFolder(e.target.value)} placeholder="粘贴你有编辑权限的飞书文件夹链接" /></label>
        <div className="dp-actions"><button className="dp-button" onClick={() => void run(saveFolder)}>保存文件夹</button><small>清空后保存可关闭写入。</small></div>
      </fieldset>}
      {(error || authError) && <div role="alert" className="dp-error">{[error, authError].filter(Boolean).join("。")} <button className="dp-link" onClick={() => void load()}>重新读取</button></div>}
      <p role="status" className="dp-toaster">{notice}</p>
    </section>
  );
}

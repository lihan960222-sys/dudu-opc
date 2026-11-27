"use client";

import { useEffect, useState } from "react";
import { request } from "@/lib/api";

type Setting = { enabled: boolean; read_folders: string[]; outbound_network: boolean };

function entries(input: string) {
  return input.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
}

export function EmployeeProjectTerminal({ employee, folderSaved, busy }: {
  employee: number; folderSaved: boolean; busy: boolean;
}) {
  const endpoint = `/staff/${employee}/project-terminal`;
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [folders, setFolders] = useState("");
  const [network, setNetwork] = useState(false);

  function load(result: Setting) {
    setEnabled(result.enabled);
    setFolders(result.read_folders.join("\n"));
    setNetwork(result.outbound_network);
  }

  useEffect(() => {
    let active = true;
    request<Setting>(endpoint).then(
      result => { if (active) { load(result); setError(""); } },
      reason => { if (active) { setEnabled(null); setError(reason instanceof Error ? reason.message : "无法读取终端设置。"); } },
    );
    return () => { active = false; };
  }, [endpoint, refresh]);

  async function change(next: boolean, saveAccess = false) {
    setSaving(true);
    setError("");
    try {
      const body = saveAccess ? {enabled: next, read_folders: entries(folders), outbound_network: network} : {enabled: next};
      const result = await request<Setting>(endpoint, {method: "PUT", body: JSON.stringify(body)});
      load(result);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "终端设置未能保存。");
      try { setEnabled((await request<Setting>(endpoint)).enabled); } catch { setEnabled(null); }
    } finally { setSaving(false); }
  }

  return <div className="dp-field">
    <label className="dp-field">
      <span>在项目目录使用终端</span>
      <select className="dp-input" value={enabled === null ? "" : String(enabled)}
        disabled={busy || saving || enabled === null} aria-busy={saving || (enabled === null && !error)}
        onChange={event => void change(event.target.value === "true")}>
        {enabled === null && <option value="">{error ? "状态未知" : "正在读取…"}</option>}
        <option value="false">已关闭</option>
        <option value="true" disabled={!folderSaved && !enabled}>已开启</option>
      </select>
    </label>
    <small>{saving ? "正在保存…" : !folderSaved ? "请先保存这名员工自己的工作目录。"
      : "切换后立即生效。开启后，管家派来的工单可以在此目录运行命令，读写范围沿用档案权限。模型登录在“基本资料”中管理。"}</small>
    {enabled && <details>
      <summary>外部资料与联网范围</summary>
      <label className="dp-field"><span>可额外读取的资料或软件目录（每行一个绝对路径）</span>
        <textarea className="dp-input" value={folders} disabled={busy || saving}
          onChange={event => setFolders(event.target.value)} /></label>
      <label><input type="checkbox" checked={network} disabled={busy || saving}
        onChange={event => setNetwork(event.target.checked)} /> 允许项目终端直接联网</label>
      <small>开启后，终端可向任意可达地址发送数据和下载文件。默认关闭。其他员工的目录在跨员工只读权限中设置。</small>
      <button type="button" className="dp-link" disabled={busy || saving}
        onClick={() => void change(true, true)}>保存访问范围</button>
    </details>}
    {error && <p className="dp-error" role="alert">{error} <button type="button" className="dp-link"
      disabled={busy || saving} onClick={() => setRefresh(value => value + 1)}>重新读取</button></p>}
  </div>;
}

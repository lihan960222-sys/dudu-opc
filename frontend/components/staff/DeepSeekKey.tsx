"use client";

import { useEffect, useState } from "react";
import { request } from "@/lib/api";

const endpoint = "/staff/deepseek-key";

export function DeepSeekKey() {
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    request<{configured: boolean}>(endpoint).then(
      result => { if (active) setConfigured(result.configured); },
      () => { if (active) setError("无法读取密钥状态，请重新打开管家档案。"); },
    );
    return () => { active = false; };
  }, []);

  async function save() {
    setBusy(true);
    setError("");
    try {
      const result = await request<{configured: boolean}>(endpoint, {
        method: "PUT", body: JSON.stringify({ api_key: key.trim() }),
      });
      setConfigured(result.configured);
      setKey("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "密钥验证失败。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dp-fields">
      <label className="dp-field">
        <span>管家的 DeepSeek API Key</span>
        <input className="dp-input" type="password" value={key} autoComplete="off"
          maxLength={512} disabled={busy} onChange={e => setKey(e.target.value)}
          placeholder={configured ? "输入新密钥可替换" : "输入 DeepSeek API Key"} />
      </label>
      <div className="dp-actions">
        <button type="button" className="dp-button" disabled={!key.trim() || busy} onClick={() => void save()}>
          {busy ? "正在验证…" : "验证并保存密钥"}
        </button>
        <small role="status">{configured === null ? "" : configured ? "主机已配置密钥" : "主机尚未配置密钥"}</small>
      </div>
      {error && <p role="alert" className="dp-error">{error}</p>}
      <small>保存员工资料后，管家发起的新模型请求会使用 DeepSeek。</small>
    </div>
  );
}

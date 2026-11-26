"use client";

import { useEffect, useState } from "react";
import { ChevronRight } from "lucide-react";
import { request } from "@/lib/api";

export function DeepSeekKey({ employee }: { employee?: number }) {
  const endpoint = employee ? "/staff/" + employee + "/deepseek-key" : "/staff/deepseek-key";
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [open, setOpen] = useState(false);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    request<{configured: boolean}>(endpoint).then(
      result => { if (active) setConfigured(result.configured); },
      () => { if (active) setError("无法读取密钥状态，请重新打开档案。"); },
    );
    return () => { active = false; };
  }, [endpoint]);

  async function save() {
    setBusy(true);
    setError("");
    try {
      const result = await request<{configured: boolean}>(endpoint, {
        method: "PUT", body: JSON.stringify({ api_key: key.trim() }),
      });
      setConfigured(result.configured);
      setKey("");
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "密钥验证失败。");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dp-key">
      <button type="button" className="dp-key-toggle" aria-expanded={open} disabled={busy}
        onClick={() => { setOpen(!open); if (open) setKey(""); }}>
        <ChevronRight aria-hidden="true" />管理 DeepSeek 密钥
        <span className={configured == null ? "dp-key-state" : configured ? "dp-status" : "dp-status pending"} role="status">
          {configured == null ? (error ? "状态未知" : "读取中") : configured ? "已配置" : "未配置"}
        </span>
      </button>
      {open && <div className="dp-fields">
      <label className="dp-field">
        <span>{employee ? "这名员工" : "管家"}的 DeepSeek API Key</span>
        <input className="dp-input" type="password" value={key} autoComplete="off"
          maxLength={512} disabled={busy} onChange={e => setKey(e.target.value)}
          placeholder={configured ? "输入新密钥可替换" : "输入 DeepSeek API Key"} />
      </label>
      <div>
        <button type="button" className="dp-button" disabled={!key.trim() || busy} onClick={() => void save()}>
          {busy ? "正在验证…" : "验证并保存密钥"}
        </button>
      </div>
      <small>密钥验证后保存在家庭主机，只供{employee ? "这名员工" : "管家"}使用。</small>
      </div>}
      {error && <p role="alert" className="dp-error">{error}</p>}
    </div>
  );
}

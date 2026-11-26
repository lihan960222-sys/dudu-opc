"use client";

import { useEffect, useMemo, useState } from "react";
import { employeeOpenAIAccount, type CodexLoginOperation } from "@/lib/api";
import { CodexEngineSetup } from "../CodexEngineSetup";

export function EmployeeOpenAI({ employee, name }: { employee: number; name: string }) {
  const account = useMemo(() => employeeOpenAIAccount(employee), [employee]);
  const [open, setOpen] = useState(false);
  const [attempt, refresh] = useState(0);
  const [status, setStatus] = useState<{signed_in: boolean; login?: CodexLoginOperation} | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    let active = true;
    account.status().then(
      value => { if (active) { setStatus(value); setError(""); } },
      err => { if (active) { setStatus(null); setError(err instanceof Error ? err.message : "无法读取员工登录状态。"); } },
    );
    return () => { active = false; };
  }, [account, open, attempt]);

  return <section className="dp-key">
    <button type="button" className="dp-key-toggle" aria-expanded={open} onClick={() => {
      if (open) { setStatus(null); setError(""); }
      setOpen(!open);
    }}>管理{name}的 OpenAI 登录</button>
    {open && <div className="dp-fields">
      <p role="status">{error ? "登录状态未知" : !status ? "正在检查…" : status.signed_in ? "已连接此员工的 OpenAI 账号"
        : status.login ? "等待设备授权" : "员工尚未登录"}</p>
      {error && <p role="alert" className="dp-error">{error}</p>}
      {status && <CodexEngineSetup signedIn={status.signed_in} initialFlow={status.login} account={account}
        onChanged={() => refresh(n => n + 1)} />}
      {(status || error) && <button type="button" className="dp-link" onClick={() => refresh(n => n + 1)}>重新检查</button>}
    </div>}
  </section>;
}

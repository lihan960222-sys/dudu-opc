"use client";

import { useEffect, useRef, useState } from "react";
import { request } from "@/lib/api";
import { clearConversationCaches } from "@/lib/history-reset";

type Preview = { conversations: number; turns: number; tasks: number; jobs: number; approvals: number; busy: boolean | number; revision: string };
type Operation = { id: string; state: "queued" | "running" | "succeeded" | "failed"; message: string };
type Snapshot = { available: boolean; preview: Preview | null; operation: Operation | null };

const path = "/staff/history-reset";
const pendingKey = "dudu:history-reset:previous-operation";

export function HistoryReset() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [review, setReview] = useState<Preview | null>(null);
  const [loading, setLoading] = useState(true);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const submitted = useRef(false);
  const previous = useRef<string | null>(null);
  const busy = !!snapshot?.preview?.busy;

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    try { previous.current ??= sessionStorage.getItem(pendingKey); } catch { /* optional persistence */ }
    async function read() {
      try {
        const next = await request<Snapshot>(path, { cache: "no-store" });
        if (!active) return;
        const operation = next.operation;
        setSnapshot(next);
        const pending = operation?.state === "queued" || operation?.state === "running";
        setWaiting(pending);
        if (pending) {
          if (previous.current === null) {
            previous.current = "";
            try { sessionStorage.setItem(pendingKey, ""); } catch { /* optional persistence */ }
          }
          setError("");
          setReview(null);
          timer = setTimeout(read, 2000);
        } else if (previous.current !== null) {
          const changed = operation?.id !== previous.current;
          previous.current = null;
          submitted.current = false;
          try { sessionStorage.removeItem(pendingKey); } catch { /* optional persistence */ }
          if (changed && operation?.state === "succeeded") {
            try { clearConversationCaches(localStorage); } catch { /* storage may be blocked */ }
            window.location.reload();
            return;
          }
          if (!changed || !operation) setError("后台尚未登记本次清理，请重新查看清单。");
        }
      } catch (err) {
        if (!active) return;
        if (previous.current !== null || submitted.current) {
          setWaiting(true);
          timer = setTimeout(read, 3000);
        } else {
          setError(err instanceof Error ? err.message : "读取清理状态失败。");
        }
      } finally {
        if (active) setLoading(false);
      }
    }
    void read();
    return () => { active = false; clearTimeout(timer); };
  }, [refresh]);

  function reload() {
    setError("");
    setLoading(true);
    setRefresh(n => n + 1);
  }

  async function confirm() {
    if (!review || submitted.current) return;
    submitted.current = true;
    setWaiting(true);
    setError("");
    previous.current = snapshot?.operation?.id ?? "";
    try { sessionStorage.setItem(pendingKey, previous.current); } catch { /* optional persistence */ }
    const revision = review.revision;
    setReview(null);
    try {
      await request(path, { method: "POST", body: JSON.stringify({ revision, confirmation: "清空历史记录" }) });
    } catch (err) {
      // A network interruption during restart can hide a successful response.
      // Read the persisted operation before offering another submission.
      if (err && typeof err === "object" && "status" in err && Number(err.status) < 500) {
        submitted.current = false;
        previous.current = null;
        try { sessionStorage.removeItem(pendingKey); } catch { /* optional persistence */ }
        setWaiting(false);
        setError(err instanceof Error ? err.message : "请求未被接受，请重新查看清单。");
      }
    } finally {
      setRefresh(n => n + 1);
    }
  }

  const operation = snapshot?.operation;
  return <section className="dp-history-reset">
    <h3>历史记录</h3>
    <p>清空所有员工的对话、任务记录和会话记忆。员工配置、权限、定时计划和项目文件会保留。</p>
    {waiting ? <p role="status">正在清理或等待服务恢复。可以离开此页，稍后回来查看结果。</p>
      : operation && <p role="status" className={operation.state === "failed" ? "dp-error" : "dp-notice"}>{operation.message}</p>}
    {error && <p role="alert" className="dp-error">{error}</p>}
    {!waiting && snapshot && !snapshot.available && <p>请通过家中主机的嘟嘟桌面入口启动服务后再清理。</p>}
    {!waiting && busy && <p role="status">有任务或消息正在处理，结束后可刷新状态。</p>}
    {review && !waiting ? <div className="dp-history-confirm" role="group" aria-label="确认清空历史记录">
      <strong>确认清空这些记录？</strong>
      <p>{review.conversations} 个历史会话，{review.turns} 轮对话，{review.tasks} 张工单，{review.jobs} 次派工，{review.approvals} 条审批记录。</p>
      <p>清空后无法在工作台撤销。飞书客户端中的旧消息仍然可见。</p>
      <div className="dp-actions">
        <button className="dp-button danger" onClick={() => void confirm()}>确认清空历史记录</button>
        <button className="dp-button" onClick={() => setReview(null)}>取消</button>
      </div>
    </div> : !waiting && <div className="dp-actions">
      {snapshot?.available && <button className="dp-button" disabled={loading || busy || !snapshot.preview}
        onClick={() => { setError(""); setReview(snapshot.preview); }}>清空历史记录…</button>}
      {(error || busy || !snapshot?.available) && <button className="dp-link" disabled={loading} onClick={reload}>{loading ? "正在读取…" : "刷新状态"}</button>}
    </div>}
  </section>;
}

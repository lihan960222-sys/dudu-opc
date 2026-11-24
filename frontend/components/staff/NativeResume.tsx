"use client";
import { useEffect, useState } from "react";
import { SquareTerminal } from "lucide-react";
import { Dialog } from "../ui/Dialog";
import { request } from "@/lib/api";

export function NativeResume({ threadId }: { threadId: number }) {
  const [open, setOpen] = useState(false),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(false);
  const [handoff, setHandoff] = useState<{
    cwd: string;
    resume_command: string;
    last_native_activity?: { count: number };
  } | null>(null);
  useEffect(() => {
    if (!open) return;
    let live = true;
    request<typeof handoff>(`/team/threads/${threadId}/native-handoff`)
      .then(
        (r) => {
          if (live) setHandoff(r);
        },
        (err) => {
          if (live) setError(String(err));
        },
      )
      .finally(() => {
        if (live) setLoading(false);
      });
    return () => {
      live = false;
    };
  }, [open, threadId]);
  return (
    <>
      <button
        type="button"
        className="shrink-0 rounded-md p-1.5 text-muted hover:bg-hover"
        title="在 Codex 续接"
        aria-label="在 Codex 续接"
        onClick={() => {
          setLoading(true);
          setError("");
          setHandoff(null);
          setOpen(true);
        }}
      >
        <SquareTerminal size={17} />
      </button>
      <Dialog
        open={open}
        onOpenChange={setOpen}
        title="在 Codex 续接"
        size="sm"
      >
        {loading ? (
          <p role="status">读取会话中…</p>
        ) : error ? (
          <p role="alert">{error}</p>
        ) : handoff ? (
          <div className="space-y-3">
            {[
              ["主机目录", handoff.cwd],
              ["在此目录运行", handoff.resume_command],
            ].map(([name, value]) => (
              <label className="grid gap-1 text-xs" key={name}>
                {name}
                <input
                  readOnly
                  value={value}
                  onFocus={(e) => e.target.select()}
                  className="rounded border border-line p-2 font-mono"
                />
              </label>
            ))}
            {handoff.last_native_activity && (
              <p>
                上次恢复时，此会话有 {handoff.last_native_activity.count}{" "}
                轮原生更新。
              </p>
            )}
          </div>
        ) : (
          <p>当前会话还没有可续接的 Codex 记录。</p>
        )}
      </Dialog>
    </>
  );
}

"use client";
import { EngineModelFields } from "../chat/EngineModelFields";

import { useRef, useState } from "react";
import { Bot, Box, Compass, Folder, FolderOpen, Scan } from "lucide-react";
import { request, type Engine } from "@/lib/api";
import { AgentPortrait, useAgentImage } from "../chat/AgentImage";

export function FolderField({
  value,
  onChange,
  disabled = false,
  label = "工作目录 · 家中主机",
  access,
}: {
  value: string;
  onChange: (path: string) => void;
  disabled?: boolean;
  label?: string;
  access?: string;
}) {
  const [open, setOpen] = useState(false);
  const [address, setAddress] = useState("");
  const [view, setView] = useState<{
    path: string;
    parent: string | null;
    folders: string[];
  } | null>(null);
  const [error, setError] = useState("");
  const seq = useRef(0);
  async function browse(path: string) {
    const id = ++seq.current;
    setOpen(true);
    setError("");
    setAddress(path);
    setView(null);
    try {
      const next = await request<NonNullable<typeof view>>(
        "/staff/folders" + (path ? "?" + new URLSearchParams({ path }) : ""),
      );
      if (seq.current === id) {
        setView(next);
        setAddress(next.path);
      }
    } catch (e) {
      if (seq.current === id) setError(String(e));
    }
  }
  return (
    <div className="dp-field">
      <label>
        <span className="flex flex-wrap items-center justify-between gap-2">{label}{access && <small className="font-normal text-muted">{access}</small>}</span>
        <input
          className="dp-input dp-mono"
          value={value}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
          placeholder="输入主机上的绝对路径"
        />
      </label>
      <button
        type="button"
        className="dp-link dp-browse"
        disabled={disabled}
        onClick={() => void browse(value)}
      >
        <FolderOpen size={15} /> 浏览主机文件夹
      </button>
      {open && (
        <section className="dp-folders">
          <label>
            地址栏
            <input
              className="dp-input dp-mono"
              aria-label="主机目录地址"
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void browse(address);
                }
              }}
            />
          </label>
          <div className="dp-folderbuttons">
            <button
              type="button"
              className="dp-button"
              onClick={() => void browse(address)}
            >
              前往
            </button>
            <button
              type="button"
              className="dp-button"
              onClick={() => setOpen(false)}
            >
              关闭
            </button>
          </div>
          {error && <p role="alert">{error}</p>}
          {view && (
            <>
              <div className="dp-list">
                {view.parent && (
                  <button
                    type="button"
                    className="dp-listrow"
                    onClick={() => void browse(view.parent!)}
                  >
                    ↑ 上一级
                  </button>
                )}
                {view.folders.map((name) => (
                  <button
                    type="button"
                    className="dp-listrow"
                    key={name}
                    onClick={() =>
                      void browse(
                        view.path.replace(/[\\/]$/, "") +
                          (view.path.includes("\\") ? "\\" : "/") +
                          name,
                      )
                    }
                  >
                    <Folder />
                    {name}
                  </button>
                ))}
              </div>
              <button
                type="button"
                className="dp-button primary"
                disabled={address !== view.path}
                onClick={() => {
                  onChange(view.path);
                  setOpen(false);
                }}
              >
                使用此目录
              </button>
            </>
          )}
        </section>
      )}
    </div>
  );
}

const styles = [
  { icon: Bot, color: "orange", mark: "🤖", name: "机器人" },
  { icon: Box, color: "violet", mark: "📦", name: "立方体" },
  { icon: Scan, color: "teal", mark: "🔬", name: "研究员" },
  { icon: Compass, color: "blue", mark: "🧭", name: "指南针" },
];
export function Face({
  id,
  name,
  rev,
  big = false,
  preview,
}: {
  id?: number;
  name?: string;
  rev?: number;
  big?: boolean;
  preview?: string;
}) {
  const url = useAgentImage(id ?? null, rev);
  if (id || preview) {
    return <span className={`dp-face dp-portrait ${big ? "big" : ""}`}>
      <AgentPortrait agentId={id ?? null} name={name} src={preview || url} size={big ? "lg" : "md"} />
    </span>;
  }
  const style = styles[(id ?? 1) % styles.length],
    Icon = style.icon;
  return (
    <span className={`dp-face ${style.color} ${big ? "big" : ""}`}>
      <Icon />
    </span>
  );
}
export function AvatarPicker({
  onChange,
}: {
  onChange: (dataUrl: string) => void;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const canvas = () => {
    const c = document.createElement("canvas");
    c.width = c.height = 192;
    return c;
  };
  async function upload(file: File) {
    setBusy(true);
    setError("");
    const url = URL.createObjectURL(file);
    try {
      if (
        !["image/png", "image/jpeg", "image/webp"].includes(file.type) ||
        file.size > 5 * 1024 * 1024
      )
        throw Error("请选择 5 MiB 以内的 PNG、JPEG 或 WebP 图片");
      const img = new Image();
      img.src = url;
      await img.decode();
      const c = canvas();
      const s = Math.min(img.width, img.height);
      c.getContext("2d")!.drawImage(
        img,
        (img.width - s) / 2,
        (img.height - s) / 2,
        s,
        s,
        0,
        0,
        192,
        192,
      );
      onChange(c.toDataURL("image/png"));
    } catch (e) {
      setError(String(e));
    } finally {
      URL.revokeObjectURL(url);
      setBusy(false);
    }
  }
  return (
    <div className="dp-avatar-options">
      {styles.map((s, i) => (
        <button
          type="button"
          aria-label={s.name}
          title={s.name}
          key={s.color}
          className={`dp-face ${s.color}`}
          onClick={() => {
            const c = canvas(),
              ctx = c.getContext("2d")!;
            ctx.fillStyle = ["#fae1c0", "#dedcf7", "#c9ebe2", "#d5e7fa"][i];
            ctx.fillRect(0, 0, 192, 192);
            ctx.font = "106px Segoe UI Emoji, sans-serif";
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillText(s.mark, 96, 100);
            onChange(c.toDataURL());
          }}
        >
          {s.mark}
        </button>
      ))}
      <label className="dp-button">
        上传图片
        <input
          type="file"
          className="sr-only"
          disabled={busy}
          accept="image/png,image/jpeg,image/webp"
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) void upload(f);
          }}
        />
      </label>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
export function savePicture(id: number, data: string) {
  return request<void>(`/staff/${id}/picture`, {
    method: "PUT",
    body: JSON.stringify({ png: data.split(",")[1] }),
  });
}

export function StaffModelFields({
  engines, provider, model, effort, disabled, lockProvider, requireModel, onChange,
}: {
  engines: Engine[]; provider: string; model: string | null; effort?: string | null;
  disabled?: boolean; lockProvider?: boolean; requireModel?: boolean;
  onChange: (provider: string, model: string | null, effort?: string | null) => void;
}) {
  return <EngineModelFields engines={engines} provider={provider} model={model} effort={effort}
    showEffort={effort !== undefined} disabled={disabled} lockProvider={lockProvider}
    leadDefault={null} requireModel={requireModel}
    profile preferModelSelect onChange={onChange} />;
}

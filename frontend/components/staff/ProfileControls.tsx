"use client";

import { useEffect, useRef, useState } from "react";
import { Bot, Box, Compass, Folder, FolderOpen, Scan } from "lucide-react";
import { request, api, type Engine } from "@/lib/api";

export function FolderField({
  value,
  onChange,
  disabled = false,
}: {
  value: string;
  onChange: (path: string) => void;
  disabled?: boolean;
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
        工作目录 · 家中主机
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
        className="dp-link"
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
  rev,
  big = false,
  preview,
}: {
  id?: number;
  rev?: number;
  big?: boolean;
  preview?: string;
}) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    let live = true;
    let owned = "";
    if (id)
      api
        .agentAvatarUrl(id)
        .then((v) => {
          owned = v;
          if (live) setUrl(v);
          else URL.revokeObjectURL(v);
        })
        .catch(() => {});
    return () => {
      live = false;
      if (owned) URL.revokeObjectURL(owned);
    };
  }, [id, rev]);
  const style = styles[(id ?? 1) % styles.length],
    Icon = style.icon;
  return (
    <span className={`dp-face ${style.color} ${big ? "big" : ""}`}>
      {preview || url ? <img src={preview || url} alt="" /> : <Icon />}
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
  engines,
  provider,
  model,
  disabled = false,
  lockProvider = false,
  stewardModels = false,
  onChange,
}: {
  engines: Engine[];
  provider: string;
  model: string | null;
  disabled?: boolean;
  lockProvider?: boolean;
  stewardModels?: boolean;
  onChange: (provider: string, model: string | null) => void;
}) {
  const engine = engines.find((e) => e.id === provider);
  const [custom, setCustom] = useState(false);
  const models = [...(engine?.models ?? [])];
  if (stewardModels && provider === "codex") {
    for (const [id, label] of [["deepseek-flash", "DeepSeek V4.1 Flash"], ["deepseek-v4-pro", "DeepSeek V4 Pro"]]) {
      if (!models.some(m => m.id === id)) models.push({ id, label, default: false });
    }
  }
  const customValue = !!model && !models.some((m) => m.id === model);
  return (
    <div className="dp-fields space-y-2">
      <div className="dp-pair grid min-w-0 gap-2 sm:grid-cols-2">
        <label className="dp-field grid gap-1 text-xs">
          <span>执行引擎</span>
          <select
            className="dp-select rounded-md border border-line p-2"
            value={provider}
            disabled={disabled || lockProvider}
            onChange={(e) => {
              setCustom(false);
              const selected = engines.find((v) => v.id === e.target.value);
              onChange(e.target.value, selected?.default_model || null);
            }}
          >
            {!engine && <option value={provider}>{provider}</option>}
            {engines.map((e) => (
              <option key={e.id} value={e.id}>
                {e.label}
                {e.available ? "" : " · 待配置"}
              </option>
            ))}
          </select>
        </label>
        <label className="dp-field grid gap-1 text-xs">
          <span>默认模型</span>
          <select
            className="dp-select rounded-md border border-line p-2"
            value={custom || customValue ? "__custom__" : (model ?? "")}
            disabled={disabled}
            onChange={(e) => {
              setCustom(e.target.value === "__custom__");
              if (e.target.value !== "__custom__")
                onChange(provider, e.target.value || null);
            }}
          >
            <option value="">
              引擎默认
              {engine?.default_model ? " · " + engine.default_model : ""}
            </option>
            {models.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
            {(engine?.custom_models || customValue) && (
              <option value="__custom__">自定义模型 ID</option>
            )}
          </select>
        </label>
      </div>
      {(custom || customValue) && (
        <label className="dp-field grid gap-1 text-xs">
          <span>模型 ID</span>
          <input
            className="dp-input dp-mono rounded-md border border-line p-2"
            value={model ?? ""}
            maxLength={200}
            disabled={disabled}
            onChange={(e) => onChange(provider, e.target.value || null)}
          />
        </label>
      )}
    </div>
  );
}

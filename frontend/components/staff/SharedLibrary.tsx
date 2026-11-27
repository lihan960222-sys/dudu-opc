"use client";
import { useEffect, useState } from "react";
import { request } from "@/lib/api";

type Release = {name: string; version: string; kind: "tool" | "skill" | "package"; sha256: string; url: string; trust?: string};
type Library = {releases: Release[]; pins: Release[]; publishers: string[]};
const empty: Release = { name: "", version: "", kind: "package", url: "", sha256: "" };

export function SharedLibrary({employee, hasProject}: {employee: number; hasProject: boolean}) {
  const endpoint = `/staff/shared-library/${employee}`;
  const [opened, setOpened] = useState(false);
  const [library, setLibrary] = useState<Library | null>(null);
  const [release, setRelease] = useState<Release>(empty);
  const [approval, setApproval] = useState("official");
  const [formOpen, setFormOpen] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  useEffect(() => {
    if (!opened) return;
    let active = true;
    request<Library>(endpoint).then(v => { if (active) setLibrary(v); },
      e => { if (active) setMessage(String(e)); });
    return () => { active = false; };
  }, [endpoint, opened, refresh]);

  async function save(item?: Release, remove = false) {
    setBusy(true);
    setMessage("");
    try {
      await request(item ? endpoint : "/staff/shared-library", {
        method: item ? "PUT" : "POST",
        body: JSON.stringify(item ? {name: item.name, version: remove ? null : item.version} : {
          release: {...release, sha256: release.sha256 || undefined}, approval,
          from_employee: library?.pins.some(p => p.trust === "project-trial" && p.name === release.name
            && p.version === release.version && p.url === release.url && p.sha256 === release.sha256
            && p.kind === release.kind) ? employee : undefined,
        }),
      });
      setMessage(item ? "项目版本已保存。终端会在下个回合读取。" : "已加入共享库。");
      if (!item) { setRelease(empty); setApproval("official"); }
      setRefresh(n => n + 1);
    } catch (e) { setMessage(String(e)); }
    finally { setBusy(false); }
  }
  async function forget(publisher: string) {
    setBusy(true);
    try {
      await request("/staff/shared-library/publisher/" + encodeURIComponent(publisher.slice(7)), {method:"DELETE"});
      setMessage("后续新版本需要重新批准。");
      setRefresh(n => n + 1);
    } catch (e) { setMessage(String(e)); }
    finally { setBusy(false); }
  }
  return <details className="dp-cap-section" open={opened} onToggle={e => setOpened(e.currentTarget.open)}>
    <summary>共享工具、技能与依赖</summary>
    {library?.releases.map(item => {
      const pinned = library.pins.some(p => p.name === item.name && p.version === item.version && p.sha256 === item.sha256);
      return <div className="dp-cap-row" key={item.name + "@" + item.version}>
        <span>{item.name} · {item.version} <small>{item.trust === "official" ? "官方来源" : item.trust === "trusted-publisher" ? "已信任发布者" : "已批准版本"}</small></span>
        <button type="button" disabled={busy || !hasProject} className="dp-link" onClick={() => void save(item, pinned)}>
          {pinned ? "从本项目移除" : "本项目使用"}</button>
      </div>;
    })}
    {library && !library.releases.length && <p>共享库还没有版本。</p>}
    {library?.pins.filter(p => p.trust === "project-trial" && !library.releases.some(r =>
      r.name === p.name && r.version === p.version && r.url === p.url && r.sha256 === p.sha256)).map(p =>
      <div className="dp-cap-row" key={p.name}>
        <span>{p.name} · {p.version} <small>仅本项目试用</small></span>
        <button type="button" className="dp-link" onClick={() => {
          setRelease({name:p.name, version:p.version, kind:p.kind, url:p.url, sha256:p.sha256});
          setApproval("version");
          setFormOpen(true);
          setMessage("已填入试用版本。核对来源后，可在下方加入共享库。");
        }}>准备共享</button>
        <button type="button" className="dp-link" disabled={busy} onClick={() => void save(p, true)}>移除</button>
      </div>)}
    {library?.publishers.map(p => <div key={p} className="dp-cap-row"><span>{p}</span>
      <button type="button" className="dp-link" disabled={busy} onClick={() => void forget(p)}>撤销发布者信任</button>
    </div>)}
    <details open={formOpen} onToggle={e => setFormOpen(e.currentTarget.open)}><summary>添加版本</summary>
      <p>支持固定提交的 GitHub 文件、仓库 ZIP、NVIDIA Kit 生产源 ZIP 和 PyPI wheel。PyPI 文件按版本批准。缓存只读，依赖可在项目内解压或安装。</p>
      <label className="dp-field">类型<select className="dp-input" value={release.kind}
        onChange={e => setRelease({...release, kind: e.target.value as Release["kind"]})}>
        <option value="package">依赖包（ZIP 或 wheel）</option><option value="skill">技能（Markdown）</option><option value="tool">工具（Python）</option>
      </select></label>
      {([["name","名称（小写英文、数字和短横线）"],["version","版本"],["url","固定版本下载地址"],["sha256","文件 SHA-256（受信任的 GitHub 固定提交可留空）"]] as const).map(([key, label]) =>
        <label key={key} className="dp-field">{label}<input className="dp-input" value={release[key]}
          onChange={e => setRelease({...release, [key]: e.target.value.trim()})} /></label>)}
      <label className="dp-field">来源授权<select className="dp-input" value={approval} onChange={e => setApproval(e.target.value)}>
        <option value="official">沿用现有信任</option><option value="version">仅批准此文件版本</option>
        <option value="publisher">信任这个 GitHub 发布者及其今后所有仓库</option>
      </select></label>
      <button type="button" className="dp-button" disabled={busy || !release.name || !release.version || !release.url}
        onClick={() => void save()}>校验并加入共享库</button>
    </details>
    {message && <p role="status">{message}</p>}
  </details>;
}

export function SharedLibraryReviews() {
  const [reviews, setReviews] = useState<(Release & {id: string; project: string})[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    request<(Release & {id: string; project: string})[]>("/staff/shared-library/reviews/pending").then(
      data => { if (active) setReviews(data); }, e => { if (active) setError(String(e)); });
    return () => { active = false; };
  }, []);
  async function decide(id: string, choice: string) {
    setBusy(true);
    try {
      await request("/staff/shared-library/reviews/" + id, {method:"POST", body:JSON.stringify({choice})});
      setReviews(previous => previous.filter(r => r.id !== id));
      setError("");
    } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  return <>{reviews.map(r => <article key={r.id} className="dp-feature">
    <h3>让其他员工使用 {r.name} · {r.version}？</h3>
    <p className="dp-wrap">试用目录为 {r.project}。批准后，其他员工可从共享库选用。</p>
    <p className="dp-wrap">{r.url}</p><small className="dp-wrap">SHA-256 {r.sha256}</small>
    <div className="dp-actions">
      <button type="button" disabled={busy} className="dp-button primary" onClick={() => void decide(r.id, "version")}>仅共享此版本</button>
      {/^https:\/\/(raw\.githubusercontent\.com|codeload\.github\.com)\//.test(r.url) &&
        <button type="button" disabled={busy} className="dp-button" onClick={() => void decide(r.id, "publisher")}>信任该 GitHub 发布者</button>}
      <button type="button" disabled={busy} className="dp-link" onClick={() => void decide(r.id, "private")}>保留项目试用</button>
    </div>
  </article>)}{error && <p className="dp-error" role="alert">{error}</p>}</>;
}

"""Host-managed immutable releases and project locks for the employee library."""
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.services import agent_skills
from app.services.role_workspace import checked_root, validate_configured_location

MAX_BYTES = 64 * 1024 * 1024
NAMES = {"tool": "tool.py", "skill": "SKILL.md", "package": "package.zip"}
# NVIDIA's Kit SDK Registry Reference documents the production feed separately
# from its community registry. A marketplace listing never establishes trust.
KIT_HOST = "ovextensionsprod.blob.core.windows.net"


class ReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")
    version: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._+-]{0,79}$")
    kind: Literal["skill", "tool", "package"]
    url: str = Field(max_length=1600)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class Release(ReleaseRequest):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def source(item):
    url = urlsplit(item.url)
    if (url.scheme != "https" or url.query or url.fragment or url.netloc != url.hostname
            or any(not re.fullmatch(r"[A-Za-z0-9.+_@-]+", p) or p in (".", "..")
                   for p in (unquote(raw) for raw in url.path.split("/")[1:])) or not item.url.isascii()
            or any(ord(c) <= 32 for c in item.url)):
        raise ValueError("请提供固定版本文件的 HTTPS 直链。")
    parts = [unquote(part) for part in url.path[1:].split("/")]
    wheel = item.kind == "package" and parts[-1].endswith(".whl")
    if url.hostname == KIT_HOST and parts[:3] == ["exts", "kit", "prod"] and len(parts) >= 5:
        publisher, official = "nvidia-kit-production", True
    elif (url.hostname == "files.pythonhosted.org" and wheel and len(parts) == 5 and parts[0] == "packages"
          and all(re.fullmatch(r"[a-f0-9]+", segment) for segment in parts[1:4])):
        # PyPI hosts many independent publishers. Its domain never grants publisher trust.
        publisher, official = "pypi-file", False
    elif url.hostname in {"raw.githubusercontent.com", "codeload.github.com"} and len(parts) >= 4:
        org, repo, ref, *rest = parts
        archive = url.hostname == "codeload.github.com"
        revision = rest[0] if archive else ref
        if (not re.fullmatch(r"[a-f0-9]{40}", revision)
                or archive and (ref != "zip" or len(rest) != 1 or item.kind != "package")):
            raise ValueError("GitHub 来源需要完整的 40 位提交号。")
        publisher = "github/" + org.lower()
        official = publisher == "github/nvidia-omniverse" or (publisher == "github/openai" and repo.lower() == "skills")
    else:
        raise ValueError("此来源不在可下载范围。支持固定提交的 GitHub 文件、仓库 ZIP 和 NVIDIA Kit 生产源。")
    suffix = {"tool": ".py", "skill": ".md", "package": ".zip"}[item.kind]
    if url.hostname != "codeload.github.com" and not wheel and not url.path.lower().endswith(suffix):
        raise ValueError(f"该类型需要 {suffix} 文件。")
    return publisher, official


def _home(owner):
    return settings.SQLITE_FILE.resolve().parent / "employee-settings" / "shared-library" / hashlib.sha256(owner.encode()).hexdigest()[:32]


def _project(path):
    root = checked_root(Path(path))
    validate_configured_location(root)
    return os.path.normcase(str(root))


@contextmanager
def _db(owner):
    from app.services.employee_runtime import _private_directory
    path = _private_directory(_home(owner)) / "catalog.sqlite"
    if path.resolve() != path or path.exists() and path.stat().st_nlink != 1:
        raise ValueError("共享库索引路径无效。")
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS releases(
                name TEXT, version TEXT, kind TEXT, url TEXT, sha256 TEXT, trust TEXT,
                PRIMARY KEY(name,version));
            CREATE TABLE IF NOT EXISTS pins(project TEXT, name TEXT, version TEXT, storage TEXT, PRIMARY KEY(project,name));
            CREATE TABLE IF NOT EXISTS publishers(name TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY, release TEXT, storage TEXT, state TEXT);
            CREATE TABLE IF NOT EXISTS github_provenance(
                url TEXT, sha256 TEXT, version TEXT, ref TEXT NOT NULL, PRIMARY KEY(url,sha256,version));
            CREATE VIEW IF NOT EXISTS usable_releases AS SELECT r.* FROM releases r WHERE
                r.trust='owner-approved' OR
                (r.url NOT LIKE 'https://raw.githubusercontent.com/%' AND r.url NOT LIKE 'https://codeload.github.com/%')
                OR EXISTS (SELECT 1 FROM github_provenance g WHERE
                    g.url=r.url AND g.sha256=r.sha256 AND g.version=r.version);
        """)
        with db:
            yield db
    finally:
        db.close()


def _file(owner, item):
    filename = "package.whl" if item["url"].endswith(".whl") else NAMES[item["kind"]]
    return _home(owner) / "objects" / item["sha256"] / filename


def _check(path, digest):
    if path.resolve(strict=True) != path or not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("缓存文件路径无效。")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("缓存文件校验失败。")


def _download(item):
    limit = agent_skills.FILE_MAX_BYTES if item.kind == "skill" else MAX_BYTES
    data = bytearray()
    start = time.monotonic()
    try:
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", item.url, headers={"Accept-Encoding": "identity"}) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(chunk_size=65536):
                    if len(data) + len(chunk) > limit or time.monotonic() - start > 120:
                        raise ValueError("文件超过大小或下载时间限制。")
                    data.extend(chunk)
    except httpx.HTTPError as exc:
        raise ValueError("下载失败，请核对固定版本地址和网络。") from exc
    return bytes(data)


def _cached_or_download(owner, item):
    with _db(owner) as db:
        row = db.execute("SELECT * FROM releases WHERE url=? AND sha256=? AND kind=? LIMIT 1",
                         (item.url, item.sha256, item.kind)).fetchone()
    if row is None:
        return _download(item)
    path = _file(owner, row)
    _check(path, item.sha256)
    return path.read_bytes()


def _github_json(client, path, *, optional=False):
    client.cookies.clear()
    with client.stream("GET", "https://api.github.com" + path) as response:
        if optional and response.status_code == 404:
            return {}
        response.raise_for_status()
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError("GitHub 来源验证响应格式无效。")
        data = bytearray()
        for chunk in response.iter_bytes(chunk_size=65536):
            data.extend(chunk)
            if len(data) > 2_000_000:
                raise ValueError("GitHub 来源验证响应过大。")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("GitHub 来源验证响应无效。")
        return result


def _github_ref(item):
    """A commit can exist in an upstream fork network without belonging to an upstream ref."""
    url = urlsplit(item.url)
    org, repository, ref, *rest = [unquote(p) for p in url.path[1:].split("/")]
    sha = rest[0] if url.hostname == "codeload.github.com" else ref
    base = f"/repos/{quote(org, safe='')}/{quote(repository, safe='')}"
    error = "无法确认该提交已进入发布者的默认分支或对应版本标签。"
    try:
        with httpx.Client(timeout=8, follow_redirects=False, trust_env=False,
                          headers={"Accept": "application/vnd.github+json", "Accept-Encoding":"identity"}) as client:
            repo = _github_json(client, base)
            name = f"{org}/{repository}"
            branch = repo.get("default_branch")
            if (str(repo.get("full_name", "")).lower() != name.lower() or not isinstance(branch, str)
                    or not branch or any(part in ("", ".", "..") for part in branch.split("/"))):
                raise ValueError(error)
            branch_ref = "heads/" + branch
            head = _github_json(client, base + "/git/ref/" + quote(branch_ref, safe="/"))
            obj = head.get("object") or {}
            if (head.get("ref") != "refs/" + branch_ref or obj.get("type") != "commit"
                    or not re.fullmatch(r"[a-f0-9]{40}", str(obj.get("sha", "")))):
                raise ValueError(error)
            tip = obj["sha"]
            if tip == sha:
                return branch_ref + "@" + tip
            comparison = _github_json(client, base + f"/compare/{sha}...{tip}?per_page=1&page=2", optional=True)
            if ((comparison.get("merge_base_commit") or {}).get("sha") == sha
                    and comparison.get("status") in ("ahead", "identical")):
                return branch_ref + "@" + tip
            for tag in dict.fromkeys((item.version, "v" + item.version)):
                tagged = _github_json(client, base + "/git/ref/tags/" + quote(tag, safe=""), optional=True)
                if tagged.get("ref") != "refs/tags/" + tag:
                    continue
                obj = tagged.get("object") or {}
                if obj.get("type") == "tag" and re.fullmatch(r"[a-f0-9]{40}", str(obj.get("sha", ""))):
                    obj = _github_json(client, base + "/git/tags/" + obj["sha"]).get("object") or {}
                if obj.get("type") == "commit" and obj.get("sha") == sha:
                    return "tags/" + tag + "@" + sha
    except (httpx.HTTPError, AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(error) from exc
    raise ValueError(error)


def _ensure_provenance(owner, item):
    if urlsplit(item.url).hostname not in ("raw.githubusercontent.com", "codeload.github.com"):
        return
    identity = (item.url, item.sha256, item.version)
    with _db(owner) as db:
        if db.execute("SELECT 1 FROM github_provenance WHERE url=? AND sha256=? AND version=?", identity).fetchone():
            return
    ref = _github_ref(item)
    with _db(owner) as db:
        db.execute("INSERT OR IGNORE INTO github_provenance VALUES (?,?,?,?)", (*identity, ref))


def resolve_request(owner, request):
    if request.sha256 is not None:
        return Release(**request.model_dump()), None
    publisher, official = source(request)
    if (urlsplit(request.url).hostname not in {"raw.githubusercontent.com", "codeload.github.com"}
            or not official and publisher not in publishers(owner)):
        raise ValueError("请填写该文件的 SHA-256。受信任 GitHub 仓库的固定提交可自动计算。")
    metadata = request.model_dump(exclude={"sha256"})
    same = next((r for r in catalog(owner) if all(r[k] == v for k, v in metadata.items())), None)
    if same:
        return Release(**metadata, sha256=same["sha256"]), None
    data = _download(request)
    release = Release(**metadata, sha256=hashlib.sha256(data).hexdigest())
    _ensure_provenance(owner, release)
    return release, data


def publish(owner, item, *, approve_external=False, trust_publisher=False, trial_project=None, fetched=None, review_id=None):
    from app.services.employee_runtime import _private_directory
    publisher, official = source(item)
    if trust_publisher and not publisher.startswith("github/"):
        raise ValueError("该地址没有独立的 GitHub 发布者。请仅批准这个版本。")
    release = item.model_dump()
    with _db(owner) as db:
        trusted = db.execute("SELECT 1 FROM publishers WHERE name=?", (publisher,)).fetchone()
        previous = db.execute("SELECT * FROM releases WHERE name=? AND version=?", (item.name, item.version)).fetchone()
        usable = db.execute("SELECT 1 FROM usable_releases WHERE name=? AND version=?", (item.name, item.version)).fetchone()
        if previous and any(previous[key] != value for key, value in release.items()):
            raise ValueError("同名版本已指向其他内容。请另选版本号。")
        if not (official or trusted or approve_external or usable):
            raise ValueError("这项来源尚未获准共享。老板可批准当前版本，也可信任该发布者。")
        if usable and not trust_publisher and not review_id:
            _check(_file(owner, previous), previous["sha256"])
            return dict(previous)
    if trust_publisher or not approve_external:
        _ensure_provenance(owner, item)
    data = None
    if not previous:
        if trial_project is not None:
            matching = next((r for r in project_entries(owner, trial_project) if r["trust"] == "project-trial"
                             and all(r[k] == v for k, v in release.items())), None)
            if not matching:
                raise ValueError("项目试用版本已变更，请重新选择。")
            cached = Path(matching["path"])
            _check(cached, item.sha256)
            data = cached.read_bytes()
        elif fetched is not None:
            data = fetched
        else:
            data = _cached_or_download(owner, item)
    if data is not None:
        if hashlib.sha256(data).hexdigest() != item.sha256:
            raise ValueError("文件 SHA-256 与指定值不符。")
        if item.kind == "package":
            import io
            import zipfile
            if not zipfile.is_zipfile(io.BytesIO(data)):
                raise ValueError("文件不是有效的 ZIP 包。")
        else:
            content = data.decode("utf-8-sig")
            if not content.strip() or "\x00" in content:
                raise ValueError("文件需要包含有效的 UTF-8 文本。")
            if item.kind == "skill":
                if not agent_skills.parse_markdown(content)["body"]:
                    raise ValueError("技能缺少正文。")
    trust = ("owner-approved" if approve_external and not trust_publisher else
             "official" if official else "trusted-publisher")
    with _db(owner) as db:
        db.execute("BEGIN IMMEDIATE")
        if review_id:
            changed = db.execute("UPDATE reviews SET state='approved' WHERE id=? AND state='pending'", (review_id,))
            if changed.rowcount != 1:
                raise ValueError("该版本已经处理。")
        if not (official or approve_external or usable):
            if not db.execute("SELECT 1 FROM publishers WHERE name=?", (publisher,)).fetchone():
                raise ValueError("下载期间发布者信任已撤销。")
        if trust_publisher and publisher.startswith("github/"):
            if not approve_external:
                raise ValueError("只有老板可以信任新发布者。")
            db.execute("INSERT OR IGNORE INTO publishers VALUES (?)", (publisher,))
        db.execute("INSERT OR IGNORE INTO releases VALUES (:name,:version,:kind,:url,:sha256,:trust)",
                   {**release, "trust": trust})
        if previous and not usable and trust == "owner-approved":
            db.execute("UPDATE releases SET trust=? WHERE name=? AND version=?", (trust, item.name, item.version))
        stored = db.execute("SELECT * FROM releases WHERE name=? AND version=?", (item.name, item.version)).fetchone()
        if any(stored[key] != value for key, value in release.items()):
            raise ValueError("同名版本已指向其他内容。")
        path = _file(owner, stored)
        if not path.exists() and data is not None:
            _private_directory(path.parent)
            fd, temp = tempfile.mkstemp(dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(temp, path)
            finally:
                Path(temp).unlink(missing_ok=True)
        _check(path, item.sha256)
        db.execute("UPDATE reviews SET state='approved' WHERE id=? AND state='pending'", (_review_key(item),))
        return dict(stored)


def catalog(owner):
    if not (_home(owner) / "catalog.sqlite").exists():
        return []
    with _db(owner) as db:
        return [dict(row) for row in db.execute("SELECT * FROM usable_releases ORDER BY name,version")]


def publishers(owner):
    if not (_home(owner) / "catalog.sqlite").exists():
        return []
    with _db(owner) as db:
        return [row[0] for row in db.execute("SELECT name FROM publishers ORDER BY name")]


def forget_publisher(owner, publisher):
    with _db(owner) as db:
        db.execute("DELETE FROM publishers WHERE name=?", (publisher,))


def _trial_owner(owner, root):
    return owner + "\0trial\0" + _project(root)


def _review_key(release):
    # Employees can choose different local labels for the same release.
    identity = [release.url, release.sha256, release.kind, release.version]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def request_review(owner, project, release):
    with _db(owner) as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM usable_releases WHERE name=? AND version=? AND sha256=? AND url=?",
                      (release.name, release.version, release.sha256, release.url)).fetchone():
            return
        db.execute("INSERT OR IGNORE INTO reviews VALUES (?,?,?,'pending')",
                   (_review_key(release), release.model_dump_json(), _trial_owner(owner, project)))


def pending_reviews(owner):
    if not (_home(owner) / "catalog.sqlite").exists():
        return []
    with _db(owner) as db:
        return [{"id":r["id"], "project":r["storage"].split("\0trial\0", 1)[1], **json.loads(r["release"])} for r in
                db.execute("SELECT * FROM reviews WHERE state='pending' ORDER BY rowid")]


def decide_review(owner, review_id, decision):
    with _db(owner) as db:
        row = db.execute("SELECT * FROM reviews WHERE id=? AND state='pending'", (review_id,)).fetchone()
        if row is None:
            raise ValueError("该版本已经处理。")
        if decision == "private":
            changed = db.execute("UPDATE reviews SET state='private' WHERE id=? AND state='pending'", (review_id,))
            if changed.rowcount != 1:
                raise ValueError("该版本已经处理。")
            return
    if decision not in ("version", "publisher"):
        raise ValueError("请选择此版本的共享范围。")
    release = Release.model_validate_json(row["release"])
    cached = _file(row["storage"], release.model_dump())
    _check(cached, release.sha256)
    publish(owner, release, approve_external=True, trust_publisher=decision == "publisher",
            fetched=cached.read_bytes(), review_id=review_id)


def pin(owner, root, name, version, *, trial=False):
    project = _project(root)
    storage = _trial_owner(owner, root) if trial else owner
    if version is not None:
        with _db(storage) as objects:
            row = objects.execute("SELECT * FROM usable_releases WHERE name=? AND version=?", (name, version)).fetchone()
        if row is None:
            raise ValueError("库中没有这个版本。")
        _check(_file(storage, row), row["sha256"])
    with _db(owner) as db:
        if version is None:
            db.execute("DELETE FROM pins WHERE project=? AND name=?", (project, name))
            return
        db.execute("INSERT OR REPLACE INTO pins VALUES (?,?,?,?)", (project, name, version, "trial" if trial else "shared"))
        return {**dict(row), **({"trust": "project-trial"} if trial else {}),
                "path": str(_file(storage, row)), "template_id": "shared"}


def project_entries(owner, root):
    if not (_home(owner) / "catalog.sqlite").exists():
        return []
    with _db(owner) as db:
        pins = list(db.execute("SELECT * FROM pins WHERE project=? ORDER BY name", (_project(root),)))
    entries = []
    for entry in pins:
        trial = entry["storage"] == "trial"
        storage = _trial_owner(owner, root) if trial else owner
        with _db(storage) as db:
            row = db.execute("SELECT * FROM usable_releases WHERE name=? AND version=?", (entry["name"], entry["version"])).fetchone()
        if row is None:
            continue  # Quarantine pins made before GitHub provenance verification.
        data = dict(row)
        if trial:
            data["trust"] = "project-trial"
        entries.append({**data, "path": str(_file(storage, row)), "template_id": "shared"})
    return entries


def _shared_source(owner, release):
    publisher, official = source(release)
    with _db(owner) as db:
        known = db.execute("SELECT * FROM usable_releases WHERE name=? AND version=?", (release.name, release.version)).fetchone()
        trusted = db.execute("SELECT 1 FROM publishers WHERE name=?", (publisher,)).fetchone()
    return official or trusted or (known and all(known[k] == v for k, v in release.model_dump().items()))


def prefetch(owner, release, root=None):
    """Fetch before acquiring the live team/job lock. Committing rechecks the source and identity."""
    shared = _shared_source(owner, release)
    if not shared and root is None:
        raise ValueError("此发布者尚未获准共享。请在员工项目试用后交由老板批准。")
    storage = owner if shared else _trial_owner(owner, root)
    if shared and not any(r["name"] == release.name and r["version"] == release.version and
                          all(r[k] == v for k, v in release.model_dump().items()) for r in catalog(owner)):
        _ensure_provenance(owner, release)
    for row in catalog(storage):
        if row["name"] == release.name and row["version"] == release.version:
            if any(row[k] != v for k, v in release.model_dump().items()):
                raise ValueError("同名版本已指向其他内容。")
            _check(_file(storage, row), row["sha256"])
            return _file(storage, row).read_bytes()
    return _cached_or_download(storage, release)


def acquire(owner, root, release, *, fetched=None):
    if _shared_source(owner, release):
        return publish(owner, release, fetched=fetched), False
    return publish(_trial_owner(owner, root), release, approve_external=True, fetched=fetched), True


def project_files(owner, root):
    entries = project_entries(owner, root)
    files = []
    for entry in entries:
        if entry["kind"] != "skill":
            path = Path(entry["path"])
            _check(path, entry["sha256"])
            files.append(path)
    return tuple(files)


def skill(owner, root, name, digest=None):
    if digest is not None and not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise agent_skills.SkillError(422, "invalid_shared_skill", "请选择目录中的具体版本。")
    entries = ([{**r, "path":str(_file(owner, r))} for r in catalog(owner) if r["sha256"] == digest]
               if digest else project_entries(owner, root))
    for row in entries:
        if (row["kind"], row["name"]) == ("skill", name):
            path = Path(row["path"])
            _check(path, row["sha256"])
            parsed = agent_skills.parse_markdown(path.read_text(encoding="utf-8-sig"))
            return agent_skills.Skill(name, parsed["description"] or name, parsed["body"], source="shared", revision=row["sha256"])
    raise agent_skills.SkillError(404, "unknown_shared_skill", "此项目没有固定该技能。")


async def use_release(rt, run, args):
    import asyncio
    from app.services import employee_resources, employee_runtime, employee_terminal
    from app.db.transactions import begin_write
    from app.team import service
    from app.team.steward_review import current_native_job

    async def live(db):
        allowed = employee_terminal.allowed_tools(run.ctx, live=True)
        if "list_project_files" not in allowed or not await current_native_job(db, run, write=True, lock=True):
            raise service.TeamError("outside_scope", "此操作需要正在办理的可写项目工单。")
        return await employee_runtime.checked_active_project(db, run)

    try:
        if set(args) - {"name", "version", "source", "copy_to_project"} or type(args.get("copy_to_project", False)) is not bool:
            raise ValueError("项目依赖参数无效。")
        name, version = args.get("name"), args.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError("请选择名称和固定版本。")
        async with rt.session() as db:
            project = await live(db)
        trial, release, fetched = False, None, None
        if "source" in args:
            spec = ReleaseRequest(**args["source"], name=name, version=version)
            release, fetched = await asyncio.to_thread(resolve_request, rt.owner_id, spec)
            if fetched is None:
                fetched = await asyncio.to_thread(prefetch, rt.owner_id, release, project.root)
        async with rt.session() as db:
            await begin_write(db)
            project = await live(db)
            if release:
                _, trial = await asyncio.to_thread(acquire, rt.owner_id, project.root, release, fetched=fetched)
            row = pin(rt.owner_id, project.root, name, version, trial=trial)
            if trial:
                request_review(rt.owner_id, project.root, release)
            if args.get("copy_to_project"):
                data = Path(row["path"]).read_bytes()
                if hashlib.sha256(data).hexdigest() != row["sha256"]:
                    raise ValueError("文件内容已变化。")
                filename = (unquote(urlsplit(row["url"]).path.rsplit("/", 1)[-1]) if row["url"].endswith(".whl")
                            else name + "-" + version + Path(row["path"]).suffix)
                try:
                    row["project_copy"] = str(employee_resources.save_new(project.root, filename, data))
                except (OSError, ValueError) as exc:
                    row["copy_error"] = f"已固定版本，项目副本未保存。{exc}"
            return row
    except (OSError, ValueError, TypeError, agent_skills.SkillError) as exc:
        raise service.TeamError("project_release_unavailable", str(exc)) from None

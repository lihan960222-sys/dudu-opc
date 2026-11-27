"""Stdio tools for a single feature task. Launched with isolated Python (-I)."""
import json
import ntpath
import os
import sqlite3
from contextlib import closing
import stat
import sys
import tempfile
from pathlib import Path, PureWindowsPath

SERVER = "feature_files"
MAX_TEXT = 64 * 1024
PRIVATE = {"agents.md", "memory.md", "memory", "skills", "__pycache__", "node_modules"}
SCOPES = {"type": "string", "enum": ["role", "task"]}
STRING = {"type": "string"}


def tool(name, description, fields):
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}}


TOOLS = [
    tool("list_files", "List one directory of task files or shared role materials. Use an empty path for the root.",
         {"scope": SCOPES, "path": STRING}),
    tool("read_text", "Read a UTF-8 text file from this task or its role materials.", {"scope": SCOPES, "path": STRING}),
    tool("write_text", "Save a UTF-8 file under this task.", {"path": STRING, "text": STRING}),
]


def turn_config(current, root, writable, *, consult_only=False, peer_access=None, project_runtime=None,
                scheduled_knowledge=False, approval_mode=None):
    if not isinstance(current, dict):
        raise ValueError("Cannot verify Codex configuration")
    servers = {key: {"enabled": False} for key in (current.get("mcp_servers") or {})}
    plugins = {key: {"enabled": False} for key in (current.get("plugins") or {})}
    if project_runtime is not None:
        # Native tools run in the verified OS sandbox. Only backend-created MCP
        # brokers may be attached below; project/user MCP commands run as the host.
        native_tools = writable and not scheduled_knowledge
        scoped_requests = (native_tools and getattr(project_runtime, "local_workstation", False)
                           and project_runtime.writable and approval_mode in ("accept_edits", "full_access", "approve_for_me"))
        cached_search = not scheduled_knowledge and getattr(project_runtime, "model_vendor", None) == "openai"
        return {"mcp_servers": servers, "plugins": plugins, "web_search": "cached" if cached_search else "disabled",
                **({"project_doc_max_bytes": 0} if scheduled_knowledge else {}),
                "features": {"shell_tool": native_tools, "unified_exec": native_tools, "view_image": native_tools,
                             "request_permissions_tool": scoped_requests,
                             "exec_permission_approvals": scoped_requests,
                             "multi_agent": False, "apps": False, "plugins": False,
                             "hooks": False, "code_mode": False, "skill_mcp_dependency_install": False}}
    names = [t["name"] for t in TOOLS if writable or t["name"] != "write_text"]
    servers[SERVER] = {"command": sys.executable, "args": ["-I", str(Path(__file__).resolve()), str(root),
                        "write" if writable else "read", *([json.dumps(peer_access)] if peer_access else [])], "enabled": True, "required": True,
                       "enabled_tools": names, "default_tools_approval_mode": "prompt"}
    if consult_only:
        servers[SERVER] = {"enabled": False}
    return {"mcp_servers": servers, "plugins": plugins, "web_search": "disabled", "project_doc_max_bytes": 0, "features": {
        "shell_tool": False, "unified_exec": False, "view_image": False, "multi_agent": False,
        "apps": False, "plugins": False, "code_mode": False}}


def checked(path):
    if path.is_symlink() or path.is_junction() or path.resolve() != path:
        raise ValueError("Linked or redirected paths are unavailable")
    if path.exists():
        info = path.stat()
        if getattr(info, "st_file_attributes", 0) & 1024 or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1):
            raise ValueError("Linked files are unavailable")
    return path


def checked_relative(root, name):
    path = PureWindowsPath(name)
    if path.anchor:
        raise ValueError("Path outside scope")
    current = checked(root)
    for part in path.parts:
        if (part.startswith(".") or part.endswith((" ", ".")) or part.casefold() in PRIVATE
                or any(ch in part for ch in ':*?"<>|') or ntpath.isreserved(part)):
            raise ValueError("Private or invalid path")
        current = checked(current / part)
    return current


class FeatureFiles:
    def __init__(self, task, writable=True, peer_access=None):
        self.task = checked(task)
        if not self.task.is_dir() or self.task.parent.name != "任务" or not self.task.name.isdecimal():
            raise ValueError("Invalid task folder")
        self.role = checked(self.task.parent.parent)
        self.writable = writable
        self.peer_access=peer_access

    def peer_roots(self):
        if not self.peer_access:
            return {}
        cfg=self.peer_access
        dbpath=Path(cfg["database"]).resolve(strict=True)
        with closing(sqlite3.connect(dbpath.as_uri()+"?mode=ro",uri=True)) as db:
            actor=db.execute('''select a.team_id,a.role_path,t.lead_agent_id,a.team_workspaces_readable from agent a join team t on a.team_id=t.id
                where a.id=? and a.owner_id=? and t.owner_id=a.owner_id
                and a.archived_at is null and a.id!=t.lead_agent_id''',(cfg["employee"],cfg["owner"])).fetchone()
            if not actor or not actor[1] or checked(Path(actor[1]))!=self.role:
                raise ValueError("员工目录授权已变更。")
            # Include excluded roles as barriers so nesting can never expose their files.
            rows=db.execute("select id,owner_id,team_id,name,role_path,archived_at from agent where role_path is not null").fetchall()
        paths={r[0]:Path(r[4]).resolve() for r in rows}
        result={}
        for employee,owner,team,name,raw,archived in rows:
            path=paths[employee]
            if owner!=cfg["owner"] or team!=actor[0] or employee==actor[2] or archived:
                continue
            unsafe=(not Path(raw).is_absolute() or path==Path(path.anchor) or path.drive.startswith(chr(92)*2)
                    or Path.home().resolve().is_relative_to(path) or dbpath.is_relative_to(path)
                    or (path.is_relative_to(dbpath.parent) and
                        (not cfg.get("managed_roles") or path != Path(cfg["managed_roles"]).resolve()/str(employee)))
                    or any(path.is_relative_to(other) or other.is_relative_to(path)
                           for i,other in paths.items() if i!=employee))
            if unsafe:
                if employee==cfg["employee"]:
                    raise ValueError("自己的工作目录与受保护目录重叠。")
                continue
            if actor[3] and employee!=cfg["employee"] and path.is_dir() and Path(raw)==path:
                result[f"employee:{employee}:{name}"]=checked(path)
        return result

    def tools(self):
        if not self.peer_access:
            return [t for t in TOOLS if self.writable or t["name"]!="write_text"]
        import copy
        items=copy.deepcopy(TOOLS)
        scopes=["role","task",*self.peer_roots()]
        for item in items[:2]:
            item["inputSchema"]["properties"]["scope"]["enum"]=scopes
        items[2]["inputSchema"]["properties"]["scope"]={"type":"string","enum":["task","role"]}
        items[2]["description"]="Write a UTF-8 file in your own task (default) or role workspace. Other employees are read-only."
        return items if self.writable else items[:2]

    def path(self, scope, name, *, peers=None):
        peers=self.peer_roots() if peers is None else peers
        if scope not in ("role", "task", *peers) or not isinstance(name, str):
            raise ValueError("Invalid scope or path")
        path = PureWindowsPath(name)
        if scope == "role" and path.parts and path.parts[0] == "任务":
            raise ValueError("Path outside scope")
        return checked_relative(peers.get(scope) or (self.role if scope == "role" else self.task), name)

    def call(self, name, args):
        scope=args.get("scope","task" if name=="write_text" else None)
        if name=="write_text":
            if not self.writable:
                raise ValueError("本轮任务是只读的，无法写入文件。")
            if scope=="role" and not self.peer_access:
                raise ValueError("本轮只可以写入任务目录。")
            if scope not in ("role","task"):
                raise ValueError("不能写入其他员工的目录。")
        peers=self.peer_roots()
        path = self.path(scope,args["path"],peers=peers)
        if name == "list_files":
            entries = []
            for child in sorted(path.iterdir()):
                try:
                    self.path(args["scope"], str(PureWindowsPath(args["path"]) / child.name),peers=peers)
                except (OSError, ValueError):
                    continue
                entries.append({"name": child.name, "folder": child.is_dir()})
                if len(entries) == 500:
                    break
            return {"entries": entries}
        if name == "read_text":
            with path.open("rb") as stream:
                data = stream.read(MAX_TEXT + 1)
            if len(data) > MAX_TEXT:
                raise ValueError("File exceeds 64 KiB")
            return {"text": data.decode("utf-8-sig")}
        if name == "write_text" and self.writable:
            data = args["text"].encode("utf-8")
            if len(data) > MAX_TEXT or path == self.task:
                raise ValueError("Invalid file or size")
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
                tmp = Path(f.name)
            try:
                tmp.write_bytes(data)
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
            return {"bytes": len(data)}
        raise ValueError("Unknown or disabled tool")


def main():
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    fs = FeatureFiles(Path(sys.argv[1]), sys.argv[2] == "write",json.loads(sys.argv[3]) if len(sys.argv)>3 else None)
    for line in sys.stdin:
        req = json.loads(line)
        if "id" not in req:
            continue
        method = req.get("method")
        reply = {"jsonrpc": "2.0", "id": req["id"]}
        if method == "initialize":
            reply["result"] = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                               "serverInfo": {"name": SERVER, "version": "1"}}
        elif method == "ping":
            reply["result"] = {}
        elif method == "tools/list":
            reply["result"] = {"tools": fs.tools()}
        elif method == "tools/call":
            try:
                p = req["params"]
                body = json.dumps(fs.call(p["name"], p["arguments"]), ensure_ascii=False)
                error = False
            except (KeyError, OSError, sqlite3.Error, ValueError, TypeError, AttributeError) as exc:
                body, error = str(exc), True
            reply["result"] = {"content": [{"type":"text", "text":body}], "isError":error}
        else:
            reply["error"] = {"code":-32601, "message":"Unknown method"}
        print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()

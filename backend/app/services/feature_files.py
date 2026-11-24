"""Stdio tools for a single feature task. Launched with isolated Python (-I)."""
import json
import ntpath
import os
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


def turn_config(current, root, writable, *, consult_only=False):
    if not isinstance(current, dict):
        raise ValueError("Cannot verify Codex configuration")
    servers = {key: {"enabled": False} for key in (current.get("mcp_servers") or {})}
    plugins = {key: {"enabled": False} for key in (current.get("plugins") or {})}
    names = [t["name"] for t in TOOLS if writable or t["name"] != "write_text"]
    servers[SERVER] = {"command": sys.executable, "args": ["-I", str(Path(__file__).resolve()), str(root),
                        "write" if writable else "read"], "enabled": True, "required": True,
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


class FeatureFiles:
    def __init__(self, task, writable=True):
        self.task = checked(task)
        if not self.task.is_dir() or self.task.parent.name != "任务" or not self.task.name.isdecimal():
            raise ValueError("Invalid task folder")
        self.role = checked(self.task.parent.parent)
        self.writable = writable

    def path(self, scope, name):
        if scope not in ("role", "task") or not isinstance(name, str):
            raise ValueError("Invalid scope or path")
        path = PureWindowsPath(name)
        if path.anchor or (scope == "role" and path.parts and path.parts[0] == "任务"):
            raise ValueError("Path outside scope")
        result = checked(self.role if scope == "role" else self.task)
        for part in path.parts:
            if (part.startswith(".") or part.endswith((" ", ".")) or part.casefold() in PRIVATE
                    or any(ch in part for ch in ':*?"<>|') or ntpath.isreserved(part)):
                raise ValueError("Private or invalid path")
            result = checked(result / part)
        return result

    def call(self, name, args):
        path = self.path("task" if name == "write_text" else args.get("scope"), args["path"])
        if name == "list_files":
            entries = []
            for child in sorted(path.iterdir()):
                try:
                    self.path(args["scope"], str(PureWindowsPath(args["path"]) / child.name))
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
    fs = FeatureFiles(Path(sys.argv[1]), sys.argv[2] == "write")
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
            reply["result"] = {"tools": [t for t in TOOLS if fs.writable or t["name"] != "write_text"]}
        elif method == "tools/call":
            try:
                p = req["params"]
                body = json.dumps(fs.call(p["name"], p["arguments"]), ensure_ascii=False)
                error = False
            except (KeyError, OSError, ValueError, TypeError, AttributeError) as exc:
                body, error = str(exc), True
            reply["result"] = {"content": [{"type":"text", "text":body}], "isError":error}
        else:
            reply["error"] = {"code":-32601, "message":"Unknown method"}
        print(json.dumps(reply), flush=True)


if __name__ == "__main__":
    main()

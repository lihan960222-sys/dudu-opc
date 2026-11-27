"""An owner's reusable grant for one stable native command in one employee project."""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex

from sqlalchemy import select

from app.db.models.agent import Agent
from app.db.models.permission_rule import PermissionRule
from app.services.decision_log import contains_credential

BUCKET = "employee_project_command"
_READ = re.compile(r"(?:Get-Location|Get-ChildItem(?: -Force)?(?: -Name)?)", re.I)
_PROCESS_NAME = r"[a-z0-9_][a-z0-9_.-]{0,79}"
_PROCESS_COLUMN = r"(?:Id|ProcessName|MainWindowTitle|Path)"
_PROCESS_QUERY = re.compile(
    rf"Get-Process -Name {_PROCESS_NAME}(?:,{_PROCESS_NAME}){{0,9}}"
    rf"(?: -ErrorAction SilentlyContinue)? \| Select-Object {_PROCESS_COLUMN}(?:,{_PROCESS_COLUMN}){{0,3}}", re.I)
_LAUNCH = re.compile(r"Start-Process -FilePath '([^'\r\n]+)'(?: -PassThru)?", re.I)


def _pack(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _trusted_shell(exe):
    path = Path(exe)
    system = os.environ.get("SystemRoot")
    if system and path == Path(system) / "System32/WindowsPowerShell/v1.0/powershell.exe":
        return True
    if path.name.lower() != "pwsh.exe" or not path.is_absolute() or ".." in path.parts:
        return False
    roots = [Path(os.environ[k]) for k in ("ProgramFiles", "ProgramFiles(x86)") if os.environ.get(k)]
    try:
        return (any(path.is_relative_to(root) for root in roots) and path.resolve(strict=True) == path
                and path.is_file() and (path.parent / "System.Management.Automation.dll").is_file())
    except (OSError, RuntimeError):
        return False


def _inner_command(args):
    command = args.get("command")
    if not isinstance(command, str) or len(command) > 1200 or contains_credential(command):
        return None
    argv = args.get("argv")
    if argv is None:
        # Some app-server versions supply a Windows command line instead of argv.
        match = re.fullmatch(r'''"([^"]+)" -NoProfile -Command (["'])(.+)\2''', command, re.I)
        if not match:
            return None
        exe, _, script = match.groups()
    else:
        if (not isinstance(argv, list) or len(argv) != 4 or not all(isinstance(a,str) for a in argv)
                or shlex.join(argv) != command or [a.lower() for a in argv[1:3]] != ["-noprofile","-command"]):
            return None
        exe, script = argv[0], argv[3]
    return script if _trusted_shell(exe) else None


def _project_script(project, tool, args):
    if (tool != "Bash" or not isinstance(args, dict)
            or set(args) - {"command", "cwd", "argv", "reason"} or not project
            or args.get("cwd") != str(project.root)):
        return None
    return _inner_command(args)


def readonly_request(project, tool, args):
    """An exact no-profile directory inspection, with no request for extra permissions."""
    return bool(_READ.fullmatch(_project_script(project, tool, args) or ""))


def identity(project, tool, args):
    script = _project_script(project, tool, args)
    if not script:
        return None
    target = None
    if not _READ.fullmatch(script) and not _PROCESS_QUERY.fullmatch(script):
        launched = _LAUNCH.fullmatch(script)
        if not launched:
            return None
        path = Path(launched[1])
        if not path.is_absolute() or path.suffix.lower() not in {".bat",".exe"}:
            return None
        # An editable project shortcut/script may point somewhere else on the next run.
        # Only fingerprint ordinary, installed files rooted under Program Files.
        protected = [Path(os.environ[k]) for k in ("ProgramFiles","ProgramFiles(x86)") if os.environ.get(k)]
        if not any(path.is_relative_to(root) for root in protected) or ".." in path.parts:
            return None
        try:
            if path.resolve(strict=True) != path or not path.is_file() or path.stat().st_size > 128*1024*1024:
                return None
            with path.open("rb") as stream:
                target = hashlib.file_digest(stream, "sha256").hexdigest()
        except (OSError, RuntimeError):
            return None
    return (_pack({"employee_id":project.employee_id, "project":str(project.root), "cwd":args["cwd"],
                   "profile":project.permission_name}),
            _pack({"command":args["command"], "target_sha256":target, "argv":args.get("argv")}))


def decode(rule):
    if rule.bucket != BUCKET:
        return None
    try:
        return {**json.loads(rule.connector), **json.loads(rule.tool)}
    except (ValueError, TypeError):
        return None


async def find(db, owner, key):
    if key is None:
        return None
    return await db.scalar(select(PermissionRule).where(PermissionRule.user_id == owner,
        PermissionRule.bucket == BUCKET, PermissionRule.connector == key[0],
        PermissionRule.tool == key[1], PermissionRule.decision == "allow").order_by(PermissionRule.id))


async def stage(db, owner, key):
    """Caller holds the write transaction; a lost decision rolls this back."""
    employee_id = json.loads(key[0])["employee_id"]
    await db.execute(select(Agent.id).where(Agent.owner_id == owner, Agent.id == employee_id).with_for_update())
    existing = await find(db, owner, key)
    if existing is None:
        db.add(PermissionRule(user_id=owner, bucket=BUCKET, connector=key[0], tool=key[1], decision="allow"))
        await db.flush()

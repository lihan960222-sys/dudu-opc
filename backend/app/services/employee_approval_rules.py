"""An owner's reusable grant for one stable native command in one employee project."""
from dataclasses import replace
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
        match = re.fullmatch(r'''"([^"]+)" -NoProfile -Command (["'])(.+)\2''', command, re.I | re.S)
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


def directory_inspection(script):
    # Every statement must be a complete member of the restricted grammar.
    lines = [line.strip() for line in (script or "").splitlines() if line.strip()]
    return bool(lines) and all(_READ.fullmatch(line) for line in lines)


def readonly_request(project, tool, args):
    """An exact no-profile directory inspection, with no request for extra permissions."""
    return directory_inspection(_project_script(project, tool, args))


DELIVER_FILE = "mcp__team__deliver_project_file"
_DATA_FORMATS = {".txt", ".csv", ".json", ".pdf", ".png", ".jpg", ".jpeg"}


def _approval_profile(project):
    if not getattr(project, "local_workstation", False):
        return project.permission_name
    # The sandbox gives each assignment its own scratch directory. Apply the
    # same policy builder to a reserved, unassignable task/epoch when comparing
    # grants. An actual allow still revalidates the live project's full profile.
    canonical = replace(project, task_id=0, assignment_epoch=0)
    policy = [1, project.model_vendor, str(project.profile), canonical.permission_name,
              canonical._filesystem(reviewed=True)]
    return "approval-" + hashlib.sha256(_pack(policy).encode()).hexdigest()


def _delivery_identity(project, args):
    from app.services import employee_resources, feature_files, workspace_files
    if (not project or not isinstance(args, dict) or set(args) - {"source", "folder", "sha256", "name"}
            or not isinstance(args.get("sha256"), str) or not re.fullmatch("[a-f0-9]{64}", args["sha256"])):
        return None
    try:
        if not isinstance(args.get("source"), str):
            return None
        parts = workspace_files.clean_relative(args["source"])
        if not parts or workspace_files.is_secret(parts):
            return None
        source = feature_files.checked_relative(project.root, "/".join(parts))
        name = employee_resources._name(args.get("name", source.name))
        suffix = Path(name).suffix.lower()
        if suffix not in _DATA_FORMATS or source.suffix.lower() != suffix or not source.is_relative_to(project.task_dir):
            return None
        folder = employee_resources.external_folder(project, args.get("folder"), name)
        if folder.is_relative_to(project.root) or str(folder) != args["folder"]:
            return None
    except (ValueError, OSError, RuntimeError, workspace_files.FileAccessError):
        return None
    return (_pack({"employee_id": project.employee_id, "project": str(project.root),
                   "cwd": str(project.root), "profile": _approval_profile(project)}),
            _pack({"command": f"将当前工单的 {suffix} 产物另存到 {folder}，保留已有文件",
                   "capability": "deliver_new_task_artifact", "folder": str(folder), "format": suffix}))


def identity(project, tool, args):
    if tool == DELIVER_FILE:
        return _delivery_identity(project, args)
    script = _project_script(project, tool, args)
    if not script:
        return None
    target = None
    if not directory_inspection(script) and not _PROCESS_QUERY.fullmatch(script):
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
                   "profile":_approval_profile(project)}),
            _pack({"command":args["command"], "target_sha256":target, "argv":args.get("argv")}))


def work_order_key(project, key):
    if not key or not project:
        return None
    action = json.loads(key[1])
    script = _inner_command(action)
    if directory_inspection(script):
        operations = sorted({line.strip().casefold() for line in script.splitlines() if line.strip()})
        action = {"command": "\n".join(operations), "capability": "project_directory_query"}
    return _pack({**json.loads(key[0]), "task_id": project.task_id,
                  "assignment_epoch": project.assignment_epoch}), _pack(action)


async def matching(db, owner, project, key):
    return (await find(db, owner, work_order_key(project, key))
            or await find(db, owner, key))


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


async def stage(db, owner, key, *, reserve=False):
    """Caller holds the write transaction; a lost decision rolls this back."""
    employee_id = json.loads(key[0])["employee_id"]
    await db.execute(select(Agent.id).where(Agent.owner_id == owner, Agent.id == employee_id).with_for_update())
    row = await db.scalar(select(PermissionRule).where(PermissionRule.user_id == owner,
        PermissionRule.bucket == BUCKET, PermissionRule.connector == key[0], PermissionRule.tool == key[1],
        PermissionRule.decision.in_(("allow", "reserved"))).order_by(PermissionRule.id))
    if row is None:
        row = PermissionRule(user_id=owner, bucket=BUCKET, connector=key[0], tool=key[1],
                             decision="reserved" if reserve else "allow")
        db.add(row)
        await db.flush()
    return row


async def confirm(db, owner, rule_id):
    from sqlalchemy import update
    await db.execute(update(PermissionRule).where(PermissionRule.user_id == owner,
        PermissionRule.id == rule_id, PermissionRule.bucket == BUCKET,
        PermissionRule.decision == "reserved").values(decision="allow"))
    await db.commit()

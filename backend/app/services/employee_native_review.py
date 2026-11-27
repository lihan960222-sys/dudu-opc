"""Describe and bound a pending native request using the employee's live project grant."""
import hashlib
import json
from pathlib import Path
import shlex
import tomllib

from app.services import decision_log, feature_files, workspace_files


TOOLS = {"Bash", "apply_patch", "codex.request_permissions"}
NARROW_REQUEST_HINT = ("The steward declined this exact request because it would grant more authority than the task needs. "
    "No additional permission was granted. Continue in this same task using your existing project permissions, or request "
    "only the concrete missing filesystem or network permission through Codex. Respect host denials and do not repeat "
    "the same request. If the work truly requires broader authority, report the specific effect that needs the owner.")


def fingerprint(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _path(project, value, *, relative=False):
    if not isinstance(value, str) or not value or len(value) > 1024:
        raise ValueError("invalid path")
    path = Path(value)
    if not path.is_absolute():
        if not relative or path.anchor:
            raise ValueError("absolute path required")
        path = project.root / path
    if (path.anchor.startswith("\\\\") or workspace_files.is_secret(path.parts)
            or any(p == ".." or p.endswith((" ", ".")) or any(c in p for c in ':*?"<>|\x00')
                   for p in path.parts[1:])):
        raise ValueError("indirect or sensitive path")
    for parent in reversed(path.parents):
        feature_files.checked(parent)
    return feature_files.checked(path)


def _covers(policy, path, level, *, subtree):
    levels = {"deny": 0, "read": 1, "write": 2}
    access = levels[policy.get(":root", "deny")]
    depth = 0
    for raw, mode in policy.items():
        if raw.startswith(":"):
            continue
        root = Path(raw)
        if path.is_relative_to(root) and len(root.parts) > depth:
            depth, access = len(root.parts), levels[mode]
        elif subtree and root.is_relative_to(path) and levels[mode] < levels[level]:
            return False
    return access >= levels[level]


def permission_scope(project, args):
    """Prove every requested grant is covered, including nested exceptions in a directory."""
    if not getattr(project, "local_workstation", False) or not isinstance(args, dict):
        return None
    try:
        if (set(args) - {"permissions", "cwd", "reason", "environmentId"} or args.get("environmentId") not in (None, "local")
                or not _path(project, args["cwd"]).is_relative_to(project.root)):
            return None
        grant = args["permissions"]
        if not isinstance(grant, dict) or set(grant) - {"network", "fileSystem"}:
            return None
        net = grant.get("network")
        if net is not None and (not isinstance(net, dict) or set(net) != {"enabled"} or type(net["enabled"]) is not bool):
            return None
        network = bool(net and net["enabled"])
        if network and not project.outbound_network:
            return None
        fs = grant.get("fileSystem") or {}
        if not isinstance(fs, dict) or set(fs) - {"read", "write", "entries"}:
            return None
        paths = []
        for key in ("read", "write", "entries"):
            values = fs.get(key)
            if values is None:
                continue
            if not isinstance(values, list) or len(values) > 32:
                return None
            for item in values:
                mode, value = key, item
                if key == "entries":
                    if (not isinstance(item, dict) or set(item) != {"access", "path"}
                            or not isinstance(item["path"], dict) or set(item["path"]) != {"type", "path"}
                            or item["path"]["type"] != "path"):
                        return None
                    mode, value = item["access"], item["path"]["path"]
                if mode not in ("read", "write"):
                    return None
                paths.append((mode, _path(project, value)))
        if not paths and not network:
            return None
        policy = tomllib.loads("fs=" + project._filesystem())["fs"]
        if any(not _covers(policy, path, mode, subtree=True) for mode, path in paths):
            return None
        return {"paths": sorted({(mode, str(path)) for mode, path in paths}), "network": network}
    except (KeyError, ValueError, TypeError, OSError, RuntimeError):
        return None


def _patch_scope(project, args):
    if set(args) - {"changes", "reason"} or not project.writable:
        return None
    changes = args.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 32:
        return None
    policy = tomllib.loads("fs=" + project._filesystem())["fs"]
    paths = []
    for change in changes:
        if not isinstance(change, dict) or set(change) - {"path", "kind", "diff"}:
            return None
        kind = change.get("kind")
        if isinstance(kind, str):
            kind = {"type": kind}
        if (not isinstance(kind, dict) or set(kind) - {"type", "move_path", "movePath"}
                or kind.get("type") not in ("add", "update", "delete")):
            return None
        for value in [change["path"], *(v for k, v in kind.items() if k != "type" and v is not None)]:
            path = _path(project, value, relative=True)
            if not path.is_relative_to(project.root) or path.is_dir() or not _covers(policy, path, "write", subtree=False):
                return None
            paths.append(str(path))
    return {"project_files": paths}


def _command_scope(project, args):
    from app.services import employee_approval_rules as rules, safety_floor

    key = rules.identity(project, "Bash", args)
    if key:
        return {"verified_command": json.loads(key[1])}
    permitted_fields = {"command", "argv", "cwd", "reason", "kind", "environmentId", "commandActions",
                        "additionalPermissions", "networkApprovalContext"}
    command = args.get("command")
    if (set(args) - permitted_fields or args.get("kind") not in (None, "command")
            or args.get("environmentId") not in (None, "local") or not isinstance(command, str) or not command.strip()
            or ("argv" in args and (not isinstance(args["argv"], list) or shlex.join(args["argv"]) != command))
            or not safety_floor.clears_floor(rules._inner_command(args) or command)):
        return None
    # Codex only accepts additional_permissions with WithAdditionalPermissions. An
    # unrestricted retry clears that overlay. networkApprovalContext and commandActions
    # are informational; neither alone proves how the command will be contained.
    overlay = permission_scope(project, {"cwd":args.get("cwd"), "permissions":args.get("additionalPermissions")})
    if not overlay:
        return None
    return {"bounded_command":True, "additional_permissions":overlay, "working_directory":args["cwd"],
            "network_enabled":project.outbound_network}


def describe(project, tool, args):
    """A non-null description is reviewable. Only a proven scope lets the steward approve it."""
    if not getattr(project, "local_workstation", False) or tool not in TOOLS or not isinstance(args, dict):
        return None
    if args.get("environmentId") not in (None, "local") or (tool == "Bash" and args.get("kind") not in (None, "command")):
        return None  # humanCommand checkpoints, live terminal input and remote environments stay with their owner.
    raw = json.dumps([tool, args], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    from app.services.employee_resources import model_result
    if len(raw) > 8000 or decision_log.contains_credential(raw.replace('\\\\','/')) or model_result(args) != args:
        return None
    effect = None
    try:
        if tool == "codex.request_permissions":
            effect = permission_scope(project, args)
        elif tool == "apply_patch":
            effect = _patch_scope(project, args)
        else:
            effect = _command_scope(project, args)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError):
        pass
    return {"kind": "native_action", "tool": tool, "parameters": args, "delegated_scope": effect,
            "request_digest": hashlib.sha256(raw.encode()).hexdigest()}

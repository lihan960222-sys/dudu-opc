"""Registered employee directories and the read-only grants used for project turns."""
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from app.db.models import Agent
from app.services.role_workspace import checked_root, validate_configured_location


@dataclass
class TeamWorkspaces:
    lead_id: int | None
    members: dict
    safe_roots: dict[int, Path]
    protected_roots: tuple[Path, ...]

    def readable_by(self, employee_id, *, bound_roots=None):
        reader = self.members.get(employee_id)
        from app.core.config import settings
        from app.services.employee_runtime import native_enabled
        workstation = bool(reader and reader.owner_id and reader.owner_id == settings.STEWARD_STRICT_OWNER
                           and native_enabled(reader.owner_id, employee_id))
        if (not reader or employee_id == self.lead_id or employee_id not in self.safe_roots
                or not (workstation or reader.team_workspaces_readable)):
            return {}
        return {who: path for who, path in self.safe_roots.items() if who != employee_id
                and (workstation or who != self.lead_id) and (bound_roots is None or path in bound_roots)}

    def profile(self, employee, viewer_id):
        if viewer_id in (self.lead_id, employee.id):
            return {"role_path": employee.role_path, "read_only_peer_ids": list(self.readable_by(employee.id))}
        if employee.id in self.readable_by(viewer_id):
            return {"role_path": employee.role_path}
        return {}


async def load(db, team):
    rows = (await db.execute(select(Agent.id, Agent.owner_id, Agent.team_id, Agent.name, Agent.role_path,
        Agent.workspace_path, Agent.archived_at, Agent.team_workspaces_readable).order_by(Agent.id))).all()
    # Even an archived role or an ordinary chat workspace can contain another owner's files.
    barriers = [(row.id, Path(raw).resolve()) for row in rows for raw in (row.role_path, row.workspace_path) if raw]
    members = {row.id: row for row in rows if team and row.owner_id == team.owner_id
               and row.team_id == team.id and not row.archived_at}
    roots = {}
    for member in members.values():
        if not member.role_path:
            continue
        try:
            root = checked_root(Path(member.role_path))
            validate_configured_location(root)
        except (OSError, ValueError):
            continue
        if all(who == member.id or not (path.is_relative_to(root) or root.is_relative_to(path))
               for who, path in barriers):
            roots[member.id] = root
    return TeamWorkspaces(team.lead_agent_id if team else None, members, roots,
                          tuple(path for _, path in barriers))


async def turn_peers(db, team, project):
    from app.team import fence

    directory = await load(db, team)
    roots = directory.readable_by(project.employee_id, bound_roots=project.readable_roots)
    return {who: {"id": who, "name": fence.line(directory.members[who].name), "role_path": str(root)}
            for who, root in roots.items()}

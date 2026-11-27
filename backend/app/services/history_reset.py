"""Local-owner history maintenance. The caller must run clear in a separate, offline process."""
import hashlib
import json
import os
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
import sqlite3
import stat

from app.core import data_lease
from app.core.config import settings

MAIN = """agent_job_inbox agent_job_event team_notice team_event agent_job feature_message
    feature_session feature_scope steward_suggestion work_item_event work_item_run work_item workflow_run notification
    team_setup_request steward_direct_turn approval event turn routine_webhook_receipt routine_app_action routine_run
    room_webhook_receipt room_dispatch spend_ledger permission_ask task knowledge_weekly_slot""".split()

GATEWAY = """outbox uploads resources retry decisions submissions questions bindings executions prepared direct_turns
    approval_dm direct_notices delivery_timing history_requests group_enquiries owner_enquiries enquiry_replies
    video_previews direct_requests direct_finals notice_recalls suggestion_offers team_publications
    owner_readwrite_requests private_history_jobs history_reply_policy private_summary_notes private_voice
    direct_targets direct_team_reports direct_hiring_notices history_dispatch history_dispatch_links
    history_continuations knowledge_steps knowledge_intent_candidates employee_feishu_assignments
    employee_history_reads knowledge_followups steward_reply_files assignment_outbox owner_dm_sources
    employee_source_links employee_message_observations employee_doc_pages knowledge_write_attempts
    knowledge_revisions suggestion_delivery_clock employee_document_jobs knowledge_layouts knowledge_weekly_notice
    team_cursor""".split()

ENGINE = {
    "goals_1": "thread_goals thread_goal_continuation_deferrals",
    "logs_2": "logs",
    "memories_1": "stage1_outputs jobs consolidation_progress",
    "queue_1": "queued_items queued_thread_revisions",
    "state_5": "thread_dynamic_tools thread_spawn_edges thread_sections thread_attachments guardian_review_feedback thread_read_receipts session_model_ids rollout_migration_skipped_rollouts threads",
    "thread_history_1": "thread_items thread_realtime_items thread_history_projection_state thread_turns",
}


def _checked(root, path):
    if not path.is_relative_to(root) or path.resolve() != path or not root.is_absolute():
        raise ValueError("数据目录包含链接或无效路径。")
    for part in (path, *path.parents):
        if part.exists():
            meta = part.lstat()
            if getattr(meta, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("数据目录含有重定向路径。")
            if part.is_file() and meta.st_nlink != 1:
                raise ValueError("数据文件含有硬链接。")
        if part == root:
            break
    return path


def _configuration():
    if os.name != "nt" or not os.getenv("LOCALAPPDATA") or settings.DATABASE_KIND != "sqlite":
        raise ValueError("此功能适用于 Windows 本机嘟嘟工作台。")
    root = Path(os.environ["LOCALAPPDATA"]) / "SuparagentSteward"
    if settings.SQLITE_FILE != _checked(root, root / "suparagent.db"):
        raise ValueError("无法核对当前工作台的数据目录。")
    config_path = _checked(root, root / "feishu.json")
    if Path(os.environ.get("FEISHU_CONFIG") or config_path) != config_path:
        raise ValueError("无法核对当前飞书网关的数据目录。")
    config = json.loads(config_path.read_text("utf-8-sig"))
    channel = _checked(root, root / config["state_dir"] / "channel.sqlite3")
    if config["owner_id"] != settings.STEWARD_STRICT_OWNER or not config["owner_id"]:
        raise ValueError("工作台与飞书的老板账号不一致。")
    return root, channel, config


@contextmanager
def _connection(root, channel, write):
    mode = "rw" if write else "ro"
    main = _checked(root, root / "suparagent.db")
    with closing(sqlite3.connect(main.as_uri() + "?mode=" + mode, uri=True, timeout=5)) as db:
        db.execute("pragma foreign_keys=on")
        db.execute("attach database ? as gw", (channel.as_uri() + "?mode=" + mode,))
        db.execute("begin immediate" if write else "begin")
        if write:
            db.execute("pragma defer_foreign_keys=on")
        yield db


def _tables(db, schema, candidates):
    actual = {n for n, in db.execute(f"select name from {schema}.sqlite_master where type='table'")}
    return [schema + "." + name for name in candidates if name in actual]


def _count(db, table, clause="", args=()):
    return db.execute("select count(*) from " + table + " " + clause, args).fetchone()[0]


def _snapshot(db, cfg):
    owner, anchor = cfg["owner_id"], cfg["steward_thread"]
    if (db.execute("select agent_id from thread where id=? and owner_id=? and kind='chat' and archived_at is null",
                   (anchor, owner)).fetchone() != (cfg["steward"]["agent_id"],)
            or any(_count(db, t, "where owner_id != ?", (owner,)) for t in ("agent", "thread", "team", "work_item"))):
        raise ValueError("无法核对老板或飞书会话归属。")
    result = {"conversations": _count(db, "thread", """where (id!=? and room_id is null)
              or exists(select 1 from turn where turn.thread_id=thread.id)""", (anchor,)),
              **{k: _count(db,t) for k,t in (("turns","turn"), ("tasks","work_item"),
                                              ("jobs","agent_job"), ("approvals","approval"))}}
    busy_checks = (
        ("turn","status='running'"), ("agent_job","status not in ('completed','failed','cancelled')"),
        ("approval","status='pending'"), ("room_dispatch","status in ('queued','running')"),
        ("workflow_run","status='running'"), ("routine_run","state in ('queued','running')"),
        ("gw.inbox","state in ('received','bound','submitting','running','direct_submitting','direct_running','suggestion_deciding')"),
        ("gw.outbox","state='pending'"), ("gw.direct_team_reports","finished=0"),
    )
    result["busy"] = any(db.execute(f"select 1 from {t} where {cond} limit 1").fetchone()
                         for t,cond in busy_checks if _tables(db, "gw" if t.startswith("gw.") else "main",[t.split(".")[-1]]))
    markers = [owner,anchor,cfg["steward"]["app_id"]]
    for t in ("thread","turn","work_item","agent_job","approval","event","agent_job_event","gw.inbox","gw.outbox"):
        markers.append(db.execute(f"select count(*),max(rowid) from {t}").fetchone())
    markers += list(db.execute("select id,last_seq,updated_at from thread order by id"))
    result["revision"] = hashlib.sha256(json.dumps(markers).encode()).hexdigest()
    return result


def preview():
    root, channel, cfg = _configuration()
    with _connection(root,channel,False) as db:
        return _snapshot(db,cfg)


def _tree(root, directory):
    if not _checked(root,directory).exists():
        return []
    def failed(exc):
        raise exc
    files = []
    for parent, dirs, names in os.walk(directory, onerror=failed):
        for d in dirs:
            _checked(root, Path(parent)/d)
        files.extend(_checked(root,Path(parent)/name) for name in names)
    return files


def _engine_history(root, owner, employees, channel):
    from app.services import employee_runtime
    files = _tree(root,root/"attachments") + _tree(root,channel.parent/"private-voice")
    files.extend(_checked(root,p) for p in _checked(root,root/"decisions").glob("*.ndjson"))
    homes = [(root,root/"deepseek-codex")]
    owner_hash = hashlib.sha256(owner.encode()).hexdigest()
    for storage in {employee_runtime._default_service_root(), employee_runtime._service_root()}:
        for employee in employees:
            employee_dir = _checked(storage, storage/"profiles"/owner_hash/str(employee))
            if employee_dir.is_dir():
                homes.extend((storage,_checked(storage,v)) for v in employee_dir.iterdir() if v.is_dir())
    databases = []
    for base, home in homes:
        if not _checked(base,home).is_dir():
            continue
        for folder in ("sessions","archived_sessions","memories","log","thread-writer-locks","service-bindings"):
            files.extend(_tree(base,home/folder))
        for name in ("session_index.jsonl","history.jsonl"):
            path = _checked(base,home/name)
            if path.is_file():
                files.append(path)
        for name, tables in ENGINE.items():
            path = _checked(base,home/(name+".sqlite"))
            if path.is_file():
                databases.append((path,tables.split()))
    return files,databases


def _notes(root, employee, remove):
    from app.services import agent_memory, memory_capture, memory_journal, memory_log, memory_proposals, memory_status, memory_topics
    home = root/"workspace"/"workspace"/"agents"/str(employee)
    state = root/"memory-state"
    for path in (agent_memory.memory_path(employee),memory_topics.topics_dir(employee),memory_log.logs_dir(employee)):
        _checked(home,path)
        if path.is_dir():
            _tree(home,path)
    for path in (memory_journal.journal_path(employee),memory_proposals._path(employee),
                 memory_status._path(employee),memory_capture._pending_path()):
        _checked(state,path)
    if remove:
        memory_capture._forget_pending(employee)
        agent_memory.remove_memory(employee)
        pending = memory_capture._pending_path()
        batches = json.loads(pending.read_text("utf-8")).get("batches",[]) if pending.exists() else []
        if (agent_memory.read_memory(employee).bytes or memory_topics.list_topics(employee)
                or memory_log.list_logs(employee) or memory_proposals.pending(employee)
                or memory_status.read(employee) or memory_journal.journal_path(employee).exists()
                or any(b.get("agent_id") == employee for b in batches)):
            raise RuntimeError("员工记忆仍有记录，请再次清理。")


def clear(expected_revision):
    root, channel, cfg = _configuration()
    with ExitStack() as locks:
        for path in (root/"suparagent.db.backend-lease",channel.parent/"gateway.lock"):
            lock = locks.enter_context(_checked(root,path).open("a+b"))
            if not data_lease._try_lock(lock.fileno()):
                raise RuntimeError("工作台或飞书服务仍在使用数据。")
            locks.callback(data_lease._unlock,lock.fileno())
        with _connection(root,channel,True) as db:
            before = _snapshot(db,cfg)
            if before["busy"] or before["revision"] != expected_revision:
                raise ValueError("任务仍在运行或记录已变化，请重新预览。")
            employees = [n for n, in db.execute("select id from agent")]
            files, databases = _engine_history(root,cfg["owner_id"],employees,channel)
            for employee in employees:
                _notes(root,employee,False)
            tables = _tables(db,"main",MAIN) + _tables(db,"gw",GATEWAY)
            for t in tables:
                db.execute("delete from "+t)
            from app.services.employee_approval_rules import BUCKET
            db.execute("""delete from permission_rule where user_id=? and bucket=? and
                case when json_valid(connector) then json_type(connector,'$.task_id')='integer' else 0 end""",
                (cfg["owner_id"], BUCKET))
            db.execute("delete from thread where id!=? and room_id is null", (cfg["steward_thread"],))
            # Reconnecting clients can still hold the last event cursor. Keep its
            # monotonic sequence while removing every event and provider binding.
            db.execute("""update thread set provider_session_id=null,session_state='new',
                         provider_state=case when json_extract(provider_state,'$.effort') is not null
                         then json_object('effort',json_extract(provider_state,'$.effort')) else null end""")
            db.execute("update needs_you_state set card_hash=null,card_thread_id=null,card_day=null,notified='[]'")
            db.execute("update gw.inbox set payload='{}',state='ignored',task=null,error=null")
            if (any(_count(db,t) for t in tables) or db.execute("pragma foreign_key_check").fetchone()
                    or db.execute("pragma gw.foreign_key_check").fetchone()):
                raise RuntimeError("历史数据校验失败。")
            for path, candidates in databases:
                with closing(sqlite3.connect(path.as_uri()+"?mode=rw",uri=True)) as native:
                    native.execute("pragma foreign_keys=on")
                    native.execute("begin immediate")
                    for t in _tables(native,"main",candidates):
                        native.execute("delete from "+t)
                        if _count(native,t):
                            raise RuntimeError("仍有员工会话记录。")
                    native.commit()
            for employee in employees:
                _notes(root,employee,True)
            for path in files:
                path.unlink()
            if any(path.exists() for path in files):
                raise RuntimeError("仍有本机会话文件。")
            result = _snapshot(db,cfg)
            if any(result[key] for key in ("conversations", "turns", "tasks", "jobs", "approvals", "busy")):
                raise RuntimeError("仍有未清空的记录，请重新核对。")
            db.commit()
            return result

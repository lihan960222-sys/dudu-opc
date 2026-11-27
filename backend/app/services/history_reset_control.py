"""One owner-confirmed maintenance request, shared by the web server and its host."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from uuid import uuid4

from app.core.config import settings
from app.core.history_maintenance import managed as available
from app.services import history_reset
from app.services.employee_runtime import _private_directory

TTL = 5 * 60


class Conflict(ValueError):
    pass


def directory():
    base = settings.SQLITE_FILE.parent
    return _private_directory(base / "employee-settings" / "history-reset")


@contextmanager
def transaction():
    folder = directory()
    path = folder / "status.sqlite"
    if path.resolve() != path or (path.exists() and path.stat().st_nlink != 1):
        raise OSError("维护记录路径无效。")
    db = sqlite3.connect(path, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS operation (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL,
            revision TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL)""")
        yield db, folder / "request.json"
        db.commit()
    finally:
        db.close()


def latest(db):
    return db.execute("SELECT * FROM operation WHERE singleton=1").fetchone()


def reconcile(db, marker):
    row = latest(db)
    if not row or row["state"] not in ("queued", "running"):
        return row
    try:
        matches = json.loads(marker.read_text(encoding="ascii")).get("id") == row["id"]
    except (FileNotFoundError, ValueError, AttributeError):
        matches = False
    if not matches:
        db.execute("UPDATE operation SET state='failed',message=? WHERE singleton=1",
                   ("维护请求已中断，请重新预览。",))
        return latest(db)
    return row


def public(row):
    return {k: row[k] for k in ("id", "state", "message")} if row else None


def view():
    if not available():
        return {"available": False, "preview": None, "operation": None}
    with transaction() as (db, marker):
        op = public(reconcile(db, marker))
    return {"available": True, "preview": history_reset.preview(), "operation": op}


def matching(revision):
    current = history_reset.preview()
    if current["busy"]:
        raise Conflict("有任务、审批或飞书消息正在处理，请等结束后再清空。")
    if current["revision"] != revision:
        raise Conflict("预览后记录有变化，请重新查看数量，再确认清空。")


def enqueue(revision):
    if not available():
        raise Conflict("请从这台电脑的嘟嘟桌面入口启动服务。")
    with transaction() as (db, marker):
        row = reconcile(db, marker)
        if row and row["state"] in ("queued", "running"):
            if row["revision"] != revision:
                raise Conflict("已有清理请求正在处理。")
            return public(row)
        matching(revision)
        op_id = uuid4().hex
        db.execute("INSERT OR REPLACE INTO operation VALUES(1,?,?,'queued',?,?)",
                   (op_id, revision, "等待后台清理。", time.time()))
        # The host only uses the marker as a wake-up signal. The request itself
        # is committed before the host can acquire this database's write lock.
        temp = marker.with_name(op_id + ".tmp")
        try:
            with temp.open("x", encoding="ascii") as stream:
                json.dump({"id": op_id}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, marker)
        finally:
            temp.unlink(missing_ok=True)
        return public(latest(db))


def finish(op_id, state, message):
    with transaction() as (db, marker):
        row = latest(db)
        if row and row["id"] == op_id and row["state"] in ("queued", "running"):
            db.execute("UPDATE operation SET state=?, message=? WHERE singleton=1", (state, message))
            marker.unlink(missing_ok=True)


def worker(action, expected_id):
    with transaction() as (db, marker):
        row = latest(db)
        # A late decision from the supervisor for a retired request must never
        # complete, cancel or retire an owner's subsequent confirmation.
        if row and row["id"] != expected_id:
            if marker.is_file() and json.loads(marker.read_text(encoding="ascii")).get("id") == expected_id:
                reconcile(db, marker)
                marker.unlink(missing_ok=True)
            return False
        if not row or row["state"] not in ("queued", "running"):
            marker.unlink(missing_ok=True)
            return False
        op_id, revision = row["id"], row["revision"]
        if action == "apply" and row["state"] == "queued" and time.time() - row["created"] < TTL:
            db.execute("UPDATE operation SET state='running', message=? WHERE singleton=1", ("正在清理并核对记录。",))
            apply = True
        else:
            apply = False
    try:
        if action == "fail" or row["state"] != "queued":
            raise Conflict("上次维护中断，请重新预览，核对当前记录。")
        if time.time() - row["created"] >= TTL:
            raise Conflict("确认已过期，请重新预览。")
        if action == "check":
            matching(revision)
            return True
        if not apply:
            raise Conflict("维护请求无效，请重新预览。")
        history_reset.clear(revision)
        finish(op_id, "succeeded", "已核对，历史对话、工单、派工和审批记录均已清空。")
        return True
    except Conflict as exc:
        finish(op_id, "failed", str(exc))
    except Exception:
        finish(op_id, "failed", "未能完成清理或核对。请查看当前数量，再重新预览。")
    return False

"""Receipts for what the assigned employee actually read; message bodies stay in Feishu."""
import hashlib
import json

from . import history


CHUNK = 1800


def fingerprint(item):
    relevant = [item.get(key) for key in ("update_time", "create_time", "msg_type", "body")]
    return hashlib.sha256(json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def readable(item):
    body = (item.get("body") or {}).get("content", "")
    try:
        decoded = json.loads(body)
    except (ValueError, TypeError):
        return str(body)
    if item.get("msg_type") == "text" and isinstance(decoded, dict):
        return str(decoded.get("text", ""))
    if item.get("msg_type") == "post":
        return json.dumps(decoded, ensure_ascii=False)
    return history.excerpt(item)


def indexed(db):
    db.execute("""create table if not exists employee_message_observations(
        app text, employee integer, job text, chat text, mid text, digest text, frontier integer,
        primary key(app,employee,job,chat,mid))""")


def part(db, source_key, mid, item, offset=0, *, indexed_only=False):
    indexed(db)
    row = db.execute("""select digest, frontier from employee_message_observations
        where app=? and employee=? and job=? and chat=? and mid=?""", (*source_key, mid)).fetchone()
    stamp = fingerprint(item)
    frontier = row[1] if row and row[0] == stamp else 0
    if (indexed_only and not row) or type(offset) != int or not 0 <= offset <= frontier:
        raise ValueError("请先读取消息列表，再按返回的位置读取这条消息。")
    body = readable(item)
    end = min(offset + CHUNK, len(body))
    with db:
        db.execute("""insert into employee_message_observations values(?,?,?,?,?,?,?)
            on conflict(app,employee,job,chat,mid) do update set digest=excluded.digest,frontier=excluded.frontier""",
                   (*source_key, mid, stamp, max(frontier, end)))
    return {"id": mid, "text": body[offset:end], "next_text_offset": end if end < len(body) else None}


def complete(db, source_key, records):
    indexed(db)
    seen = {mid: (stamp, last) for mid, stamp, last in db.execute("""select mid,digest,frontier
        from employee_message_observations where app=? and employee=? and job=? and chat=?""", source_key)}
    return all(seen.get(mid)==(fingerprint(item),len(readable(item))) for mid,item in records.items())


def require_read(db, source_key, records):
    if not complete(db,source_key,records):
        raise ValueError("仍有未读完或已变更的消息。请读取剩余正文，再提交分类稿。")

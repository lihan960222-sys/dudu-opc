"""Withdraw only new bot status replies after their own final reply is delivered."""
import httpx


def initialize(db):
    db.executescript("""
        create table if not exists direct_finals(
            app text,event text,outgoing text unique not null,primary key(app,event));
        create table if not exists notice_recalls(
            outgoing text primary key, remote text unique, state text default 'waiting',
            attempts integer default 0, after integer default 0, error text);
    """)


def flush(db, publisher, app_id):
    ready = db.execute("""
        select r.outgoing,r.remote from notice_recalls r
        join direct_notices n on n.outgoing=r.outgoing
        join direct_finals f using(app,event)
        join outbox answer on answer.id=f.outgoing
        join outbox notice on notice.id=n.outgoing
        where r.state='waiting' and r.after<=unixepoch()
          and n.stage in ('queued','running') and n.app=?
          and answer.role='steward' and answer.state='sent' and answer.remote is not null
          and notice.role='steward' and notice.state='sent' and r.remote=notice.remote
          and not exists(select 1 from inbox where message=r.remote)
          and not exists(select 1 from outbox other where other.remote=r.remote and other.id!=notice.id)
        order by notice.rowid limit 16
    """,(app_id,)).fetchall()
    for item in ready:
        try:
            publisher.recall_notice(item['remote'])
        except (httpx.HTTPError, ValueError, RuntimeError) as exc:
            with db:
                db.execute("""update notice_recalls set attempts=attempts+1,
                    after=unixepoch()+min(3600,10*(1<<min(attempts,9))),error=? where outgoing=?""",
                    (str(exc)[:240],item['outgoing']))
        else:
            with db:
                db.execute("update notice_recalls set state='recalled',error=null where outgoing=?",(item['outgoing'],))

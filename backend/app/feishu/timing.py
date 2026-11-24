"""Gateway wall-clock measurements, with engine timestamps provided by the backend."""
from datetime import datetime, timezone
import time


def engine_time(value):
    if not value or not isinstance(value,str):
        return None
    try:
        parsed=datetime.fromisoformat(value)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError,OverflowError):
        return None


def submitting(db,row):
    db.execute("update delivery_timing set submit=coalesce(submit,?) where app=? and event=?",
               (time.time(),row['app'],row['event']))


def accepted(db,row,turn):
    db.execute("""update delivery_timing set engine_start=coalesce(engine_start,?),
        accepted=coalesce(accepted,?) where app=? and event=?""",
        (engine_time(turn.get('started_at')),time.time(),row['app'],row['event']))


def finished(db,row,turn):
    db.execute("""update delivery_timing set engine_end=coalesce(engine_end,?),
        engine_start=coalesce(engine_start,?) where app=? and event=?""",
        (engine_time(turn.get('finished_at')),engine_time(turn.get('started_at')),row['app'],row['event']))


def report(db):
    return db.execute("""select app,event,
        round(submit-received,2) as local_wait_and_prep_s,
        round(accepted-submit,2) as submit_until_ack_s,
        round(engine_end-engine_start,2) as backend_turn_s,
        round(sent-engine_end,2) as finish_until_delivery_s
        from delivery_timing order by received desc limit 20""")

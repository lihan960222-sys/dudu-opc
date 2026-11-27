"""A supervisor decides whether another employee turn will advance the original request."""
import hashlib
import json

from sqlalchemy import select

from app.db.models import AgentJob, AgentJobEvent, Event, Turn
from app.team import fence, service


REVIEW = "continuation_review"
REPORT_PAGE = 12000


def report_excerpt(text, limit=REPORT_PAGE):
    """Keep the diagnosis and conclusion visible even for a long worker report."""
    if len(text) <= limit:
        return fence.text(text, limit)
    half = limit // 2
    return (fence.text(text[:half], half) + "\n[…完整报告可用 job_status 的 report_offset 分页读取…]\n"
            + fence.text(text[-half:], half))


async def worker_report(db, job):
    turn = await db.get(Turn, job.assignee_turn_id) if job.assignee_turn_id else None
    if turn and turn.thread_id == job.assignee_thread_id:
        return turn.result or turn.error or job.result or job.outcome_detail or ""
    return job.result or job.outcome_detail or ""


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def observations(db, job, turn_id):
    """Ignore changing call IDs, timestamps and promises. Compare recorded effects and reads."""
    facts, artifacts = set(), {}
    events = await db.scalars(select(Event.data).where(
        Event.turn_id == turn_id, Event.type == "tool.completed").order_by(Event.id))
    for data in events:
        receipts = data.get("file_receipts") or []
        if data.get("file_receipt"):
            receipts = [*receipts, data["file_receipt"]]
        for receipt in receipts:
            if not isinstance(receipt, dict):
                continue
            facts.add(_digest(receipt))
            if receipt.get("operation") in ("write_text", "fileChange") and receipt.get("sha256"):
                key = _digest([receipt.get("root"), receipt.get("scope"), receipt.get("path")])
                artifacts[key] = receipt["sha256"]
        command = data.get("command_receipt") or {}
        if command.get("status") == "completed" and command.get("exit_code") == 0:
            # Running more commands is not itself evidence of a different result.
            output = data.get("output")
            if output:
                facts.add(_digest(output))
            if command.get("inspection"):
                facts.add(_digest(command["inspection"]))
    deliveries = await db.scalars(select(AgentJobEvent.detail).where(
        AgentJobEvent.job_id == job.id, AgentJobEvent.kind == "feishu_delivered",
        AgentJobEvent.detail["turn_id"].as_integer() == turn_id).order_by(AgentJobEvent.id))
    for stamp in deliveries:
        receipt = stamp.get("result") or {}
        if stamp.get("epoch") != job.assignment_epoch or stamp.get("employee") != job.assignee_agent_id:
            continue
        if receipt.get("url") and receipt.get("artifact_sha256"):
            artifacts[_digest(receipt["url"])] = receipt["artifact_sha256"]
        facts.add(_digest(receipt))
    reported = await db.scalar(select(AgentJobEvent.id).where(
        AgentJobEvent.job_id == job.id, AgentJobEvent.kind == "completion_evidence",
        AgentJobEvent.detail["turn_id"].as_integer() == turn_id,
        AgentJobEvent.detail["state"].as_string() == "reported").limit(1))
    if reported:
        # For a text deliverable, the delivered words themselves can change.
        # A rewritten refusal has no completion receipt and earns no retry.
        turn = await db.get(Turn, turn_id)
        if turn and turn.result:
            facts.add(_digest(turn.result.strip()))
    return facts, artifacts


async def latest_work(db, parent, target):
    # Scope by the authenticated request tree and assignee. A new title, project,
    # work-item ID or rework flag cannot manufacture a fresh progress budget.
    return await db.scalar(select(AgentJob).where(
        AgentJob.owner_id == parent.owner_id, AgentJob.parent_id == parent.id,
        AgentJob.assignee_agent_id == target, AgentJob.kind == "job",
        AgentJob.mode == "work").order_by(AgentJob.id.desc()).limit(1))


async def automated_turn(db, turn):
    started = await db.scalar(select(Event.data).where(Event.turn_id == turn.id,
        Event.type == "turn.started").limit(1))
    return turn.origin not in (None, "person") or bool((started or {}).get("internal"))


async def prior_attempts(db, previous):
    events = await db.execute(select(AgentJob, AgentJobEvent.kind, AgentJobEvent.detail).join(
        AgentJobEvent, AgentJobEvent.job_id == AgentJob.id).where(
        AgentJob.owner_id == previous.owner_id,
        AgentJob.parent_id == previous.parent_id if previous.parent_id is not None else AgentJob.id == previous.id,
        AgentJob.assignee_agent_id == previous.assignee_agent_id, AgentJob.mode == "work",
        AgentJobEvent.kind.in_(("started", "adopted", "owner_clarification", "answered"))).order_by(AgentJobEvent.id.desc()))
    # Older installations have no review events. Use their real worker starts, too.
    prior_turns = {}
    for job, kind, stamp in events:
        if kind in ("owner_clarification", "answered"):
            break
        if type(stamp.get("turn_id")) is int and stamp["turn_id"] < (previous.assignee_turn_id or 0):
            prior_turns[stamp["turn_id"]] = job
    return prior_turns


async def last_attempt(db, previous):
    turns = await prior_attempts(db, previous)
    if not turns:
        return None
    turn_id = max(turns)
    job = turns[turn_id]
    if job.task_id:
        from app.services.feature_tasks import assignment_current
        if not await assignment_current(db, job):
            return None
    turn = await db.get(Turn, turn_id)
    if not turn or turn.thread_id != job.assignee_thread_id:
        return None
    return {"job_id":job.id, "worker_turn_id":turn_id,
            "report":report_excerpt(turn.result or turn.error or "", 2400)}


async def review(db, previous, supplied):
    """Only called within the team write transaction. The caller records acceptance atomically."""
    turn_id = previous.assignee_turn_id
    if not turn_id:
        raise service.TeamError("progress_stopped", "上一单没有可核对的执行回合。请汇报现状，等新的事实或老板指示。")
    prior_turns = await prior_attempts(db, previous)
    if prior_turns:
        facts, artifacts = await observations(db, previous, turn_id)
        seen_facts, seen_artifacts = set(), set()
        repeated_artifact = False
        for old_turn, job in prior_turns.items():
            old_facts, old_artifacts = await observations(db, job, old_turn)
            repeated_artifact |= bool(artifacts and artifacts == old_artifacts)
            seen_facts.update(old_facts)
            seen_artifacts.update(old_artifacts.items())
        if repeated_artifact or (not facts - seen_facts and not artifacts.items() - seen_artifacts):
            raise service.TeamError("progress_stopped",
                "两轮执行没有取得新的可核实结果。请交付已有成果或说明阻塞，等待条件变化。")
    if (not isinstance(supplied, dict) or type(supplied.get("previous_turn_id")) is not int
            or supplied["previous_turn_id"] != turn_id or supplied.get("decision") != "continue"
            or not all(isinstance(supplied.get(key), str) and 0 < len(supplied[key].strip()) <= 1000
                       for key in ("evidence", "different_action", "expected_result"))):
        raise service.TeamError("progress_review_required",
            f"继续前请评估员工回合 {turn_id}，在 progress_review 中引用报告里的证据，写清与上次不同的做法和可核对的结果。没有可行的新做法就汇报现状。")
    report = await worker_report(db, previous)
    quote = supplied["evidence"].strip()
    searchable = "\n".join((report, previous.outcome_detail or "", previous.question or ""))
    if quote not in searchable and quote not in report_excerpt(searchable):
        raise service.TeamError("progress_evidence", "请引用该员工上轮报告或核对结果中的原文。")
    return {**{key: supplied[key].strip() for key in ("evidence", "different_action", "expected_result")},
            "previous_turn_id": turn_id, "previous_job_id": previous.id}

"""Owner-facing summaries. Full replies remain available in the attached Markdown."""
import re

LIMIT = 1000
SUFFIX = "详见完整回复。"
VERBATIM = re.compile(
    r"^运行记录：[ \t]*\r?\n[\s\S]*"
    r"|^(?:(?:报错|错误)(?:原文|信息)?|原话|工具回执|命令输出)[：:][ \t]*\r?\n[^\r\n]+(?:\r?\n(?![ \t]*\r?$)[^\r\n]+)*"
    r"|^ {0,3}(?P<fence>`{3,}|~{3,})[^\r\n]*(?:\r?\n|\Z)[\s\S]*?(?:^ {0,3}(?P=fence)[`~]*[ \t]*\r?$|\Z)"
    r"|^ {0,3}>[^\r\n]*(?:\r?\n(?![ \t]*\r?$| {0,3}(?:>|`{3,}|~{3,}))[^\r\n]+)*"
    r"|^(?: {4}|\t)[^\r\n]*|(?P<ticks>`+)(?!`)[\s\S]*?(?P=ticks)(?!`)"
    r"""|“[^”]*”|‘[^’]*’|「[^」]*」|『[^』]*』|《[^》]*》|"(?:\\.|[^"\\])*"|(?<![A-Za-z0-9_])'(?:\\.|[^'\\\r\n])*'"""
    r"|\[(?:\\.|[^\]\\\r\n])*\]\((?:\\.|[^)\\\r\n])*\)",
    re.MULTILINE,
)
DIRECT_REQUEST = re.compile(
    r"(?<![^\r\n。！？!?，,])(?P<lead>[ \t]*(?:老板[，,][ \t]*)?)"
    r"(?:(?P<urgent>你(?:必须|得|应该|赶快|赶紧|快点))(?=(?:先)?(?:确认|选择|授权|决定|提供|指定|填写|回复|打开|点击))"
    r"|(?P<you>你)(?=希望|想|愿意|方便|可以|能否|是否)"
    r"|(?:(?:请|麻烦)?[你您](?:这边|自己)?|麻烦)?(?:先|直接)?(?:给我?(?:一?个)|给我)"
    r"(?P<field>(?:任务|工单|项目|派工)?(?:编号|号)|单号|文件名|文档名|标题|日期)(?:就(?:行|可以)了?)?(?P<stop>[，,。.!！?？]|(?=\r?$)))",
    re.MULTILINE,
)
INSTRUCTIONS = (
    "向老板说话时称「老板」或「您」。老板有交代，就照您说的办。老板指出问题，先听取、核对，再回答。"
    "语气谦恭，不顶嘴，不催促，不把能自己查清的事推给老板。确实办错了就直接承担，例如「老板，是我没核对清楚」。"
    "说清哪一步没办好、自己已经做了什么，还能怎样补救。确实需要老板决定时，先说原因，再问一个具体问题。"
    "这些称呼只用于你自己对老板的回话。引文、代码、附件和代写文稿沿用原文或老板指定的口吻。"
    "按实际进度说话，有派工回执才说已派，有核验结果才说办妥。只承诺权限内能做的事，缺授权就如实说明。"
    f"先用一两句说清结果或卡点。每条给老板的主消息最多 {LIMIT} 字，正好 {LIMIT} 字可直接发。"
    "完整内容超过上限时，保留全部明细和原文，系统会发简短摘要和完整 Markdown。"
)


def is_owner_chat(thread, lead_id):
    return (lead_id is not None and thread.agent_id == lead_id and thread.kind == "chat"
            and thread.job_id is None and thread.room_id is None)


def turn_status(status, detail=""):
    message = {
        "failed": "老板，这次我没办完。",
        "cancelled": "老板，这次办理已经停止。",
        "interrupted": "老板，这次回复中断了，执行结果尚未核实。",
    }.get(status, "老板，这一轮已经结束，我还没有可交付的结果。")
    return message + "\n\n运行记录：\n" + detail if detail else message


def _polish(text, *, compact=False):
    if "你" not in text and "给" not in text:
        return text
    spans = VERBATIM.finditer(text)
    span = next(spans, None)

    def replace(match):
        nonlocal span
        while span and span.end() <= match.start():
            span = next(spans, None)
        if span and span.start() < match.end():
            return match[0]
        if match["field"]:
            if compact:
                return match["lead"] + "请问" + match["field"] + ("？" if match["stop"] else "")
            return match["lead"] + "您方便告诉我" + match["field"] + "吗？"
        return match["lead"] + ("请您" if match["urgent"] else "您")

    return DIRECT_REQUEST.sub(replace, text)


def polish(text):
    edited = _polish(text)
    return _polish(text, compact=True) if len(text) <= LIMIT < len(edited) else edited


def preview(text, *, attached=False):
    long_reply = attached or len(text) > LIMIT
    text = polish(text)
    if not long_reply:
        return text
    # Keep only the opening prose. List numbers, line breaks and Markdown
    # punctuation are not evidence that a sentence is complete.
    opening = re.split(r"(?:^|\s|(?<=[：:。！？；]))(?:\d+[.)、](?=\s|[^\x00-\x7f])|[-*+]\s)|\n", text.strip(), maxsplit=1)[0]
    opening = re.sub(r"\*\*([^*]+)\*\*", r"\1", opening)
    sentences = list(re.finditer(r"[。！？!?]|(?<=[A-Za-z])\.(?=\s|$)", opening))
    first = ""
    for stop in sentences[:2]:
        candidate = opening[:stop.end()].strip()
        if len(candidate) > 135 or any(mark in candidate for mark in ("`", "[", "]", "://", "|", "#")):
            break
        if candidate.count("（") == candidate.count("）") and candidate.count("(") == candidate.count(")"):
            first = candidate
    if not first and opening and not any(mark in opening for mark in ("`", "[", "]", "#", "|", "<", ">", "://")):
        first = "原文开头：" + opening[:80] + ("…" if len(opening) > 80 else "")
    return first + "\n" + SUFFIX if first else "具体内容见完整回复。"

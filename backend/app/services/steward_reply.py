"""Owner-facing summaries. Full replies remain available in the attached Markdown."""
import re

LIMIT = 200
SUFFIX = "详见完整回复。"
INSTRUCTIONS = (
    "给老板汇报时，先用一两句说清结果或卡点，以及接下来由谁处理。"
    "语气自然、礼貌，像熟悉业务的秘书。正文不超过200字；必要的完整明细放在结论之后。"
    "只在需要老板决定时提一个具体问题。"
)


def polish(text):
    # Replace only a direct request at the start of a plain prose paragraph.
    # Avoid editing quoted material, code samples and multiline Markdown blocks.
    if any(mark in text for mark in ('```', '~~~', '“', '「', '"', '\n>')):
        return text
    return re.sub(r"(?m)^(?:你必须|你得)(?=(?:先)?(?:确认|选择|授权|决定|提供|指定|填写|回复|打开|点击))", "请您", text)


def preview(text):
    text = polish(text)
    if len(text) <= LIMIT:
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
    return first + "\n" + SUFFIX if first else "具体内容见完整回复。"

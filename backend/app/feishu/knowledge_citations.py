"""Feishu chat positions locate a message. Its stable message ID identifies the source."""
import re
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit


BASE = "https://applink.feishu.cn/client/chat/open"
SOURCE_ID = r"[A-Za-z0-9_-]{1,256}"
LABEL = re.compile(r"　来源[0-9]+〔(" + SOURCE_ID + r")〕")
ARCHIVED = re.compile(r"　历史来源〔(" + SOURCE_ID + r")〕（仅编号）")


def position(item):
    value = item.get("message_position")
    if (type(value) not in (str, int) or not re.fullmatch(r"[0-9]{1,20}", str(value))):
        raise ValueError("飞书未提供这条消息的有效定位，无法生成原消息链接。")
    return str(int(value))


def element(number, mid, item, chat):
    if (not isinstance(mid, str) or not re.fullmatch(SOURCE_ID, mid)
            or item.get("message_id", mid) != mid or item.get("chat_id", chat) != chat):
        raise ValueError("来源编号与本次读取的消息或会话不符。")
    url = BASE + "?" + urlencode({"openChatId": chat, "position": position(item)})
    # Docx Link.url uses a URL-encoded link, including its delimiters.
    return {"text_run": {"content": f"　来源{number}〔{mid}〕", "text_element_style": {"link": {"url": quote(url, safe="")}}}}


def decoded_link(value):
    if isinstance(value, str) and value.lower().startswith("https%3a%2f%2f"):
        return unquote(value)
    return value


def archived_ids(run):
    text = run.get("content", "")
    if run.get("text_element_style", {}).get("italic") is not True or not text:
        return []
    matches = list(ARCHIVED.finditer(text))
    return [m[1] for m in matches] if "".join(m[0] for m in matches) == text else []


def identify(run, chat, *, records=None):
    """Read citation IDs and flag legacy links. Known IDs must match provider positions."""
    label = run.get("content", "")
    link = run.get("text_element_style", {}).get("link")
    if not link:
        archived = archived_ids(run)
        if records is not None and any(mid in records for mid in archived):
            raise ValueError("本次来源缺少可定位的消息链接。")
        return archived, False
    url = urlsplit(decoded_link(link.get("url", "")))
    params = parse_qs(url.query, keep_blank_values=True)
    if (url.scheme != "https" or url.netloc != "applink.feishu.cn" or url.path != "/client/chat/open"
            or url.fragment or params.get("openChatId") != [chat]):
        raise ValueError("原综述有无法对应到授权会话的来源。")
    if set(params) == {"openChatId", "messageId"} and len(params["messageId"]) == 1:
        mid = params["messageId"][0]
        if not re.fullmatch(SOURCE_ID, mid) or records is not None and mid in records:
            raise ValueError("旧来源编号不能证明本次消息的位置。")
        return [mid], True
    source = LABEL.fullmatch(label)
    values = params.get("position")
    if (not source or set(params) != {"openChatId", "position"} or not values or len(values) != 1
            or not re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", values[0])):
        raise ValueError("原综述缺少可核对的消息编号或位置。")
    mid = source[1]
    if records is not None and mid in records and position(records[mid]) != values[0]:
        raise ValueError("飞书正文里的来源位置与原消息不符。")
    return [mid], False


def archive_old_links(block, chat):
    """Weekly authority covers new messages. An old chat-only link can retain its ID without a link."""
    if block.get("block_type") != 2:
        return block
    original = block["text"]["elements"]
    elements = list(original[:1])
    for el in original[1:]:
        ids, legacy = identify(el.get("text_run", {}), chat)
        if legacy:
            content = f"　历史来源〔{ids[0]}〕（仅编号）"
            # Write one styled span for adjacent archived IDs. The provider can
            # merge equal styles, and cannot merge it into the unstyled summary.
            if archived_ids(elements[-1].get("text_run", {})):
                content = elements.pop()["text_run"]["content"] + content
            el = {"text_run": {"content": content, "text_element_style": {"italic": True}}}
        elements.append(el)
    return {**block, "text": {**block["text"], "elements": elements}}

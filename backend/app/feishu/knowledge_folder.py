"""Read only the immediate children of the employee's configured knowledge folder."""
import httpx

from . import documents
from .document_access import FOLDER_SCOPE, READ_SCOPE
from .publisher import API, PlatformError, checked

MAX_PAGES = 5
PAGE_SIZE = 200


def inspect(client, headers, folder, title, document=None):
    """Return counts and bound-document membership without disclosing other entries."""
    result = {"state": "incomplete", "complete": False, "pages_checked": 0,
              "entries_checked": 0, "same_title_entries_seen": 0, "bound_document_in_folder": None,
              "bound_title_matches": None}
    params = {"folder_token": folder, "page_size": PAGE_SIZE}
    cursors = set()
    entries = {}
    for _ in range(MAX_PAGES):
        try:
            response = client.get(API + "/drive/v1/files", headers=headers(), params=params, timeout=15)
        except httpx.RequestError:
            return {**result, "state": "unavailable"}
        try:
            data = checked(response)["data"]
        except (httpx.HTTPStatusError, PlatformError, ValueError, TypeError, KeyError, AttributeError):
            result.update(state="unavailable", http_status=response.status_code)
            try:
                error = response.json()
            except ValueError:
                error = None
            if isinstance(error, dict) and type(error.get("code")) is int:
                result["feishu_code"] = error["code"]
            return result
        if not isinstance(data, dict) or not isinstance(data.get("files"), list) or len(data["files"]) > PAGE_SIZE:
            return result
        result["pages_checked"] += 1
        for item in data["files"]:
            if not isinstance(item, dict) or item.get("parent_token") != folder:
                return result
            token, name, kind = item.get("token"), item.get("name"), item.get("type")
            if not all(isinstance(field, str) and field for field in (token, name, kind)):
                return result
            if token in entries:
                if entries[token] != (name,kind):
                    return result
                continue
            entries[token] = (name,kind)
            result["entries_checked"] += 1
            if name == title:
                result["same_title_entries_seen"] += 1
            if document == token and kind == "docx":
                result["bound_document_in_folder"] = True
                result["bound_title_matches"] = name == title
        if data.get("has_more") is False:
            result.update(state="verified", complete=True)
            if document and result["bound_document_in_folder"] is None:
                result["bound_document_in_folder"] = False
            return result
        cursor = data.get("next_page_token")
        if data.get("has_more") is not True or not isinstance(cursor, str) or not cursor or cursor in cursors:
            return result
        params["page_token"] = cursor
        cursors.add(cursor)
    return result


def creation_blocker(folder):
    if not folder.get("complete"):
        return "未能完整核对指定文件夹，无法判断是否已有同名文件。"
    if folder["same_title_entries_seen"]:
        return "指定文件夹已有同名条目，请核对原件，暂不重建。"
    return None


def existing_blocker(folder, title_matches):
    if not folder.get("complete"):
        return "未能完整核对原文档所在的文件夹。"
    if not folder.get("bound_document_in_folder"):
        return "原文档已移出指定文件夹或当前无法看见。"
    if not title_matches or not folder.get("bound_title_matches"):
        return "原文档名称已变更，请核对原件。"
    if folder["same_title_entries_seen"] != 1:
        return "目标文件夹有多个同名条目，请先核对原件。"
    return None


def status(client, headers, dest, title, binding, *, grant, may_publish, weekly):
    """Describe current cloud facts separately from permission to submit a draft."""
    from . import knowledge_document
    from .history import SCOPES as HISTORY_SCOPES

    doc = binding[0] if binding else None
    target = {"title": title, "url": None, "remote_state": "unbound" if binding is None else "creation_unknown",
              "sync_available": False, "publish_action": "blocked"}
    scopes = set(grant.get("scopes", ())) if grant.get("connected") else set()
    if {READ_SCOPE, FOLDER_SCOPE} <= scopes:
        folder = inspect(client, lambda: headers({READ_SCOPE, FOLDER_SCOPE}), dest["folder_token"], title, doc)
        folder["url"] = dest["tenant_origin"] + "/drive/folder/" + dest["folder_token"]
    else:
        folder = {"state": "permission_required", "complete": False}
    target["folder"] = folder
    if doc:
        target["remote_state"] = "unknown"
        if READ_SCOPE in scopes:
            try:
                metadata = knowledge_document.inspect_document(client, lambda: headers({READ_SCOPE}), doc)
            except (ValueError, httpx.HTTPError, PlatformError, TypeError, KeyError):
                metadata = None
            else:
                target["remote_state"] = "present" if metadata else "missing"
            if metadata:
                target.update(url=dest["tenant_origin"] + "/docx/" + doc, remote_title=metadata.get("title"))
    if not (HISTORY_SCOPES | documents.SCOPES | {FOLDER_SCOPE, READ_SCOPE}) <= scopes:
        reason = "请核对该员工的聊天记录、文档读写和云盘文件夹授权。"
    elif not may_publish:
        reason = "当前派工只允许核对来源和目标，没有发布权限。"
    elif target["remote_state"] == "creation_unknown":
        reason = "上次创建的结果待核对，不能再次创建。"
    elif target["remote_state"] == "unknown":
        reason = "无法确认原文档是否仍在，请先核对飞书读取权限。"
    elif weekly and (doc != weekly.get("document") or target["remote_state"] != "present"):
        reason = "周计划只能更新原文档，请先核对已登记的原件。"
    elif target["remote_state"] == "present":
        reason = existing_blocker(folder, target["remote_title"] == title)
        action = "update_bound_document"
    else:
        reason = creation_blocker(folder)
        action = "create_in_bound_folder"
    if reason:
        target["reason"] = reason
    else:
        effect = "按已登记的名称在此文件夹新建一份文档" if action == "create_in_bound_folder" else "更新已核对的原文档"
        target.update(sync_available=True, publish_action=action, tool="feishu_publish_knowledge",
                      next_step="读完所有来源后，用 feishu_publish_knowledge 提交带消息来源的分类稿。该工具会"+effect+
                      "，写入后核对正文、来源链接和父文件夹，成功时返回文档链接。")
    return target

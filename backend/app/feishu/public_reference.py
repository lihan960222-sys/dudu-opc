"""Read an already scoped public source without sending employee credentials."""
import asyncio
from html.parser import HTMLParser
import re
from urllib.parse import urljoin, urlsplit

import httpx
from app.community_mcp import McpError, PinnedHTTPSTransport


class ArticleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.omit = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template", "svg"):
            self.omit += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "template", "svg") and self.omit:
            self.omit -= 1

    def handle_data(self, data):
        if not self.omit and data.strip():
            self.parts.append(data.strip())


async def read(url, offset=0):
    if type(offset) != int or offset < 0:
        raise ValueError("正文起始位置无效。")
    try:
        async with asyncio.timeout(20), httpx.AsyncClient(
            transport=PinnedHTTPSTransport(), trust_env=False, timeout=8,
            headers={"Accept-Encoding":"identity","Accept":"text/html,text/plain,application/pdf"}
        ) as client:
            for _ in range(5):
                parsed = urlsplit(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None,443) or parsed.username is not None:
                    raise ValueError("公开来源须为 HTTPS 网页。")
                client.cookies.clear()
                async with client.stream("GET",url) as response:
                    if response.is_redirect and "location" in response.headers:
                        url = urljoin(url,response.headers["location"])
                        continue
                    response.raise_for_status()
                    mime=response.headers.get("content-type","").split(";")[0].lower().strip()
                    if mime not in ("text/html","text/plain","application/xhtml+xml","application/pdf"):
                        raise ValueError("此链接格式暂不支持，尚未取得正文。")
                    if response.headers.get("content-encoding","identity").lower() != "identity":
                        raise ValueError("此来源需要压缩传输，尚未取得正文。")
                    raw=bytearray()
                    async for chunk in response.aiter_raw():
                        if len(raw)+len(chunk)>2_000_000:
                            raise ValueError("来源正文过大，尚未读完。")
                        raw.extend(chunk)
                    if mime == "application/pdf":
                        from app.services.attachments import AttachmentError, extract_document_text
                        try:
                            body=await asyncio.to_thread(extract_document_text,bytes(raw),".pdf","reference.pdf")
                        except AttachmentError as exc:
                            raise ValueError("PDF 被加密或无法解析，尚未取得正文。") from exc
                        if body.startswith("[PDF "):
                            raise ValueError("PDF 没有可提取的文字层，尚未读取正文。")
                    else:
                        body=raw.decode(response.encoding or "utf-8", errors="replace")
                    if mime in ("text/html","application/xhtml+xml"):
                        page=ArticleText()
                        page.feed(body)
                        page.close()
                        body="\n".join(page.parts)
                    body=re.sub(r"[^\S\n]+"," ",body).strip()
                    if not body:
                        raise ValueError("网页没有返回可读取的正文。")
                    end=min(len(body),offset+12000)
                    return {"url":url,"text":body[offset:end],"complete":end==len(body) and "[... Truncated:" not in body,
                            "coverage":"PDF 可提取文字层，不含扫描图像" if mime=="application/pdf" else "公开页面返回的文字",
                            "next_offset":end if end<len(body) else None}
            raise ValueError("链接重定向次数过多，尚未取得正文。")
    except (httpx.HTTPError, McpError, TimeoutError, LookupError, OSError) as exc:
        raise ValueError("未能读取公开来源，请记录为未核实并保留原链接。") from exc

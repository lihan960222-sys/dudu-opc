import httpx
import pytest

from app.feishu import public_reference


@pytest.mark.asyncio
async def test_reference_reads_visible_text_and_marks_partial_page(monkeypatch):
    seen = []
    def serve(request):
        seen.append(request)
        if request.url.path == "/paper":
            return httpx.Response(302, headers={"location":"https://publisher.example/article","set-cookie":"secret=1"})
        return httpx.Response(200, headers={"content-type":"text/html; charset=utf-8"},
                              stream=httpx.ByteStream(b"<script>hidden</script><p>" + b"a"*12003 + b"</p>"))
    monkeypatch.setattr(public_reference, "PinnedHTTPSTransport", lambda: httpx.MockTransport(serve))
    first = await public_reference.read("https://journal.example/paper")
    assert first["text"] == "a"*12000 and first["next_offset"] == 12000 and not first["complete"]
    last = await public_reference.read("https://journal.example/paper",12000)
    assert last["text"] == "aaa" and last["complete"]
    assert all("authorization" not in req.headers and "cookie" not in req.headers for req in seen)


@pytest.mark.asyncio
async def test_reference_refuses_login_and_untrusted_protocol(monkeypatch):
    def serve(request):
        return httpx.Response(403)
    monkeypatch.setattr(public_reference, "PinnedHTTPSTransport", lambda: httpx.MockTransport(serve))
    with pytest.raises(ValueError, match="未能读取"):
        await public_reference.read("https://example.org/login")
    with pytest.raises(ValueError, match="HTTPS"):
        await public_reference.read("file:///etc/passwd")

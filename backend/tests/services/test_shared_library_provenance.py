import hashlib

import httpx
import pytest

from app.core.config import settings
from app.services import shared_library as lib

BASE = "/repos/NVIDIA-Omniverse/demo"
SHA = "a" * 40
TIP = "b" * 40


def release():
    return lib.Release(name="kit", version="1.2", kind="tool", sha256=hashlib.sha256(b"pass").hexdigest(),
                       url=f"https://raw.githubusercontent.com/NVIDIA-Omniverse/demo/{SHA}/demo.py")


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("GITHUB_TOKEN", "dummy-for-test")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    factory = httpx.Client
    state = {"comparison":"ahead", "merge_base":SHA, "tag":False, "outage":False, "requests":[]}

    def handle(request):
        assert request.url.host == "api.github.com"
        assert not ({"authorization","cookie","proxy-authorization"} & set(request.headers))
        state["requests"].append(request.url.path)
        p = request.url.path
        if state["outage"]:
            return httpx.Response(503)
        if p == BASE:
            body = {"full_name":"NVIDIA-Omniverse/demo", "default_branch":"dev"}
        elif p == BASE + "/git/ref/heads/dev":
            body = {"ref":"refs/heads/dev","object":{"type":"commit","sha":TIP}}
        elif p == BASE + f"/compare/{SHA}...{TIP}":
            body = {"status":state["comparison"], "merge_base_commit":{"sha":state["merge_base"]}}
        elif p == BASE + "/git/ref/tags/v1.2" and state["tag"]:
            body = {"ref":"refs/tags/v1.2","object":{"type":"tag","sha":"c" * 40}}
        elif p == BASE + "/git/tags/" + "c"*40:
            body = {"object":{"type":"commit","sha":SHA}}
        else:
            return httpx.Response(404)
        return httpx.Response(200,json=body,headers={"set-cookie":"session=fixture; Path=/"})

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return factory(transport=httpx.MockTransport(handle), **kwargs)
    monkeypatch.setattr(httpx, "Client", client)
    return state


def test_upstream_ancestor_and_exact_annotated_tag(api):
    item = release()
    assert lib._github_ref(item) == "heads/dev@" + TIP
    api.update(comparison="diverged", merge_base=TIP, tag=True)
    assert lib._github_ref(item) == "tags/v1.2@" + SHA


@pytest.mark.parametrize("outage", [True, False])
def test_unproven_fork_or_unavailable_api_blocks_legacy_objects(api, tmp_path, outage):
    item = release()
    root = tmp_path / "role"
    root.mkdir()
    lib.publish("owner", item, fetched=b"pass")
    lib.pin("owner", root, item.name, item.version)
    with lib._db("owner") as db:
        db.execute("DELETE FROM github_provenance")  # Simulate catalog written before the verifier existed.
    api.update(comparison="diverged", merge_base=TIP, outage=outage)
    assert lib.catalog("owner") == [] and lib.project_files("owner", root) == ()
    with pytest.raises(ValueError):
        lib.pin("owner", root, item.name, item.version)
    with pytest.raises(ValueError):
        lib.publish("owner", item, fetched=b"pass")
    with pytest.raises(ValueError):
        lib.prefetch("owner", item, root)
    with pytest.raises(ValueError):
        lib.publish("owner", item, approve_external=True, trust_publisher=True, fetched=b"pass")
    assert lib.publishers("owner") == [] and lib.catalog("owner") == []
    api.update(comparison="ahead", merge_base=SHA, outage=False)
    assert lib.publish("owner", item)["trust"] == "official"
    assert len(lib.project_files("owner", root)) == 1

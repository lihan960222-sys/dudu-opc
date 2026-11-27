import hashlib
import pytest

from app.core.config import settings
from app.services import shared_library as lib


@pytest.fixture(autouse=True)
def official_ref_fixture(monkeypatch):
    # Provenance transport and legacy records have dedicated integration tests.
    monkeypatch.setattr(lib, "_github_ref", lambda item: "heads/main@" + "a" * 40)


def spec(data, version="1", org="NVIDIA-Omniverse", name="kit-example"):
    return lib.Release(name=name, version=version, kind="tool", sha256=hashlib.sha256(data).hexdigest(),
        url=f"https://raw.githubusercontent.com/{org}/new-kit-project/{'a' * 40}/examples/tool.py")


def test_immutable_cached_releases_and_separate_project_pins(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "private" / "app.db"))
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    one, two = spec(b"print(1)"), spec(b"print(2)", "2")
    monkeypatch.setattr(lib, "_download", lambda item: b"print(1)")
    lib.publish("owner", one)
    monkeypatch.setattr(lib, "_download", lambda item: pytest.fail("unexpected download"))
    lib.publish("owner", one)
    with pytest.raises(ValueError):
        lib.publish("owner", spec(b"different"))
    monkeypatch.setattr(lib, "_download", lambda item: b"print(2)")
    lib.publish("owner", two)
    lib.pin("owner", a, one.name, "1")
    lib.pin("owner", b, one.name, "2")
    assert [p.read_bytes() for p in lib.project_files("owner", a)] == [b"print(1)"]
    assert [p.read_bytes() for p in lib.project_files("owner", b)] == [b"print(2)"]
    assert lib.project_files("other", b) == ()
    lib.pin("owner", a, one.name, None)
    assert lib.project_files("owner", a) == ()


def test_authority_digest_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "app.db"))
    source = spec(b"pass", org="community")
    with pytest.raises(ValueError):
        lib.publish("owner", source)
    monkeypatch.setattr(lib, "_download", lambda item: b"tampered")
    with pytest.raises(ValueError):
        lib.publish("owner", source, approve_external=True)
    assert lib.catalog("owner") == []
    monkeypatch.setattr(lib, "_download", lambda item: b"pass")
    assert lib.publish("owner", source, approve_external=True)["trust"] == "owner-approved"
    for url in ["https://raw.githubusercontent.com.evil.test/x", "http://localhost/tool.py",
                "https://raw.githubusercontent.com/NVIDIA-Omniverse/demo/main/tool.py",
                "https://ovextensionsprod.blob.core.windows.net/exts/kit/community/foo.zip",
                "https://ovextensionsprod.blob.core.windows.net/exts/kit/prod/../community/foo.zip"]:
        with pytest.raises(ValueError):
            lib.publish("owner", spec(b"pass", "2").model_copy(update={"url": url}))


def test_trial_promotion_and_trusting_future_publisher(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "app.db"))
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    data = b"---\nname: demo\ndescription: Remote source\n---\nFollow the project notes."
    monkeypatch.setattr(lib, "_download", lambda _: data)
    item = spec(data, org="outsider").model_copy(update={"kind":"skill", "url":
        f"https://raw.githubusercontent.com/outsider/skills/{'d' * 40}/demo/SKILL.md"})
    registered, trial = lib.acquire("owner", a, item)
    assert trial and lib.catalog("owner") == []
    lib.pin("owner", a, item.name, item.version, trial=trial)
    assert lib.project_entries("owner", a)[0]["trust"] == "project-trial"
    assert lib.skill("owner", a, item.name).body == "Follow the project notes."
    assert lib.project_entries("owner", b) == []
    lib.publish("owner", item, approve_external=True)
    lib.pin("owner", b, item.name, item.version)
    assert lib.skill("owner", b, item.name).revision == item.sha256
    future = item.model_copy(update={"version":"2"})
    assert lib.acquire("owner", a, future)[1]
    lib.publish("owner", item, approve_external=True, trust_publisher=True)
    assert not lib.acquire("owner", b, future)[1]


def test_exact_production_and_archive_sources():
    item = spec(b"p")
    for url in ["https://ovextensionsprod.blob.core.windows.net/exts/kit/prod/110.2/shared/tool.zip",
                f"https://codeload.github.com/NVIDIA-Omniverse/new-template/zip/{'c' * 40}"]:
        assert lib.source(item.model_copy(update={"url":url,"kind":"package"}))[1]
    for url in ["https://dw290v42wisod.cloudfront.net/exts/kit/community/tool.zip",
                f"https://codeload.github.com/NVIDIA-Omniverse/new-template/zip/main"]:
        with pytest.raises(ValueError):
            lib.source(item.model_copy(update={"url":url,"kind":"package"}))


def test_team_tools_control_publishers_and_plan_mode(tmp_path):
    from app.team.mcp_server import Server
    from app.team.policy import automatic, TurnTeam
    employee = TurnTeam(1,2,False,False,strict_hierarchy=True,feature_consult=True)
    steward = TurnTeam(1,1,True,False,strict_hierarchy=True)
    assert automatic("mcp__team__publish_shared_release", "ask", employee, {})[0] == "denied"
    assert automatic("mcp__team__publish_shared_release", "ask", steward, {})[0] == "allowed"
    assert automatic("mcp__team__use_project_release", "ask", employee, {})[0] == "allowed"
    assert automatic("mcp__team__use_project_release", "plan", employee, {})[0] == "denied"
    assert "publish_shared_release" in {t["name"] for t in Server(tmp_path,steward=True).tools}
    worker_tools = {t["name"] for t in Server(tmp_path,employee_only=True,project_release=True).tools}
    assert "publish_shared_release" not in worker_tools and "use_project_release" in worker_tools


def test_digest_and_offline_promotion_and_publisher_revocation(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "lib.db"))
    root = tmp_path / "project"
    root.mkdir()
    file = spec(b"print(1)")
    monkeypatch.setattr(lib, "_download", lambda item: b"print(1)")
    request = lib.ReleaseRequest(**file.model_dump(exclude={"sha256"}))
    release, raw = lib.resolve_request("owner", request)
    assert release == file
    lib.publish("owner", release, fetched=raw)
    outsider = spec(b"print(1)", org="another", name="trial")
    with pytest.raises(ValueError):
        lib.resolve_request("owner", lib.ReleaseRequest(**outsider.model_dump(exclude={"sha256"})))
    _, trial = lib.acquire("owner", root, outsider)
    lib.pin("owner", root, outsider.name, outsider.version, trial=trial)
    monkeypatch.setattr(lib, "_download", lambda item: pytest.fail("promotion should use cached trial"))
    lib.publish("owner", outsider, approve_external=True, trust_publisher=True, trial_project=root)
    assert lib.publishers("owner") == ["github/another"]
    lib.forget_publisher("owner", "github/another")
    assert lib.publishers("owner") == []
    with pytest.raises(ValueError):
        lib.publish("owner", outsider.model_copy(update={"version":"2"}))


def test_release_permission_rechecked_before_install(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "db.sqlite"))
    root = tmp_path / "p"
    root.mkdir()
    monkeypatch.setattr(lib, "_download", lambda _: b"pass")
    one = spec(b"pass", org="third-party")
    lib.publish("owner", one, approve_external=True, trust_publisher=True)
    two = one.model_copy(update={"version":"2"})
    data = lib.prefetch("owner", two)
    lib.forget_publisher("owner", "github/third-party")
    with pytest.raises(ValueError):
        lib.publish("owner", two, fetched=data)
    assert lib.acquire("owner", root, two, fetched=data)[1]


async def test_writable_worker_pins_and_copies_without_touching_others(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock
    from app.services import employee_runtime, employee_terminal
    from app.team import steward_review, service
    from app.db import transactions

    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "private" / "db.sqlite"))
    project = NS(root=tmp_path / "employee", writable=True)
    project.root.mkdir()
    monkeypatch.setattr(employee_terminal, "allowed_tools", lambda *args, **kw: ["list_project_files"])
    monkeypatch.setattr(employee_runtime, "checked_active_project", AsyncMock(return_value=project))
    job = AsyncMock(return_value=True)
    monkeypatch.setattr(steward_review, "current_native_job", job)
    monkeypatch.setattr(transactions, "begin_write", AsyncMock())
    monkeypatch.setattr(lib, "_download", lambda _: b"print(1)")

    @asynccontextmanager
    async def session():
        yield None

    run, rt = NS(ctx=NS()), NS(session=session, owner_id="owner")
    fields = spec(b"print(1)",org="community").model_dump()
    args = {k: fields.pop(k) for k in ("name","version")}
    args.update(source=fields, copy_to_project=True)
    result = await lib.use_release(rt, run, args)
    assert result["trust"] == "project-trial"
    assert (project.root / "kit-example-1.py").read_bytes() == b"print(1)"
    assert lib.catalog("owner") == []
    job.side_effect = [True, False]
    with pytest.raises(service.TeamError):
        await lib.use_release(rt, run, {**args, "version":"2"})
    assert lib.project_entries("owner", project.root)[0]["version"] == "1"


def test_review_queue_deduplicates_and_decides_once(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "private" / "db.sqlite"))
    roots = [tmp_path / n for n in ("one", "two")]
    for folder in roots:
        folder.mkdir()
    monkeypatch.setattr(lib, "_download", lambda _: b"pass")
    version = spec(b"pass", org="community")
    for folder in roots:
        assert lib.acquire("owner", folder, version)[1]
        lib.request_review("owner", folder, version)
    queue = lib.pending_reviews("owner")
    assert len(queue) == 1
    assert not lib.pending_reviews("other")
    lib.decide_review("owner", queue[0]["id"], "private")
    lib.request_review("owner", roots[0], version)
    assert lib.pending_reviews("owner") == []
    next_version = version.model_copy(update={"version":"2"})
    lib.acquire("owner", roots[0], next_version)
    lib.request_review("owner", roots[0], next_version)
    review_id = lib.pending_reviews("owner")[0]["id"]
    monkeypatch.setattr(lib, "_download", lambda _: pytest.fail("promote from cache"))
    lib.decide_review("owner", review_id, "publisher")
    assert lib.pending_reviews("owner") == [] and lib.publishers("owner") == ["github/community"]
    assert lib.catalog("owner")[0]["version"] == "2"
    with pytest.raises(ValueError):
        lib.decide_review("owner", review_id, "private")


def test_public_wheel_cannot_establish_marketplace_trust(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "lib.db"))
    wheel = spec(b"x").model_copy(update={"kind":"package",
        "url":"https://files.pythonhosted.org/packages/ab/cd/abcd/pytest-8.0.0-py3-none-any.whl"})
    assert lib.source(wheel) == ("pypi-file", False)
    with pytest.raises(ValueError):
        lib.publish("owner", wheel, approve_external=True, trust_publisher=True)


def test_redirect_and_digest_failure_leave_catalog_empty(tmp_path, monkeypatch):
    import httpx
    monkeypatch.setattr(settings, "SQLITE_PATH", str(tmp_path / "app.db"))
    requests = []
    def redirect(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location":"https://third-party.example/tool.py"})
    factory = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: factory(transport=httpx.MockTransport(redirect), **kwargs))
    item = spec(b"pass")
    with pytest.raises(ValueError):
        lib.publish("owner", item)
    assert requests == [item.url] and not lib.catalog("owner")

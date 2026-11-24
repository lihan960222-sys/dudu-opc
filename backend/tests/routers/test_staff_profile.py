import os

import pytest
from fastapi import HTTPException

from app.routers.staff import EditRules, folders, read_rules, write_rules
from tests.services.thread_harness import cli, owner, rt, sessions  # noqa: F401


def test_edit_rules_preserves_model_header_format_and_rejects_a_stale_revision(tmp_path):
    directory = tmp_path / "reviewer"
    directory.mkdir()
    path = directory / "AGENTS.md"
    path.write_bytes(b"\xef\xbb\xbf---\r\n  default_model:  old\r\n---\r\nold text\r\n")
    first = read_rules(directory)
    changes = EditRules(**{**first, "instructions": "new text\n", "model": "next"})
    result = write_rules(directory, changes)
    assert result["model"] == "next" and result["instructions"] == "new text"
    assert path.read_bytes() == b"\xef\xbb\xbf---\r\n  default_model:  next\r\n---\r\nnew text\r\n"
    with pytest.raises(HTTPException) as error:
        write_rules(directory, changes)
    assert error.value.status_code == 409


def test_rules_cannot_write_linked_files_and_browser_lists_directories(tmp_path):
    directory = tmp_path / "role"
    directory.mkdir()
    original = tmp_path / "original"
    original.write_text("---\ndefault_model: old\n---\noriginal")
    os.link(original, directory / "AGENTS.md")
    with pytest.raises(ValueError):
        read_rules(directory)
    assert folders(str(tmp_path))["folders"] == ["role"]
    assert folders(str(directory))["parent"] == str(tmp_path)
    with pytest.raises(HTTPException):
        folders("../")
async def test_http_profile_and_owner_scope(sessions, owner, rt, cli, tmp_path, monkeypatch):
    from tests.routers.test_agents_api import app_for, client_for, make
    from app.core.config import settings
    from app.routers.staff import router
    from app.services import image_gen
    from app.services.role_workspace import initialize
    from app.db.models import Agent
    import base64
    import struct
    import zlib

    monkeypatch.setattr(settings, "LOCAL_EXECUTION_ENABLED", True)
    monkeypatch.setattr(image_gen, "avatars_root", lambda: tmp_path / "pictures")
    app = app_for(rt)
    app.include_router(router, prefix="/api/v1")
    directory = tmp_path / "staff"
    directory.mkdir()
    initialize(directory, "sonnet", "first")
    async with client_for(app) as client:
        person = await make(client, name="Profile reviewer", model="sonnet")
        ident = person["id"]
        async with sessions() as db:
            row = await db.get(Agent, ident)
            row.role_path = str(directory)
            await db.commit()
        endpoint = f"/api/v1/staff/{ident}/rules"
        before = (await client.get(endpoint)).json()
        edit = {**before, "instructions": "second"}
        assert (await client.put(endpoint, json=edit)).status_code == 200
        assert (await client.put(endpoint, json=edit)).status_code == 409
        assert (await client.get(endpoint)).json()["instructions"] == "second"
        def chunk(kind, data):
            return struct.pack(">I",len(data)) + kind + data + struct.pack(">I",zlib.crc32(kind+data))
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR",struct.pack(">IIBBBBB",192,192,8,2,0,0,0))
               + chunk(b"IDAT",zlib.compress(bytes((192*3+1)*192))) + chunk(b"IEND",b""))
        avatar = await client.put(f"/api/v1/staff/{ident}/picture", json={"png":base64.b64encode(png).decode()})
        assert avatar.status_code == 204
        assert image_gen.read_avatar(ident) == (png, "image/png")
        async with sessions() as db:
            person = await db.get(Agent, ident)
            person.owner_id = "another-owner"
            await db.commit()
        assert (await client.get(endpoint)).status_code == 404

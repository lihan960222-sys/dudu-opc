from pathlib import Path
import os
import pytest
from app.services.feature_files import FeatureFiles

def test_files_are_scoped(tmp_path):
    role = tmp_path / "role"
    task = role / "任务" / "17"
    task.mkdir(parents=True)
    other = role / "任务" / "18"
    other.mkdir()
    (other/"private.txt").write_text("other")
    (role/"notes.txt").write_text("common")
    fs = FeatureFiles(task)
    assert fs.call("read_text", {"scope":"role", "path":"notes.txt"})["text"] == "common"
    assert fs.call("write_text", {"path":"out.txt","text":"some work"})["bytes"] == 9
    assert fs.call("read_text", {"scope":"task","path":"out.txt"})["text"] == "some work"
    for scope, path in [("role","任务/18/private.txt"), ("task","../18/private.txt"), ("task","out.txt:secret"), ("role","MEMORY.md")]:
        with pytest.raises(ValueError):
            fs.call("read_text", {"scope":scope,"path":path})
    os.link(other/"private.txt", task/"alias.txt")
    with pytest.raises(ValueError):
        fs.call("read_text", {"scope":"task","path":"alias.txt"})

def test_codex_images_and_scoped_config():
    from app.services.codex_adapter import CodexAdapter
    from app.services.feature_files import turn_config
    assert CodexAdapter.capabilities.images
    config = turn_config({"mcp_servers":{"global-tools":{}}, "plugins":{"plugin@global":{}}}, Path.cwd(), False)
    assert config["mcp_servers"]["global-tools"] == {"enabled":False}
    assert config["plugins"]["plugin@global"] == {"enabled":False}
    assert "write_text" not in config["mcp_servers"]["feature_files"]["enabled_tools"]
    assert config["features"]["shell_tool"] is False

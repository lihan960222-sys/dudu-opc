import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from app.services import feature_files
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


def test_stdio_reads_a_chinese_filename(tmp_path):
    task = tmp_path / "任务" / "17"
    task.mkdir(parents=True)
    (tmp_path / "验收.txt").write_text("蓝桥-27", encoding="utf-8")
    request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "read_text", "arguments": {"scope": "role", "path": "验收.txt"}}}
    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-I", feature_files.__file__, str(task), "read"],
        input=(json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"),
        capture_output=True, check=True, timeout=10,
    )
    reply = json.loads(result.stdout)["result"]
    assert not reply["isError"], reply
    assert json.loads(reply["content"][0]["text"])["text"] == "蓝桥-27"

from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import hashlib

import pytest

from app.services import employee_resources as resources


def test_only_source_url_from_owner_message_can_be_fetched():
    source = {"text": "查看 https://docs.example/guide", "quoted_owner": {"text": "参考 https://docs.example/v2"}}
    assert resources.source_url(source, "https://docs.example/v2").endswith("/v2")
    with pytest.raises(ValueError):
        resources.source_url(source, "https://docs.example/guide?q=some-local-data")


def test_delivery_collision_retry_and_concurrency(tmp_path):
    (tmp_path / "report.md").write_bytes(b"previous")
    with ThreadPoolExecutor(2) as pool:
        paths = list(pool.map(lambda _: resources.save_new(tmp_path, "report.md", b"updated"), range(2)))
    assert paths[0] == paths[1] != tmp_path / "report.md"
    assert paths[0].read_bytes() == b"updated"
    assert (tmp_path / "report.md").read_bytes() == b"previous"
    assert len(list(tmp_path.iterdir())) == 2


def test_model_result_masks_file_and_chat_secrets():
    result = {"text": 'config = {"password": "synthetic-only-password"}',
              "message": {"content": "api_key=synthetic-only-key"}, "sha256": "a"*64}
    masked = resources.model_result(result)
    assert "synthetic-only" not in str(masked)
    assert masked["sha256"] == result["sha256"]
    assert "synthetic-only" in result["text"]


def test_delivery_requires_matching_source_and_target_versions(tmp_path):
    root = tmp_path / "project"
    dest = tmp_path / "deliveries"
    root.mkdir()
    dest.mkdir()
    project = SimpleNamespace(root=root, employee_folders=())
    written = root / "result.md"
    original = dest / written.name
    written.write_bytes(b"new")
    original.write_bytes(b"old")
    digest = lambda content: hashlib.sha256(content).hexdigest()
    args = {"source": written.name, "folder": str(dest), "sha256": digest(b"new")}
    # This test's Windows temporary folder lives inside AppData, which is not
    # an external delivery destination. Exercise the publication independently.
    from unittest.mock import patch
    with patch.object(resources, "external_folder", return_value=dest):
        with pytest.raises(ValueError):
            resources.deliver(project, {**args, "sha256": digest(b"changed after review")})
        receipt = resources.deliver(project, args)
        assert receipt["path"] != str(original) and original.read_bytes() == b"old"
        with pytest.raises(ValueError):
            resources.deliver(project, {**args, "previous_sha256": digest(b"stale")})
        for _ in range(2):
            assert resources.deliver(project, {**args, "previous_sha256": digest(b"old")})["path"] == str(original)
        assert original.read_bytes() == b"new"


def test_output_cannot_overwrite_a_colleagues_project(tmp_path):
    own, peer = tmp_path / "p1", tmp_path / "p2"
    own.mkdir()
    peer.mkdir()
    with pytest.raises(ValueError):
        resources.external_folder(SimpleNamespace(root=own, employee_folders=(peer,)), str(peer), "report.txt")


def test_search_separated_from_terminal_network():
    from app.services.feature_files import turn_config
    project = SimpleNamespace(model_vendor="openai")
    assert turn_config({}, None, True, project_runtime=project)["web_search"] == "cached"
    assert turn_config({}, None, True, project_runtime=project, scheduled_knowledge=True)["web_search"] == "disabled"
    project.model_vendor = "deepseek"
    assert turn_config({}, None, True, project_runtime=project)["web_search"] == "disabled"

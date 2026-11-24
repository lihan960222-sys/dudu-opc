import pytest
from app.services.role_workspace import initialize, inventory, load


def test_role_reload_and_preserve(tmp_path):
    tmp_path = tmp_path / 'role'
    tmp_path.mkdir()
    config = tmp_path / "AGENTS.md"
    initialize(tmp_path, "gpt-test", "按图核对尺寸")
    first = load(tmp_path)
    assert first.model == "gpt-test"
    config.write_text("---\ndefault_model: model-two\n---\n新职责", encoding="utf-8")
    initialize(tmp_path, "other", "")
    second = load(tmp_path)
    assert second.instructions == "新职责"
    assert second.digest != first.digest
    assert second.model == "model-two"


def test_scope_and_unknown_setting(tmp_path):
    tmp_path = tmp_path / 'role'
    tmp_path.mkdir()
    initialize(tmp_path, "test", "")
    for name in ("资料", "任务/a", "任务/b", ".git"):
        folder = tmp_path / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "log.txt").write_text(name, encoding="utf-8")
    assert [r["path"] for r in inventory(tmp_path, "a")] == ["任务/a/log.txt", "资料/log.txt"]
    (tmp_path / "AGENTS.md").write_text("---\napproval_mode: full_access\n---\n", encoding="utf-8")
    with pytest.raises(ValueError, match="default_model"):
        load(tmp_path)
from pathlib import Path

def test_role_cannot_cover_sensitive_paths(tmp_path):
    from app.services.role_workspace import validate_location
    private = tmp_path / "profile" / "key.dpapi"
    for folder in (tmp_path, private.parent, Path.home(), Path(Path.home().anchor)):
        with pytest.raises(ValueError):
            validate_location(folder, [private])
    validate_location(tmp_path / "roles" / "developer", [private])


def test_worker_workspace_cannot_overlap_steward_model_sessions(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services.role_workspace import validate_configured_location

    profile = tmp_path / 'home-profile'
    monkeypatch.setattr(settings, 'SQLITE_PATH', str(profile / 'suparagent.db'))
    with pytest.raises(ValueError, match='private'):
        validate_configured_location(profile / 'deepseek-codex' / 'sessions')
    validate_configured_location(profile / 'workspace' / 'worker')

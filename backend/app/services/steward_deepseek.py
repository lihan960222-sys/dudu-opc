"""The Windows home steward's DeepSeek key. Used by both the gateway and Codex adapter."""
import os
from pathlib import Path

import httpx

from app.feishu.setup_specialist import dpapi, write_atomic


MODELS_URL = "https://api.deepseek.com/models"
MODELS = {"deepseek-flash", "deepseek-v4-pro"}


def _credential_path() -> Path | None:
    configured = os.getenv("FEISHU_CONFIG")
    if os.name != "nt" or not configured:
        return None
    profile = Path(configured)
    if not profile.is_absolute() or not profile.is_file():
        return None
    folder = profile.resolve().parent
    if any((p / ".git").exists() for p in (folder, *folder.parents)):
        return None
    return folder / "steward-deepseek.dpapi"


def api_key() -> str:
    path = _credential_path()
    if path is not None and path.is_file():
        try:
            return dpapi(path.read_bytes(), unlock=True).decode("utf-8")
        except (OSError, ValueError):
            return ""
    return os.getenv("DEEPSEEK_API_KEY", "").strip()


def verify_and_save(key: str) -> None:
    path = _credential_path()
    if path is None:
        raise ValueError("请先在 Windows 家庭主机设置独立的飞书配置目录。")
    key = key.strip() if isinstance(key, str) else ""
    if not (1 <= len(key) <= 512 and key.isascii() and key.isprintable() and " " not in key):
        raise ValueError("请输入有效的 DeepSeek API Key。")
    try:
        with httpx.Client(timeout=12, follow_redirects=False, trust_env=False) as client:
            response = client.get(MODELS_URL, headers={"Authorization": "Bearer " + key})
            if response.status_code in (401, 403):
                raise PermissionError
            response.raise_for_status()
            available = response.json()["data"]
            if not isinstance(available, list) or not any(
                isinstance(model, dict) and model.get("id") in MODELS for model in available
            ):
                raise ValueError
    except PermissionError:
        raise ValueError("DeepSeek 拒绝了这把密钥，请核对后重试。") from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise ValueError("未能向 DeepSeek 核实模型列表，请检查主机网络和密钥权限。") from None
    try:
        write_atomic(path, dpapi(key.encode("utf-8"), unlock=False))
    except (ValueError, OSError):
        raise ValueError("Windows 未能保存加密的密钥，请检查家庭主机配置目录。") from None

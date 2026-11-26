"""Windows service credentials for the steward and separately provisioned employees."""
import hashlib
import json
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
    _verify_models(key)
    _save_protected(path, key.encode("utf-8"))


def _verify_models(key: str) -> list[str]:
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
            return sorted({item["id"] for item in available if isinstance(item, dict) and item.get("id") in MODELS})
    except PermissionError:
        raise ValueError("DeepSeek 拒绝了这把密钥，请核对后重试。") from None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise ValueError("未能向 DeepSeek 核实模型列表，请检查主机网络和密钥权限。") from None


def _save_protected(path: Path, payload: bytes) -> None:
    try:
        write_atomic(path, dpapi(payload, unlock=False))
    except (ValueError, OSError):
        raise ValueError("Windows 未能保存加密的密钥，请检查家庭主机配置目录。") from None


def _employee_file(owner_id: str, employee_id: int) -> Path:
    from app.core.config import settings
    owner = hashlib.sha256(owner_id.encode("utf-8")).hexdigest()
    return settings.SQLITE_FILE.parent / "employee-credentials" / owner / f"{int(employee_id)}.dpapi"


def _employee_record(owner_id: str, employee_id: int) -> dict:
    path = _employee_file(owner_id, employee_id)
    try:
        record = json.loads(dpapi(path.read_bytes(), unlock=True))
        if isinstance(record, dict) and record.get("owner") == owner_id and record.get("employee") == employee_id:
            return record
    except (ValueError, TypeError, OSError):
        pass
    return {}


def employee_api_key(owner_id: str, employee_id: int) -> str:
    return _employee_record(owner_id, employee_id).get("key", "")


def employee_models(owner_id: str, employee_id: int) -> list[str]:
    return _employee_record(owner_id, employee_id).get("models", [])


def save_employee_key(owner_id: str, employee_id: int, key: str) -> None:
    key = key.strip()
    models = _verify_models(key)
    record = {"owner": owner_id, "employee": employee_id, "key": key, "models": models}
    _save_protected(_employee_file(owner_id, employee_id), json.dumps(record).encode("utf-8"))

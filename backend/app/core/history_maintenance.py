"""Admission barrier during owner-confirmed maintenance of a managed local instance."""
import os

from starlette.responses import JSONResponse

from app.core.config import settings


def managed():
    return (os.name == "nt" and os.environ.get("DUDU_HISTORY_RESET_LAUNCHER") == "1"
            and settings.DATABASE_KIND == "sqlite" and bool(settings.STEWARD_STRICT_OWNER))


def pending():
    return managed() and (settings.SQLITE_FILE.parent / "employee-settings" / "history-reset" / "request.json").is_file()


class HistoryMaintenanceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope["type"] == "http" and scope["method"] not in ("HEAD", "GET", "OPTIONS")
                and scope["path"] != "/api/v1/staff/history-reset" and pending()):
            response = JSONResponse({"detail": "正在维护历史记录，服务恢复后可继续操作。"},
                                    status_code=503, headers={"Retry-After": "3"})
            return await response(scope, receive, send)
        await self.app(scope, receive, send)

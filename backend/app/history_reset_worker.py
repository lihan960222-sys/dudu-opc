"""Offline maintenance entry point used by the managed Windows launcher."""
import sys
import re

from app.services.history_reset_control import worker


if __name__ == "__main__":
    if (len(sys.argv) != 3 or sys.argv[1] not in ("check", "apply", "fail")
            or not re.fullmatch(r"[0-9a-f]{32}", sys.argv[2])):
        sys.exit(2)
    sys.exit(0 if worker(sys.argv[1], sys.argv[2]) else 1)

"""
scripts/server.py — 薄 shim（≤60 行）
行為零改變：--host/--port/--snapshot-once CLI 不變；
re-export 下列名稱供舊呼叫者使用（agent_arena.py、test_arena_final.py）。
"""
import sys
from pathlib import Path

# 確保 repo root 在 sys.path 最前面，讓 serenity 套件可被 import
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# --- Re-exports（import server 的舊呼叫者不能壞）---
from serenity.gemini import call_gemini                          # noqa: F401,E402
from serenity.keypool import _key_manager                        # noqa: F401,E402
from serenity.db import db                                       # noqa: F401,E402
from serenity.config import DB_PATH, ROOT                        # noqa: F401,E402
from serenity.services.signal import signal_payload, snapshot_signals  # noqa: F401,E402
from serenity.services.arena_views import (                      # noqa: F401,E402
    arena_leaderboard_payload,
    arena_nav_payload,
    arena_trades_payload,
    arena_reflections_payload,
)

if __name__ == "__main__":
    import serenity.app
    serenity.app.main()

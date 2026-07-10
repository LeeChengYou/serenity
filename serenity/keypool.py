"""
serenity/keypool.py
KeyManager 類 + _key_manager 單例（原 server.py 96-256 行）
"""
import os
import threading
import time
from datetime import datetime


class KeyManager:
    """
    Thread-safe Gemini API key pool.

    Task affinity:
      interactive → KEY_1   (chat, dossier)
      batch       → KEY_2   (scorecard generation)
      translate   → KEY_3   (translation)
      memory      → KEY_1   (memory extraction, lite model)
      agent_arena → round-robin across all 4 keys (9 agents spread evenly)
    Overflow: KEY_4 used when affinity key is 429/503-cooling.
    Cooling:  first 429/503 → 60 s; 3rd within 10 min → until next Pacific midnight.
    """
    _AFFINITY = {
        "interactive":  "KEY_1",
        "batch":        "KEY_2",
        "translate":    "KEY_3",
        "memory":       "KEY_1",
        # agent_arena uses round-robin (see _arena_rr_index), not a fixed key
    }
    _OVERFLOW = "KEY_4"
    _ALL_LABELS = ["KEY_1", "KEY_2", "KEY_3", "KEY_4"]
    _arena_rr_index = 0  # class-level round-robin counter for agent_arena
    _ENV_NAMES  = ["GEMINI_API_KEY", "GEMINI_API_KEY_2", "GEMINI_API_KEY_3", "GEMINI_API_KEY_4"]

    def __init__(self):
        self._lock = threading.RLock()
        self._entries: dict = {}
        for label, env_name in zip(self._ALL_LABELS, self._ENV_NAMES):
            val = os.environ.get(env_name)
            if val:
                self._entries[label] = {
                    "label":            label,
                    "key":              val,
                    "cooling_until":    None,   # Unix timestamp or None
                    "calls_today":      0,
                    "errors_429_today": 0,
                    "recent_429s":      [],     # timestamps within 10 min
                }

    def has_any_key(self) -> bool:
        return bool(self._entries)

    def _ordered_labels(self, task_class: str) -> list:
        """Preferred label order for a given task class.

        agent_arena: round-robin starting point rotates across all keys so
        9 concurrent agents spread their quota load evenly.
        Others: affinity key first → KEY_4 overflow → remaining.
        """
        if task_class == "agent_arena":
            # Round-robin: each call advances the starting index
            # Already inside self._lock from pick_key, so we can access safely
            idx = KeyManager._arena_rr_index % len(self._ALL_LABELS)
            KeyManager._arena_rr_index += 1
            # Build order starting from round-robin position
            rotated = self._ALL_LABELS[idx:] + self._ALL_LABELS[:idx]
            return [lbl for lbl in rotated if lbl in self._entries]

        affinity = self._AFFINITY.get(task_class, "KEY_1")
        order = [affinity]
        if self._OVERFLOW not in order:
            order.append(self._OVERFLOW)
        for label in self._ALL_LABELS:
            if label not in order:
                order.append(label)
        return [lbl for lbl in order if lbl in self._entries]

    def pick_key(self, task_class: str = "interactive", exclude: set = None) -> dict:
        """Return the best available (non-cooling, non-excluded) key entry."""
        exclude = exclude or set()
        with self._lock:
            now = time.time()
            for label in self._ordered_labels(task_class):
                if label in exclude:
                    continue
                entry = self._entries[label]
                cool = entry["cooling_until"]
                if cool is None or now >= cool:
                    return entry
            raise ValueError("所有 Gemini API Key 目前均在冷卻中，請稍後再試。")

    def _mark_error(self, entry: dict, code: int) -> None:
        """Shared logic for 429/503: record error and set cooling."""
        with self._lock:
            now = time.time()
            entry["errors_429_today"] += 1
            entry["recent_429s"].append(now)
            # Prune older than 10 min
            entry["recent_429s"] = [t for t in entry["recent_429s"] if now - t <= 600]
            if len(entry["recent_429s"]) >= 3:
                cool_ts = self._next_pacific_midnight()
                entry["cooling_until"] = cool_ts
                print(f"[KeyManager] {entry['label']}: 3 {code}s in 10 min → cooling until Pacific midnight")
            else:
                entry["cooling_until"] = now + 60
                print(f"[KeyManager] {entry['label']}: HTTP {code} → cooling 60 s")

    def mark_429(self, entry: dict) -> None:
        """Record a 429 and update cooling state for the given entry."""
        self._mark_error(entry, 429)

    def mark_503(self, entry: dict) -> None:
        """Record a 503 and update cooling state (same policy as 429)."""
        self._mark_error(entry, 503)

    def record_call(self, entry: dict) -> None:
        with self._lock:
            entry["calls_today"] += 1

    def _next_pacific_midnight(self) -> float:
        """Unix timestamp of next midnight in US/Pacific time."""
        from datetime import timedelta
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo("US/Pacific")
            now_pac = datetime.now(tz)
            midnight = (now_pac + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            return midnight.timestamp()
        except Exception:
            # Fallback: approximate PDT = UTC-7
            now_utc = datetime.utcnow()
            pac_now = now_utc - timedelta(hours=7)
            midnight = (pac_now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            return time.time() + (midnight - pac_now).total_seconds()

    def status(self) -> list:
        """Return masked status for /api/keypool (last-4 suffix only, never full key values)."""
        with self._lock:
            now = time.time()
            result = []
            for label in self._ALL_LABELS:
                if label not in self._entries:
                    continue
                entry = self._entries[label]
                cool = entry["cooling_until"]
                available = cool is None or now >= cool
                cooling_iso = None
                if cool and not available:
                    try:
                        cooling_iso = datetime.fromtimestamp(cool).isoformat()
                    except Exception:
                        pass
                result.append({
                    "label":            label,
                    "suffix":           f"...{entry['key'][-4:]}",
                    "available":        available,
                    "cooling_until":    cooling_iso,
                    "calls_today":      entry["calls_today"],
                    "errors_429_today": entry["errors_429_today"],
                })
            return result


_key_manager = KeyManager()

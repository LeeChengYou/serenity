"""
serenity/quant.py
indicators/signals/score_serenity_stock 動態載入
（_compute_indicators、_compute_ema、_evaluate_signal、_quant_score）
（原 server.py 18-69 行）
"""
from pathlib import Path

from .config import ROOT

# ---------------------------------------------------------------------------
# Benchmark symbols — same constant as ingest.py (R3-2).
# Excluded from all universe queries: signals, snapshots, hit-rate, /api/symbols.
# ---------------------------------------------------------------------------
BENCHMARK_SYMBOLS: set = {"SPY", "SOXX", "QQQ"}

# Technical indicators (stdlib only, no pandas/numpy)
_compute_ema = None  # populated below
try:
    from indicators import compute_all as _compute_indicators
    from indicators import compute_ema as _compute_ema
except ImportError:
    # If server is run from a different cwd, try the scripts/ folder path
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location(
        "indicators",
        ROOT / "scripts" / "indicators.py",
    )
    _mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _compute_indicators = _mod.compute_all
    _compute_ema = _mod.compute_ema

# Signal rules engine (SPEC F-06 / F-07)
try:
    from signals import evaluate_signal as _evaluate_signal
except ImportError:
    import importlib.util as _ilu2
    _spec2 = _ilu2.spec_from_file_location(
        "signals",
        ROOT / "scripts" / "signals.py",
    )
    _mod2 = _ilu2.module_from_spec(_spec2)
    _spec2.loader.exec_module(_mod2)
    _evaluate_signal = _mod2.evaluate_signal

# Quantitative X-corpus scorer (from the serenity-stock-scorer skill).  Used as
# a fallback score for symbols that have no AI-generated bottleneck scorecard,
# so every symbol with mentions still gets a signal score.
try:
    import importlib.util as _ilu3
    _spec3 = _ilu3.spec_from_file_location(
        "score_serenity_stock",
        ROOT / "skills" / "serenity-stock-scorer" / "scripts" / "score_serenity_stock.py",
    )
    _mod3 = _ilu3.module_from_spec(_spec3)
    _spec3.loader.exec_module(_mod3)
    _quant_score = _mod3.score_symbol
except Exception:
    _quant_score = None

"""Small production hardening layer for Short-bot V2.6.

Keeps the public V2.6 version while:
- avoiding a duplicate expensive dynamic-market deep scan during first preopen pass;
- keeping the newest notebook symbols in the fixed fallback universe if either
  official all-market endpoint is temporarily unavailable.
"""

import strategy_v26 as core

app = core.app
legacy = core.legacy
logger = core.logger
TW_TZ = core.TW_TZ

v25 = core.v25
v24 = core.v24
v23 = core.v23
v22 = core.v22
v21 = core.v21

_trial_history = core._trial_history
_book_history = core._book_history
_open_store = core._open_store
_first_bar_cache = core._first_bar_cache
_market_stats = core._market_stats

SAFE_OPEN_NORMAL_PCT = core.SAFE_OPEN_NORMAL_PCT
SAFE_OPEN_PROTECT_PCT = core.SAFE_OPEN_PROTECT_PCT
SAFE_OPEN_STRICT_PCT = core.SAFE_OPEN_STRICT_PCT
SAFE_OPEN_VOL_PCT = core.SAFE_OPEN_VOL_PCT
MAX_STRUCTURAL_RISK_PCT = core.MAX_STRUCTURAL_RISK_PCT
REBOUND_MIN_DROP_PCT = core.REBOUND_MIN_DROP_PCT
REBOUND_MIN_BOUNCE_PCT = core.REBOUND_MIN_BOUNCE_PCT
MAX_ALERTS_PER_SYMBOL = core.MAX_ALERTS_PER_SYMBOL
MONITOR_END_MINUTE = core.MONITOR_END_MINUTE
PRIMARY_END_MINUTE = core.PRIMARY_END_MINUTE
DYNAMIC_MIN_VOLUME = core.DYNAMIC_MIN_VOLUME
DYNAMIC_MAX_SYMBOLS = core.DYNAMIC_MAX_SYMBOLS
LOCKED_B_MAX_PCT = core.LOCKED_B_MAX_PCT

# Official market suffixes for the newest note symbols. Dynamic discovery remains
# the primary path; these are only used if an official snapshot is unavailable.
_FALLBACK_NOTEBOOK_SYMBOLS = {
    "2338.TW": "光罩",
    "3624.TWO": "光頡",
    "6226.TW": "光鼎",
    "1528.TW": "恩德",
    "6706.TW": "惠特",
}
for _symbol, _name in _FALLBACK_NOTEBOOK_SYMBOLS.items():
    _code = _symbol.split(".")[0]
    legacy.STOCK_NAMES[_code] = _name
    if _symbol not in core._FALLBACK_SYMBOLS:
        core._FALLBACK_SYMBOLS.append(_symbol)

screen_v26 = core.screen_v26
intraday_monitor_v26 = core.intraday_monitor_v26
format_preopen_summary_v26 = core.format_preopen_summary_v26
format_report_v26 = core.format_report_v26
format_line_morning_v26 = core.format_line_morning_v26
format_daily_summary_v26 = core.format_daily_summary_v26
format_test_v26 = core.format_test_v26


def preopen_scan_once_v26():
    """Build/populate V2.6 watchlist once, then only collect trial/book data."""
    if not legacy._watchlist_today:
        candidates = core.screen_v26()
        if candidates:
            core.v21._populate_watchlist(candidates)
    return core._v25_preopen_scan()


# Cover both manual /trial and the inherited background preopen loop call chain.
core.preopen_scan_once_v26 = preopen_scan_once_v26
core.v25.preopen_scan_once_v25 = preopen_scan_once_v26
core.v24.preopen_scan_once_v24 = preopen_scan_once_v26
core.v23.preopen_scan_once_v23 = preopen_scan_once_v26
core.v22.preopen_scan_once_v22 = preopen_scan_once_v26
core.v21.preopen_scan_once = preopen_scan_once_v26

legacy.screen = screen_v26
legacy.intraday_monitor = intraday_monitor_v26

logger.info("Short-bot V2.6 production hardening loaded")

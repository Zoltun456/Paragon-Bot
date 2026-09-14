from __future__ import annotations

"""Shared generic helpers for small coercion, formatting, and UTC/ISO utilities."""

from datetime import datetime, timezone
import math
from typing import Any, Optional


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _fmt_num(value: int | float) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}"
    f = _as_float(value, 0.0)
    if not math.isfinite(f):
        return str(value)
    if abs(f - round(f)) < 1e-9:
        return f"{int(round(f)):,}"
    return f"{f:,.2f}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _inc_num(d: dict, key: str, amount: int | float) -> None:
    old = d.get(key, 0)
    if isinstance(old, bool):
        old = 0
    if isinstance(amount, int) and not isinstance(amount, bool):
        if isinstance(old, int) and not isinstance(old, bool):
            d[key] = old + amount
            return
        if isinstance(old, float) and math.isfinite(old) and old.is_integer():
            d[key] = int(old) + amount
            return
    d[key] = _as_float(old) + float(amount)

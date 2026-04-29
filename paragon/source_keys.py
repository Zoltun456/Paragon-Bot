from __future__ import annotations

from typing import Any


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _inc_num(d: dict, key: str, amount: int | float) -> None:
    old = d.get(key, 0)
    if isinstance(old, bool):
        old = 0
    if isinstance(amount, int) and isinstance(old, int):
        d[key] = old + amount
        return
    d[key] = _as_float(old) + float(amount)


def canonical_boost_source(source: Any, *, default: str = "activity") -> str:
    src = str(source or "").strip().lower()
    if not src:
        return str(default).strip().lower() or "activity"

    if (
        src == "boss victory"
        or src.startswith("boss victory:")
        or src == "boss commendation"
        or src.startswith("boss commendation:")
        or src == "boss survivor"
        or src.startswith("boss survivor:")
    ):
        return "boss victory"

    if (
        src == "boss retaliation"
        or src.startswith("boss retaliation:")
        or src == "boss failure"
        or src.startswith("boss failure:")
    ):
        return "boss retaliation"

    return src


def normalize_boosts_by_source_map(value: Any) -> tuple[dict, bool]:
    raw = value if isinstance(value, dict) else {}
    out: dict[str, dict] = {}
    changed = not isinstance(value, dict)

    for src_raw, row_raw in raw.items():
        src = canonical_boost_source(src_raw)
        row = _as_dict(row_raw)
        if src != str(src_raw or "").strip().lower():
            changed = True
        if row is not row_raw:
            changed = True

        dest = out.setdefault(src, {})
        for key, amount in row.items():
            if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                continue
            _inc_num(dest, str(key), amount)

    if len(out) != len(raw):
        changed = True
    return out, changed


def normalize_effect_source_rows(value: Any) -> tuple[list[dict], bool]:
    raw = value if isinstance(value, list) else []
    out: list[dict] = []
    changed = not isinstance(value, list)

    for row_raw in raw:
        if not isinstance(row_raw, dict):
            changed = True
            continue
        row = dict(row_raw)
        src = canonical_boost_source(row.get("source", "activity"))
        if src != str(row.get("source", "activity") or "").strip().lower():
            changed = True
        row["source"] = src
        out.append(row)

    return out, changed


def migrate_user_boost_sources(user: Any) -> bool:
    if not isinstance(user, dict):
        return False

    changed = False

    boosts, boosts_changed = normalize_effect_source_rows(user.get("xp_boosts"))
    if boosts_changed:
        user["xp_boosts"] = boosts
        changed = True

    debuffs, debuffs_changed = normalize_effect_source_rows(user.get("xp_debuffs"))
    if debuffs_changed:
        user["xp_debuffs"] = debuffs
        changed = True

    stats = _as_dict(user.get("stats"))
    if stats is not user.get("stats"):
        user["stats"] = stats
        changed = True

    xp = _as_dict(stats.get("xp"))
    if xp is not stats.get("xp"):
        stats["xp"] = xp
        changed = True

    boosts_by_source, source_changed = normalize_boosts_by_source_map(xp.get("boosts_by_source"))
    if source_changed:
        xp["boosts_by_source"] = boosts_by_source
        changed = True

    return changed

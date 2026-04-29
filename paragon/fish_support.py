from __future__ import annotations

from .storage import _udict


def _inventory(gid: int, uid: int) -> dict:
    u = _udict(gid, uid)
    st = u.get("fishing_inventory")
    if not isinstance(st, dict):
        st = {}
        u["fishing_inventory"] = st
    st.setdefault("bait", 0)
    return st


def add_bait(gid: int, uid: int, *, amount: int = 1) -> int:
    st = _inventory(gid, uid)
    add_n = max(0, int(amount))
    st["bait"] = max(0, int(st.get("bait", 0))) + add_n
    return int(st["bait"])


def get_bait(gid: int, uid: int) -> int:
    st = _inventory(gid, uid)
    return max(0, int(st.get("bait", 0)))


def consume_bait(gid: int, uid: int, *, amount: int = 1) -> bool:
    st = _inventory(gid, uid)
    need = max(1, int(amount))
    cur = max(0, int(st.get("bait", 0)))
    if cur < need:
        return False
    st["bait"] = cur - need
    return True


def refund_bait(gid: int, uid: int, *, amount: int = 1) -> int:
    return add_bait(gid, uid, amount=amount)

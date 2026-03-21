from __future__ import annotations

from .storage import _udict


def _wheel_buffs(gid: int, uid: int) -> dict:
    u = _udict(gid, uid)
    b = u.get("wheel_buffs")
    if not isinstance(b, dict):
        b = {}
        u["wheel_buffs"] = b
    b.setdefault("blackjack_natural_charges", 0)
    b.setdefault("wordle_reward_multiplier", 1.0)
    b.setdefault("wordle_reward_charges", 0)
    b.setdefault("anagram_reward_multiplier", 1.0)
    b.setdefault("anagram_reward_charges", 0)
    b.setdefault("roulette_accuracy_bonus", 0.0)
    b.setdefault("roulette_accuracy_charges", 0)
    b.setdefault("roulette_backfire_shield_charges", 0)
    b.setdefault("coinflip_win_edge_bonus", 0.0)
    b.setdefault("coinflip_win_edge_charges", 0)
    b.setdefault("lotto_bonus_tickets_pct", 0.0)
    b.setdefault("lotto_bonus_tickets_charges", 0)
    b.setdefault("lotto_jackpot_boost_multiplier", 1.0)
    b.setdefault("lotto_jackpot_boost_charges", 0)
    return b


def add_blackjack_natural_charges(gid: int, uid: int, *, charges: int = 1) -> int:
    b = _wheel_buffs(gid, uid)
    add_n = max(0, int(charges))
    b["blackjack_natural_charges"] = max(0, int(b.get("blackjack_natural_charges", 0))) + add_n
    return int(b["blackjack_natural_charges"])


def consume_blackjack_natural_charge(gid: int, uid: int) -> bool:
    b = _wheel_buffs(gid, uid)
    cur = max(0, int(b.get("blackjack_natural_charges", 0)))
    if cur <= 0:
        return False
    b["blackjack_natural_charges"] = cur - 1
    return True


def set_wordle_reward_multiplier(gid: int, uid: int, *, multiplier: float, charges: int = 1) -> dict:
    b = _wheel_buffs(gid, uid)
    mult = max(1.0, float(multiplier))
    n = max(1, int(charges))
    existing_mult = max(1.0, float(b.get("wordle_reward_multiplier", 1.0)))
    existing_charges = max(0, int(b.get("wordle_reward_charges", 0)))
    if existing_charges > 0:
        b["wordle_reward_multiplier"] = max(existing_mult, mult)
        b["wordle_reward_charges"] = existing_charges + n
    else:
        b["wordle_reward_multiplier"] = mult
        b["wordle_reward_charges"] = n
    return {
        "multiplier": float(b["wordle_reward_multiplier"]),
        "charges": int(b["wordle_reward_charges"]),
    }


def consume_wordle_reward_multiplier(gid: int, uid: int) -> float:
    b = _wheel_buffs(gid, uid)
    charges = max(0, int(b.get("wordle_reward_charges", 0)))
    if charges <= 0:
        b["wordle_reward_multiplier"] = 1.0
        b["wordle_reward_charges"] = 0
        return 1.0

    mult = max(1.0, float(b.get("wordle_reward_multiplier", 1.0)))
    charges -= 1
    b["wordle_reward_charges"] = charges
    if charges <= 0:
        b["wordle_reward_multiplier"] = 1.0
    return mult


def set_anagram_reward_multiplier(gid: int, uid: int, *, multiplier: float, charges: int = 1) -> dict:
    b = _wheel_buffs(gid, uid)
    mult = max(1.0, float(multiplier))
    n = max(1, int(charges))
    existing_mult = max(1.0, float(b.get("anagram_reward_multiplier", 1.0)))
    existing_charges = max(0, int(b.get("anagram_reward_charges", 0)))
    if existing_charges > 0:
        b["anagram_reward_multiplier"] = max(existing_mult, mult)
        b["anagram_reward_charges"] = existing_charges + n
    else:
        b["anagram_reward_multiplier"] = mult
        b["anagram_reward_charges"] = n
    return {
        "multiplier": float(b["anagram_reward_multiplier"]),
        "charges": int(b["anagram_reward_charges"]),
    }


def consume_anagram_reward_multiplier(gid: int, uid: int) -> float:
    b = _wheel_buffs(gid, uid)
    charges = max(0, int(b.get("anagram_reward_charges", 0)))
    if charges <= 0:
        b["anagram_reward_multiplier"] = 1.0
        b["anagram_reward_charges"] = 0
        return 1.0

    mult = max(1.0, float(b.get("anagram_reward_multiplier", 1.0)))
    charges -= 1
    b["anagram_reward_charges"] = charges
    if charges <= 0:
        b["anagram_reward_multiplier"] = 1.0
    return mult


def add_roulette_backfire_shield(gid: int, uid: int, *, charges: int = 1) -> int:
    b = _wheel_buffs(gid, uid)
    add_n = max(0, int(charges))
    b["roulette_backfire_shield_charges"] = max(0, int(b.get("roulette_backfire_shield_charges", 0))) + add_n
    return int(b["roulette_backfire_shield_charges"])


def consume_roulette_backfire_shield(gid: int, uid: int) -> bool:
    b = _wheel_buffs(gid, uid)
    cur = max(0, int(b.get("roulette_backfire_shield_charges", 0)))
    if cur <= 0:
        return False
    b["roulette_backfire_shield_charges"] = cur - 1
    return True


def set_roulette_accuracy_bonus(gid: int, uid: int, *, bonus: float, charges: int = 1) -> dict:
    b = _wheel_buffs(gid, uid)
    add = max(0.0, float(bonus))
    n = max(1, int(charges))
    existing_bonus = max(0.0, float(b.get("roulette_accuracy_bonus", 0.0)))
    existing_charges = max(0, int(b.get("roulette_accuracy_charges", 0)))
    if existing_charges > 0:
        b["roulette_accuracy_bonus"] = max(existing_bonus, add)
        b["roulette_accuracy_charges"] = existing_charges + n
    else:
        b["roulette_accuracy_bonus"] = add
        b["roulette_accuracy_charges"] = n
    return {
        "bonus": float(b["roulette_accuracy_bonus"]),
        "charges": int(b["roulette_accuracy_charges"]),
    }


def consume_roulette_accuracy_bonus(gid: int, uid: int) -> float:
    b = _wheel_buffs(gid, uid)
    charges = max(0, int(b.get("roulette_accuracy_charges", 0)))
    if charges <= 0:
        b["roulette_accuracy_bonus"] = 0.0
        b["roulette_accuracy_charges"] = 0
        return 0.0

    bonus = max(0.0, float(b.get("roulette_accuracy_bonus", 0.0)))
    charges -= 1
    b["roulette_accuracy_charges"] = charges
    if charges <= 0:
        b["roulette_accuracy_bonus"] = 0.0
    return bonus


def set_coinflip_win_edge(gid: int, uid: int, *, bonus: float, charges: int = 1) -> dict:
    b = _wheel_buffs(gid, uid)
    add = max(0.0, float(bonus))
    n = max(1, int(charges))
    existing_bonus = max(0.0, float(b.get("coinflip_win_edge_bonus", 0.0)))
    existing_charges = max(0, int(b.get("coinflip_win_edge_charges", 0)))
    if existing_charges > 0:
        b["coinflip_win_edge_bonus"] = max(existing_bonus, add)
        b["coinflip_win_edge_charges"] = existing_charges + n
    else:
        b["coinflip_win_edge_bonus"] = add
        b["coinflip_win_edge_charges"] = n
    return {
        "bonus": float(b["coinflip_win_edge_bonus"]),
        "charges": int(b["coinflip_win_edge_charges"]),
    }


def consume_coinflip_win_edge(gid: int, uid: int) -> float:
    b = _wheel_buffs(gid, uid)
    charges = max(0, int(b.get("coinflip_win_edge_charges", 0)))
    if charges <= 0:
        b["coinflip_win_edge_bonus"] = 0.0
        b["coinflip_win_edge_charges"] = 0
        return 0.0

    bonus = max(0.0, float(b.get("coinflip_win_edge_bonus", 0.0)))
    charges -= 1
    b["coinflip_win_edge_charges"] = charges
    if charges <= 0:
        b["coinflip_win_edge_bonus"] = 0.0
    return bonus


def set_lotto_bonus_tickets_pct(gid: int, uid: int, *, pct: float, charges: int = 1) -> dict:
    b = _wheel_buffs(gid, uid)
    add = max(0.0, float(pct))
    n = max(1, int(charges))
    existing = max(0.0, float(b.get("lotto_bonus_tickets_pct", 0.0)))
    existing_charges = max(0, int(b.get("lotto_bonus_tickets_charges", 0)))
    if existing_charges > 0:
        b["lotto_bonus_tickets_pct"] = max(existing, add)
        b["lotto_bonus_tickets_charges"] = existing_charges + n
    else:
        b["lotto_bonus_tickets_pct"] = add
        b["lotto_bonus_tickets_charges"] = n
    return {
        "pct": float(b["lotto_bonus_tickets_pct"]),
        "charges": int(b["lotto_bonus_tickets_charges"]),
    }


def consume_lotto_bonus_tickets_pct(gid: int, uid: int) -> float:
    b = _wheel_buffs(gid, uid)
    charges = max(0, int(b.get("lotto_bonus_tickets_charges", 0)))
    if charges <= 0:
        b["lotto_bonus_tickets_pct"] = 0.0
        b["lotto_bonus_tickets_charges"] = 0
        return 0.0

    pct = max(0.0, float(b.get("lotto_bonus_tickets_pct", 0.0)))
    charges -= 1
    b["lotto_bonus_tickets_charges"] = charges
    if charges <= 0:
        b["lotto_bonus_tickets_pct"] = 0.0
    return pct


def set_lotto_jackpot_boost_multiplier(gid: int, uid: int, *, multiplier: float, charges: int = 1) -> dict:
    b = _wheel_buffs(gid, uid)
    mult = max(1.0, float(multiplier))
    n = max(1, int(charges))
    existing_mult = max(1.0, float(b.get("lotto_jackpot_boost_multiplier", 1.0)))
    existing_charges = max(0, int(b.get("lotto_jackpot_boost_charges", 0)))
    if existing_charges > 0:
        b["lotto_jackpot_boost_multiplier"] = max(existing_mult, mult)
        b["lotto_jackpot_boost_charges"] = existing_charges + n
    else:
        b["lotto_jackpot_boost_multiplier"] = mult
        b["lotto_jackpot_boost_charges"] = n
    return {
        "multiplier": float(b["lotto_jackpot_boost_multiplier"]),
        "charges": int(b["lotto_jackpot_boost_charges"]),
    }


def consume_lotto_jackpot_boost_multiplier(gid: int, uid: int) -> float:
    b = _wheel_buffs(gid, uid)
    charges = max(0, int(b.get("lotto_jackpot_boost_charges", 0)))
    if charges <= 0:
        b["lotto_jackpot_boost_multiplier"] = 1.0
        b["lotto_jackpot_boost_charges"] = 0
        return 1.0

    mult = max(1.0, float(b.get("lotto_jackpot_boost_multiplier", 1.0)))
    charges -= 1
    b["lotto_jackpot_boost_charges"] = charges
    if charges <= 0:
        b["lotto_jackpot_boost_multiplier"] = 1.0
    return mult


def wheel_buff_lines(gid: int, uid: int) -> list[str]:
    b = _wheel_buffs(gid, uid)
    lines: list[str] = []
    bj = max(0, int(b.get("blackjack_natural_charges", 0)))
    if bj > 0:
        lines.append(f"Blackjack natural charges: **{bj}**")
    w_charges = max(0, int(b.get("wordle_reward_charges", 0)))
    if w_charges > 0:
        w_mult = max(1.0, float(b.get("wordle_reward_multiplier", 1.0)))
        lines.append(f"Wordle buff multiplier: **x{w_mult:.2f}** ({w_charges} use(s) left)")
    a_charges = max(0, int(b.get("anagram_reward_charges", 0)))
    if a_charges > 0:
        a_mult = max(1.0, float(b.get("anagram_reward_multiplier", 1.0)))
        lines.append(f"Anagram buff multiplier: **x{a_mult:.2f}** ({a_charges} use(s) left)")
    rr_charges = max(0, int(b.get("roulette_accuracy_charges", 0)))
    if rr_charges > 0:
        bonus = max(0.0, float(b.get("roulette_accuracy_bonus", 0.0))) * 100.0
        lines.append(f"Roulette aim bonus: **+{bonus:.1f}%** ({rr_charges} use(s) left)")
    shield = max(0, int(b.get("roulette_backfire_shield_charges", 0)))
    if shield > 0:
        lines.append(f"Roulette backfire shield: **{shield}**")
    cf_charges = max(0, int(b.get("coinflip_win_edge_charges", 0)))
    if cf_charges > 0:
        edge = max(0.0, float(b.get("coinflip_win_edge_bonus", 0.0))) * 100.0
        lines.append(f"Coinflip edge bonus: **+{edge:.1f}%** ({cf_charges} use(s) left)")
    lt_charges = max(0, int(b.get("lotto_bonus_tickets_charges", 0)))
    if lt_charges > 0:
        pct = max(0.0, float(b.get("lotto_bonus_tickets_pct", 0.0))) * 100.0
        lines.append(f"Lotto bonus tickets: **+{pct:.0f}%** ({lt_charges} buy(s) left)")
    lj_charges = max(0, int(b.get("lotto_jackpot_boost_charges", 0)))
    if lj_charges > 0:
        mult = max(1.0, float(b.get("lotto_jackpot_boost_multiplier", 1.0)))
        lines.append(f"Lotto jackpot amp: **x{mult:.2f}** ({lj_charges} jackpot(s) left)")
    return lines

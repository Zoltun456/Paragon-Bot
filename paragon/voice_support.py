from __future__ import annotations

from typing import Tuple


def dave_support_status() -> Tuple[bool, str]:
    """
    Return whether the active Discord stack can do DAVE voice negotiation.
    """
    try:
        from discord.voice_state import has_dave  # type: ignore
    except Exception:
        return (
            False,
            "This Discord library build does not expose DAVE support. Use discord.py>=2.7 with davey installed.",
        )

    if bool(has_dave):
        return True, ""

    return (
        False,
        "DAVE support is unavailable (missing davey). Install `davey` and restart the bot.",
    )


def is_dave_close_4017(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    try:
        return int(code) == 4017
    except Exception:
        return False


def dave_4017_message() -> str:
    return (
        "Voice connection failed with close code 4017 (Discord requires DAVE). "
        "Use discord.py>=2.7 with davey installed."
    )

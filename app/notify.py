"""Telegram progress notifications without coupling the pipeline to the bot.

main.py registers the sender at startup; pipeline/scheduler call notify().
Verbose stage-by-stage messages can be silenced later with NOTIFY_VERBOSE=false
— important events (always=True) still go through.
"""
import logging
from typing import Awaitable, Callable

from app.config import settings

log = logging.getLogger(__name__)

_sender: Callable[[str], Awaitable[None]] | None = None


def set_notifier(fn: Callable[[str], Awaitable[None]]) -> None:
    global _sender
    _sender = fn


async def notify(text: str, always: bool = False) -> None:
    if _sender is None or not (always or settings.notify_verbose):
        return
    try:
        await _sender(text)
    except Exception:
        log.exception("notification failed: %s", text[:80])

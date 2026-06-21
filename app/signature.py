"""Fixed sign-off appended to every published caption.

A constant footer ("Edited & narrated by my own AI agent and cloned voice.")
goes at the very end of each post — all platforms, both languages — separated
from the body by blank lines. Applied at preview/publish time (like the X
shoutout), so it stays out of the Draft and is configured in one place
(`POST_SIGNATURE_EN` / `POST_SIGNATURE_ES`). Empty config = no footer.
"""
from app.config import settings


def signature_text(lang: str = "en") -> str:
    sig = settings.post_signature_es if lang == "es" else settings.post_signature_en
    return (sig or "").strip()


def apply_signature(caption: str, lang: str = "en") -> str:
    """Append the sign-off after blank lines. No-op if empty or already present."""
    sig = signature_text(lang)
    if not sig or sig in caption:
        return caption
    return f"{caption}\n\n\n{sig}" if caption else sig

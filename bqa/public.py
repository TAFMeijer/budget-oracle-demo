"""What a public, sign-in-less deployment shows and does not show.

Two small helpers, kept out of app.py so they can be tested: a JavaScript string literal that is
safe inside a <script> element, and the message a visitor sees when the detail of a failure is
nobody's business but the operator's (the audit log keeps the detail).
"""
from __future__ import annotations

import json


def js_string(text: str) -> str:
    """`text` as a JavaScript string literal that cannot end the <script> it is written into.

    json.dumps escapes quotes and backslashes but leaves '<' alone, so text containing
    '</script>' would close the element and whatever followed would run as markup in a frame that
    can reach the app's own document. Escaping '<', '>' and '&' as \\uXXXX keeps the literal intact.
    """
    return (json.dumps(text or "").replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def public_message(status: str, message: str) -> str:
    """The failure text for a visitor when internals are hidden (SHOW_SQL=false)."""
    low = (message or "").lower()
    if status == "error" and "could not be reached" in low:
        return "The assistant is not available right now. Please try again in a few minutes."
    if status == "error" and ("took longer" in low or "was stopped" in low):
        return message                                   # already plain language, no internals in it
    if status == "rejected":
        return "That question could not be turned into a safe query. Try rephrasing it."
    return "That question could not be answered. Try rephrasing it, or make it more specific."

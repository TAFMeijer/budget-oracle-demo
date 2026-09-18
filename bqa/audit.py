"""Append-only audit trail of every question: what was asked, what ran, what came back.

Records no IP addresses. A user identifier (the signed-in email) is written only when
access control is enabled and the caller passes one — then attribution is the point;
otherwise the log reviews the assistant's behaviour, not people.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .query import Outcome


class AuditLog:
    def __init__(self, path: str):
        self.path = Path(path)

    def write(self, out: "Outcome", history: int = 0) -> None:
        """One line per turn. Besides the outcome, the record keeps everything a later review of
        the conversation needs — which conversation and turn, what the user answered when asked
        back, each attempt the guard or database sent back and why, the model's final reply —
        so nothing has to be reconstructed from server logs. Result rows are never written."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "status": out.status,
                "question": out.question,
                "sql": out.sql,
                "rows": None if out.df is None else int(len(out.df)),
                "message": out.message[:500],
            }
            if getattr(out, "conversation_id", ""):
                rec["conversation"] = out.conversation_id
                rec["turn"] = out.turn_no
            if history:
                rec["history"] = history
            if getattr(out, "clarification", ""):
                rec["clarification"] = out.clarification
            if getattr(out, "attempts", None):
                rec["attempts"] = out.attempts
            if getattr(out, "raw", ""):
                rec["raw"] = out.raw[:6000]
            if getattr(out, "seconds", 0):
                rec["seconds"] = round(out.seconds, 1)
            if out.repairs:
                rec["repairs"] = out.repairs
            if out.note:
                rec["note"] = out.note
            if out.options:
                rec["options"] = out.options
                if out.multi:
                    rec["multi"] = True
            if out.user:
                rec["user"] = out.user
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass  # never let logging break the app

    def feedback(self, question: str, sql: str, thumbs_up: bool | None, text: str, user: str = "",
                 conversation_id: str = "") -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "status": "feedback",
                   "question": question, "sql": sql, "thumbs_up": thumbs_up, "message": text[:500]}
            if conversation_id:
                rec["conversation"] = conversation_id
            if user:
                rec["user"] = user
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def login(self, email: str, ip: str, ok: bool, reason: str = "") -> None:
        """One line per sign-in attempt, so a brute-force run is visible in the log."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "status": "login_ok" if ok else "login_failed", "user": email, "ip": ip}
            if reason:
                rec["message"] = reason
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

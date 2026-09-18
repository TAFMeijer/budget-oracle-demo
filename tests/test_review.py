"""scripts/review_evidence.py — the audit log turned into review evidence."""
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review_evidence.py"
spec = importlib.util.spec_from_file_location("review_evidence", SCRIPT)
rev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rev)


def _records():
    return [
        {"ts": "2026-09-16T10:00:00+00:00", "status": "login_ok", "user": "a@x.org"},
        # an old-style conversation without ids: grouped by user and time gap
        {"ts": "2026-09-16T10:01:00+00:00", "status": "sql", "question": "HRH budget", "sql": "SELECT 1", "rows": 3,
         "user": "a@x.org", "seconds": 12.0},
        {"ts": "2026-09-16T10:02:00+00:00", "status": "sql", "question": "GC7 definitely had SPI, please look for it",
         "sql": "SELECT 2", "rows": 2, "user": "a@x.org", "repairs": 1,
         "attempts": [{"sql": "SELECT bad", "problem": "Your query filters on a real name but in the wrong column\n- Intervention: ..."}]},
        {"ts": "2026-09-16T10:03:00+00:00", "status": "error", "question": "per country", "sql": "SELECT 3",
         "message": "The database returned an error: Invalid column name 'region_code'.", "user": "a@x.org"},
        # a quiet conversation by another user, an hour later
        {"ts": "2026-09-16T11:30:00+00:00", "status": "sql", "question": "Total GC8 budget by country", "sql": "SELECT 4",
         "rows": 53, "user": "b@x.org", "seconds": 4.0},
        # a new-style conversation with ids, a clarification and feedback
        {"ts": "2026-09-17T08:00:00+00:00", "status": "clarify", "question": "budget for treatment", "conversation": "abc",
         "turn": 1, "message": "Which treatment theme?", "options": ["A", "B"], "user": "a@x.org"},
        {"ts": "2026-09-17T08:00:30+00:00", "status": "sql", "question": "budget for treatment", "conversation": "abc",
         "turn": 2, "clarification": "A", "sql": "SELECT 5", "rows": 7, "user": "a@x.org"},
        {"ts": "2026-09-17T08:01:00+00:00", "status": "feedback", "question": "budget for treatment", "conversation": "abc",
         "thumbs_up": False, "message": "wrong theme", "user": "a@x.org"},
    ]


def test_grouping_friction_and_anonymity():
    ev = rev.build_evidence(_records())
    assert ev["turns"] == 6 and ev["statuses"] == {"sql": 4, "error": 1, "clarify": 1}
    assert [c["user"] for c in ev["conversations"]] == ["user-1", "user-2", "user-1"]
    assert ev["conversations"][0]["friction"] == ["repairs", "correction", "error"]
    assert ev["conversations"][1]["friction"] == []
    assert ev["conversations"][2]["friction"] == ["clarify", "thumbs down"]
    assert ev["clarifications"][0]["answered"] == "A"
    assert ev["repair_reasons"][0][1] == 1 and "wrong column" in ev["repair_reasons"][0][0]
    text = rev.render(ev)
    assert "a@x.org" not in text and "user-1" in text
    assert "GC7 definitely had SPI" in text and "Invalid column name" in text
    assert "→ answered: A" in text and "👎 feedback: wrong theme" in text
    assert "Quiet conversations (1)" in text


def test_since_and_mark(tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(r) for r in _records()) + "\n", encoding="utf-8")
    records = rev.load(audit)
    assert len(rev.select(records, "2026-09-16T12:00:00+00:00")) == 3
    state = rev.mark(audit, records)
    assert state["reviewed_through"] == "2026-09-17T08:01:00+00:00"
    assert rev.read_state(audit)["reviewed_through"] == state["reviewed_through"]
    assert rev.build_evidence(records, rev.read_state(audit)["reviewed_through"])["turns"] == 0


def test_cli_prints_markdown(tmp_path, capsys):
    audit = tmp_path / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(r) for r in _records()) + "\n", encoding="utf-8")
    assert rev.main(["--audit", str(audit), "--all"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# Conversation review — evidence") and "## Conversations with friction (2 of 3)" in out

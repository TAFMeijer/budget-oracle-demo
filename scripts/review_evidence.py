"""Evidence for a conversation review: what people asked, what went wrong, what it cost.

    python scripts/review_evidence.py               # everything since the last --mark
    python scripts/review_evidence.py --all
    python scripts/review_evidence.py --since 2026-09-16
    python scripts/review_evidence.py --json         # the same, as data
    python scripts/review_evidence.py --mark         # record that the log is reviewed up to now

Reads the audit log (AUDIT_LOG in .env, default logs/audit.jsonl), groups the records into
conversations and prints the ones worth a second look — errors, guard repairs, unusable answers,
clarifying questions and what was answered, corrections, thumbs-down — as markdown for the
review-conversations skill. E-mail addresses are replaced by user-1, user-2, …: the review learns
from the questions, not from who asked them. Nothing here talks to the model or the database.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Phrases that mean "that was wrong, try again" — a turn like this is the user doing the guard's
# job by hand, which is exactly what a review should look at.
CORRECTION = re.compile(r"^(no|nope|wrong)\b|\b(wrong|definitely|instead of|should be|try again|"
                        r"please look|look again|different spelling|that's not|thats not|i meant|"
                        r"you missed|is missing|not what i)\b", re.I)
UNUSABLE = "unusable answer"
SLOW_SECONDS = 60
GAP_SECONDS = 30 * 60           # records without a conversation id: a new one after this long a silence


def load(path: Path) -> list[dict]:
    records = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _ts(rec: dict) -> datetime:
    try:
        return datetime.fromisoformat(rec.get("ts", "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.fromtimestamp(0, tz=timezone.utc)


def select(records: list[dict], since: str | None) -> list[dict]:
    """Question, outcome and feedback records after `since` (ISO timestamp, exclusive)."""
    out = [r for r in records if not str(r.get("status", "")).startswith("login")]
    if since:
        cut = datetime.fromisoformat(since.replace("Z", "+00:00"))
        if cut.tzinfo is None:
            cut = cut.replace(tzinfo=timezone.utc)
        out = [r for r in out if _ts(r) > cut]
    return out


def anonymise(records: list[dict]) -> dict[str, str]:
    """E-mail -> user-N, in order of first appearance."""
    names: dict[str, str] = {}
    for r in records:
        u = r.get("user")
        if u and u not in names:
            names[u] = f"user-{len(names) + 1}"
    return names


def group(records: list[dict]) -> list[list[dict]]:
    """Conversations, oldest first. Records carry a conversation id since 17 Sep 2026; older
    ones are grouped by user and a half-hour gap, which is what a conversation looked like."""
    by_id: dict[str, list[dict]] = {}
    loose: list[dict] = []
    for r in sorted(records, key=_ts):
        cid = r.get("conversation")
        (by_id.setdefault(cid, []) if cid else loose).append(r)
    convs = list(by_id.values())
    current: list[dict] = []
    for r in loose:
        if current and (r.get("user") != current[-1].get("user") or
                        (_ts(r) - _ts(current[-1])).total_seconds() > GAP_SECONDS):
            convs.append(current)
            current = []
        current.append(r)
    if current:
        convs.append(current)
    return sorted(convs, key=lambda c: _ts(c[0]))


def friction(conv: list[dict]) -> list[str]:
    """Why this conversation deserves a look — empty when nothing went wrong."""
    reasons: list[str] = []
    for i, r in enumerate(conv):
        st = r.get("status")
        if st == "error":
            reasons.append("error")
        elif st == "rejected":
            reasons.append("rejected")
        elif st == "cannot" and UNUSABLE in (r.get("message") or ""):
            reasons.append("unusable answer")
        elif st == "clarify":
            reasons.append("clarify")
        if r.get("repairs"):
            reasons.append("repairs")
        if st == "feedback" and r.get("thumbs_up") is False:
            reasons.append("thumbs down")
        if st == "feedback" and r.get("thumbs_up") is None and r.get("message"):
            reasons.append("comment")
        if (r.get("seconds") or 0) > SLOW_SECONDS:
            reasons.append("slow")
        if i > 0 and st != "feedback" and not r.get("clarification") and CORRECTION.search(r.get("question", "")):
            reasons.append("correction")
    seen: list[str] = []
    for x in reasons:
        if x not in seen:
            seen.append(x)
    return seen


def build_evidence(records: list[dict], since: str | None = None) -> dict:
    chosen = select(records, since)
    names = anonymise(chosen)
    convs = group(chosen)
    turns = [r for r in chosen if r.get("status") != "feedback"]
    statuses = Counter(r.get("status") for r in turns)
    repairs = Counter()
    clarifies: list[dict] = []
    feedback: list[dict] = []
    conversations = []
    for n, conv in enumerate(convs, 1):
        why = friction(conv)
        user = names.get(conv[0].get("user", ""), "anonymous")
        entry = {"n": n, "user": user, "start": conv[0].get("ts"), "end": conv[-1].get("ts"),
                 "turns": [], "friction": why}
        for i, r in enumerate(conv):
            if r.get("status") == "feedback":
                feedback.append({"conversation": n, "thumbs_up": r.get("thumbs_up"), "comment": r.get("message", ""),
                                 "question": r.get("question", "")})
                entry["turns"].append({"kind": "feedback", "thumbs_up": r.get("thumbs_up"), "comment": r.get("message", "")})
                continue
            for a in r.get("attempts") or []:
                repairs[(a.get("problem") or "").split("\n")[0][:110]] += 1
            answered = next((x.get("clarification") for x in conv[i + 1:] if x.get("clarification")), None) \
                if r.get("status") == "clarify" else None
            if r.get("status") == "clarify":
                clarifies.append({"conversation": n, "question": r.get("message", ""), "options": r.get("options") or [],
                                  "answered": answered})
            entry["turns"].append({
                "kind": "turn", "ts": r.get("ts"), "status": r.get("status"), "question": r.get("question", ""),
                "clarification": r.get("clarification", ""), "rows": r.get("rows"), "seconds": r.get("seconds"),
                "repairs": r.get("repairs", 0), "note": r.get("note", ""), "message": r.get("message", ""),
                "options": r.get("options") or [], "answered": answered, "attempts": r.get("attempts") or [],
                "sql": r.get("sql", ""),
            })
        conversations.append(entry)
    return {"since": since, "from": chosen[0].get("ts") if chosen else None, "to": chosen[-1].get("ts") if chosen else None,
            "turns": len(turns), "statuses": dict(statuses), "conversations": conversations,
            "repair_reasons": repairs.most_common(15), "clarifications": clarifies, "feedback": feedback}


def _short(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def render(ev: dict) -> str:
    out = ["# Conversation review — evidence", ""]
    out.append(f"Period: {ev['from'] or '—'} → {ev['to'] or '—'}   (since last review: {ev['since'] or 'never marked'})")
    st = ev["statuses"]
    out.append(f"{len(ev['conversations'])} conversations, {ev['turns']} turns: " +
               ", ".join(f"{k} {v}" for k, v in sorted(st.items(), key=lambda kv: -kv[1])) +
               f"; feedback records: {len(ev['feedback'])}")
    friction_convs = [c for c in ev["conversations"] if c["friction"]]
    out += ["", f"## Conversations with friction ({len(friction_convs)} of {len(ev['conversations'])})", ""]
    for c in friction_convs:
        out.append(f"### C{c['n']} · {c['user']} · {c['start']} → {c['end']} · {len(c['turns'])} turns · "
                   + ", ".join(c["friction"]))
        for i, t in enumerate(c["turns"], 1):
            if t["kind"] == "feedback":
                mark = "👍" if t["thumbs_up"] else ("👎" if t["thumbs_up"] is False else "💬")
                out.append(f"  {mark} feedback: {_short(t['comment'], 200) or '(no comment)'}")
                continue
            bits = [t["status"]]
            if t["rows"] is not None:
                bits.append(f"{t['rows']} rows")
            if t["repairs"]:
                bits.append(f"{t['repairs']} repair{'s' if t['repairs'] > 1 else ''}")
            if t["seconds"]:
                bits.append(f"{t['seconds']:.0f}s")
            out.append(f"  T{i} [{' · '.join(bits)}] {_short(t['question'], 160)}")
            if t["clarification"]:
                out.append(f"      user answered: {_short(t['clarification'], 160)}")
            if t["status"] == "clarify":
                out.append(f"      asked: {_short(t['message'], 220)}")
                for k, o in enumerate(t["options"], 1):
                    out.append(f"        {k}. {_short(o, 120)}")
                if t["answered"]:
                    out.append(f"      → answered: {_short(t['answered'], 160)}")
            elif t["status"] in ("error", "rejected", "cannot"):
                out.append(f"      {t['status']}: {_short(t['message'], 260)}")
            if t["note"]:
                out.append(f"      note: {_short(t['note'], 200)}")
            for k, a in enumerate(t["attempts"], 1):
                out.append(f"      attempt {k} sent back: {_short(a.get('problem', ''), 220)}")
                out.append(f"         SQL: {_short(a.get('sql', ''), 200)}")
            if t["status"] == "sql" and t["sql"] and (t["repairs"] or "correction" in c["friction"]):
                out.append(f"      final SQL: {_short(t['sql'], 240)}")
        out.append("")
    if ev["repair_reasons"]:
        out += ["## What the guard sent back (all conversations)", ""]
        out += [f"- {n} × {reason}" for reason, n in ev["repair_reasons"]]
        out.append("")
    if ev["clarifications"]:
        out += ["## Clarifying questions asked", ""]
        for q in ev["clarifications"]:
            out.append(f"- C{q['conversation']}: {_short(q['question'], 150)}"
                       + (f" → answered: {_short(q['answered'], 120)}" if q["answered"] else " → (no answer logged)"))
        out.append("")
    quiet = [c for c in ev["conversations"] if not c["friction"]]
    if quiet:
        out += [f"## Quiet conversations ({len(quiet)}) — first question of each", ""]
        for c in quiet:
            first = next((t for t in c["turns"] if t["kind"] == "turn"), None)
            if first:
                out.append(f"- C{c['n']} ({len(c['turns'])} turns): {_short(first['question'], 140)}")
        out.append("")
    return "\n".join(out)


def state_path(audit: Path) -> Path:
    return audit.with_name("review_state.json")


def read_state(audit: Path) -> dict:
    p = state_path(audit)
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except json.JSONDecodeError:
        return {}


def mark(audit: Path, records: list[dict]) -> dict:
    latest = max((r.get("ts", "") for r in records), default="")
    state = {"reviewed_through": latest, "marked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "records": len(records)}
    state_path(audit).write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="ignore the review marker")
    ap.add_argument("--since", help="ISO timestamp; records after it")
    ap.add_argument("--json", action="store_true", help="print the evidence as JSON")
    ap.add_argument("--mark", action="store_true", help="record that the log is reviewed up to its last record")
    ap.add_argument("--audit", help="audit log path (default: AUDIT_LOG from .env)")
    args = ap.parse_args(argv)
    if args.audit:
        audit = Path(args.audit)
    else:
        from bqa.config import settings
        audit = Path(settings.audit_log)
        if not audit.is_absolute():
            audit = ROOT / audit
    records = load(audit)
    if args.mark:
        state = mark(audit, records)
        print(f"marked: reviewed through {state['reviewed_through']} ({state['records']} records)")
        return 0
    since = None if args.all else (args.since or read_state(audit).get("reviewed_through"))
    ev = build_evidence(records, since)
    print(json.dumps(ev, indent=2, ensure_ascii=False) if args.json else render(ev))
    return 0


if __name__ == "__main__":
    sys.exit(main())

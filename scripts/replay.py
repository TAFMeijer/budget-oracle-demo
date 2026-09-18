"""Replay a conversation against the live model and database, turn by turn.

    python scripts/replay.py --last                        # the most recent logged conversation
    python scripts/replay.py --conversation 3f2a9c1b4d5e   # a logged conversation, by id
    python scripts/replay.py "HRH budget in WCA" "and by country"   # ad-hoc turns

A logged conversation is replayed as the user had it: each turn with the clarification they
gave, in order, with the conversation history. The replay is written to logs/replay.jsonl, not
to the audit log, so a review never counts its own experiments as user traffic.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bqa.config import settings  # noqa: E402  (after sys.path)
from bqa.query import Assistant  # noqa: E402


def show(label: str, out, took: float) -> None:
    print(f"\n=== {label}\nstatus={out.status} in {took:.0f}s repairs={out.repairs}")
    if out.note:
        print("NOTE:", out.note)
    if out.status == "clarify":
        print("Q:", out.message)
        for n, o in enumerate(out.options, 1):
            print(f"  {n}. {o}")
    elif out.status != "sql":
        print("MSG:", out.message[:400])
    for k, a in enumerate(out.attempts, 1):
        print(f"attempt {k} sent back: {' '.join(a['problem'].split())[:200]}")
    if out.sql:
        print("SQL:", " ".join(out.sql.split())[:700])
    if out.df is not None:
        print(out.df.head(8).to_string())
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("questions", nargs="*", help="ad-hoc turns to ask in order")
    ap.add_argument("--conversation", help="id of a logged conversation to replay")
    ap.add_argument("--last", action="store_true", help="replay the most recent logged conversation")
    args = ap.parse_args(argv)

    turns_to_ask: list[tuple[str, str | None]] = [(q, None) for q in args.questions]
    if args.conversation or args.last:
        from scripts.review_evidence import group, load, select
        records = select(load(Path(settings.audit_log) if Path(settings.audit_log).is_absolute()
                              else ROOT / settings.audit_log), None)
        convs = group(records)
        if args.conversation:
            convs = [c for c in convs if c[0].get("conversation") == args.conversation]
        if not convs:
            print("no such conversation"); return 1
        conv = convs[-1]
        turns_to_ask = [(r["question"], r.get("clarification") or None) for r in conv if r.get("status") != "feedback"]
        print(f"replaying conversation {conv[0].get('conversation') or '(ungrouped)'}: {len(turns_to_ask)} turns")
    if not turns_to_ask:
        ap.print_help(); return 1

    settings.audit_log = str(ROOT / "logs" / "replay.jsonl")
    a = Assistant(settings)
    history = []
    for q, clarification in turns_to_ask:
        t = time.time()
        out = a.ask(q, clarification=clarification, turns=history, conversation_id="replay", turn_no=len(history) + 1)
        show(q + (f"  [answer: {clarification}]" if clarification else ""), out, time.time() - t)
        if out.turn:
            history.append(out.turn)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Orchestration: question → model decision → guard → database → result (+ optional observations)."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

import pandas as pd

from . import prompts
from .audit import AuditLog
from .config import Settings
from .guard import GuardError, misplaced_values, repair_request, unknown_columns, unknown_values, validate
from .llm import (Decision, LLMProvider, Message, build_messages, estimate_messages, estimate_tokens,
                  get_provider, parse_decision)
from .schema import make_engine, run_query, schema_for


@dataclass
class Turn:
    """One completed exchange, stored verbatim so it can be replayed to the model.

    Keeping the exact strings that were sent means the history the model sees on turn 5 is
    literally the conversation it had, not a reconstruction of it.
    """
    user_content: str                # the user message exactly as it was sent
    assistant_content: str           # the model's decision, canonicalised to compact JSON
    result_note: str = ""            # what the query returned; prepended to the NEXT question
    question: str = ""               # the raw question, for the UI
    status: str = ""


def _decision_json(d: Decision) -> str:
    """The decision as the model should see it in its own voice on the next turn."""
    if d.status == "sql":
        obj = {"status": "sql", "sql": d.sql}
        if d.note:                       # replayed too, so the model remembers what it assumed
            obj["note"] = d.note
    elif d.status == "clarify":
        obj = {"status": "clarify", "question": d.question}
        if d.options:                    # and what it offered, so a pick by number reads back naturally
            obj["options"] = d.options
            if d.multi:
                obj["multi"] = True
    else:
        obj = {"status": "cannot", "reason": d.reason}
    return json.dumps(obj, ensure_ascii=False)


def _db_error_text(e: Exception) -> str:
    """The database's own message, without the SQL and the driver's framing.

    SQLAlchemy wraps the driver error and appends the whole statement; pymssql wraps its message
    in a bytes literal followed by DB-Lib boilerplate. What the user, and the model, need is the
    one line the server said: "Invalid column name 'region_code'."
    """
    text = str(getattr(e, "orig", None) or e)
    m = re.match(r"Execution failed on sql '.*': (.*)$", text, re.S)  # pandas: "... on sql '<sql>': <error>"
    if m:
        text = m.group(1)
    text = text.split("[SQL:")[0].split("(Background on this error")[0].strip()   # SQLAlchemy's trailer
    text = re.sub(r"^\(\w[\w.]*\)\s*", "", text)                             # "(sqlite3.OperationalError) "
    m = re.match(r"\(\d+, b?[\"'](.*)[\"']\)$", text, re.S)                   # pymssql: (207, b"message...")
    if m:
        text = m.group(1).split("DB-Lib error message")[0]
    return " ".join(text.replace("\\n", " ").split())[:300]


def _db_error_is_repairable(message: str) -> bool:
    """A rewrite can fix a wrong column or a bad expression; it cannot fix a timeout or a
    connection that dropped, so those go straight to the user."""
    return not re.search(r"timeout|timed out|took longer|interrupt|cancel|connection|login|network|deadlock|"
                         r"out of memory", message, re.I)


def _result_note(out: "Outcome") -> str:
    """One line telling the model what its last query actually returned — shape, not data."""
    if out.status == "sql" and out.df is not None:
        cols = ", ".join(map(str, out.df.columns))
        return f"(Context: that query returned {len(out.df):,} rows with columns: {cols}.)"
    if out.status in ("rejected", "error"):
        return "(Context: that query did not run — it was rejected or failed — so no results were returned.)"
    return ""


@dataclass
class Outcome:
    status: str                      # sql | clarify | cannot | rejected | error
    question: str
    sql: str = ""
    df: pd.DataFrame | None = None
    message: str = ""                # clarifying question / reason / error text
    observations: str = ""
    note: str = ""                   # sql only: the reading the model made, in its own words
    options: list[str] = field(default_factory=list)   # clarify only: answers offered for a pick by number
    multi: bool = False              # clarify only: the options can be combined (tick several)
    decision: Decision | None = None
    row_limit_hit: bool = False
    repairs: int = 0                 # times the guard sent the query back to the model to fix
    user: str = ""                   # signed-in email when access control is enabled
    turn: Turn | None = None         # append to the conversation history; None = nothing worth remembering
    # Everything the review of a conversation needs, kept with the record so the audit log is
    # the complete story of a turn: what was asked, what the user answered when asked back, every
    # attempt the guard or the database sent back and why, the model's final words, and how long
    # it took. Only the result rows stay out — the audit holds no budget figures.
    conversation_id: str = ""        # the browser conversation this turn belongs to
    turn_no: int = 0                 # 1-based position in that conversation
    clarification: str = ""          # the user's answer to the previous clarifying question, if this turn is one
    attempts: list[dict] = field(default_factory=list)   # [{"sql": rejected query, "problem": why}, ...]
    raw: str = ""                    # the model's final reply, verbatim
    seconds: float = 0.0             # wall time of the whole turn


_CHOICE = re.compile(r"^\s*\d+(?:\s*(?:,|;|/|&|and|\s)\s*\d+)*\s*$", re.I)


def expand_choice(reply: str, options: list[str]) -> str | None:
    """The option text(s) a reply such as '2' or '1, 3 and 4' picks from a clarifying question's
    options, or None when the reply is an ordinary answer. Numbers count from 1; one out of range
    makes the whole reply ordinary rather than a partial pick."""
    if not options or not _CHOICE.match(reply or ""):
        return None
    picks: list[str] = []
    for n in re.findall(r"\d+", reply):
        i = int(n)
        if not 1 <= i <= len(options):
            return None
        if options[i - 1] not in picks:
            picks.append(options[i - 1])
    return "; ".join(picks)


def add_share_column(df: pd.DataFrame) -> pd.DataFrame:
    """Append '% of total' when the result has exactly one numeric column and more than one row."""
    if len(df) <= 1:
        return df
    numeric = df.select_dtypes(include="number").columns.tolist()
    if len(numeric) != 1:
        return df
    total = df[numeric[0]].sum()
    if not total:
        return df
    out = df.copy()
    out["% of total"] = out[numeric[0]] / total
    return out


class Assistant:
    def __init__(self, settings: Settings, provider: LLMProvider | None = None):
        self.s = settings
        self.provider = provider or get_provider(settings)
        self.engine = make_engine(settings)
        self.schema = schema_for(settings)
        self.audit = AuditLog(settings.audit_log)

    # ---- prompt -------------------------------------------------------------------------
    def system_prompt(self) -> str:
        system = prompts.SQL_SYSTEM.format(dialect=self.s.dialect, schema=self.schema.as_prompt())
        if self.s.prompt_notes:
            system += f"\n## Site notes\n{self.s.prompt_notes}\n"
        return system

    # ---- conversation history --------------------------------------------------------------
    @staticmethod
    def history_messages(turns: list[Turn] | None) -> list[Message]:
        """The earlier turns, replayed as alternating user/assistant messages."""
        msgs: list[Message] = []
        for t in turns or []:
            msgs.append({"role": "user", "content": t.user_content})
            msgs.append({"role": "assistant", "content": t.assistant_content})
        return msgs

    def user_content(self, question: str, clarification: str | None = None,
                     turns: list[Turn] | None = None) -> str:
        """The user message for this question, carrying forward what the last query returned."""
        content = prompts.SQL_USER.format(
            question=question,
            clarification=f"Clarification from the user: {clarification}" if clarification else "")
        note = turns[-1].result_note if turns else ""
        return f"{note}\n{content}" if note else content

    def context_usage(self, turns: list[Turn] | None = None, question: str = "") -> dict:
        """What the next request would cost, in approximate tokens. Drives the fullness meter.

        'used' counts everything that would be sent; 'reserve' is room the answer needs on top,
        so the conversation is full when used + reserve reaches the window.
        """
        cpt = self.s.chars_per_token
        system = self.system_prompt()
        history = self.history_messages(turns)
        live = self.user_content(question or "…", turns=turns)
        base = estimate_tokens(system, cpt) + 4
        return {
            "base": base,                                        # system prompt: rules + schema + valid values
            "history": estimate_messages(history, cpt),
            "question": estimate_tokens(live, cpt) + 4,
            "used": base + estimate_messages(history, cpt) + estimate_tokens(live, cpt) + 4,
            "reserve": self.s.context_reserve,
            "limit": self.s.context_window,
            "turns": len(turns or []),
        }

    def context_full(self, turns: list[Turn] | None = None, question: str = "") -> bool:
        u = self.context_usage(turns, question)
        return u["used"] + u["reserve"] >= u["limit"]

    # ---- main entry point ------------------------------------------------------------------
    def ask(self, question: str, clarification: str | None = None, with_observations: bool | None = None,
            user_email: str = "", on_event=None, turns: list[Turn] | None = None,
            conversation_id: str = "", turn_no: int = 0) -> Outcome:
        """on_event(phase, payload) reports progress to a UI: ("thinking", "") when the model is
        called, ("executing", sql) when the guard-approved query is about to run.

        'turns' is the conversation so far, owned by the caller (the browser session), so the
        Assistant itself stays stateless and shareable between simultaneous users.
        """
        emit = on_event or (lambda *_: None)
        question = (question or "").strip()
        if not question:
            return Outcome(status="error", question=question, message="Please type a question.")
        turns = list(turns or [])
        user = self.user_content(question, clarification, turns)
        history = self.history_messages(turns)
        emit("thinking", "")
        started = time.time()
        attempts: list[dict] = []

        # The guard talks back to the model. A query whose shape is rejected, or whose filters
        # name values that do not exist, goes back with the specific problem and the real
        # candidates, and the model rewrites it. Only when it cannot be repaired does the user
        # see anything — as the model's own clarifying question, not a database error.
        repairs, decision, safe_sql, problem, executed = 0, None, "", "", None
        while True:
            try:
                raw = self.provider.complete(self.system_prompt(), user, json_mode=True,
                                             history=history + self._repair_messages(decision, problem))
            except Exception as e:  # noqa: BLE001 - surface provider problems to the UI
                out = Outcome(status="error", question=question,
                              message=f"The model could not be reached: {e}", user=user_email,
                              conversation_id=conversation_id, turn_no=turn_no,
                              clarification=clarification or "", attempts=attempts,
                              seconds=time.time() - started)
                self.audit.write(out)
                return out
            decision = parse_decision(raw)
            if decision.status != "sql":
                break
            executed = None
            try:
                safe_sql = validate(decision.sql, self.s.allowed_tables, self.s.dialect, self.s.row_limit,
                                    required_filters=self.s.required_filters)
                problem = unknown_columns(safe_sql, self.schema, self.s.dialect) if self.s.check_values else ""
                bad = [] if problem else (unknown_values(safe_sql, self.schema, self.s.dialect)
                                          if self.s.check_values else [])
                if bad:
                    problem = repair_request(bad, misplaced_values(bad, self.schema))
                    short = "; ".join(f"'{v}' is not a valid {c}" for c, v, _ in bad[:2])
                elif problem:
                    short = problem.split(". ", 1)[0]
                else:
                    # The guard is satisfied: run it. A database error is what the guard could not
                    # foresee (a conversion, an expression the server rejects), and the server's
                    # message is exactly what the model needs to fix it — so it goes back too,
                    # unless it is a timeout or a dropped connection, which a rewrite cannot cure.
                    executed = self._execute(question, decision, safe_sql, emit)
                    if executed.status != "error" or not _db_error_is_repairable(executed.message):
                        break
                    problem = (f"The database rejected that query: {executed.message} Rewrite it so it "
                               "runs. Each SELECT may use only the columns of the table it reads, and "
                               "the two budget tables name the same concepts differently.")
                    short = executed.message
            except GuardError as e:
                problem = (f"The SQL guard rejected that query: {e} Rewrite it so it passes: a single "
                           f"SELECT, every column named, only the tables above, no data changes.")
                short = str(e)
            attempts.append({"sql": decision.sql, "problem": problem[:1500]})
            if repairs >= self.s.guard_repair_attempts:
                break
            repairs += 1
            emit("repairing", short)

        if decision.status == "clarify":
            out = Outcome(status="clarify", question=question, message=decision.question, decision=decision,
                          options=decision.options, multi=decision.multi)
        elif decision.status == "cannot":
            out = Outcome(status="cannot", question=question, message=decision.reason or
                          "This cannot be answered from the available tables.", decision=decision)
        elif executed is not None:
            out = executed              # the answer — or the database error the model could not fix
            if out.status == "sql" and (self.s.observations if with_observations is None else with_observations):
                out.observations = self._observations(question, out.df)
        elif problem:
            # Out of repair attempts: say plainly what is wrong instead of running a query that
            # would return nothing and read like a real answer of zero.
            shape = problem.startswith("The SQL guard rejected")      # vs. filters on invented values
            out = Outcome(status="rejected", question=question, sql=decision.sql, decision=decision,
                          message=("The model could not write a query the guard accepts. " if shape else
                                   "The query kept referring to values that do not exist in the data. ")
                                  + problem.split("\n", 1)[-1].split("Rewrite the query")[0].strip())
        else:                       # not reachable: a passing query is executed inside the loop
            out = self._execute(question, decision, safe_sql, emit)
        out.repairs = repairs
        out.user = user_email
        out.conversation_id, out.turn_no = conversation_id, turn_no
        out.clarification, out.attempts, out.raw = clarification or "", attempts, decision.raw
        out.seconds = time.time() - started
        out.turn = Turn(user_content=user, assistant_content=_decision_json(decision),
                        result_note=_result_note(out), question=question, status=out.status)
        self.audit.write(out, history=len(turns))
        return out

    # ---- the model's own correction round-trips ------------------------------------------------
    @staticmethod
    def _repair_messages(decision: Decision | None, problem: str) -> list[Message]:
        """The rejected query and why, in the model's own voice and then ours."""
        if decision is None or not problem:
            return []
        return [{"role": "assistant", "content": _decision_json(decision)},
                {"role": "user", "content": problem}]

    # ---- execute -------------------------------------------------------------------------------
    def _execute(self, question: str, decision: Decision, safe_sql: str, emit=lambda *_: None) -> Outcome:
        emit("executing", safe_sql)
        try:
            df = run_query(self.engine, safe_sql)
        except Exception as e:  # noqa: BLE001
            detail = _db_error_text(e)
            if re.search(r"interrupt|out of memory", detail, re.I):      # the per-statement deadline or heap cap
                detail = (f"The query took longer than {self.s.db_query_timeout} seconds or too much memory "
                          "and was stopped. Narrow the question — a country, a year, a module — and try again.")
                return Outcome(status="error", question=question, sql=safe_sql, message=detail, decision=decision)
            return Outcome(status="error", question=question, sql=safe_sql,
                           message=f"The database returned an error: {detail}", decision=decision)
        return Outcome(status="sql", question=question, sql=safe_sql, df=add_share_column(df),
                       decision=decision, row_limit_hit=len(df) >= self.s.row_limit, note=decision.note)

    # ---- optional second call ----------------------------------------------------------------
    def _observations(self, question: str, df: pd.DataFrame | None) -> str:
        if df is None or df.empty:
            return ""
        preview = df.head(60).copy()
        if "% of total" in preview.columns:
            preview["% of total"] = preview["% of total"].map(lambda x: f"{x * 100:.1f}%")
        try:
            return self.provider.complete(
                prompts.OBSERVATIONS_SYSTEM,
                prompts.OBSERVATIONS_USER.format(question=question, rows=len(df),
                                                 columns=", ".join(map(str, df.columns)),
                                                 table=preview.to_string(index=False)),
            ).strip()
        except Exception:  # noqa: BLE001 - observations are optional
            return ""

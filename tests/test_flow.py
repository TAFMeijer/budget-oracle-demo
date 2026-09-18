import json

import pytest

from bqa.config import Settings
from bqa.llm import MockProvider, parse_decision
from bqa.query import Assistant


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the schema cache at a temp directory so tests never write into logs/."""
    import bqa.config
    monkeypatch.setattr(bqa.config, "ROOT", tmp_path)
    return tmp_path


class Recorder(MockProvider):
    """MockProvider that keeps every (system, user, history) it was called with."""

    def __init__(self):
        self.calls = []

    def complete(self, system, user, *, json_mode=False, history=None):
        self.calls.append((system, user, list(history or [])))
        return super().complete(system, user, json_mode=json_mode)


@pytest.fixture(scope="module")
def assistant(tmp_path_factory):
    from data.make_demo_db import build
    db = tmp_path_factory.mktemp("db") / "demo.sqlite"
    build(db)
    s = Settings(db_url=f"sqlite:///file:{db.as_posix()}?mode=ro&uri=true", provider="mock",
                 audit_log=str(tmp_path_factory.mktemp("logs") / "audit.jsonl"), observations=False)
    return Assistant(s, provider=MockProvider())


def test_sql_path_returns_rows_and_share(assistant):
    out = assistant.ask("Total budget by country")
    assert out.status == "sql", out.message
    assert len(out.df) > 1
    assert "% of total" in out.df.columns
    assert abs(out.df["% of total"].sum() - 1) < 1e-9


def test_filter_and_join(assistant):
    out = assistant.ask("Budget by region for Kenya")
    assert out.status == "sql", out.message
    assert "JOIN" in out.sql.upper() and "Kenya" in out.sql


def test_clarify_path(assistant):
    out = assistant.ask("Budget for prevention")
    assert out.status == "clarify"
    assert out.message.endswith("?")
    followed = assistant.ask("Budget for prevention", clarification="the module Prevention programs for key populations")
    assert followed.status == "sql"


def test_cannot_path(assistant):
    out = assistant.ask("What is the population of Uganda?")
    assert out.status == "cannot"


def test_guard_rejects_model_output(assistant):
    class Evil(MockProvider):
        def complete(self, system, user, *, json_mode=False, history=None):
            return json.dumps({"status": "sql", "sql": "SELECT * FROM budget_lines"})
    a = Assistant(assistant.s, provider=Evil())
    out = a.ask("anything")
    assert out.status == "rejected" and "SELECT *" in out.message


def test_audit_log_written(assistant):
    assistant.ask("Total budget by country")
    lines = open(assistant.s.audit_log, encoding="utf-8").read().strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["status"] == "sql" and rec["rows"] > 0 and "ip" not in rec


def test_allowed_columns_and_prompt_notes(assistant, tmp_path):
    s = Settings(db_url=assistant.s.db_url, provider="mock", observations=False,
                 audit_log=str(tmp_path / "audit.jsonl"),
                 allowed_columns={"geography": ["country"]},
                 prompt_notes="Always exclude test rows.")
    a = Assistant(s, provider=MockProvider())
    prompt = a.schema.as_prompt()
    assert "- region (" not in prompt          # trimmed from geography
    assert "- country (" in prompt
    assert "## Site notes\nAlways exclude test rows." in a.system_prompt()


def test_allowlist_parsing(tmp_path):
    assert Settings(allowed_emails="").load_allowed_emails() is None
    assert Settings(allowed_emails=" A@x.org , b@y.org ").load_allowed_emails() == {"a@x.org", "b@y.org"}
    f = tmp_path / "allowed.txt"
    f.write_text("a@x.org\n# comment\nB@y.org  # inline\n\n")
    assert Settings(allowed_emails=str(f)).load_allowed_emails() == {"a@x.org", "b@y.org"}


def test_audit_records_user(assistant, tmp_path):
    import json
    s = Settings(db_url=assistant.s.db_url, provider="mock", observations=False,
                 audit_log=str(tmp_path / "audit.jsonl"))
    a = Assistant(s, provider=MockProvider())
    a.ask("Total budget by country", user_email="a@x.org")
    a.ask("Total budget by country")
    recs = [json.loads(l) for l in open(s.audit_log, encoding="utf-8")]
    assert recs[0]["user"] == "a@x.org" and "user" not in recs[1]


@pytest.mark.parametrize("text,status", [
    ('{"status":"sql","sql":"SELECT country FROM budget_lines"}', "sql"),
    ('```json\n{"status":"clarify","question":"Which one?"}\n```', "clarify"),
    ("CLARIFICATION_NEEDED: Which period?", "clarify"),
    ("CANNOT_ANSWER", "cannot"),
    ("SELECT country FROM budget_lines", "sql"),
    ("I think the answer is 42", "cannot"),
    ('<think>The user wants totals.\n</think>{"status":"sql","sql":"SELECT country FROM budget_lines"}', "sql"),
    ("<think>hmm</think>CLARIFICATION_NEEDED: Which period?", "clarify"),
])
def test_parse_decision(text, status):
    assert parse_decision(text).status == status


def test_ask_emits_phase_events(assistant):
    events = []
    out = assistant.ask("Total budget by country", on_event=lambda p, x: events.append((p, bool(x))))
    assert out.status == "sql"
    assert events == [("thinking", False), ("executing", True)]   # sql payload only on executing


def test_schema_disk_cache(assistant, tmp_path, cache_dir):
    import bqa.schema as schema_mod
    s = Settings(db_url=assistant.s.db_url, provider="mock", observations=False,
                 audit_log=str(tmp_path / "a.jsonl"), schema_cache_ttl=3600)
    ctx1 = schema_mod.schema_for(s)
    assert list((cache_dir / "logs").glob("schema_*.pkl")), "cache file written"
    ctx2 = schema_mod.schema_for(s)
    assert ctx2.as_prompt() == ctx1.as_prompt()


# ---------------------------------------------------------------- conversation history

def test_turn_is_recorded_with_result_shape(assistant):
    out = assistant.ask("Total budget by country")
    assert out.turn is not None and out.turn.status == "sql"
    assert json.loads(out.turn.assistant_content)["status"] == "sql"
    assert "rows with columns" in out.turn.result_note and "country" in out.turn.result_note


def test_history_is_replayed_to_the_model(assistant):
    rec = Recorder()
    a = Assistant(assistant.s, provider=rec)
    first = a.ask("Total budget by country")
    second = a.ask("And by module?", turns=[first.turn])
    system, user, history = rec.calls[-1]
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert "Total budget by country" in history[0]["content"]
    assert json.loads(history[1]["content"])["status"] == "sql"
    assert "rows with columns" in user          # what the last query returned rides on the new question
    assert second.status == "sql"


def test_no_turns_means_no_history(assistant):
    rec = Recorder()
    a = Assistant(assistant.s, provider=rec)
    a.ask("Total budget by country")
    a.ask("And by module?")                     # caller passes nothing -> stateless, as before
    assert rec.calls[-1][2] == []


def test_clarify_turn_carries_no_result_note(assistant):
    out = assistant.ask("Budget for prevention")
    assert out.status == "clarify"
    assert out.turn.result_note == ""
    assert json.loads(out.turn.assistant_content)["status"] == "clarify"


def test_context_usage_grows_with_the_conversation(assistant):
    empty = assistant.context_usage([])
    assert empty["base"] > 0 and empty["history"] == 0 and empty["turns"] == 0
    turn = assistant.ask("Total budget by country").turn
    grown = assistant.context_usage([turn])
    assert grown["history"] > 0 and grown["used"] > empty["used"] and grown["turns"] == 1
    assert not assistant.context_full([])


def test_context_full_blocks_when_the_window_is_too_small(assistant, tmp_path):
    s = Settings(db_url=assistant.s.db_url, provider="mock", observations=False,
                 audit_log=str(tmp_path / "audit.jsonl"), context_window=64)
    a = Assistant(s, provider=MockProvider())
    assert a.context_full([])                   # the schema alone already overflows a 64-token window


def test_context_reserve_defaults_to_max_tokens():
    assert Settings(llm_max_tokens=1500).context_reserve == 1500
    assert Settings(llm_max_tokens=1500, context_reserve=300).context_reserve == 300


# ---------------------------------------------------------------- schema cache

def test_stale_cache_is_served_not_reintrospected(assistant, tmp_path, monkeypatch, cache_dir):
    """A cache older than the TTL is still served: a user must never wait for introspection."""
    import os
    import bqa.schema as schema_mod
    s = Settings(db_url=assistant.s.db_url, provider="mock", observations=False,
                 audit_log=str(tmp_path / "a.jsonl"), schema_cache_ttl=1,
                 schema_cache_serve_stale=True)
    cache = schema_mod.schema_cache_path(s)
    ctx = schema_mod.schema_for(s)                     # writes the cache
    os.utime(cache, (0, 0))                            # pretend it is from 1970
    assert schema_mod.schema_cache_age(s) > 86400

    calls = []
    real = schema_mod.load_schema
    monkeypatch.setattr(schema_mod, "load_schema",
                        lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    served = schema_mod.schema_for(s)
    assert calls == [], "a stale cache must be served, not re-introspected"
    assert served.as_prompt() == ctx.as_prompt()

    forced = schema_mod.schema_for(s, force_refresh=True)
    assert calls, "force_refresh must go to the database"
    assert forced.as_prompt() == ctx.as_prompt()
    assert schema_mod.schema_cache_age(s) < 60, "a forced refresh rewrites the cache"


def test_serve_stale_off_reintrospects(assistant, tmp_path, monkeypatch, cache_dir):
    import os
    import bqa.schema as schema_mod
    s = Settings(db_url=assistant.s.db_url, provider="mock", observations=False,
                 audit_log=str(tmp_path / "a.jsonl"), schema_cache_ttl=1,
                 schema_cache_serve_stale=False)
    cache = schema_mod.schema_cache_path(s)
    schema_mod.schema_for(s)
    os.utime(cache, (0, 0))
    calls = []
    real = schema_mod.load_schema
    monkeypatch.setattr(schema_mod, "load_schema",
                        lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    schema_mod.schema_for(s)
    assert calls, "with serve_stale off, an expired cache is rebuilt"


def test_cache_path_changes_with_the_allow_list(assistant, tmp_path, cache_dir):
    import bqa.schema as schema_mod
    a = Settings(db_url=assistant.s.db_url, provider="mock", audit_log=str(tmp_path / "a.jsonl"))
    b = Settings(db_url=assistant.s.db_url, provider="mock", audit_log=str(tmp_path / "a.jsonl"),
                 allowed_columns={"geography": ["country"]})
    assert schema_mod.schema_cache_path(a) != schema_mod.schema_cache_path(b)


# ---------------------------------------------------------------- reasoning-effort fallback

def test_empty_answer_retries_with_thinking_off(monkeypatch):
    """An empty reply means reasoning never finished; the retry must find a spelling the
    server accepts — 'none' for OpenAI-style servers and current LM Studio, 'off' for older
    gemma builds. It must never drop the parameter instead: absent means the model's own
    default, which is thinking ON — the very state that produced the empty reply."""
    from bqa.llm import OpenAICompatibleProvider

    class Resp:
        def __init__(self, content, status=200):
            self._c, self.status_code = content, status

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError(f"unexpected raise_for_status at {self.status_code}")

        def json(self):
            return {"choices": [{"message": {"content": self._c}}]}

    sent = []

    def fake_post(url, json=None, timeout=None, headers=None):
        sent.append(json.get("reasoning_effort", "<absent>"))
        if json.get("reasoning_effort") == "off":
            return Resp('{"status":"sql","sql":"SELECT 1 AS x"}')
        return Resp("")                       # thinking on: reasoned until the cap, said nothing

    monkeypatch.setattr("bqa.llm.requests.post", fake_post)
    p = OpenAICompatibleProvider("http://localhost:1234/v1", "", "gemma", reasoning_effort="on")
    out = p.complete("sys", "user")
    assert "<absent>" not in sent, f"dropped reasoning_effort, restoring thinking-on: {sent}"
    assert sent[0] == "on" and sent[-1] == "off", sent
    assert "SELECT 1" in out


def test_rejected_reasoning_spelling_is_respelled_not_dropped(monkeypatch):
    """A server that rejects the configured spelling with 400 must be offered another one.
    Dropping the parameter silently restores thinking-on and the model answers nothing."""
    from bqa.llm import OpenAICompatibleProvider

    class Resp:
        def __init__(self, content, status=200):
            self._c, self.status_code = content, status
            self.text = "" if status == 200 else '{"error":{"param":"reasoning_effort"}}'

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError(f"unexpected raise_for_status at {self.status_code}")

        def json(self):
            if self.status_code >= 400:
                return {"error": {"param": "reasoning_effort", "message": "bad value"}}
            return {"choices": [{"message": {"content": self._c}}]}

    sent = []

    def fake_post(url, json=None, timeout=None, headers=None):
        sent.append(json.get("reasoning_effort", "<absent>"))
        if json.get("reasoning_effort") == "off":          # this server rejects 'off'
            return Resp("", status=400)
        return Resp('{"status":"sql","sql":"SELECT 1 AS x"}')

    monkeypatch.setattr("bqa.llm.requests.post", fake_post)
    p = OpenAICompatibleProvider("http://localhost:1234/v1", "", "gemma", reasoning_effort="off")
    out = p.complete("sys", "user")
    assert "<absent>" not in sent, f"dropped reasoning_effort instead of re-spelling: {sent}"
    assert sent == ["off", "none"], sent
    assert "SELECT 1" in out


def test_no_retry_when_the_answer_is_fine(monkeypatch):
    from bqa.llm import OpenAICompatibleProvider

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    calls = []
    monkeypatch.setattr("bqa.llm.requests.post",
                        lambda *a, **k: (calls.append(1), Resp())[1])
    p = OpenAICompatibleProvider("http://x/v1", "", "m", reasoning_effort="off")
    assert p.complete("s", "u") == "ok"
    assert len(calls) == 1


# ---------------------------------------------------------------- guard talks back to the model

def test_suggest_recovers_a_dropped_prefix():
    from bqa.guard import suggest
    options = {"RSSH: Health products management systems", "RSSH: Health financing systems",
               "RSSH/PP: Laboratory systems (including national and peripheral)"}
    assert suggest("Health products management systems", options)[0] == \
        "RSSH: Health products management systems"


def test_unknown_values_flags_an_invented_value(assistant):
    from bqa.guard import unknown_values
    ok = "SELECT module, SUM(amount_usd) AS total_amount FROM budget_lines WHERE module = 'RSSH: Health financing systems' GROUP BY module"
    bad = "SELECT module, SUM(amount_usd) AS total_amount FROM budget_lines WHERE module = 'Health financing systems' GROUP BY module"
    real = [v for v in assistant.schema.tables[0].valid_values["module"]]
    ok = ok.replace("RSSH: Health financing systems", real[0])
    assert unknown_values(ok, assistant.schema) == []
    problems = unknown_values(bad, assistant.schema)
    assert problems and problems[0][0].lower() == "module"


def test_unknown_values_ignores_unlistable_columns(assistant, tmp_path):
    """A column whose values are not listed for every table carrying them is never checked."""
    from bqa.guard import checkable_values
    known = checkable_values(assistant.schema)
    assert "module" in known
    assert "amount_usd" not in known          # numeric, no listed values


def test_guard_sends_the_query_back_and_the_model_fixes_it(assistant):
    """The user must never see the invented value: the guard hands it back, the model retries."""
    real = assistant.schema.tables[0].valid_values["module"][0]
    # A near miss, the way it actually fails: the user's wording, not the listed value.
    near_miss = real.split(":")[-1].strip() if ":" in real else real.lower()

    class Sloppy(MockProvider):
        """First answer echoes the user's wording; after the guard objects, it copies the real one."""
        def __init__(self):
            self.seen = []

        def complete(self, system, user, *, json_mode=False, history=None):
            self.seen.append(user if not history else history[-1]["content"])
            told = any("not a valid value" in m.get("content", "") for m in (history or []))
            module = real if told else near_miss
            return json.dumps({"status": "sql", "sql":
                               f"SELECT module, SUM(amount_usd) AS total_amount FROM budget_lines "
                               f"WHERE module = '{module}' GROUP BY module"})

    p = Sloppy()
    out = Assistant(assistant.s, provider=p).ask("budget for that module")
    assert out.status == "sql", out.message
    assert out.repairs == 1
    assert out.sql.count(real) == 1
    assert "not a valid value" in p.seen[-1]
    assert any(real in s for s in p.seen[-1].split("\n")), "the real candidates were offered"


def test_repair_gives_up_and_says_so(assistant):
    """A model that keeps inventing gets stopped, not executed against the database."""
    class Stubborn(MockProvider):
        def complete(self, system, user, *, json_mode=False, history=None):
            return json.dumps({"status": "sql", "sql":
                               "SELECT module, SUM(amount_usd) AS total_amount FROM budget_lines "
                               "WHERE module = 'Never A Real Module' GROUP BY module"})

    out = Assistant(assistant.s, provider=Stubborn()).ask("budget by module")
    assert out.status == "rejected"
    assert out.repairs == assistant.s.guard_repair_attempts
    assert "do not exist" in out.message and "Never A Real Module" in out.message


def test_repair_can_be_turned_off(assistant, tmp_path):
    s = Settings(db_url=assistant.s.db_url, provider="mock", observations=False,
                 audit_log=str(tmp_path / "a.jsonl"), check_values=False)

    class Sloppy(MockProvider):
        def complete(self, system, user, *, json_mode=False, history=None):
            return json.dumps({"status": "sql", "sql":
                               "SELECT module, SUM(amount_usd) AS total_amount FROM budget_lines "
                               "WHERE module = 'Never A Real Module' GROUP BY module"})

    out = Assistant(s, provider=Sloppy()).ask("budget by module")
    assert out.status == "sql" and out.repairs == 0      # runs as before, returns nothing
    assert len(out.df) == 0


def test_guard_rejection_is_also_sent_back(assistant):
    """A shape rejection (SELECT *) is a repairable problem too, not a dead end."""
    class Careless(MockProvider):
        def __init__(self):
            self.n = 0

        def complete(self, system, user, *, json_mode=False, history=None):
            self.n += 1
            sql = ("SELECT * FROM budget_lines" if self.n == 1 else
                   "SELECT country, SUM(amount_usd) AS total_amount FROM budget_lines GROUP BY country")
            return json.dumps({"status": "sql", "sql": sql})

    out = Assistant(assistant.s, provider=Careless()).ask("everything")
    assert out.status == "sql" and out.repairs == 1
    assert "*" not in out.sql


# ---------------------------------------------------------------- prompt size

def test_hierarchy_is_grouped_under_its_parent(assistant):
    """The parent name is written once, not once per child."""
    prompt = assistant.schema.as_prompt()
    hierarchies = assistant.schema.hierarchies
    key, pairs = next(iter(hierarchies.items()))
    parent_name = pairs[0][0]
    # the flat rendering would repeat the parent for each of its children
    children = [c for p, c in pairs if p == parent_name]
    assert len(children) > 1, "need a parent with several children to test this"
    assert prompt.count(f"\n  {parent_name}\n") == 1
    assert f"{parent_name} → " not in prompt


def test_excluded_columns_get_no_value_list(assistant, tmp_path, cache_dir):
    import bqa.schema as schema_mod
    from bqa.guard import checkable_values
    base = Settings(db_url=assistant.s.db_url, provider="mock", observations=False,
                    audit_log=str(tmp_path / "a.jsonl"))
    trimmed = Settings(db_url=assistant.s.db_url, provider="mock", observations=False,
                       audit_log=str(tmp_path / "a.jsonl"),
                       exclude_values={"budget_lines": ["module"]})
    full_ctx = schema_mod.schema_for(base)
    trim_ctx = schema_mod.schema_for(trimmed)
    assert "module" in [c for t in full_ctx.tables for c in t.valid_values]
    assert "module" not in [c for t in trim_ctx.tables for c in t.valid_values]
    assert len(trim_ctx.as_prompt()) < len(full_ctx.as_prompt())
    # and the trade-off: an unlisted column is no longer checkable by the guard
    assert "module" in checkable_values(full_ctx)
    assert "module" not in checkable_values(trim_ctx)


# ---------------------------------------------------------------- the model's stated assumptions

class Noting(MockProvider):
    """MockProvider whose SQL answers carry a note, as a real model's do when it made a reading."""

    def complete(self, system, user, *, json_mode=False, history=None):
        obj = json.loads(super().complete(system, user, json_mode=json_mode))
        if obj.get("status") == "sql":
            obj["note"] = "Read 'the budget' as GC8."
        return json.dumps(obj)


def test_parse_decision_keeps_the_note():
    d = parse_decision('{"status":"sql","sql":"SELECT country FROM budget_lines","note":"Assumed GC8."}')
    assert d.status == "sql" and d.note == "Assumed GC8."
    assert parse_decision('{"status":"sql","sql":"SELECT country FROM budget_lines"}').note == ""
    assert parse_decision('{"status":"clarify","question":"Which?","note":"x"}').note == ""


def test_note_reaches_the_answer_the_history_and_the_audit(assistant, tmp_path):
    s = Settings(db_url=assistant.s.db_url, provider="mock", observations=False,
                 audit_log=str(tmp_path / "audit.jsonl"))
    a = Assistant(s, provider=Noting())
    out = a.ask("Total budget by country")
    assert out.status == "sql" and out.note == "Read 'the budget' as GC8."
    assert json.loads(out.turn.assistant_content)["note"] == out.note     # replayed, so it remembers
    rec = json.loads(open(s.audit_log, encoding="utf-8").readline())
    assert rec["note"] == out.note
    plain = assistant.ask("Total budget by country")                      # no note -> nothing added
    assert plain.note == "" and "note" not in json.loads(plain.turn.assistant_content)


def test_prompt_asks_for_closest_match_not_confirmation(assistant):
    system = assistant.system_prompt()
    assert "closest listed value" in system
    assert '"note"' in system
    assert "Do not guess" not in system


def test_parse_decision_takes_the_first_complete_object():
    """A model that changes its mind mid-answer can close the object early and keep writing
    ('..."} , "note": "..."}'). The object that parses is the decision; the tail is ignored."""
    d = parse_decision('{"status":"sql","sql":"SELECT country FROM budget_lines"} , "note": "late"}')
    assert d.status == "sql" and d.sql == "SELECT country FROM budget_lines" and d.note == ""
    d = parse_decision('Sure: {"status":"clarify","question":"Which one?"} Let me know.')
    assert d.status == "clarify" and d.question == "Which one?"


def test_required_filters_setting_parses(monkeypatch):
    monkeypatch.setenv("REQUIRED_FILTERS", "Combined Budget Table:Funding Cycle; other: a, b")
    assert Settings().required_filters == {"Combined Budget Table": ["Funding Cycle"], "other": ["a", "b"]}
    monkeypatch.setenv("REQUIRED_FILTERS", "")
    assert Settings().required_filters == {}


# ---------------------------------------------------------------- clarifying questions with options

def test_parse_decision_keeps_clarify_options():
    d = parse_decision('{"status":"clarify","question":"Which?","options":["A","B"," "]}')
    assert d.status == "clarify" and d.options == ["A", "B"]
    assert parse_decision('{"status":"sql","sql":"SELECT country FROM budget_lines","options":["x"]}').options == []
    assert parse_decision('{"status":"clarify","question":"Which?","options":"A or B"}').options == []


def test_clarify_options_reach_the_outcome_history_and_audit(assistant, tmp_path):
    s = Settings(db_url=assistant.s.db_url, provider="mock", observations=False,
                 audit_log=str(tmp_path / "audit.jsonl"))
    a = Assistant(s, provider=MockProvider())
    out = a.ask("Budget for prevention")
    assert out.status == "clarify" and len(out.options) == 2
    assert json.loads(out.turn.assistant_content)["options"] == out.options
    rec = json.loads(open(s.audit_log, encoding="utf-8").readline())
    assert rec["options"] == out.options
    picked = a.ask("Budget for prevention", clarification=out.options[0], turns=[out.turn])
    assert picked.status == "sql"


@pytest.mark.parametrize("reply,expected", [
    ("2", "B"),
    (" 1 ", "A"),
    ("1, 3", "A; C"),
    ("1 and 3", "A; C"),
    ("2 2", "B"),
    ("4", None),                 # out of range: an ordinary answer, not a pick
    ("0", None),
    ("the second one", None),
    ("", None),
])
def test_expand_choice(reply, expected):
    from bqa.query import expand_choice
    assert expand_choice(reply, ["A", "B", "C"]) == expected
    assert expand_choice(reply, []) is None


def test_clarify_multi_flag_flows_through(assistant, tmp_path):
    d = parse_decision('{"status":"clarify","question":"Which?","options":["A","B"],"multi":true}')
    assert d.multi is True
    assert parse_decision('{"status":"clarify","question":"Which?","options":["A","B"]}').multi is False
    assert parse_decision('{"status":"clarify","question":"Which?","multi":true}').multi is False   # no options, no pick
    s = Settings(db_url=assistant.s.db_url, provider="mock", observations=False,
                 audit_log=str(tmp_path / "audit.jsonl"))
    a = Assistant(s, provider=MockProvider())
    out = a.ask("Compare the budgets")
    assert out.status == "clarify" and out.multi is True and "Kenya" in out.options
    assert json.loads(out.turn.assistant_content)["multi"] is True
    assert json.loads(open(s.audit_log, encoding="utf-8").readline())["multi"] is True
    single = a.ask("Budget for prevention")
    assert single.multi is False and "multi" not in json.loads(single.turn.assistant_content)
    both = a.ask("Compare the budgets", clarification="Kenya; Uganda", turns=[out.turn])
    assert both.status == "sql" and "Kenya" in both.sql and "Uganda" in both.sql


# ---------------------------------------------------------------- wrong-table columns and database errors

class Scripted(MockProvider):
    """Answers with the given SQL strings in turn, then falls back to the canned answers."""

    def __init__(self, *first):
        self.first, self.seen = list(first), []

    def complete(self, system, user, *, json_mode=False, history=None):
        self.seen.append(list(history or []))
        if self.first:
            return json.dumps({"status": "sql", "sql": self.first.pop(0)})
        return super().complete(system, user, json_mode=json_mode)


def test_column_from_the_wrong_table_is_sent_back(assistant):
    p = Scripted("SELECT b.region AS region, SUM(b.amount_usd) AS total_amount FROM budget_lines b GROUP BY b.region")
    out = Assistant(assistant.s, provider=p).ask("Total budget by region")
    assert out.status == "sql" and out.repairs == 1
    sent = p.seen[-1][-1]["content"]
    assert "[region]" in sent and "[geography]" in sent


def test_database_error_is_sent_back_once(assistant):
    p = Scripted("SELECT b.country AS country, nosuchfunc(b.amount_usd) AS total_amount FROM budget_lines b GROUP BY b.country")
    out = Assistant(assistant.s, provider=p).ask("Total budget by country")
    assert out.status == "sql" and out.repairs == 1
    sent = p.seen[-1][-1]["content"]
    assert sent.startswith("The database rejected that query") and "nosuchfunc" in sent.lower()


def test_persistent_database_error_is_shown_trimmed(assistant):
    bad = "SELECT b.country AS country, nosuchfunc(b.amount_usd) AS total_amount FROM budget_lines b GROUP BY b.country"
    p = Scripted(bad, bad, bad)
    out = Assistant(assistant.s, provider=p).ask("Total budget by country")
    assert out.status == "error" and out.repairs == assistant.s.guard_repair_attempts
    assert out.message.startswith("The database returned an error: no such function") and "SELECT" not in out.message
    assert out.sql                                              # kept, so the UI can show what did not run


def test_timeout_is_not_retried(assistant, monkeypatch):
    import bqa.query as q
    def boom(engine, sql):
        raise RuntimeError("Query timed out after 90 s")
    monkeypatch.setattr(q, "run_query", boom)
    out = Assistant(assistant.s, provider=MockProvider()).ask("Total budget by country")
    assert out.status == "error" and out.repairs == 0 and "timed out" in out.message


def test_pymssql_error_text_is_trimmed():
    from bqa.query import _db_error_text
    class Orig(Exception):
        pass
    class Wrapped(Exception):
        orig = Orig('(207, b"Invalid column name \'region_code\'.DB-Lib error message 20018, severity 16:\\nGeneral SQL Server error")')
    assert _db_error_text(Wrapped("long sqlalchemy text [SQL: ...]")) == "Invalid column name 'region_code'."
    assert _db_error_text(RuntimeError("no such column: x")) == "no such column: x"


def test_parse_decision_forgives_a_stray_backslash_and_raw_newlines():
    d = parse_decision('{"status":"sql","sql":"SELECT 1 )\\ AS gc7","note":"x"}')   # ')\ AS gc7': not a JSON escape
    assert d.status == "sql" and d.sql == "SELECT 1 ) AS gc7" and d.note == "x"
    d = parse_decision('{"status":"sql","sql":"SELECT 1\nFROM t"}')                 # a raw newline inside the string
    assert d.status == "sql" and d.sql == "SELECT 1\nFROM t"
    d = parse_decision('{"status":"sql","sql":"SELECT \\"x\\" FROM t\\\\z"}')       # valid escapes are kept
    assert d.sql == 'SELECT "x" FROM t\\z'


# ---------------------------------------------------------------- the audit record tells the whole story

def test_turn_record_carries_the_whole_story(assistant, tmp_path):
    s = Settings(db_url=assistant.s.db_url, provider="mock", observations=False,
                 audit_log=str(tmp_path / "audit.jsonl"))
    p = Scripted("SELECT b.region AS region, SUM(b.amount_usd) AS total_amount FROM budget_lines b GROUP BY b.region")
    a = Assistant(s, provider=p)
    first = a.ask("Total budget by region", conversation_id="c1", turn_no=1)          # one repair on the way
    asked = a.ask("Budget for prevention", turns=[first.turn], conversation_id="c1", turn_no=2)   # a clarify
    answered = a.ask("Budget for prevention", clarification=asked.options[0], turns=[first.turn, asked.turn],
                     conversation_id="c1", turn_no=3)
    assert first.status == "sql" and asked.status == "clarify" and answered.status == "sql"
    recs = [json.loads(l) for l in open(s.audit_log, encoding="utf-8")]
    assert [r["conversation"] for r in recs] == ["c1"] * 3 and [r["turn"] for r in recs] == [1, 2, 3]
    assert len(recs[0]["attempts"]) == 1 and "region" in recs[0]["attempts"][0]["sql"]
    assert "[region]" in recs[0]["attempts"][0]["problem"]              # why the guard sent it back
    assert recs[0]["raw"].startswith("{") and recs[0]["seconds"] >= 0    # the model's final words, and the cost
    assert "attempts" not in recs[1] and recs[1]["options"]              # nothing to repair on a clarify
    assert recs[2]["clarification"] == asked.options[0] and recs[2]["history"] == 2
    a.audit.feedback("Budget for prevention", answered.sql, True, "", conversation_id="c1")
    assert json.loads(open(s.audit_log, encoding="utf-8").readlines()[-1])["conversation"] == "c1"

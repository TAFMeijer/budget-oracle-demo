"""What a public, sign-in-less deployment must withstand: hostile text in a script, hostile
queries against the local file, and failures whose detail is nobody's business."""
import json
import time

from sqlalchemy import text

from bqa.config import Settings
from bqa.public import js_string, public_message
from bqa.query import Assistant, _db_error_is_repairable
from bqa.schema import make_engine
from tests.test_flow import Scripted, assistant  # noqa: F401  (fixture)


def test_js_string_cannot_close_the_script_element():
    hostile = 'Which one?</script><img src=x onerror="alert(1)"> '
    lit = js_string(hostile)
    assert "<" not in lit and ">" not in lit and " " not in lit
    assert json.loads(lit) == hostile                       # still the same string once parsed


def test_public_message_hides_the_detail():
    assert "not available" in public_message("error", "The model could not be reached: ConnectionError(...)")
    db = public_message("error", "The database returned an error: no such column: secret_col")
    assert "secret_col" not in db and "rephras" in db
    assert "safe query" in public_message("rejected", "The SQL guard rejected that query: Table(s) not allowed: x.")
    slow = "The query took longer than 15 seconds or too much memory and was stopped. Narrow the question"
    assert public_message("error", slow) == slow


def test_sqlite_connections_are_read_only_and_capped(assistant):  # noqa: F811
    eng = make_engine(assistant.s)
    with eng.connect() as conn:
        assert conn.execute(text("PRAGMA query_only")).scalar() == 1
        assert conn.execute(text("PRAGMA hard_heap_limit")).scalar() == assistant.s.sqlite_heap_limit_mb * 1024 * 1024


def test_runaway_query_is_stopped_at_the_deadline_and_not_retried(assistant, tmp_path):  # noqa: F811
    s = Settings(db_url=assistant.s.db_url, provider="mock", observations=False,
                 audit_log=str(tmp_path / "audit.jsonl"), db_query_timeout=1)
    runaway = ("SELECT a.country AS country, SUM(b.amount_usd) AS total_amount "
               "FROM budget_lines a, budget_lines b, budget_lines c GROUP BY a.country")
    p = Scripted(runaway)
    t = time.time()
    out = Assistant(s, provider=p).ask("Total budget by country")
    assert time.time() - t < 15
    assert out.status == "error" and out.repairs == 0
    assert "took longer than 1 seconds" in out.message and "SELECT" not in out.message
    assert not _db_error_is_repairable("interrupted") and not _db_error_is_repairable(out.message)

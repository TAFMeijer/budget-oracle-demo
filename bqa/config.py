"""Runtime settings, all from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = f"sqlite:///file:{(ROOT / 'data' / 'demo.sqlite').as_posix()}?mode=ro&uri=true"


def _env_list(name: str, default: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


@dataclass
class Settings:
    # --- database (read-only connection strongly recommended) ---
    db_url: str = os.getenv("DB_URL", DEFAULT_DB)
    dialect: str = os.getenv("SQL_DIALECT", "")          # sqlite | tsql | postgres ... (derived from db_url if empty)
    db_query_timeout: int = int(os.getenv("DB_QUERY_TIMEOUT", "90"))  # seconds; kills runaway queries server-side
    schema_cache_ttl: int = int(os.getenv("SCHEMA_CACHE_TTL", "0"))   # seconds to reuse the introspected schema from disk (0 = off)
    # Serve a cache older than the TTL rather than making a user wait for introspection.
    # Leave this on and refresh the cache from a scheduled job (see refresh_schema.py).
    schema_cache_serve_stale: bool = os.getenv("SCHEMA_CACHE_SERVE_STALE", "true").lower() in ("1", "true", "yes")
    allowed_tables: list[str] = field(default_factory=lambda: _env_list("ALLOWED_TABLES", "budget_lines,geography"))
    hierarchies: list[tuple[str, str]] = field(default_factory=lambda: [
        tuple(h.split(">")) for h in _env_list("HIERARCHIES", "module>intervention,cost_category>cost_input")
    ])
    valid_values_max: int = int(os.getenv("VALID_VALUES_MAX", "80"))   # list distinct values for text columns up to this cardinality
    # Columns never to list values for, however few they have: "table:colA,colB;table2:colC".
    # For identifiers and free text a list is noise on every question. Note the trade-off: the
    # guard can only check filter values for columns whose list is complete, so an excluded
    # column is one the model may invent values for unchecked.
    exclude_values: dict[str, list[str]] = field(default_factory=lambda: {
        t.split(":")[0].strip(): [c.strip() for c in t.split(":", 1)[1].split(",") if c.strip()]
        for t in os.getenv("VALID_VALUES_EXCLUDE", "").split(";") if ":" in t
    })
    # optional column allow-list per table: "table1:colA,colB;table2:colC" (empty = every column)
    allowed_columns: dict[str, list[str]] = field(default_factory=lambda: {
        t.split(":")[0].strip(): [c.strip() for c in t.split(":", 1)[1].split(",") if c.strip()]
        for t in os.getenv("ALLOWED_COLUMNS", "").split(";") if ":" in t
    })
    prompt_notes: str = os.getenv("PROMPT_NOTES", "")    # site-specific guidance appended to the system prompt (text or a file path)
    # What the sidebar tells users they can ask, and the starter questions on an empty screen.
    # Both are deployment-specific: keep them out of the code so a site can describe its own data.
    user_guide: str = os.getenv("USER_GUIDE", "")        # markdown shown in the sidebar (text or a file path)
    example_questions: str = os.getenv("EXAMPLE_QUESTIONS", "")  # one per line (text or a file path)
    row_limit: int = int(os.getenv("ROW_LIMIT", "500"))
    # The guard checks the model's filter values against the real ones and hands back any it
    # invented, so the model can correct itself before the query ever reaches the database.
    check_values: bool = os.getenv("CHECK_VALUES", "true").lower() in ("1", "true", "yes")
    guard_repair_attempts: int = int(os.getenv("GUARD_REPAIR_ATTEMPTS", "2"))  # 0 = never retry
    # Filters a query must carry to read a table: "table:colA,colB;table2:colC". Every SELECT that
    # reads the table (each branch of a UNION on its own) must compare each listed column in its
    # WHERE. Written for a table holding several funding cycles: a branch that forgets the cycle
    # filter returns every cycle added together, and nothing in the result says so. With this set
    # it is a guard rejection the model repairs instead.
    required_filters: dict[str, list[str]] = field(default_factory=lambda: {
        t.split(":")[0].strip(): [c.strip() for c in t.split(":", 1)[1].split(",") if c.strip()]
        for t in os.getenv("REQUIRED_FILTERS", "").split(";") if ":" in t
    })
    # ---- public exposure -------------------------------------------------------------------
    # With no sign-in in front of the app, anyone can make the machine work. These bound what one
    # visitor, everyone together, one question and one query may cost. 0 = no limit.
    app_title: str = os.getenv("APP_TITLE", "Budget Query Assistant")
    show_sql: bool = os.getenv("SHOW_SQL", "true").lower() in ("1", "true", "yes")  # false: no SQL, internals or raw errors in the UI
    max_question_chars: int = int(os.getenv("MAX_QUESTION_CHARS", "2000"))
    questions_per_ip: int = int(os.getenv("QUESTIONS_PER_IP", "0"))              # per QUESTIONS_WINDOW, per client address
    questions_global: int = int(os.getenv("QUESTIONS_GLOBAL", "0"))              # per QUESTIONS_WINDOW, everyone together
    questions_window: int = int(os.getenv("QUESTIONS_WINDOW", "3600"))           # seconds
    max_concurrent_questions: int = int(os.getenv("MAX_CONCURRENT_QUESTIONS", "0"))
    sqlite_heap_limit_mb: int = int(os.getenv("SQLITE_HEAP_LIMIT_MB", "512"))    # hard memory cap per SQLite connection

    # --- language model ---
    provider: str = os.getenv("LLM_PROVIDER", "ollama")     # ollama | openai | mock
    model: str = os.getenv("LLM_MODEL", "")
    ollama_url: str = os.getenv("OLLAMA_URL", "http://localhost:11434")
    llm_timeout: int = int(os.getenv("LLM_TIMEOUT", "300"))   # seconds; thinking models can reason long on ambiguous questions
    llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "2000"))  # a decision JSON is short; caps runaway generation
    llm_reasoning_effort: str = os.getenv("LLM_REASONING_EFFORT", "")  # openai provider: none|minimal|low|... ('' = server default)
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    observations: bool = os.getenv("ENABLE_OBSERVATIONS", "true").lower() in ("1", "true", "yes")
    observations_model: str = os.getenv("OBSERVATIONS_MODEL", "")

    # --- conversation memory ---
    # Earlier turns of the same conversation are replayed to the model, so follow-ups
    # ("now split that by module") work. History lives in the browser session, never on the
    # Assistant: two people using the app at the same time never see each other's turns.
    remember_conversation: bool = os.getenv("CONVERSATION_HISTORY", "true").lower() in ("1", "true", "yes")
    context_window: int = int(os.getenv("CONTEXT_WINDOW", "8192"))     # MUST match the context length the model server is loaded with
    context_reserve: int = int(os.getenv("CONTEXT_RESERVE", "0"))      # tokens kept free for the answer (0 = LLM_MAX_TOKENS)
    chars_per_token: float = float(os.getenv("CHARS_PER_TOKEN", "4.0"))  # heuristic behind the context meter

    # --- governance ---
    audit_log: str = os.getenv("AUDIT_LOG", str(ROOT / "logs" / "audit.jsonl"))
    # access control (empty = open): comma-separated emails, or the path of a file with one email per line
    allowed_emails: str = os.getenv("ALLOWED_EMAILS", "")
    access_code: str = os.getenv("ACCESS_CODE", "")     # optional shared code asked for next to the email
    # Brute-force protection for the sign-in form (see bqa/ratelimit.py). Failures are counted
    # per client IP and per e-mail; reaching LOGIN_MAX_FAILURES within LOGIN_WINDOW seconds
    # locks that IP / e-mail for LOGIN_LOCKOUT seconds, doubling on each repeat up to
    # LOGIN_MAX_LOCKOUT. LOGIN_GLOBAL_MAX_FAILURES from everyone combined locks the form for all.
    login_max_failures: int = int(os.getenv("LOGIN_MAX_FAILURES", "5"))
    login_window: int = int(os.getenv("LOGIN_WINDOW", "600"))
    login_lockout: int = int(os.getenv("LOGIN_LOCKOUT", "60"))
    login_max_lockout: int = int(os.getenv("LOGIN_MAX_LOCKOUT", "3600"))
    login_global_max_failures: int = int(os.getenv("LOGIN_GLOBAL_MAX_FAILURES", "30"))
    login_global_lockout: int = int(os.getenv("LOGIN_GLOBAL_LOCKOUT", "300"))
    # Staying signed in across page refreshes: a signed cookie that lives SESSION_DAYS days.
    # SESSION_SECRET signs it; leave it empty and a random secret is kept in logs/session_secret.
    session_days: int = int(os.getenv("SESSION_DAYS", "7"))
    session_secret: str = os.getenv("SESSION_SECRET", "")

    def load_allowed_emails(self) -> set[str] | None:
        """None = access control off; otherwise the normalised allowlist."""
        raw = self.allowed_emails.strip()
        if not raw:
            return None
        p = Path(raw)
        if p.is_file():
            lines = p.read_text(encoding="utf-8").splitlines()
            entries = [ln.split("#")[0] for ln in lines]
        else:
            entries = raw.split(",")
        return {e.strip().lower() for e in entries if e.strip()}

    def __post_init__(self) -> None:
        if not self.dialect:
            scheme = self.db_url.split(":", 1)[0].lower()
            self.dialect = {"sqlite": "sqlite", "mssql": "tsql", "mssql+pymssql": "tsql",
                            "mssql+pyodbc": "tsql", "postgresql": "postgres", "postgres": "postgres"}.get(scheme, "sqlite")
        if not self.model:
            self.model = {"ollama": "qwen3.6:27b", "openai": "gpt-4o-mini", "mock": "mock"}.get(self.provider, "")
        if not self.observations_model:
            self.observations_model = self.model
        if not self.context_reserve:
            self.context_reserve = self.llm_max_tokens
        for attr in ("prompt_notes", "user_guide", "example_questions"):
            try:                                 # each may name a file instead of holding inline text
                value = getattr(self, attr).strip()
                p = Path(value)
                if value and p.is_file():
                    setattr(self, attr, p.read_text(encoding="utf-8").strip())
            except OSError:
                pass

    def examples(self) -> list[str]:
        """Starter questions for the empty screen, one per line."""
        return [q.strip() for q in self.example_questions.splitlines() if q.strip()]


settings = Settings()

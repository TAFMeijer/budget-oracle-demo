"""Builds the schema context the model sees: tables, columns, valid values, hierarchies.

Everything is introspected from the database itself, so there is no hand-maintained
schema file to drift out of date — and nothing about the schema lives in the code base.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine

from .config import Settings

# Introspection can run for minutes against a remote database. A caller that wants to show its
# progress (refresh_schema.py does) sets this to a print-like callable; the app leaves it None.
# It is a module global rather than an argument so that load_schema stays lru_cache-able.
progress = None


def _say(message: str) -> None:
    if progress is not None:
        progress(message)


@dataclass
class TableInfo:
    name: str
    columns: list[tuple[str, str]]                      # (name, type)
    valid_values: dict[str, list[str]] = field(default_factory=dict)
    row_count: int = 0


@dataclass
class SchemaContext:
    tables: list[TableInfo]
    hierarchies: dict[str, list[tuple[str, str]]]      # "module>intervention" -> [(module, intervention), ...]

    def as_prompt(self) -> str:
        out: list[str] = []
        for t in self.tables:
            out.append(f"### Table: {t.name}  ({t.row_count:,} rows)")
            for c, ty in t.columns:
                out.append(f"  - {c} ({ty})")
            for col, vals in t.valid_values.items():
                out.append(f"  Valid values of {t.name}.{col}: " + " | ".join(vals))
            out.append("")
        for key, pairs in self.hierarchies.items():
            parent, child = key.split(">")
            out.append(f"### Hierarchy: each {child} belongs to exactly one {parent}")
            # Grouped under the parent rather than one pair per line. A flat list repeats the
            # parent name once per child — 'RSSH: Health products management systems' ten times
            # over — which on this schema was 70% of everything the model reads. Same
            # information, a third fewer characters, and the children of one parent now sit
            # together where they are easier to find.
            grouped: dict[str, list[str]] = {}
            for p, c in pairs:
                grouped.setdefault(p, []).append(c)
            for p, children in grouped.items():
                out.append(f"  {p}")
                out.extend(f"    - {c}" for c in children)
            out.append("")
        return "\n".join(out)


def make_engine(settings: Settings) -> Engine:
    kwargs = {}
    if settings.db_url.startswith("mssql+pymssql"):
        kwargs["connect_args"] = {"timeout": settings.db_query_timeout, "login_timeout": 15}
    engine = create_engine(settings.db_url, pool_pre_ping=True, **kwargs)
    if engine.dialect.name == "sqlite":
        _limit_sqlite(engine, settings.db_query_timeout, settings.sqlite_heap_limit_mb)
    return engine


def _limit_sqlite(engine: Engine, seconds: int, heap_mb: int) -> None:
    """SQLite has no statement timeout and no memory quota of its own, and the guard cannot
    tell a slow-but-fair query from a self cross-join of a 170k-row table. So every connection
    is read-only twice over (query_only on top of the URI's mode=ro), capped in memory, and
    every statement gets a deadline: SQLite calls the progress handler every few thousand VM
    steps, and a non-zero return interrupts the statement ("interrupted"). The handler is
    re-armed before each statement and left in place, because pandas fetches the rows after
    execute() returns and that is where the work happens.
    """
    import time as _time

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):            # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA query_only = ON")
        if heap_mb:
            cur.execute(f"PRAGMA hard_heap_limit = {int(heap_mb) * 1024 * 1024}")
        cur.close()

    @event.listens_for(engine, "before_cursor_execute")
    def _arm(conn, cursor, statement, parameters, context, executemany):   # noqa: ANN001
        deadline = _time.monotonic() + max(1, seconds)
        conn.connection.driver_connection.set_progress_handler(
            lambda: 1 if _time.monotonic() > deadline else 0, 10_000)


def _is_text(type_name: str) -> bool:
    t = type_name.lower()
    return any(k in t for k in ("char", "text", "string", "clob"))


@lru_cache(maxsize=4)
def load_schema(db_url: str, allowed: tuple[str, ...], hierarchies: tuple[str, ...], valid_values_max: int,
                allowed_columns: tuple[str, ...] = (), exclude_values: tuple[str, ...] = ()) -> SchemaContext:
    col_filter = {t.split(":")[0]: t.split(":", 1)[1].split(",") for t in allowed_columns}
    # Columns whose values are deliberately not listed: identifiers and free text where a list
    # would be noise the model has to read past on every question.
    no_values = {(t.split(":")[0], c.strip())
                 for t in exclude_values for c in t.split(":", 1)[1].split(",") if c.strip()}
    _say("connecting…")
    engine = create_engine(db_url)
    insp = inspect(engine)
    tables: list[TableInfo] = []
    hier: dict[str, list[tuple[str, str]]] = {}
    with engine.connect() as conn:
        for name in allowed:
            if name not in insp.get_table_names():
                raise ValueError(f"Allowed table '{name}' does not exist in the database")
            _say(f"{name}: reading columns…")
            cols = [(c["name"], str(c["type"])) for c in insp.get_columns(name)]
            if name in col_filter:
                keep = col_filter[name]
                missing = [k for k in keep if k not in [c for c, _ in cols]]
                if missing:
                    raise ValueError(f"ALLOWED_COLUMNS for '{name}' not in table: {missing}")
                cols = [(c, ty) for c, ty in cols if c in keep]
            info = TableInfo(name=name, columns=cols)
            info.row_count = conn.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar_one()
            _say(f"{name}: {info.row_count:,} rows, {len(cols)} columns")
            text_cols = [(c, ty) for c, ty in cols if _is_text(ty) and (name, c) not in no_values]
            for i, (c, ty) in enumerate(text_cols, 1):
                _say(f"{name}: [{i}/{len(text_cols)}] counting distinct values of {c}…")
                n = conn.execute(text(f'SELECT COUNT(DISTINCT "{c}") FROM "{name}"')).scalar_one()
                if n <= valid_values_max:
                    vals = conn.execute(text(f'SELECT DISTINCT "{c}" FROM "{name}" WHERE "{c}" IS NOT NULL ORDER BY 1')).scalars().all()
                    info.valid_values[c] = [str(v) for v in vals]
                    _say(f"{name}: {c} — {n} values listed")
                else:
                    _say(f"{name}: {c} — {n:,} distinct, too many to list")
            tables.append(info)
        for h in hierarchies:
            parent, child = h.split(">")
            for t in tables:
                names = [c for c, _ in t.columns]
                if parent in names and child in names:
                    _say(f"hierarchy {parent} > {child}…")
                    rows = conn.execute(text(
                        f'SELECT DISTINCT "{parent}", "{child}" FROM "{t.name}" ORDER BY 1, 2')).all()
                    hier[h] = [(str(a), str(b)) for a, b in rows]
                    break
    return SchemaContext(tables=tables, hierarchies=hier)


def _cache_args(settings: Settings) -> tuple:
    args = (settings.db_url, tuple(settings.allowed_tables),
            tuple(">".join(h) for h in settings.hierarchies), settings.valid_values_max,
            tuple(f"{t}:{','.join(cs)}" for t, cs in sorted(settings.allowed_columns.items())))
    if not settings.exclude_values:
        return args           # keeps the cache key stable for deployments that exclude nothing
    return args + (tuple(f"{t}:{','.join(cs)}" for t, cs in sorted(settings.exclude_values.items())),)


def schema_cache_path(settings: Settings) -> Path:
    """Where this exact configuration's cached schema lives. Change a table or a column
    allow-list and the path changes with it, so a stale cache is never mistaken for a fresh one."""
    import hashlib
    from .config import ROOT
    return ROOT / "logs" / f"schema_{hashlib.sha256(repr(_cache_args(settings)).encode()).hexdigest()[:16]}.pkl"


def schema_cache_age(settings: Settings) -> float | None:
    """Seconds since the cache was written, or None if there is no cache."""
    import time
    p = schema_cache_path(settings)
    try:
        return time.time() - p.stat().st_mtime if p.is_file() else None
    except OSError:
        return None


def schema_for(settings: Settings, force_refresh: bool = False) -> SchemaContext:
    """The schema the model is shown.

    Introspecting a large remote database takes minutes, so nobody should ever wait for it
    inside a request. A scheduled job calls this with force_refresh=True; the app reads
    whatever that job last wrote. With SCHEMA_CACHE_SERVE_STALE on (the default) an old cache
    is still served rather than blocking a user — a schema a day out of date is a far smaller
    problem than a question that hangs for two minutes. Only a missing cache forces the app
    to introspect for itself.
    """
    import pickle
    import time
    args = _cache_args(settings)
    if settings.schema_cache_ttl <= 0 and not force_refresh:
        return load_schema(*args)
    cache = schema_cache_path(settings)
    if not force_refresh:
        try:
            if cache.is_file():
                fresh = time.time() - cache.stat().st_mtime < settings.schema_cache_ttl
                if fresh or settings.schema_cache_serve_stale:
                    with cache.open("rb") as f:
                        return pickle.load(f)
        except (OSError, pickle.PickleError):
            pass
    else:
        getattr(load_schema, "cache_clear", lambda: None)()   # a forced refresh must hit the database, not the lru_cache
    ctx = load_schema(*args)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(".pkl.tmp")
        with tmp.open("wb") as f:          # write-then-rename: the app never reads a half-written cache
            pickle.dump(ctx, f)
        tmp.replace(cache)
    except OSError:
        pass
    return ctx


def run_query(engine: Engine, sql: str) -> pd.DataFrame:
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn)

"""Rebuild the cached database schema so no user ever waits for introspection.

Reading a large remote schema — every column, plus the distinct values of every
low-cardinality one — takes minutes. Run this from a scheduled job (see
scripts/README.md) and the app only ever reads the file it leaves behind.

    python refresh_schema.py            # refresh if the cache is older than SCHEMA_CACHE_TTL
    python refresh_schema.py --force    # refresh regardless of age
    python refresh_schema.py --columns  # also list every column of every allowed table

Exit codes: 0 refreshed or still fresh, 1 failed (the old cache is left untouched).
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

import bqa.schema
from bqa.config import settings
from bqa.schema import schema_cache_age, schema_cache_path, schema_for


def main(argv: list[str]) -> int:
    force = "--force" in argv
    age = schema_cache_age(settings)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not force and age is not None and age < settings.schema_cache_ttl:
        print(f"{stamp} cache is {age / 3600:.1f}h old, under the {settings.schema_cache_ttl / 3600:.1f}h "
              f"TTL — nothing to do (use --force to refresh anyway)")
        return 0

    t0 = time.time()
    # Trace every step: a silent process that runs for minutes is indistinguishable from a hung one.
    bqa.schema.progress = lambda m: print(f"    [{time.time() - t0:5.0f}s] {m}", flush=True)
    print(f"{stamp} introspecting {len(settings.allowed_tables)} tables — this takes minutes", flush=True)
    try:
        ctx = schema_for(settings, force_refresh=True)
    except Exception as e:  # noqa: BLE001 - a failed refresh must not delete a working cache
        print(f"{stamp} REFRESH FAILED after {time.time() - t0:.0f}s: {e.__class__.__name__}: {e}",
              file=sys.stderr)
        print(f"{stamp} the previous cache is untouched and will still be served", file=sys.stderr)
        return 1

    path = schema_cache_path(settings)
    print(f"{stamp} refreshed in {time.time() - t0:.0f}s -> {path}")
    for t in ctx.tables:
        listed = sum(len(v) for v in t.valid_values.values())
        print(f"    {t.name}: {t.row_count:,} rows, {len(t.columns)} columns, "
              f"{len(t.valid_values)} columns with listed values ({listed:,} values)")
        if "--columns" in argv:
            for c, ty in t.columns:
                n = len(t.valid_values.get(c, []))
                print(f"        - {c} ({ty})" + (f"  [{n} values]" if n else ""))
    print(f"    prompt size: {len(ctx.as_prompt()):,} characters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Build data/gf_budgets.sqlite from the Global Fund's public Data Service API (OData v4.2).

Public grant budgets — no key needed: https://data-service.theglobalfund.org/api
The same data is offered as a CSV on https://data-service.theglobalfund.org/downloads
("Grant Budgets - Reference Rate"); this script reads it from the API and resolves the ids.

One row per implementation period x module/intervention x cost category x year, in US$ at the
Global Fund reference rate, latest version of each budget only. Amounts are budgets — what a
grant plans to spend — not expenditure or disbursements.

Two columns say what a line is for. `component` is the budget line's own, taken from the module
it belongs to in the modular framework (HIV, Tuberculosis, Malaria, RSSH, Multicomponent for
program management and payment for results). `grant_type` is the grant's ('HIV grant',
'TB/HIV grant', …): health-system modules sit mostly inside disease grants, so the two differ,
and they carry different values on purpose so that one is never mistaken for the other.

    python data/fetch_gf_budgets.py          # about a minute; writes data/gf_budgets.sqlite

Then run the app against it:

    DB_URL="sqlite:///file:data/gf_budgets.sqlite?mode=ro&uri=true" ALLOWED_TABLES=grant_budgets \
    HIERARCHIES="module>intervention" REQUIRED_FILTERS="grant_budgets:funding_cycle|year" streamlit run app.py
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import requests

BASE = "https://fetch.theglobalfund.org/v4.2/odata"
OUT = Path(__file__).resolve().parent / "gf_budgets.sqlite"
PAGE = 5000
FIN = ("FinancialIndicators(indicatorName='Budget - Reference Rate',"
       "financialDatasetName='GrantBudget_ReferenceRate')")
CYCLES = ["2017-2019", "2020-2022", "2023-2025", "2026-2028"]


def get(path: str, **params) -> dict:
    for attempt in range(4):
        try:
            r = requests.get(f"{BASE}/{path}", params=params, timeout=180)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:  # noqa: PERF203
            if attempt == 3:
                raise
            print(f"  retry {attempt + 1} after {e.__class__.__name__}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    raise RuntimeError("unreachable")


def get_all(path: str, **params) -> list[dict]:
    rows, skip = [], 0
    while True:
        batch = get(path, **{"$top": PAGE, "$skip": skip, **params})["value"]
        rows.extend(batch)
        print(f"  {path.split('(')[0]}: {len(rows):,} rows", end="\r")
        if len(batch) < PAGE:
            print()
            return rows
        skip += PAGE


def cycle_of(start: str) -> str:
    """The allocation period a grant belongs to, derived from when its implementation period
    starts: grants of the 2017-2019 allocation start in 2018 (some later), those of 2020-2022
    in 2021, and so on. The API publishes no cycle for a budget line, so this is an inference."""
    try:
        year = int(start[:4])
    except ValueError:
        return "Unknown"
    return CYCLES[min(len(CYCLES) - 1, max(0, (year - 2018) // 3))]


def main() -> None:
    print("Geographies…")
    geo = {g["id"]: g for g in get_all("Geographies")}

    def region_of(geo_id: str | None) -> str:
        seen = set()
        while geo_id and geo_id in geo and geo_id not in seen:
            seen.add(geo_id)
            g = geo[geo_id]
            if g.get("level") == "Sub-continent":   # e.g. "Eastern Africa"
                return g["name"]
            geo_id = g.get("parentId")
        return ""

    print("ActivityAreas…")
    areas = {a["id"]: a for a in get_all("ActivityAreas")}

    def module_of(area_id: str | None) -> tuple[str, str, str]:
        """(component, module, intervention) of a budget line, from the modular-framework tree."""
        # some (mostly 2017-2022) area ids are not published in the ActivityAreas feed
        a = areas.get(area_id)
        if not a:
            return "Unspecified", "Unspecified", "Unspecified"
        intervention = "Unspecified"
        if a["type"] != "Module":
            intervention = a["name"]
            seen = set()
            a = areas.get(a.get("parentId"))
            while a and a["type"] != "Module" and a["id"] not in seen:   # climb past groups
                seen.add(a["id"])
                a = areas.get(a.get("parentId"))
        if not a:
            return "Unspecified", "Unspecified", intervention
        parent = areas.get(a.get("parentId")) or {}
        component = parent["name"] if parent.get("type") == "Component" else "Unspecified"
        return component, a["name"], intervention

    print("FinancialCategories…")
    cats = get_all("FinancialCategories")
    by_hierarchy = {c["hierarchyId"]: c for c in cats}     # budget lines point at hierarchyId, not id
    by_id = {c["id"]: c for c in cats}

    def cost_of(hierarchy_id: str | None) -> tuple[str, str]:
        c = by_hierarchy.get(hierarchy_id)
        if not c:
            return "Unspecified", "Unspecified"
        parent = by_id.get(c.get("parentId")) or {}
        return c["name"], parent.get("name") or "Unspecified"

    print("Grants + implementation periods…")
    ips: dict[str, tuple[dict, dict]] = {}
    for g in get_all("Grants", **{"$select": "id,code,geographyId,activityAreaId",
                                  "$expand": "implementationPeriods($select=id,code,periodStartDate,periodEndDate)"}):
        for ip in g.get("implementationPeriods") or []:
            ips[ip["id"]] = (g, ip)

    print("Budget lines…")
    fin = get_all(FIN, **{"$filter": "isLatestReported eq true",
                          "$select": "recordId,activityAreaId,financialCategoryId,periodFrom,plannedAmount"})

    lines, missing = [], 0
    for f in fin:
        if not f.get("plannedAmount"):
            continue
        hit = ips.get(f["recordId"])
        if hit is None:
            missing += 1
            continue
        grant, ip = hit
        component, module, intervention = module_of(f["activityAreaId"])
        cost_category, cost_group = cost_of(f.get("financialCategoryId"))
        start = (ip.get("periodStartDate") or "")[:10]
        lines.append((grant["code"], ip.get("code") or "", start, (ip.get("periodEndDate") or "")[:10], cycle_of(start),
                      geo.get(grant.get("geographyId"), {}).get("name", "") or "Unspecified",
                      region_of(grant.get("geographyId")) or "Multicountry/Other",
                      (areas.get(grant.get("activityAreaId"), {}).get("name", "") or "Unspecified") + " grant",
                      component, module, intervention, cost_group, cost_category,
                      int(float(f["periodFrom"])) if f.get("periodFrom") else None, f["plannedAmount"]))
    if missing:
        print(f"  note: {missing} rows dropped (implementation period not in the Grants feed)")

    OUT.unlink(missing_ok=True)
    con = sqlite3.connect(OUT)
    con.executescript("""
        CREATE TABLE grant_budgets (
            grant_code TEXT, implementation_period TEXT, period_start TEXT, period_end TEXT,
            funding_cycle TEXT, country TEXT, region TEXT, grant_type TEXT, component TEXT,
            module TEXT, intervention TEXT, cost_group TEXT, cost_category TEXT,
            year INTEGER, budget_usd REAL);
        CREATE INDEX ix_gb_country ON grant_budgets(country);
        CREATE INDEX ix_gb_module ON grant_budgets(module);
        CREATE INDEX ix_gb_year ON grant_budgets(year);
    """)
    con.executemany("INSERT INTO grant_budgets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", lines)
    con.commit()
    n_c, n_g, total, y0, y1 = con.execute(
        "SELECT COUNT(DISTINCT country), COUNT(DISTINCT grant_code), SUM(budget_usd), MIN(year), MAX(year) "
        "FROM grant_budgets").fetchone()
    con.close()
    print(f"Wrote {OUT.name}: {len(lines):,} lines, {n_g} grants, {n_c} countries, {y0}-{y1}, "
          f"US$ {total / 1e9:.1f}bn budgeted")


if __name__ == "__main__":
    main()

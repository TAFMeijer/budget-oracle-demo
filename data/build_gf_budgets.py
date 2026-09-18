"""Build data/gf_budgets.sqlite from the Global Fund's published "Grant Budgets - Reference
Rate" data set.

    python data/build_gf_budgets.py [path/to/grant_budgets_reference_rate_YYYYMDD.csv]

Get the CSV from https://data-service.theglobalfund.org/downloads (Data Set tab, "Grant Budgets -
Reference Rate"). With no argument the newest grant_budgets_reference_rate_*.csv in data/ or
~/Downloads is used. The Global Fund generates that file from its public API
(https://data-service.theglobalfund.org/api, data set GrantBudget_ReferenceRate). The one thing
added here is the Global Fund grant-management region of each country (WCA, HIA1, HIA2,
MENASEA, EECA, LAC, Asia), from data/gf_regions.csv — the published file has no such column.

One row per implementation period x module/intervention x cost category x year, in US$ at the
Global Fund reference rate. Amounts are budgets — what a grant plans to spend — not expenditure
or disbursements. Columns get plain names; empty module fields become 'Unspecified' and the
grant's component becomes `grant_type` ('HIV grant', …) so that it is never mistaken for the
budget line's own `component`, which comes from its module.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / "gf_budgets.sqlite"
REGIONS = HERE / "gf_regions.csv"

COLUMNS = {                       # CSV header -> column here (the file's headers end in a stray "1")
    "GeographyName1": "country", "Continent1": "continent", "SubContinent1": "subcontinent",
    "Grant1": "grant_code", "ImplementationPeriod1": "implementation_period",
    "ImplementationPeriodStartDate1": "period_start", "ImplementationPeriodEndDate1": "period_end",
    "Status1": "status", "GrantCycleName": "grant_cycle", "GrantComponent1": "grant_type",
    "PrincipalRecipient1": "principal_recipient", "PrincipalRecipientType1": "pr_type",
    "PrincipalRecipientSubType1": "pr_subtype", "LeadImplementer1": "lead_implementer",
    "ModuleComponent1": "component", "Module1": "module", "Intervention1": "intervention",
    "InvestmentLandscape_Level11": "investment_landscape_1", "InvestmentLandscape_Level21": "investment_landscape_2",
    "CostCategory1": "cost_category", "BudgetYearFrom1": "year", "BudgetAmount_ReferenceRate": "budget_usd",
}
ORDER = ["country", "continent", "subcontinent", "gf_region", "grant_code", "implementation_period",
         "period_start", "period_end", "status", "grant_cycle", "grant_type", "principal_recipient",
         "pr_type", "pr_subtype", "lead_implementer", "component", "module", "intervention",
         "investment_landscape_1", "investment_landscape_2", "cost_category", "year", "budget_usd"]


def find_csv() -> Path:
    found = sorted([*HERE.glob("grant_budgets_reference_rate_*.csv"),
                    *(Path.home() / "Downloads").glob("grant_budgets_reference_rate_*.csv")],
                   key=lambda p: p.stat().st_mtime)
    if not found:
        sys.exit("No grant_budgets_reference_rate_*.csv in data/ or ~/Downloads — download it from "
                 "https://data-service.theglobalfund.org/downloads (Data Set tab) or pass its path.")
    return found[-1]


def gf_regions() -> dict[str, str]:
    """Country -> Global Fund grant-management region, from the mapping file next to this script."""
    if not REGIONS.exists():
        print(f"  {REGIONS.name} not found; gf_region will be 'Unspecified'")
        return {}
    table = pd.read_csv(REGIONS)
    return dict(zip(table["country"], table["gf_region"]))


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else find_csv()
    print(f"Reading {src} …")
    df = pd.read_csv(src, low_memory=False)
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        sys.exit(f"Unexpected file layout — missing columns: {missing}")
    df = df[list(COLUMNS)].rename(columns=COLUMNS)
    df = df[df["budget_usd"].fillna(0) != 0]
    for col in ("period_start", "period_end"):
        df[col] = pd.to_datetime(df[col], format="%d-%b-%Y", errors="coerce").dt.strftime("%Y-%m-%d")
    df["year"] = df["year"].astype(float).astype(int)
    df["grant_type"] = df["grant_type"].astype(str) + " grant"
    for col in ("component", "module", "intervention"):
        df[col] = df[col].fillna("Unspecified")
    for col in ("continent", "subcontinent"):                       # multicountry grants have neither
        df[col] = df[col].fillna("Multicountry")
    regions = gf_regions()
    df["gf_region"] = df["country"].map(regions).fillna("Unspecified")
    df = df[ORDER]

    OUT.unlink(missing_ok=True)
    con = sqlite3.connect(OUT)
    df.to_sql("grant_budgets", con, index=False)
    con.executescript("""
        CREATE INDEX ix_gb_country ON grant_budgets(country);
        CREATE INDEX ix_gb_module ON grant_budgets(module);
        CREATE INDEX ix_gb_cycle ON grant_budgets(grant_cycle);
        CREATE INDEX ix_gb_year ON grant_budgets(year);
    """)
    n_c, n_g, total, y0, y1 = con.execute(
        "SELECT COUNT(DISTINCT country), COUNT(DISTINCT grant_code), SUM(budget_usd), MIN(year), MAX(year) "
        "FROM grant_budgets").fetchone()
    con.close()
    print(f"Wrote {OUT.name}: {len(df):,} lines, {n_g} grants, {n_c} countries, {y0}-{y1}, "
          f"US$ {total / 1e9:.1f}bn budgeted")


if __name__ == "__main__":
    main()

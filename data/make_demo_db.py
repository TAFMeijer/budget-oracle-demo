"""Generate the synthetic demo database (data/demo.sqlite).

Every amount is random. Only the *names* are realistic: countries and regions are real, and
modules, interventions and cost categories follow the vocabulary of the Global Fund's public
modular framework and cost groupings, so the demo feels like a real grant-budget database
without containing a single real budget line.

    python data/make_demo_db.py            # deterministic (seed 7)
"""
from __future__ import annotations

import random
import sqlite3
from pathlib import Path

SEED = 7
OUT = Path(__file__).resolve().parent / "demo.sqlite"

GEOGRAPHY = [  # country, region, income group
    ("Kenya", "Eastern Africa", "Lower-middle income"), ("Uganda", "Eastern Africa", "Low income"),
    ("Tanzania", "Eastern Africa", "Lower-middle income"), ("Ethiopia", "Eastern Africa", "Low income"),
    ("Rwanda", "Eastern Africa", "Low income"), ("Malawi", "Southern Africa", "Low income"),
    ("Zambia", "Southern Africa", "Lower-middle income"), ("Mozambique", "Southern Africa", "Low income"),
    ("Zimbabwe", "Southern Africa", "Lower-middle income"), ("South Africa", "Southern Africa", "Upper-middle income"),
    ("Nigeria", "Western & Central Africa", "Lower-middle income"), ("Ghana", "Western & Central Africa", "Lower-middle income"),
    ("Senegal", "Western & Central Africa", "Lower-middle income"), ("Cameroon", "Western & Central Africa", "Lower-middle income"),
    ("Côte d'Ivoire", "Western & Central Africa", "Lower-middle income"), ("DR Congo", "Western & Central Africa", "Low income"),
    ("Ukraine", "Eastern Europe & Central Asia", "Upper-middle income"), ("Kyrgyzstan", "Eastern Europe & Central Asia", "Lower-middle income"),
    ("Viet Nam", "Asia", "Lower-middle income"), ("Indonesia", "Asia", "Upper-middle income"),
    ("Philippines", "Asia", "Lower-middle income"), ("Bangladesh", "Asia", "Lower-middle income"),
    ("Peru", "Latin America & Caribbean", "Upper-middle income"), ("Haiti", "Latin America & Caribbean", "Lower-middle income"),
]

MODULES = {  # component -> module -> interventions
    "HIV": {
        "Prevention programs for key populations": ["Condoms and lubricant", "HIV testing services", "Behavioural interventions", "PrEP"],
        "Treatment, care and support": ["Antiretroviral therapy", "Viral load monitoring", "Differentiated service delivery"],
        "Prevention of mother-to-child transmission": ["Prong 3: ART for pregnant women", "Early infant diagnosis"],
    },
    "TB": {
        "TB care and prevention": ["Case detection and diagnosis", "Treatment", "TB preventive treatment"],
        "Multidrug-resistant TB": ["MDR-TB diagnosis", "MDR-TB treatment"],
    },
    "Malaria": {
        "Vector control": ["Long-lasting insecticidal nets", "Indoor residual spraying"],
        "Case management": ["Diagnosis (RDTs)", "Treatment (ACTs)", "Severe malaria"],
        "Specific prevention interventions": ["Seasonal malaria chemoprevention", "Intermittent preventive treatment in pregnancy"],
    },
    "RSSH": {
        "RSSH: Health management information systems and M&E": ["Routine reporting", "Surveys", "Data quality and use"],
        "RSSH: Human resources for health": ["Community health workers", "In-service training"],
        "RSSH: Health products management systems": ["Supply chain", "Laboratory systems"],
        "Program management": ["Grant management", "Policy and planning"],
    },
}

COSTS = {  # cost category -> cost inputs
    "1. Human Resources": ["Salaries - program management", "Salaries - service delivery", "Incentives / performance-based supplements"],
    "2. Travel related costs": ["Training related per diems", "Supervision travel", "Transport"],
    "3. External professional services": ["Technical assistance fees", "Audit fees", "Surveys and evaluations"],
    "4. Health products - pharmaceutical products": ["Antiretroviral medicines", "Anti-TB medicines", "Antimalarial medicines"],
    "5. Health products - non-pharmaceuticals": ["Condoms", "Bed nets", "Rapid diagnostic tests"],
    "6. Health products - equipment": ["Laboratory equipment", "IT equipment for HMIS"],
    "7. Procurement and supply-chain management costs": ["Freight and insurance", "Warehousing and in-country distribution"],
    "8. Infrastructure": ["Renovation of facilities", "Maintenance"],
    "9. Non-health equipment": ["Vehicles", "Office equipment"],
    "10. Communication material and publications": ["Printed materials", "Media campaigns"],
    "11. Indirect and overhead costs": ["Office rent and utilities", "Indirect cost recovery"],
    "12. Living support to client/target population": ["Food and nutrition support", "Cash transfers"],
    "13. Payment for results": ["Results-based financing"],
}

PERIODS = ["GC7 2024-2026", "GC8 2027-2029"]


def build(out: Path = OUT, seed: int = SEED) -> int:
    rng = random.Random(seed)
    rows = []
    for country, region, income in GEOGRAPHY:
        scale = rng.lognormvariate(0, 0.6) * {"Low income": 1.4, "Lower-middle income": 1.0, "Upper-middle income": 0.6}[income]
        components = rng.sample(list(MODULES), k=rng.choice([2, 3, 4]))
        if "RSSH" not in components:
            components.append("RSSH")
        for period in PERIODS:
            growth = 1.0 if period.startswith("GC7") else rng.uniform(0.85, 1.15)
            for comp in components:
                for module, interventions in MODULES[comp].items():
                    for intervention in interventions:
                        for cat, inputs in COSTS.items():
                            if rng.random() > 0.35:      # not every cost input appears under every intervention
                                continue
                            for ci in rng.sample(inputs, k=min(len(inputs), rng.choice([1, 1, 2]))):
                                base = {"4.": 6.0, "5.": 5.0, "1.": 4.5, "7.": 3.5}.get(cat[:2], 2.5)
                                amount = round(rng.lognormvariate(base + 6, 0.9) * scale * growth, 2)
                                rows.append((country, period, comp, module, intervention, cat, ci, amount))
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    con = sqlite3.connect(out)
    con.executescript("""
        CREATE TABLE geography (
            country TEXT PRIMARY KEY, region TEXT NOT NULL, income_group TEXT NOT NULL);
        CREATE TABLE budget_lines (
            id INTEGER PRIMARY KEY, country TEXT NOT NULL REFERENCES geography(country),
            implementation_period TEXT NOT NULL, component TEXT NOT NULL, module TEXT NOT NULL,
            intervention TEXT NOT NULL, cost_category TEXT NOT NULL, cost_input TEXT NOT NULL,
            amount_usd REAL NOT NULL);
        CREATE INDEX ix_budget_country ON budget_lines(country);
        CREATE INDEX ix_budget_module ON budget_lines(module);
    """)
    con.executemany("INSERT INTO geography VALUES (?,?,?)", GEOGRAPHY)
    con.executemany("INSERT INTO budget_lines (country, implementation_period, component, module, intervention, "
                    "cost_category, cost_input, amount_usd) VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return len(rows)


if __name__ == "__main__":
    n = build()
    print(f"wrote {OUT} with {n:,} budget lines")

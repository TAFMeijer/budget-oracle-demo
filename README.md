# Budget Oracle (demo)

Ask the Global Fund's **published grant budgets** a question in plain English and get a table,
a chart and an Excel download back — or a question with options to pick from when yours could
mean two things.

> A personal demo on public data, not an official Global Fund product. Numbers are budgets at
> the Global Fund reference rate, as published on the
> [Data Service](https://data-service.theglobalfund.org/downloads). Check them before quoting.

## How it works

A language model turns the question into a query over one local file; it never answers from
memory and it never sees the result rows.

1. The model is given the columns, every valid value (countries, modules, cost categories …)
   and a page of notes about the data. It maps your wording to those values — "HRH", "DRC",
   "labs" — and says so in a note when its reading might surprise you.
2. A guard parses what the model wrote before anything runs: one read-only statement, named
   columns, the one allowed table, every filter value and column checked against the data,
   no recursion, no functions that read files or manufacture data. A query that fails goes
   back to the model with the specific problem; you only see the result.
3. The file is opened read-only, each connection is capped in memory, and every query has a
   deadline after which it is interrupted.

Visitors see none of the inner workings. A public deployment is also bounded per visitor
address, in total, in concurrent answers and in question length — see `.env.example`.

## Run it

```bash
git clone https://github.com/TAFMeijer/budget-oracle-demo.git && cd budget-oracle-demo
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
# download "Grant Budgets - Reference Rate" (CSV) from the Data Set tab of
# https://data-service.theglobalfund.org/downloads into data/ or ~/Downloads, then:
.venv/bin/python data/build_gf_budgets.py     # a few seconds: builds data/gf_budgets.sqlite
./start.sh                                    # http://localhost:8602/budget-oracle
```

The model is any OpenAI-compatible endpoint (`OPENAI_BASE_URL`, `LLM_MODEL` in `.env`). The
demo runs on a local one — [LM Studio](https://lmstudio.ai) serving Gemma — so questions never
leave the machine; `LLM_PROVIDER=ollama` works the same way, and `LLM_PROVIDER=mock` runs the
app without any model for a look around.

## The data

`data/build_gf_budgets.py` turns the Global Fund's published CSV into one table,
`grant_budgets`, with plain column names: country, continent, sub-continent and the Global
Fund's grant-management region (from `data/gf_regions.csv`; the published file has none); grant, implementation period, status, grant cycle and grant type;
principal recipient, its type and the lead implementer; component, module and intervention;
investment landscape level 1 and 2 and cost category; year; budget in US$. `component` is what
a budget line funds, taken from its module; `grant_type` is the grant it sits in —
health-system modules live mostly inside disease grants, so they differ. About 172,000 lines,
450 grants, 137 countries, calendar years 2017–2028 across Grant Cycles 5, 6 and 7.

The published data leaves the module of some lines empty: about 7% of the Grant Cycle 5 budget
and 25% of Grant Cycle 6 carry "Unspecified", so module totals for those cycles undercount
while country, grant and cost totals are complete. The assistant says so when it matters.

**From the API instead.** The CSV is the Global Fund's own export of its public API (OData,
no key): the same lines are
`https://fetch.theglobalfund.org/v4.2/odata/FinancialIndicators(indicatorName='Budget - Reference Rate',financialDatasetName='GrantBudget_ReferenceRate')`,
with ids that resolve through the `Grants`, `Geographies`, `ActivityAreas` and
`FinancialCategories` feeds (a budget line's `financialCategoryId` is the category's
`hierarchyId`).

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
./scripts/test.sh        # runs the suite on a copy without your .env
```

`docs/architecture.md` describes the design. MIT licence.

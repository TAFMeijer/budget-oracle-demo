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
.venv/bin/python data/fetch_gf_budgets.py     # about a minute: builds data/gf_budgets.sqlite
./start.sh                                    # http://localhost:8602/budget-oracle
```

The model is any OpenAI-compatible endpoint (`OPENAI_BASE_URL`, `LLM_MODEL` in `.env`). The
demo runs on a local one — [LM Studio](https://lmstudio.ai) serving Gemma — so questions never
leave the machine; `LLM_PROVIDER=ollama` works the same way, and `LLM_PROVIDER=mock` runs the
app without any model for a look around.

## The data

`data/fetch_gf_budgets.py` reads the "Budget - Reference Rate" dataset from the Global Fund's
public API (`https://fetch.theglobalfund.org/v4.2/odata`, no key needed), resolves its ids to
names and writes one table, `grant_budgets`: grant, implementation period, funding cycle,
country, region, grant type, component, module, intervention, cost group, cost category, year,
budget in US$. `component` is what a budget line funds, taken from its module; `grant_type` is
the grant it sits in — health-system modules live mostly inside disease grants, so they differ. About 172,000 lines, 450 grants, 137 countries, calendar years 2017–2028 across the
2017-2019, 2020-2022 and 2023-2025 funding cycles.

Two things to know. The funding cycle is derived from when an implementation period starts;
the API publishes none for a budget line. And the API does not publish the activity area of
every line: about 7% of the 2017-2019 budget and 25% of 2020-2022 carry the module
"Unspecified", so module totals for those cycles undercount while country, component and cost
totals are complete. The assistant says so when it matters.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
./scripts/test.sh        # runs the suite on a copy without your .env
```

`docs/architecture.md` describes the design. MIT licence.

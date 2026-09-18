## The dataset — public Global Fund grant budgets

One table, [grant_budgets]: the Global Fund's published grant budgets ("Grant Budgets -
Reference Rate" on data-service.theglobalfund.org). One row per implementation period x
module/intervention x cost category x calendar year, in US$ at the Global Fund reference rate.
172k rows, 450 grants, 137 countries, calendar years 2017-2028.

These are BUDGETS: what a grant plans to spend. There is no expenditure, disbursement,
commitment or results data here. A question about money spent, disbursed or absorbed gets
status "cannot" with that reason — never answer it with budget figures. "Investment" and
"funding" in a question mean budget.

Columns. Where: country, continent, subcontinent, gf_region. Which grant: grant_code,
implementation_period, period_start, period_end, status, grant_cycle, grant_type. Who:
principal_recipient, pr_type, pr_subtype, lead_implementer. What: component, module,
intervention. How: investment_landscape_1, investment_landscape_2, cost_category. When: year
(calendar year of the budget line). Amount: budget_usd.

## Grant cycles and years

grant_cycle is the Global Fund's grant cycle: 'Grant Cycle 5', 'Grant Cycle 6', 'Grant Cycle 7'.
Always filter on these values. Users say it many ways — map them silently:

    GC5, NFM2, the 2017-2019 cycle or allocation  -> 'Grant Cycle 5'
    GC6, NFM3, the 2020-2022 cycle or allocation  -> 'Grant Cycle 6'
    GC7, NFM4, the 2023-2025 cycle or allocation, "current cycle"  -> 'Grant Cycle 7'

A cycle's budgets run past the years in its name: Grant Cycle 7 grants budget calendar years
2024-2028. So "the 2023-2025 cycle" is a grant_cycle, while "in 2024" or "2021 to 2023" is a
filter on year. Never infer one from the other.

The public data has nothing for Grant Cycle 8 (GC8, 2026-2028). A question about it gets status
"cannot": those budgets are not published yet.

Every query must say what it covers: filter on grant_cycle or on year, or GROUP BY one of them.
When the user names no cycle and no year, answer for the current cycle — grant_cycle = 'Grant
Cycle 7' — and say so in the note. "All cycles", "over time", "since 2017" mean all three:
grant_cycle IN ('Grant Cycle 5', 'Grant Cycle 6', 'Grant Cycle 7'), or GROUP BY grant_cycle when
they want them side by side. To compare cycles GROUP BY grant_cycle in ONE query — everything is
in one table, so a comparison never needs UNION. Alias grant_cycle AS cycle in results.

## Component and grant type

component is what the budget LINE funds, taken from its module: 'HIV/AIDS', 'Tuberculosis',
'Malaria', 'RSSH', 'Multi-Component' (program management, payment for results) or
'Unspecified'. A disease or RSSH named in a question ALWAYS means component: "HIV budget",
"RSSH budget", "RSSH budget by module", "health systems budget", "malaria investments", "TB"
-> component = 'HIV/AIDS' / 'RSSH' / 'Malaria' / 'Tuberculosis'.

grant_type is a different thing — the kind of GRANT the line sits in — and is used ONLY when
the user says the word "grant" or "grants" about it ("malaria grants", "the standalone RSSH
grants", "TB/HIV grants"). Its values: 'HIV grant', 'TB/HIV grant', 'Tuberculosis grant',
'Malaria grant', 'RSSH grant', 'Multicomponent grant'. Health-system modules sit mostly inside
disease grants, which is why the two differ: RSSH lines total about US$4bn, standalone RSSH
grants about US$1.2bn. Filtering grant_type for a question that does not mention grants gives
the wrong number. 'TB/HIV' is a grant type and a module name, never a component.

## Module names change between cycles

The same theme has a different module name per cycle. For a theme across cycles, or when no
single cycle is named, include every name with IN (...). Within one cycle use that cycle's name.

| theme | Grant Cycle 5 and 6 | Grant Cycle 7 |
|---|---|---|
| human resources for health (HRH), health workforce, CHWs | 'RSSH: Human resources for health, including community health workers' | 'RSSH/PP: Human resources for health (HRH) and quality of care' |
| M&E, HMIS, data systems | 'RSSH: Health management information systems and M&E' | 'RSSH: Monitoring and evaluation systems' |
| laboratory, labs | 'RSSH: Laboratory systems' | 'RSSH/PP: Laboratory systems (including national and peripheral)' |
| governance, planning | 'RSSH: Health sector governance and planning' | 'RSSH: Health sector planning and governance for integrated people-centered services' |
| health financing, financial management | 'RSSH: Financial management systems' | 'RSSH: Health financing systems' |
| supply chain, procurement systems, HPM | 'RSSH: Health products management systems' | same |
| community systems, CSS | 'RSSH: Community systems strengthening' | same |
| service delivery, quality | 'RSSH: Integrated service delivery and quality improvement' | (none) |
| medical oxygen | (none) | 'RSSH/PP: Medical oxygen and respiratory care system' |
| TB care | 'TB care and prevention' | 'TB diagnosis, treatment and care' and 'TB/DR-TB Prevention' |
| drug-resistant TB, MDR-TB | 'MDR-TB' | 'Drug-resistant (DR)-TB diagnosis, treatment and care' |
| vertical transmission, PMTCT | 'PMTCT' | 'Elimination of vertical transmission of HIV, syphilis and hepatitis B' |
| HIV prevention for key populations | Grant Cycle 5: the 'Comprehensive prevention programs for ...' modules; Grant Cycle 6: 'Prevention' | the 'Prevention package for ...' modules |

Unchanged across cycles: 'Treatment, care and support' (HIV treatment, ART), 'Differentiated
HIV Testing Services' (HTS), 'TB/HIV', 'Case management' (malaria), 'Vector control' (nets,
ITN, IRS), 'Specific prevention interventions (SPI)' (SMC, IPTp, chemoprevention), 'Program
management', 'Payment for results', 'Reducing human rights-related barriers to HIV/TB services'.

Shorthand: KP/KVP = key (and vulnerable) populations; AGYW = adolescent girls and young women;
MSM = men who have sex with men; PWID/PUD = people who inject/use drugs; SW = sex workers;
TG = transgender people; PP = pandemic preparedness = names starting 'RSSH/PP:'.

## Some lines have no module name

The published data leaves the activity area of some lines empty: component, module and
intervention are 'Unspecified' for about 7% of the Grant Cycle 5 budget and about 25% of Grant
Cycle 6 (none in Grant Cycle 7). Totals by country, region, grant type, cost or year are
complete; totals by component, module or intervention for those two cycles undercount. Whenever
such an answer includes Grant Cycle 5 or 6, say in the note that part of those cycles' budget
is not attributed to a module. Never filter 'Unspecified' out silently.

## Costs — three levels, no cost inputs

There is no cost-input level in this data. Costs are described at three levels:

    investment_landscape_1   3 values: 'Health Commodities/Equipments and Supply Chain Costs',
                             'Program Related Activity Cost', 'Program Management Related Cost'
    investment_landscape_2   9 values under them, e.g. 'Health Products/Commodities',
                             'Human Resources for Health', 'Program related costs'
    cost_category            13 values, e.g. 'Health Products - Pharmaceutical Products (HPPP)',
                             'Human Resources (HR)', 'Travel related costs (TRC)'

Each level-2 group belongs to one level-1 group. A cost category can sit under several level-2
groups ('Human Resources (HR)' is under both 'Human Resources for Health' and 'Human Resources
including Fiscal Agents'), so cost_category is a view of its own, not the bottom of a strict tree.
"Investment landscape" with no level named -> investment_landscape_1; "level 2", "cost
grouping" -> investment_landscape_2; "cost category", "cost type", "what is it spent on" ->
cost_category. "Cost inputs" are not published, but never refuse such a question: answer it at
cost_category — the finest level there is — and say in the note that cost inputs are not
published, so cost categories are shown instead.
Shorthand: HR = 'Human Resources (HR)' (a cost — HRH is the module); HPPP / HPNP / HPE = health
products pharmaceutical / non-pharmaceutical / equipment; PSM = 'Procurement and Supply-Chain
Management costs (PSM)'; TRC = travel; EPS = external professional services; INF =
infrastructure; NHP = non-health equipment; LSCTP = living support; RBF = 'Results Based
Financing'; commodities = investment_landscape_2 'Health Products/Commodities'.

## Geography — three ways to group countries

country holds the names as listed (e.g. 'Congo (Democratic Republic)', 'Viet Nam', "Côte
d'Ivoire", 'Tanzania (United Republic)'); match the user's spelling or abbreviation (DRC,
Vietnam, Ivory Coast) to the listed name without asking. Multicountry grants have their own
names starting 'Multicountry'.

    continent      'Africa', 'Asia', 'Americas', 'Europe', 'Oceania'; 'Multicountry' for regional grants
    subcontinent   the UN sub-region: 'Western Africa', 'Eastern Africa', 'Middle Africa',
                   'Southern Africa', 'Northern Africa', 'South-Eastern Asia', 'Southern Asia', …
    gf_region      the Global Fund's grant-management regions, as codes:
                   'WCA'      West and Central Africa
                   'HIA1'     High Impact Africa 1 (Burkina Faso, Congo (Democratic Republic),
                              Côte d'Ivoire, Ghana, Mali, Nigeria)
                   'HIA2'     High Impact Africa 2 (Ethiopia, Kenya, Mozambique, South Africa,
                              Tanzania, Uganda, Zambia, Zimbabwe, …)
                   'MENASEA'  Middle East and North Africa plus the rest of Southern and Eastern
                              Africa (Malawi, Rwanda, Madagascar, Sudan, Angola, Namibia, Morocco, …)
                   'EECA'     Eastern Europe and Central Asia
                   'LAC'      Latin America and the Caribbean
                   'Asia'     Asia and the Pacific

"GF region", "Global Fund region", "region" or "by region" with nothing more -> gf_region, and
say so in the note. Users name these regions by code or in words: "West and Central Africa" ->
'WCA'; "High Impact Africa" -> both 'HIA1' and 'HIA2'; "Eastern Europe" -> 'EECA'; "Latin
America" -> 'LAC'. A high-impact country is NOT in 'WCA' or 'MENASEA' even when it lies there
geographically (Nigeria is 'HIA1'), so for geography proper use the other two columns:
"Africa", "Asia" -> continent; "West Africa", "Western Africa", "Southern Africa", "South-East
Asia" -> subcontinent. "Sub-Saharan Africa" = continent 'Africa' without subcontinent
'Northern Africa'. There is no income group in this data.

## Who implements

principal_recipient and lead_implementer are full official names ('United Nations Development
Programme (UNDP)', 'Ministry of Health of …'), too many to list: match them with LIKE on the
distinctive part (principal_recipient LIKE '%UNDP%'). pr_type is one of 'Governmental
Organization', 'Civil Society Organization', 'Multilateral Organization', 'Private Sector';
pr_subtype is finer ('Ministry of Health', 'United Nations Organization', 'Local
Non-Governmental Organization', …). "Government", "civil society", "UN", "NGOs" -> pr_type or
pr_subtype as listed. status is the implementation period's: 'Active', 'Financial Closure',
'Financially Closed'; "active grants" -> status = 'Active'.

## Query rules

Always aggregate: SUM(budget_usd) AS total_amount with a GROUP BY over what the user asked for;
never return raw budget lines unless they ask for the lines. When they name no dimension,
return the single total. Alias result columns to plain names (country, module, intervention,
cost_category, cycle, year). Order by total_amount DESC unless asked otherwise. "Top N" means
ORDER BY total_amount DESC LIMIT N. Percent shares are added by the app; do not compute them.
A change between two cycles or years: one row per item with a column per cycle, using
SUM(CASE WHEN grant_cycle = '...' THEN budget_usd END), and the change as a third column.

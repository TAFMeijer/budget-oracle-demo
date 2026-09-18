import pytest

from bqa.guard import GuardError, validate

ALLOWED = ["budget_lines", "geography"]


def test_valid_select_gets_a_limit():
    sql = validate("SELECT country, SUM(amount_usd) AS total_amount FROM budget_lines GROUP BY country ORDER BY total_amount DESC",
                   ALLOWED, "sqlite", 100)
    assert sql.upper().startswith("SELECT")
    assert "LIMIT 100" in sql.upper()


def test_existing_limit_is_kept():
    sql = validate("SELECT country, amount_usd FROM budget_lines LIMIT 5", ALLOWED, "sqlite", 100)
    assert "LIMIT 5" in sql.upper() and "LIMIT 100" not in sql.upper()


def test_tsql_dialect_renders_top():
    sql = validate("SELECT country, SUM(amount_usd) AS total_amount FROM budget_lines GROUP BY country", ALLOWED, "tsql", 50)
    assert "TOP 50" in sql.upper()


@pytest.mark.parametrize("bad", [
    "DELETE FROM budget_lines",
    "UPDATE budget_lines SET amount_usd = 0",
    "DROP TABLE budget_lines",
    "INSERT INTO budget_lines (country) VALUES ('x')",
    "SELECT country FROM budget_lines; DROP TABLE budget_lines",
    "SELECT * FROM budget_lines",
    "SELECT b.* FROM budget_lines b",
    "SELECT name FROM sqlite_master",
    "SELECT country INTO other FROM budget_lines",
    "SELECT load_extension('evil') FROM budget_lines",
    "",
    "this is not sql",
])
def test_rejections(bad):
    with pytest.raises(GuardError):
        validate(bad, ALLOWED, "sqlite", 100)


def test_join_to_allowed_table_ok():
    sql = validate("SELECT g.region, SUM(b.amount_usd) AS total_amount FROM budget_lines b "
                   "LEFT JOIN geography g ON b.country = g.country GROUP BY g.region", ALLOWED, "sqlite", 100)
    assert "JOIN" in sql.upper()


def test_cte_is_allowed_and_not_treated_as_unknown_table():
    sql = validate("WITH t AS (SELECT country, amount_usd FROM budget_lines) SELECT country, SUM(amount_usd) AS total_amount FROM t GROUP BY country",
                   ALLOWED, "sqlite", 100)
    assert "WITH" in sql.upper()


def test_union_with_trailing_order_by_wraps_for_tsql():
    sql = validate(
        "SELECT 'a' AS cycle, country, SUM(amount_usd) AS total_amount FROM budget_lines GROUP BY country "
        "UNION ALL SELECT 'b', country, SUM(amount_usd) FROM budget_lines GROUP BY country "
        "ORDER BY total_amount DESC",
        ALLOWED, "tsql", 100)
    inner = sql[sql.index("("):sql.rindex(")")]
    assert "ORDER BY" not in inner.upper()          # hoisted out of the derived table
    flat = " ".join(sql.upper().split())
    assert flat.endswith("ORDER BY TOTAL_AMOUNT DESC")
    assert "TOP 100" in flat


@pytest.mark.parametrize("bad", [
    # table-valued functions produce rows without being on the table allow-list
    "SELECT permission_name FROM fn_my_permissions(NULL, 'DATABASE')",
    "SELECT text FROM sys.dm_exec_sql_text(0x0)",
    "SELECT value FROM STRING_SPLIT('a,b', ',')",
    "SELECT country FROM budget_lines b JOIN sys.dm_exec_sql_text(0x0) x ON 1 = 1",
    "SELECT country FROM budget_lines CROSS APPLY OPENJSON('[1]')",
    "SELECT country FROM budget_lines b CROSS APPLY STRING_SPLIT(b.country, ',') s",
    # the same table name in another database or schema is not the table that was allowed
    "SELECT country FROM master.dbo.budget_lines",
    "SELECT country FROM sys.budget_lines",
])
def test_row_sources_that_are_not_allowed_tables(bad):
    with pytest.raises(GuardError):
        validate(bad, ALLOWED, "tsql", 100)


def test_dbo_schema_prefix_is_allowed():
    sql = validate("SELECT country, amount_usd FROM dbo.budget_lines", ALLOWED, "tsql", 100)
    assert "budget_lines" in sql


def test_value_check_is_case_insensitive_on_sql_server():
    """SQL Server's default collation ignores case, and the data itself holds both
    'RSSH: Community Systems Strengthening' and 'RSSH: Community systems strengthening'.
    A filter the database would match must not be bounced back as invented — on sqlite,
    where '=' is case-sensitive, it must."""
    from bqa.guard import unknown_values
    from bqa.schema import SchemaContext, TableInfo
    schema = SchemaContext(
        tables=[TableInfo(name="b", columns=[("module", "varchar")],
                          valid_values={"module": ["RSSH: Community Systems Strengthening"]})],
        hierarchies={})
    sql = ("SELECT module, SUM(amount) AS total_amount FROM b "
           "WHERE module = 'RSSH: Community systems strengthening' GROUP BY module")
    assert unknown_values(sql, schema, "tsql") == []
    assert unknown_values(sql, schema, "sqlite")[0][:2] == ("module", "RSSH: Community systems strengthening")


def test_union_branches_must_group_by_the_same_dimensions():
    """One branch grouped by component beside another that is not splits one cycle over
    several rows while the other shows a single total — it reads like an answer and is wrong."""
    bad = ("SELECT 'GC7' AS cycle, SUM(amount) AS total_amount FROM budget_lines "
           "UNION ALL SELECT 'GC8' AS cycle, SUM(amount) AS total_amount FROM budget_lines GROUP BY component")
    with pytest.raises(GuardError, match="same dimensions"):
        validate(bad, ["budget_lines"], "tsql")
    ok = ("SELECT 'GC7' AS cycle, component, SUM(amount) AS total_amount FROM budget_lines GROUP BY component "
          "UNION ALL SELECT 'GC8' AS cycle, component, SUM(amount) AS total_amount FROM budget_lines GROUP BY component")
    assert "UNION ALL" in validate(ok, ["budget_lines"], "tsql").upper()
    flat = ("SELECT 'GC7' AS cycle, SUM(amount) AS total_amount FROM budget_lines "
            "UNION ALL SELECT 'GC8' AS cycle, SUM(amount) AS total_amount FROM budget_lines")
    assert "UNION ALL" in validate(flat, ["budget_lines"], "tsql").upper()


def test_required_filters_are_enforced_per_select():
    """A table that holds several cycles may only be read with the cycle filter present — in
    every SELECT that reads it, so a UNION branch cannot forget it either."""
    req = {"combined": ["cycle"]}
    with pytest.raises(GuardError, match=r"without filtering on \[cycle\]"):
        validate("SELECT SUM(amount) AS total_amount FROM combined WHERE module = 'x'",
                 ["combined"], "tsql", required_filters=req)
    ok = "SELECT SUM(amount) AS total_amount FROM combined WHERE cycle = '2023-2025' AND module = 'x'"
    assert validate(ok, ["combined"], "tsql", required_filters=req)
    bad = ("SELECT 'GC7' AS c, SUM(amount) AS total_amount FROM combined WHERE cycle = '2023-2025' "
           "UNION ALL SELECT 'C19RM' AS c, SUM(amount) AS total_amount FROM combined WHERE module = 'y'")
    with pytest.raises(GuardError, match="without filtering"):
        validate(bad, ["combined"], "tsql", required_filters=req)
    assert validate("SELECT SUM(amount) AS total_amount FROM other", ["other"], "tsql", required_filters=req)


def test_unknown_columns_flags_a_column_read_from_the_other_table():
    from bqa.guard import unknown_columns
    from bqa.schema import SchemaContext, TableInfo
    schema = SchemaContext(
        tables=[TableInfo(name="new_budget", columns=[("Geo Name", "varchar"), ("Amount New", "float")]),
                TableInfo(name="old_budget", columns=[("Country", "varchar"), ("Amount Old", "float")])],
        hierarchies={})
    bad = ("SELECT [Geo Name] AS country, SUM([Amount Old]) AS total_amount FROM old_budget GROUP BY [Geo Name] "
           "UNION ALL SELECT [Geo Name], SUM([Amount New]) FROM new_budget GROUP BY [Geo Name]")
    msg = unknown_columns(bad, schema, "tsql")
    assert "[Geo Name]" in msg and "[new_budget]" in msg
    ok = ("SELECT [Country] AS country, SUM([Amount Old]) AS total_amount FROM old_budget GROUP BY [Country] "
          "UNION ALL SELECT [Geo Name], SUM([Amount New]) FROM new_budget GROUP BY [Geo Name]")
    assert unknown_columns(ok, schema, "tsql") == ""
    assert "No table has a column [Nope]" in unknown_columns("SELECT [Nope] AS x FROM new_budget", schema, "tsql")
    derived = ("SELECT country, SUM(total_amount) AS total_amount FROM (SELECT [Country] AS country, "
               "[Amount Old] AS total_amount FROM old_budget) AS q GROUP BY country")
    assert unknown_columns(derived, schema, "tsql") == ""


def _themed_schema():
    from bqa.schema import SchemaContext, TableInfo
    return SchemaContext(
        tables=[TableInfo(name="b", columns=[("module", "varchar"), ("intervention", "varchar"), ("amount", "float")],
                          valid_values={"module": ["Specific prevention interventions (SPI)", "Vector control"]})],
        hierarchies={"module>intervention": [("Specific prevention interventions (SPI)", "Seasonal malaria chemoprevention"),
                                             ("Vector control", "Indoor residual spraying (IRS)")]})


def test_hierarchy_children_make_a_wide_column_checkable():
    from bqa.guard import checkable_values
    known = checkable_values(_themed_schema())
    assert known["intervention"] == {"Seasonal malaria chemoprevention", "Indoor residual spraying (IRS)"}


def test_like_pattern_matching_nothing_is_flagged_with_candidates():
    """The model filtered GC7 interventions with LIKE 'Specific prevention interventions (SPI)%' —
    SPI is a module there, so the pattern matched nothing and the answer read as a real zero."""
    from bqa.guard import unknown_values, repair_request
    schema = _themed_schema()
    sql = "SELECT module, SUM(amount) AS total_amount FROM b WHERE intervention LIKE 'Specific prevention interventions (SPI)%' GROUP BY module"
    problems = unknown_values(sql, schema, "tsql")
    assert len(problems) == 1 and problems[0][0] == "intervention"
    assert "Seasonal malaria chemoprevention" in problems[0][2]
    assert "LIKE pattern that matches none" in repair_request(problems)
    ok = "SELECT module, SUM(amount) AS total_amount FROM b WHERE module LIKE '%prevention%' OR intervention LIKE 'Seasonal%' GROUP BY module"
    assert unknown_values(ok, schema, "tsql") == []
    excluded = "SELECT module, SUM(amount) AS total_amount FROM b WHERE intervention NOT LIKE 'zzz%' GROUP BY module"
    assert unknown_values(excluded, schema, "tsql") == []


def test_unknown_columns_reports_a_qualified_column_too():
    from bqa.guard import unknown_columns
    from bqa.schema import SchemaContext, TableInfo
    schema = SchemaContext(tables=[TableInfo(name="b", columns=[("country", "varchar"), ("amount", "float")]),
                                   TableInfo(name="g", columns=[("country", "varchar"), ("region", "varchar")])],
                           hierarchies={})
    msg = unknown_columns("SELECT b.region AS region, SUM(b.amount) AS total_amount FROM b GROUP BY b.region", schema, "sqlite")
    assert "[region]" in msg and "[g]" in msg


def test_dead_like_arm_inside_a_live_or_is_tolerated():
    """'([Intervention] LIKE x OR [Module] LIKE x)' where only one side matches anything is
    still a correct query: the dead arm selects nothing extra. ANDed in, or with every arm
    dead, the pattern empties the answer and must go back."""
    from bqa.guard import unknown_values
    schema = _themed_schema()
    live_or = ("SELECT module, SUM(amount) AS total_amount FROM b "
               "WHERE (module LIKE '%zzz%' OR intervention LIKE 'Seasonal%') GROUP BY module")
    assert unknown_values(live_or, schema, "tsql") == []
    live_eq = ("SELECT module, SUM(amount) AS total_amount FROM b "
               "WHERE (intervention LIKE 'zzz%' OR module = 'Vector control') GROUP BY module")
    assert unknown_values(live_eq, schema, "tsql") == []
    dead_or = ("SELECT module, SUM(amount) AS total_amount FROM b "
               "WHERE (module LIKE '%zzz%' OR intervention LIKE 'yyy%') GROUP BY module")
    assert {c for c, _, _ in unknown_values(dead_or, schema, "tsql")} == {"module", "intervention"}
    anded = ("SELECT module, SUM(amount) AS total_amount FROM b "
             "WHERE module LIKE '%zzz%' AND intervention LIKE 'Seasonal%' GROUP BY module")
    assert [c for c, _, _ in unknown_values(anded, schema, "tsql")] == ["module"]


def test_a_value_at_the_wrong_level_is_pointed_to_its_column():
    """SPI filtered on intervention is a module: say so, rather than offering the nearest
    interventions as if the name were misspelt."""
    from bqa.guard import misplaced_values, repair_request, unknown_values
    schema = _themed_schema()
    sql = "SELECT module, SUM(amount) AS total_amount FROM b WHERE intervention LIKE 'Specific prevention interventions (SPI)%' GROUP BY module"
    problems = unknown_values(sql, schema, "tsql")
    where = misplaced_values(problems, schema)
    assert where == {("intervention", "Specific prevention interventions (SPI)%"):
                     [("module", ["Specific prevention interventions (SPI)"])]}
    text = repair_request(problems, where)
    assert "it is a value of [module]" in text
    assert "Replace the intervention filter with [module] = 'Specific prevention interventions (SPI)'" in text
    assert "Closest valid values" not in text                    # the red herrings are withheld
    assert text.startswith("Your query filters on a real name but in the wrong column")
    exact = "SELECT module, SUM(amount) AS total_amount FROM b WHERE intervention = 'Vector control' GROUP BY module"
    problems = unknown_values(exact, schema, "tsql")
    assert misplaced_values(problems, schema) == {("intervention", "Vector control"): [("module", ["Vector control"])]}
    assert misplaced_values(unknown_values("SELECT module FROM b WHERE intervention = 'zzz'", schema, "tsql"), schema) == {}


@pytest.mark.parametrize("sql,why", [
    ("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT SUM(x) AS total_amount FROM c", "Recursive"),
    ("SELECT randomblob(1000000000) AS r FROM budget_lines", "randomblob"),
    ("SELECT hex(zeroblob(1000000000)) AS r FROM budget_lines", "zeroblob"),
])
def test_unbounded_queries_are_rejected(sql, why):
    """A query may be slow; it may not be unbounded. An endless CTE under an aggregate never
    returns, and the blob functions manufacture a gigabyte per row on request."""
    with pytest.raises(GuardError, match=why):
        validate(sql, ["budget_lines"], "sqlite")


def test_required_filter_alternatives_and_grouping():
    """'cycle|year': either column satisfies the rule, and grouping by it counts — a result
    split by cycle says which cycle each number belongs to."""
    req = {"budgets": ["cycle|year"]}
    base = "SELECT country, SUM(amount) AS total_amount FROM budgets {} GROUP BY country"
    with pytest.raises(GuardError, match=r"without filtering on \[cycle\] or \[year\]"):
        validate(base.format(""), ["budgets"], "sqlite", required_filters=req)
    assert validate(base.format("WHERE cycle = '2023-2025'"), ["budgets"], "sqlite", required_filters=req)
    assert validate(base.format("WHERE year >= 2024"), ["budgets"], "sqlite", required_filters=req)
    grouped = "SELECT cycle, SUM(amount) AS total_amount FROM budgets GROUP BY cycle"
    assert validate(grouped, ["budgets"], "sqlite", required_filters=req)


def test_required_filter_is_satisfied_by_a_pivot_on_the_column():
    req = {"budgets": ["cycle|year"]}
    pivot = ("SELECT SUM(CASE WHEN cycle = '2020-2022' THEN amount END) AS before, "
             "SUM(CASE WHEN cycle = '2023-2025' THEN amount END) AS after FROM budgets WHERE module = 'x'")
    assert validate(pivot, ["budgets"], "sqlite", required_filters=req)

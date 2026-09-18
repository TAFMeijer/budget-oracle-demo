"""SQL guard: the model proposes, this module decides whether the query may run.

Checks (all must pass):
  1. exactly one statement, and it is a SELECT (unions of selects are fine)
  2. no data-modifying or DDL nodes anywhere in the tree
  3. no SELECT * — every column must be named
  4. every table referenced is on the allow-list
  5. no dangerous functions (file access, shell, extension loading)
  6. a row limit is enforced (added if the model did not set one)
  7. the branches of a UNION group by the same number of dimensions
  8. a table with required filters (REQUIRED_FILTERS) is only read with those filters present
  9. no recursive CTEs and no functions that manufacture data (randomblob, zeroblob): a query
     may be slow, it may not be unbounded

The validated query is re-rendered from the AST in the target dialect, so what runs is what was checked.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.optimizer.qualify import qualify

FORBIDDEN_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter, exp.Command,
    exp.TruncateTable, exp.Merge, exp.Grant, exp.Set, exp.Transaction, exp.Commit, exp.Rollback,
)
FORBIDDEN_FUNCTIONS = {
    "load_extension", "readfile", "writefile", "fts3_tokenizer",          # sqlite
    "randomblob", "zeroblob",                                             # sqlite: a gigabyte per row on request
    "xp_cmdshell", "openrowset", "opendatasource", "openquery", "sp_executesql",  # sql server
    "pg_read_file", "pg_sleep", "lo_import", "lo_export", "copy",         # postgres
}


class GuardError(ValueError):
    """Raised when a query is rejected. The message is safe to show to the user."""


def _table_names(tree: exp.Expression) -> set[str]:
    """Every base table the query reads. Raises for row sources that are not plain tables.

    The allow-list is a list of table *names*, so anything that produces rows without being a
    named table must not slip past it: a table-valued function in FROM or JOIN (fn_my_permissions,
    sys.dm_exec_sql_text, STRING_SPLIT...), an APPLY of one, or a name that reaches into another
    database or schema where the same table name could mean something else.
    """
    names = set()
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for node in tree.find_all(exp.Lateral, exp.Unnest):
        raise GuardError("Table-valued functions are not allowed as a row source (APPLY / UNNEST).")
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            raise GuardError("Table-valued functions are not allowed as a row source.")
        if t.catalog:
            raise GuardError("Three-part table names (database.schema.table) are not allowed.")
        if t.db and t.db.lower() not in ("", "dbo", "main"):
            raise GuardError(f"Schema not allowed: {t.db}.")
        name = t.name.lower()
        if name and name not in ctes:
            names.add(name)
    return names


def _union_branches(tree: exp.Expression) -> list[exp.Select]:
    """The SELECTs a UNION joins, in order, with nested unions flattened."""
    if isinstance(tree, exp.Union):
        return _union_branches(tree.this) + _union_branches(tree.expression)
    return [tree] if isinstance(tree, exp.Select) else []


def _check_union_shape(tree: exp.Expression) -> None:
    """Every branch of a UNION must GROUP BY the same number of dimensions.

    A cycle comparison is one row per cycle per dimension. One branch grouped by component
    beside another that is not splits that cycle over several rows while the other shows a single
    total — a result that reads like an answer and is wrong. The model is told exactly this and
    still slips; the guard sends it back to fix.
    """
    for union in tree.find_all(exp.Union):
        if isinstance(union.parent, exp.Union):
            continue                                    # the outermost union covers its parts
        counts = []
        for branch in _union_branches(union):
            group = branch.args.get("group")
            counts.append(len(group.expressions) if group is not None else 0)
        if len(set(counts)) > 1:
            raise GuardError("The UNION's branches group by different numbers of columns "
                             f"({', '.join(map(str, counts))}); every branch must GROUP BY the same dimensions.")


def _check_required_filters(tree: exp.Expression, required: dict[str, list[str]]) -> None:
    """Every SELECT that reads a table with required filters must compare each of those columns
    in its own WHERE — each branch of a UNION on its own. Written for a table that holds several
    funding cycles: a branch that forgets the cycle filter returns every cycle added together,
    and nothing about the result says so."""
    # A requirement may offer alternatives, "funding_cycle|year": any one of them satisfies it.
    # Grouping by the column counts as well as filtering on it — a result split by cycle says
    # which cycle each number belongs to, which is all the rule is there to guarantee.
    wanted = {t.lower(): (t, cols) for t, cols in required.items()}
    for select in tree.find_all(exp.Select):
        # The FROM clause's arg key has changed across sqlglot versions ("from" / "from_"), so
        # look for the From node itself among this SELECT's own arguments.
        sources = [a.this for a in select.args.values() if isinstance(a, exp.From)]
        sources += [j.this for j in select.args.get("joins") or []]
        read = {s.name.lower() for s in sources if isinstance(s, exp.Table)}
        # Covered = referenced in this SELECT's own WHERE, GROUP BY, HAVING or select list. The
        # select list counts because of pivots: SUM(CASE WHEN cycle = '…' THEN amount END) per
        # cycle is as explicit about what each number covers as a WHERE would be.
        covered: set[str] = set()
        clauses = [select.args.get("where"), select.args.get("group"), select.args.get("having"),
                   *select.expressions]
        for clause in clauses:
            if clause is not None:
                covered |= {c.name.lower() for c in clause.find_all(exp.Column)}
        for key in read & set(wanted):
            table, cols = wanted[key]
            missing = [c for c in cols if not any(alt.strip().lower() in covered for alt in c.split("|"))]
            if missing:
                names = ", ".join(" or ".join(f"[{alt.strip()}]" for alt in c.split("|")) for c in missing)
                raise GuardError(f"A SELECT reads [{table}] without filtering on {names} (grouping by it counts too) "
                                 "— every SELECT on that table must say what it covers, or it silently returns "
                                 "everything. When the user named none, apply the default the notes give and say "
                                 "so in the note.")


def validate(sql: str, allowed_tables: list[str], dialect: str = "sqlite", row_limit: int = 500,
             required_filters: dict[str, list[str]] | None = None) -> str:
    """Return a safe, dialect-rendered version of `sql`, or raise GuardError."""
    if not sql or not sql.strip():
        raise GuardError("Empty query.")
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.ParseError as e:
        raise GuardError(f"The query could not be parsed: {e}") from e
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise GuardError("Exactly one statement is allowed.")
    tree = statements[0]

    if not isinstance(tree, (exp.Select, exp.Union)):
        raise GuardError("Only SELECT queries are allowed.")
    # WITH RECURSIVE under an aggregate never terminates, and no budget question needs one.
    if any(w.args.get("recursive") for w in tree.find_all(exp.With)):
        raise GuardError("Recursive queries (WITH RECURSIVE) are not allowed.")

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise GuardError(f"Statement type not allowed: {type(node).__name__}.")
        if isinstance(node, exp.Star) and isinstance(node.parent, (exp.Select, exp.Column)):
            raise GuardError("SELECT * is not allowed — name the columns.")
        if isinstance(node, exp.Into):
            raise GuardError("SELECT ... INTO is not allowed.")
        if isinstance(node, exp.Func):
            fname = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if fname in FORBIDDEN_FUNCTIONS:
                raise GuardError(f"Function not allowed: {fname}.")

    allowed = {t.lower() for t in allowed_tables}
    unknown = _table_names(tree) - allowed
    if unknown:
        raise GuardError("Table(s) not allowed: " + ", ".join(sorted(unknown)) + ".")

    _check_union_shape(tree)
    if required_filters:
        _check_required_filters(tree, required_filters)

    # enforce a row cap
    outer = tree
    has_limit = outer.args.get("limit") is not None or (isinstance(outer, exp.Select) and outer.args.get("top") is not None)
    if not has_limit:
        if isinstance(outer, exp.Union):
            # wrap with explicit columns for the star rule; hoist a trailing ORDER BY out of the
            # subquery (T-SQL forbids ORDER BY inside a derived table without TOP/OFFSET)
            order = tree.args.pop("order", None)
            if order is None:                       # sqlglot attaches a trailing ORDER BY to the last SELECT
                last = tree
                while isinstance(last, exp.Union):
                    last = last.args.get("expression")
                if isinstance(last, exp.Select):
                    order = last.args.pop("order", None)
            outer = exp.Select(expressions=[exp.column(c) for c in tree.named_selects]).from_(
                exp.Subquery(this=tree, alias="q"))
            if order is not None:
                outer.set("order", order)
        outer = outer.limit(row_limit)
    return outer.sql(dialect=dialect, pretty=True)


# ---------------------------------------------------------------------------------------------
# Value checking: the query's shape can be perfect while its filters name values that do not
# exist. The model sees every valid value in its prompt, but retrieving an exact string from
# tens of thousands of characters is exactly where it fails — it reproduces something plausible
# ("RSSH: Monitoring & Evaluation (including national and peripheral)") rather than copying.
# We hold those values in memory, so we can check rather than hope.

def checkable_values(schema) -> dict[str, set[str]]:
    """Column name -> every value it may hold.

    Only columns whose values are listed for EVERY table carrying them are checkable: where one
    table lists a column's values and another does not, a legitimate value from the unlisted
    table would look invented. Better to check nothing than to reject something real.
    """
    tables_with: dict[str, int] = {}
    tables_listing: dict[str, int] = {}
    values: dict[str, set[str]] = {}
    for t in schema.tables:
        for c, _ in t.columns:
            key = c.lower()
            tables_with[key] = tables_with.get(key, 0) + 1
            if c in t.valid_values:
                tables_listing[key] = tables_listing.get(key, 0) + 1
                values.setdefault(key, set()).update(t.valid_values[c])
    complete = {k: v for k, v in values.items() if tables_listing.get(k) == tables_with.get(k)}
    # A hierarchy is introspected with SELECT DISTINCT parent, child, so its children are the
    # complete list of that column's values — for the one table it was read from. That makes
    # a column too wide for a value list (interventions run to hundreds) checkable after all,
    # as long as only one table has the column, so the list cannot belong to another table.
    # Parents are left alone: a parent missing from the value lists was excluded on purpose
    # (VALID_VALUES_EXCLUDE), and that setting promises the guard will not check it.
    for key, pairs in (getattr(schema, "hierarchies", None) or {}).items():
        child = key.split(">", 1)[1].lower()
        if child not in complete and tables_with.get(child) == 1:
            complete[child] = {c for _, c in pairs}
    return complete


def _like_patterns(tree: exp.Expression) -> list[tuple[str, str, exp.Expression]]:
    """Every (column, pattern, node) the query filters with LIKE. NOT LIKE is left alone: a
    pattern that excludes nothing is harmless, one that includes nothing returns an empty answer
    that reads like a real zero."""
    found: list[tuple[str, str, exp.Expression]] = []
    for node in tree.find_all(exp.Like, exp.ILike):
        if node.args.get("negate") or isinstance(node.parent, exp.Not):    # NOT LIKE, either way sqlglot spells it
            continue
        pattern = node.expression
        if isinstance(node.this, exp.Column) and isinstance(pattern, exp.Literal) and pattern.is_string:
            found.append((node.this.name, pattern.this, node))
    return found


def _or_arms(node: exp.Expression) -> list[exp.Expression] | None:
    """The arms of the (outermost) OR this predicate sits in, flattened — or None when it is
    ANDed in, where a dead predicate empties the whole result."""
    cur = node
    while isinstance(cur.parent, exp.Paren):
        cur = cur.parent
    if not isinstance(cur.parent, exp.Or):
        return None
    top = cur.parent
    while True:
        up = top.parent
        if isinstance(up, exp.Or):
            top = up
        elif isinstance(up, exp.Paren) and isinstance(up.parent, exp.Or):
            top = up.parent
        else:
            break

    def flatten(e: exp.Expression) -> list[exp.Expression]:
        if isinstance(e, exp.Paren):
            return flatten(e.this)
        if isinstance(e, exp.Or):
            return flatten(e.this) + flatten(e.expression)
        return [e]
    return flatten(top)


def _arm_is_alive(arm: exp.Expression, known: dict[str, set[str]]) -> bool:
    """Whether a predicate can match at least one listed value. Anything the guard cannot judge
    (an unlisted column, a non-literal comparison) is given the benefit of the doubt."""
    def options(col) -> set[str] | None:
        return known.get(col.name.lower()) if isinstance(col, exp.Column) else None

    if isinstance(arm, (exp.Like, exp.ILike)) and not arm.args.get("negate"):
        opts, pat = options(arm.this), arm.expression
        if opts is None or not (isinstance(pat, exp.Literal) and pat.is_string):
            return True
        return any(_like_matches(pat.this, v) for v in opts)
    if isinstance(arm, exp.EQ):
        for col, other in ((arm.this, arm.expression), (arm.expression, arm.this)):
            opts = options(col)
            if opts is not None and isinstance(other, exp.Literal) and other.is_string:
                return other.this.lower() in {o.lower() for o in opts}
        return True
    if isinstance(arm, exp.In) and options(arm.this) is not None:
        opts = {o.lower() for o in options(arm.this)}
        lits = [e.this for e in arm.args.get("expressions") or [] if isinstance(e, exp.Literal) and e.is_string]
        return not lits or any(v.lower() in opts for v in lits)
    return True


def _like_matches(pattern: str, value: str) -> bool:
    """SQL LIKE, case-insensitively as SQL Server's default collation compares."""
    regex = "".join(".*" if ch == "%" else "." if ch == "_" else re.escape(ch) for ch in pattern)
    return re.fullmatch(regex, value, re.I | re.S) is not None


def _compared_literals(tree: exp.Expression) -> list[tuple[str, str]]:
    """Every (column, string literal) the query compares for equality. LIKE patterns are
    collected separately by _like_patterns and checked as patterns."""
    found: list[tuple[str, str]] = []

    def literal(node) -> str | None:
        return node.this if isinstance(node, exp.Literal) and node.is_string else None

    for node in tree.find_all(exp.EQ, exp.NEQ, exp.In):
        if isinstance(node, exp.In):
            if not isinstance(node.this, exp.Column):
                continue
            for e in node.args.get("expressions") or []:
                v = literal(e)
                if v is not None:
                    found.append((node.this.name, v))
            continue
        left, right = node.this, node.expression
        for col, other in ((left, right), (right, left)):
            if isinstance(col, exp.Column):
                v = literal(other)
                if v is not None:
                    found.append((col.name, v))
    return found


def suggest(value: str, options: set[str], limit: int = 3) -> list[str]:
    """The listed values a wrong one was most likely meant to be.

    Two failure shapes to cover, and they pull in different directions. A dropped prefix
    ('health products management systems' for 'RSSH: Health products management systems') is
    caught by substring. A blend of two neighbouring list entries ('RSSH: Monitoring & Evaluation
    (including national and peripheral)' — an M&E name wearing the Laboratory entry's
    parenthetical) fools substring badly, because the borrowed fragment matches the wrong entry.

    So do not try to be clever about which single value was meant: score every candidate on both
    character similarity and shared words, and hand back the best few. The model still has the
    user's question in front of it and can choose sensibly between them.
    """
    import difflib
    import re
    low = value.lower().strip()
    exact = [o for o in options if o.lower() == low]
    if exact:
        return exact[:limit]

    def words(text: str) -> set[str]:
        return {w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) > 2}

    target = words(value)

    def score(option: str) -> float:
        chars = difflib.SequenceMatcher(None, low, option.lower()).ratio()
        shared = words(option) & target
        overlap = len(shared) / len(target | words(option)) if target else 0.0
        contains = 0.15 if low and low in option.lower() else 0.0
        return max(chars, overlap) + contains

    ranked = sorted(options, key=score, reverse=True)
    return [o for o in ranked[:limit] if score(o) > 0.3]


def unknown_values(sql: str, schema, dialect: str = "sqlite") -> list[tuple[str, str, list[str]]]:
    """(column, value, suggestions) for every filter value that is not a listed value.

    An empty list means every filter the query applies names a value that really exists.
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return []                       # validate() reports parse failures; do not double up
    known = checkable_values(schema)
    # SQL Server compares strings case-insensitively under its default collation, and the data
    # itself holds both 'RSSH: Community Systems Strengthening' and 'RSSH: Community systems
    # strengthening'. A filter the database would match is not an invented value.
    folded = {col: {v.lower() for v in vals} for col, vals in known.items()} if dialect == "tsql" else {}
    problems: list[tuple[str, str, list[str]]] = []
    seen: set[tuple[str, str]] = set()
    for column, value in _compared_literals(tree):
        options = known.get(column.lower())
        if options is None or value in options:
            continue
        if value.lower() in folded.get(column.lower(), ()):
            continue
        if (column, value) in seen:
            continue
        seen.add((column, value))
        problems.append((column, value, suggest(value, options)))
    # A pattern that matches none of the listed values is an invented filter too — the one that
    # returns nothing and reads like a real zero. Unless it is a dead arm of an OR whose other
    # arm is alive: then it selects nothing extra and the answer is right; sending it back would
    # only cost a round-trip. Suggest from the pattern's plain text.
    for column, pattern, node in _like_patterns(tree):
        options = known.get(column.lower())
        if options is None or (column, pattern) in seen or any(_like_matches(pattern, v) for v in options):
            continue
        arms = _or_arms(node)
        if arms is not None and any(_arm_is_alive(a, known) for a in arms if a is not node):
            continue
        seen.add((column, pattern))
        problems.append((column, pattern, suggest(pattern.replace("%", " ").replace("_", " "), options)))
    return problems


def unknown_columns(sql: str, schema, dialect: str = "sqlite") -> str:
    """A repair message when the query reads a column from a table that does not have it, else ''.

    Tables that hold the same kind of data often name the same concept differently (the country
    column of one is not the country column of the other), and a model writing a UNION or a pivot across them drifts:
    it selects one table's column from the other. SQL Server answers "Invalid column name";
    resolving every column against the schema here turns that into a repair round instead of an
    error on screen. sqlglot's qualifier does the scope work (aliases, derived tables, CTEs).
    """
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return ""                       # validate() reports parse failures; do not double up
    columns = {t.name: {c: "TEXT" for c, _ in t.columns} for t in schema.tables}
    try:
        qualify(tree.copy(), schema=columns, dialect=dialect, validate_qualify_columns=True, infer_schema=False)
        return ""
    except OptimizeError as e:
        m = re.search(r"Column '([^']+)' could not be resolved|Unknown column:? '?([^'\s]+)'?", str(e))
        if not m:
            return ""                   # some other complaint: let the database be the judge
    name = m.group(1) or m.group(2)
    spelled = next((c.name for c in tree.find_all(exp.Column) if c.name.lower() == name.lower()), name)
    holders = [t.name for t in schema.tables if any(c.lower() == name.lower() for c, _ in t.columns)]
    if holders:
        return (f"The query reads [{spelled}] from a table that does not have that column. It exists only in "
                + ", ".join(f"[{t}]" for t in holders)
                + ". Each SELECT may use only the columns of the table it reads, and the tables name the "
                  "same concept differently, so use the reading table's own column. Rewrite the query.")
    return (f"No table has a column [{spelled}]. The columns of every table are listed above; rewrite "
            "the query using only those.")


def misplaced_values(problems: list[tuple[str, str, list[str]]], schema) -> dict[tuple[str, str], list[tuple[str, list[str]]]]:
    """For each unknown value, the OTHER checkable columns that do hold it, with the values held.

    The commonest way to name a real thing wrongly is the level: 'Specific prevention
    interventions (SPI)' filtered on [Intervention] when it is a [Module]. The nearest
    interventions are then a red herring; what the model needs to hear is which column has it.
    """
    known = checkable_values(schema)
    names = {c.lower(): c for t in schema.tables for c, _ in t.columns}
    out: dict[tuple[str, str], list[tuple[str, list[str]]]] = {}
    for column, value, _ in problems:
        pattern = "%" in value
        # Only columns of the table(s) that have the flagged column: the fix must be a filter the
        # same SELECT can apply, not the other table's column of the same name.
        same_table = {c.lower() for t in schema.tables if column.lower() in {c.lower() for c, _ in t.columns}
                      for c, _ in t.columns}
        hits = []
        for k, vals in known.items():
            if k == column.lower() or k not in same_table:
                continue
            held = sorted(v for v in vals if (_like_matches(value, v) if pattern else v.lower() == value.lower()))
            if held:
                hits.append((names.get(k, k), held[:3]))
        if hits:
            out[(column, value)] = hits
    return out


def repair_request(problems: list[tuple[str, str, list[str]]],
                   elsewhere: dict[tuple[str, str], list[tuple[str, list[str]]]] | None = None) -> str:
    """What to send back to the model so it can correct itself."""
    lines: list[str] = []
    misplaced = 0
    for column, value, options in problems:
        held_by = (elsewhere or {}).get((column, value))
        if held_by:
            # The name is real, the level is wrong. The nearest values of the wrong column would
            # only mislead, so they are not offered; the right column and its exact value are.
            misplaced += 1
            where = "; ".join(f"[{col}] = " + " / ".join(f"'{v}'" for v in vals) for col, vals in held_by)
            lines.append(f"- {column}: '{value}' is not a {column} at all — it is a value of "
                         + ", ".join(f"[{col}]" for col, _ in held_by)
                         + f". Replace the {column} filter with {where}. Do not pick a {column} instead, "
                           f"and do not ask the user which {column} they meant.")
            continue
        what = (f"'{value}' is a LIKE pattern that matches none of the listed values"
                if "%" in value or "_" in value else f"'{value}' is not a valid value")
        if options:
            lines.append(f"- {column}: {what}. Closest valid values: " + ", ".join(f"'{o}'" for o in options))
        else:
            lines.append(f"- {column}: {what}, and nothing listed for {column} resembles it.")
    # The header sets the model's frame, so it must match the problem: a real name filtered at
    # the wrong level is not a misspelling, and "copy a listed value exactly" would send the
    # model shopping among the wrong column's values.
    if misplaced == len(problems):
        header = ("Your query filters on a real name but in the wrong column — the name belongs to "
                  "another level of the hierarchy.")
        tail = "Rewrite the query with the filter moved to the column named above; nothing else needs to change."
    else:
        header = ("Your query filters on values that do not exist in the database. The listed valid "
                  "values are the only ones that exist — copy one exactly, character for character, "
                  "including any prefix.")
        tail = ("Rewrite the query using exact valid values. If you cannot tell which value the "
                "user meant, answer with status 'clarify' and ask them, quoting the candidates.")
    return "\n".join([header, *lines, tail])

"""
Step 2 - Read the database.
For every table: columns, data types, per-column profile, a sample of rows,
and the most likely primary key (single column or composite).

Generic: nothing in this file knows about any particular dataset.
"""
import itertools
import re

# --------------------------------------------------------------------------- helpers
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
ID_TOKENS = {"id", "key", "pk", "fk", "uuid", "guid"}
SEQ_TOKENS = {"seq", "sequence", "sequential", "number", "num", "no", "index", "idx", "line", "position", "pos"}
NOISE_TABLE_TOKENS = {"dataset", "table", "tbl", "tb", "dim", "fact", "data", "tab", "view", "vw"}
KEY_FAMILIES = {"int", "text"}  # column types a key / join column can reasonably have


def q(ident):
    """Quote an SQL identifier."""
    return '"' + ident.replace('"', '""') + '"'


def qt(schema, table):
    return f"{q(schema)}.{q(table)}" if schema else q(table)


def tokens(name):
    """'customerZipCode' or 'customer_zip_code' -> ['customer', 'zip', 'code']"""
    return [t for t in re.split(r"[^A-Za-z0-9]+", _CAMEL.sub("_", name).lower()) if t]


def singular(tok):
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if tok.endswith(("ss", "us", "is")):
        return tok
    if len(tok) > 3 and tok.endswith("s"):
        return tok[:-1]
    return tok


def table_tokens(table):
    return {singular(t) for t in tokens(table) if t not in NOISE_TABLE_TOKENS}


def type_family(sql_type):
    """Collapse a database type into: int, text, float, decimal, bool, time, other."""
    t = str(sql_type).lower()
    if "interval" in t:
        return "other"
    if "bool" in t:
        return "bool"
    if any(k in t for k in ("timestamp", "date", "time")):
        return "time"
    if any(k in t for k in ("int", "serial")):
        return "int"
    if any(k in t for k in ("float", "double", "real")):
        return "float"
    if any(k in t for k in ("numeric", "decimal", "money")):
        return "decimal"
    if any(k in t for k in ("char", "text", "uuid", "string", "citext")):
        return "text"
    return "other"


def id_name_score(col, table):
    """How much does this column NAME look like an identifier of `table`? (0..1)"""
    toks = tokens(col)
    if not toks:
        return 0.0
    own = table_tokens(table)
    if toks == ["id"]:
        return 1.0
    if toks[-1] in ID_TOKENS:
        return 0.95 if any(singular(t) in own for t in toks[:-1]) else 0.8
    if any(t in ID_TOKENS for t in toks):
        return 0.5
    if col.lower().endswith("id") and len(col) > 2:
        return 0.6
    if all(singular(t) in own for t in toks):
        return 0.6
    if toks[-1] in SEQ_TOKENS:
        return 0.5 if any(singular(t) in own for t in toks[:-1]) else 0.4
    return 0.0


class Db:
    """Thin wrapper around a SQLAlchemy engine: list tables, list columns, run a query."""

    def __init__(self, engine, schema=None):
        self.engine, self.schema = engine, schema

    def tables(self):
        from sqlalchemy import inspect
        return sorted(inspect(self.engine).get_table_names(schema=self.schema))

    def columns(self, table):
        from sqlalchemy import inspect
        cols = inspect(self.engine).get_columns(table, schema=self.schema)
        return [(c["name"], str(c["type"])) for c in cols]

    def query(self, sql):
        from sqlalchemy import text
        with self.engine.connect() as conn:
            res = conn.execute(text(sql))
            return list(res.keys()), [tuple(r) for r in res.fetchall()]


def as_db(obj, schema=None):
    """Accept a SQLAlchemy engine or any object that already has tables/columns/query."""
    if all(hasattr(obj, m) for m in ("tables", "columns", "query")):
        return obj
    return Db(obj, schema)


# --------------------------------------------------------------------------- profiling
def _json_safe(v, max_len=60):
    if v is None or isinstance(v, (bool, int, float)):
        return v
    s = str(v)
    return s if len(s) <= max_len else s[:max_len] + "..."


def _profile_table(db, table, names, types, chunk=40):
    """One scan per chunk of columns: row count, non-null count, distinct count."""
    schema = getattr(db, "schema", None)
    qtab = qt(schema, table)
    row_count, stats = 0, {}
    for i in range(0, len(names), chunk):
        part = names[i:i + chunk]
        sel = ["COUNT(*)"]
        for col in part:
            sel += [f"COUNT({q(col)})", f"COUNT(DISTINCT {q(col)})"]
        _, rows = db.query(f"SELECT {', '.join(sel)} FROM {qtab}")
        r = rows[0]
        row_count = int(r[0])
        for j, col in enumerate(part):
            stats[col] = (int(r[1 + 2 * j]), int(r[2 + 2 * j]))
    profile = {}
    for col in names:
        non_null, distinct = stats.get(col, (0, 0))
        fam = type_family(types[col])
        profile[col] = {
            "type": types[col],
            "family": fam,
            "non_null": non_null,
            "nulls": row_count - non_null,
            "null_fraction": round((row_count - non_null) / row_count, 4) if row_count else 0.0,
            "distinct": distinct,
            "is_unique": bool(non_null > 0 and distinct == non_null and fam in KEY_FAMILIES),
        }
    return row_count, profile


def _sample(db, table, n):
    schema = getattr(db, "schema", None)
    cols, rows = db.query(f"SELECT * FROM {qt(schema, table)} LIMIT {int(n)}")
    return [{c: _json_safe(v) for c, v in zip(cols, r)} for r in rows]


# --------------------------------------------------------------------------- primary key
def infer_primary_key(db, table, names, profile, row_count, max_composite=3, candidates=6):
    """
    Returns (columns, confidence, reason).  columns == [] means no key was found.
      1. a single column that is unique and never null (best-named one wins)
      2. otherwise the smallest combination of key-like columns that is unique
    A column that is merely "mostly unique" is NEVER accepted.
    """
    if row_count == 0:
        return [], 0.0, "table is empty"

    singles = [c for c in names
               if profile[c]["family"] in KEY_FAMILIES
               and profile[c]["nulls"] == 0 and profile[c]["distinct"] == row_count]
    if singles:
        best = max(singles, key=lambda c: (id_name_score(c, table), -names.index(c)))
        return [best], round(0.6 + 0.4 * id_name_score(best, table), 3), \
            f"unique and non-null ({len(singles)} candidate(s)); chosen by name"

    elig = [c for c in names
            if profile[c]["family"] in KEY_FAMILIES and profile[c]["nulls"] == 0 and profile[c]["distinct"] > 1]
    elig.sort(key=lambda c: (-id_name_score(c, table), -profile[c]["distinct"], names.index(c)))
    cands = elig[:candidates]
    qtab = qt(getattr(db, "schema", None), table)
    for size in range(2, max_composite + 1):
        found = []
        for combo in itertools.combinations(cands, size):
            product = 1
            for c in combo:
                product *= profile[c]["distinct"]
            if product < row_count:  # cannot be unique - skip the query
                continue
            cols_sql = ", ".join(q(c) for c in combo)
            _, rows = db.query(f"SELECT COUNT(*) FROM (SELECT DISTINCT {cols_sql} FROM {qtab}) x")
            if int(rows[0][0]) == row_count:
                found.append(combo)
        if found:
            def mean_name(cb):
                return sum(id_name_score(c, table) for c in cb) / len(cb)
            best = max(found, key=mean_name)
            return list(best), round(0.5 + 0.3 * mean_name(best), 3), \
                f"no single unique column; smallest unique combination has {size} columns"
    return [], 0.0, "no unique column or small unique combination"


def get_table_info(engine, schema=None, sample_size=5, verbose=True):
    """
    {table: {columns, column_types, row_count, profile, sample, unique_columns,
             inferred_pk (list of columns), pk_confidence, pk_reason}}
    """
    db = as_db(engine, schema)
    info = {}
    for table in db.tables():
        cols = db.columns(table)
        names = [c for c, _ in cols]
        types = dict(cols)
        row_count, profile = _profile_table(db, table, names, types)
        sample = _sample(db, table, sample_size)
        for col in names:
            vals = []
            for row in sample:
                v = row.get(col)
                if v is not None and v not in vals:
                    vals.append(v)
            profile[col]["sample_values"] = vals[:5]
        pk, conf, reason = infer_primary_key(db, table, names, profile, row_count)
        info[table] = {
            "columns": names,
            "column_types": types,
            "row_count": row_count,
            "profile": profile,
            "sample": sample,
            "unique_columns": [c for c in names if profile[c]["is_unique"]],
            "inferred_pk": pk,
            "pk_confidence": conf,
            "pk_reason": reason,
        }
        if verbose:
            print(f"  {table}: {row_count} rows, PK={pk or 'none'} (confidence {conf})")
    return info
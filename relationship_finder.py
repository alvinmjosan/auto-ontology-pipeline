"""
Step 3 - Find the relationships (no foreign keys exist in the database).

For every (child column -> unique parent column) pair we combine three kinds of evidence:
  * name similarity      (token based, not just exact equality)
  * value containment    (share of the child's distinct values that exist in the parent column)
  * parent role          (is the parent column the table's inferred primary key?)
into one confidence score between 0 and 1.

A foreign key must point at a UNIQUE parent column, so two child tables that merely share a
column (e.g. two tables both holding order_id) are never linked to each other.
Columns that are not unique on either side (e.g. a zip code) are reported as lower-confidence
"lookup" links.
"""
from db_inspector import (ID_TOKENS, KEY_FAMILIES, as_db, q, qt, singular, table_tokens, tokens)

MIN_CONFIDENCE = 0.50           # below this a link is dropped
ACCEPT_THRESHOLD = 0.80         # >= accepted ("high"), otherwise "review"
MIN_CONTAINMENT = 0.85          # child values must (almost) all exist in the parent
VALUE_ONLY_MIN_DISTINCT = 50    # names do not match: only consider columns with enough distinct values
LOOKUP_MIN_DISTINCT = 100
LOOKUP_MIN_CONTAINMENT = 0.90
LOOKUP_MAX_CONFIDENCE = 0.75
VERIFY_BAND = (0.50, 0.80)      # borderline links are double-checked by the LLM


# --------------------------------------------------------------------------- name evidence
def _owns(table, col):
    """Does the column name mention the table's own entity (e.g. customer_id in customers)?"""
    own = table_tokens(table)
    return any(singular(t) in own for t in tokens(col) if t not in ID_TOKENS)


def name_score(child_table, child_col, parent_table, parent_col):
    if child_col.lower() == parent_col.lower():
        return 1.0
    ct, pt = tokens(child_col), tokens(parent_col)
    parent_own = table_tokens(parent_table)
    child_core = [t for t in ct if t not in ID_TOKENS]
    parent_is_own_id = not [t for t in pt if t not in ID_TOKENS and singular(t) not in parent_own]
    # child column names the parent entity and the parent column is that entity's own id
    if child_core and all(singular(t) in parent_own for t in child_core) and parent_is_own_id:
        return 0.9
    # compare the "role" words left after removing each table's own name
    cr = {singular(t) for t in ct if t not in ID_TOKENS} - table_tokens(child_table)
    pr = {singular(t) for t in pt if t not in ID_TOKENS} - parent_own
    if cr and pr:
        return round(0.8 * len(cr & pr) / len(cr | pr), 3)
    full_c, full_p = {singular(t) for t in ct}, {singular(t) for t in pt}
    return round(0.6 * len(full_c & full_p) / len(full_c | full_p), 3) if full_c | full_p else 0.0


# --------------------------------------------------------------------------- value evidence
def _containment(db, ct, cc, pt, pc):
    """(distinct child values, how many of them exist in the parent column)."""
    schema = getattr(db, "schema", None)
    sql = (f"SELECT COUNT(*), COUNT(b.v) "
           f"FROM (SELECT DISTINCT {q(cc)} AS v FROM {qt(schema, ct)} WHERE {q(cc)} IS NOT NULL) a "
           f"LEFT JOIN (SELECT DISTINCT {q(pc)} AS v FROM {qt(schema, pt)} WHERE {q(pc)} IS NOT NULL) b "
           f"ON a.v = b.v")
    _, rows = db.query(sql)
    return int(rows[0][0]), int(rows[0][1])


# --------------------------------------------------------------------------- main entry point
def find_relationships(engine, table_info, llm_verifier=None, verbose=True):
    """
    Returns a list of dicts:
      child_table, child_column, parent_table, parent_column, kind, cardinality,
      confidence, tier, evidence
    `llm_verifier(relationship) -> {"references": bool, "reason": str}` is optional.
    The LLM can only LOWER a score (and force "review"); it can never create a link and never deletes one silently.
    """
    db = as_db(engine)
    pk_of = {t: (i["inferred_pk"][0] if len(i["inferred_pk"]) == 1 else None) for t, i in table_info.items()}

    # ---- 1. foreign-key candidates: child column -> UNIQUE parent column
    cands = {}
    for ct, ci in table_info.items():
        for cc in ci["columns"]:
            cp = ci["profile"][cc]
            if cp["family"] not in KEY_FAMILIES or cp["non_null"] == 0:
                continue
            for pt, pi in table_info.items():
                for pc in pi["unique_columns"]:
                    if (ct, cc) == (pt, pc):
                        continue
                    pp = pi["profile"][pc]
                    if pp["family"] != cp["family"]:
                        continue
                    if cp["distinct"] > pp["distinct"] * 1.1 + 5:   # child cannot hold far more values than parent
                        continue
                    ns = name_score(ct, cc, pt, pc)
                    value_only_ok = (cp["distinct"] >= VALUE_ONLY_MIN_DISTINCT
                                     and pp["distinct"] >= VALUE_ONLY_MIN_DISTINCT)
                    if ns < 0.4 and not value_only_ok:
                        continue
                    total, matched = _containment(db, ct, cc, pt, pc)
                    if total == 0:
                        continue
                    containment = matched / total
                    if containment < MIN_CONTAINMENT:
                        continue
                    coverage = min(1.0, matched / pp["distinct"]) if pp["distinct"] else 0.0
                    parent_is_pk = pk_of[pt] == pc
                    conf = (0.45 * containment + 0.35 * ns
                            + 0.10 * (1.0 if parent_is_pk else 0.6) + 0.10 * coverage)
                    cands[(ct, cc, pt, pc)] = {
                        "child_table": ct, "child_column": cc, "parent_table": pt, "parent_column": pc,
                        "kind": "foreign_key",
                        "cardinality": "one-to-one" if cc in ci["unique_columns"] else "many-to-one",
                        "confidence": round(conf, 3),
                        "evidence": {
                            "containment": round(containment, 4),
                            "parent_coverage": round(coverage, 4),
                            "name_score": ns,
                            "child_distinct": cp["distinct"],
                            "parent_distinct": pp["distinct"],
                            "parent_is_primary_key": parent_is_pk,
                        },
                    }

    # ---- 2. when both columns are unique the link could point either way: pick the owner as parent
    for key in list(cands):
        ct, cc, pt, pc = key
        rev = (pt, pc, ct, cc)
        if key in cands and rev in cands:
            a_score = (_owns(pt, pc) and not _owns(ct, cc),
                       pk_of[pt] == pc and pk_of[ct] != cc,
                       cands[key]["confidence"])
            b_score = (_owns(ct, cc) and not _owns(pt, pc),
                       pk_of[ct] == cc and pk_of[pt] != pc,
                       cands[rev]["confidence"])
            del cands[rev if a_score >= b_score else key]

    # ---- 3. keep the best parent for every child column
    best = {}
    for key, rel in cands.items():
        slot = best.setdefault((key[0], key[1]), [])
        slot.append(rel)
    relationships = []
    for (ct, cc), rels in best.items():
        rels.sort(key=lambda r: -r["confidence"])
        top = rels[0]
        if len(rels) > 1:
            top["evidence"]["alternatives"] = [
                f'{r["parent_table"]}.{r["parent_column"]} ({r["confidence"]})' for r in rels[1:3]]
        relationships.append(top)

    # ---- 4. lookup links: non-unique columns (e.g. zip codes) that no foreign key already explains
    linked_children = {(r["child_table"], r["child_column"]) for r in relationships}
    for ct, ci in table_info.items():
        for cc in ci["columns"]:
            cp = ci["profile"][cc]
            if (ct, cc) in linked_children or cc in ci["unique_columns"]:
                continue
            if cp["family"] not in KEY_FAMILIES or cp["distinct"] < LOOKUP_MIN_DISTINCT:
                continue
            options = []
            for pt, pi in table_info.items():
                if pt == ct:
                    continue
                for pc in pi["columns"]:
                    pp = pi["profile"][pc]
                    if pc in pi["unique_columns"] or pp["family"] != cp["family"]:
                        continue
                    if pp["distinct"] < cp["distinct"]:    # the lookup side must be the larger value set
                        continue
                    ns = name_score(ct, cc, pt, pc)
                    if ns < 0.6:
                        continue
                    total, matched = _containment(db, ct, cc, pt, pc)
                    if total == 0 or matched / total < LOOKUP_MIN_CONTAINMENT:
                        continue
                    containment = matched / total
                    conf = min(LOOKUP_MAX_CONFIDENCE, 0.5 * containment + 0.4 * ns + 0.05)
                    options.append((containment, pp["distinct"], {
                        "child_table": ct, "child_column": cc, "parent_table": pt, "parent_column": pc,
                        "kind": "lookup", "cardinality": "many-to-many",
                        "confidence": round(conf, 3),
                        "evidence": {"containment": round(containment, 4), "name_score": ns,
                                     "child_distinct": cp["distinct"], "parent_distinct": pp["distinct"],
                                     "note": "neither side is unique; shared value domain, not a strict foreign key"},
                    }))
            if options:
                options.sort(key=lambda o: (-o[0], -o[1]))
                relationships.append(options[0][2])

    # ---- 5. optional LLM double-check of borderline links (can only lower the score)
    if llm_verifier:
        for rel in relationships:
            if VERIFY_BAND[0] <= rel["confidence"] < VERIFY_BAND[1]:
                verdict = llm_verifier(rel)
                if verdict:
                    rel["evidence"]["llm_check"] = verdict
                    if verdict.get("references") is False:
                        # lower the score and force "review", but keep the link visible with the LLM's objection
                        rel["confidence"] = round(rel["confidence"] * 0.7, 3)
                        rel["force_review"] = True

    relationships = [r for r in relationships if r["confidence"] >= MIN_CONFIDENCE]
    for r in relationships:
        r["tier"] = "high" if r["confidence"] >= ACCEPT_THRESHOLD and not r.pop("force_review", False) else "review"
        r.pop("force_review", None)
    relationships.sort(key=lambda r: -r["confidence"])
    if verbose:
        print(f"  Found {len(relationships)} relationships "
              f"({sum(r['tier'] == 'high' for r in relationships)} high confidence)")
    return relationships
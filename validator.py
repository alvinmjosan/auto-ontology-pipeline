"""
Step 4 (check) - How we verify the LLM output instead of trusting it.

Every claim in the final ontology is tested against the real database:
  1. Grounding     entity -> real table; attribute -> real column OF THAT table (nothing invented)
  2. Completeness  every entity/attribute has a real description (not empty, not a template);
                   every table is covered; entity names are unique
  3. Relationships both entities exist, both columns exist in the right tables, and the stored
                   evidence is re-computed with a fresh SQL join (child values missing from parent)
The report lists every failure; "all checks passed" is only written when there are none.
"""
import os
import re

import yaml

from db_inspector import as_db
from relationship_finder import _containment

TEMPLATED = re.compile(r"business entity (represented|representing)|^attribute \w+ for |^\s*$", re.I)


def _words(s):
    return len((s or "").split())


def validate_ontology(ontology_path, table_info, engine, report_path):
    db = as_db(engine)
    with open(ontology_path, encoding="utf-8") as f:
        onto = yaml.safe_load(f)
    entities, rels = onto.get("entities", []) or [], onto.get("relationships", []) or []
    issues, checks = [], {}

    def record(name, total, bad):
        checks[name] = (total - bad, total)

    # ---- 1 + 2. entities and attributes
    bad_ground = bad_desc = bad_meaning = bad_attr_ground = n_attr = 0
    names = [e.get("name") for e in entities]
    entity_table = {}
    for e in entities:
        table = e.get("source_table")
        entity_table[e.get("name")] = table
        if table not in table_info:
            bad_ground += 1
            issues.append(f"Entity '{e.get('name')}' points to table '{table}' which does not exist")
            continue
        if _words(e.get("description")) < 3 or TEMPLATED.search(e.get("description") or ""):
            bad_desc += 1
            issues.append(f"Entity '{e.get('name')}' has an empty or templated description")
        real_cols = set(table_info[table]["columns"])
        for a in e.get("attributes", []):
            n_attr += 1
            if a.get("column") not in real_cols:
                bad_attr_ground += 1
                issues.append(f"Attribute '{a.get('name')}' of '{e.get('name')}' maps to column "
                              f"'{a.get('column')}' which is not in table '{table}'")
            if TEMPLATED.search(a.get("meaning") or "") or _words(a.get("meaning")) < 1:
                bad_meaning += 1
                issues.append(f"Attribute '{a.get('column')}' of '{e.get('name')}' has no real meaning")
    record("Entities map to real tables", len(entities), bad_ground)
    record("Attributes map to real columns of their own table", n_attr, bad_attr_ground)
    record("Entities have real descriptions", len(entities), bad_desc)
    record("Attributes have real meanings", n_attr, bad_meaning)

    covered = {e.get("source_table") for e in entities}
    missing_tables = [t for t in table_info if t not in covered]
    for t in missing_tables:
        issues.append(f"Table '{t}' has no entity in the ontology")
    record("Every table is covered by an entity", len(table_info), len(missing_tables))
    dupes = len(names) - len(set(names))
    if dupes:
        issues.append("Entity names are not unique")
    record("Entity names are unique", len(names), dupes)

    # ---- 3. relationships
    bad_rel = bad_sql = 0
    for r in rels:
        label = f"{r.get('from_entity')}.{r.get('from_column')} -> {r.get('to_entity')}.{r.get('to_column')}"
        ct, pt = entity_table.get(r.get("from_entity")), entity_table.get(r.get("to_entity"))
        if ct not in table_info or pt not in table_info:
            bad_rel += 1
            issues.append(f"Relationship {label}: an entity does not exist")
            continue
        if r.get("from_column") not in table_info[ct]["columns"] or r.get("to_column") not in table_info[pt]["columns"]:
            bad_rel += 1
            issues.append(f"Relationship {label}: a column does not exist in its table")
            continue
        if _words(r.get("description")) < 3:
            bad_rel += 1
            issues.append(f"Relationship {label}: missing description")
        total, matched = _containment(db, ct, r["from_column"], pt, r["to_column"])
        actual = matched / total if total else 0.0
        stored = r.get("evidence", {}).get("containment")
        if stored is None or abs(actual - stored) > 0.01:
            bad_sql += 1
            issues.append(f"Relationship {label}: stored containment {stored} differs from re-computed {actual:.4f}")
        elif r.get("kind") == "foreign_key" and actual < 0.85:
            bad_sql += 1
            issues.append(f"Relationship {label}: only {actual:.1%} of child values exist in the parent")
    record("Relationships reference real entities and columns", len(rels), bad_rel)
    record("Relationship evidence confirmed by a fresh SQL join", len(rels), bad_sql)
    if not rels:
        issues.append("The ontology contains no relationships")

    # ---- report
    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("# Validation report\n\n| Check | Passed | Total |\n|---|---|---|\n")
        for name, (ok, total) in checks.items():
            f.write(f"| {name} | {ok} | {total} |\n")
        weak = [r for r in rels if r.get("tier") == "review"]
        f.write(f"\nRelationships: {len(rels)} ({len(rels) - len(weak)} high confidence, {len(weak)} flagged for review)\n\n")
        if issues:
            f.write(f"## Issues ({len(issues)})\n\n" + "\n".join(f"- {i}" for i in issues) + "\n")
        else:
            f.write("All checks passed.\n")
    print(f"  Validation: {len(issues)} issue(s). Report: {report_path}")
    return {"checks": checks, "issues": issues}
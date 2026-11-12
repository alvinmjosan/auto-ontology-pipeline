"""
Step 4 - Create the final ontology file.

Inputs
  * the raw export from NVIDIA Auto Ontology   (optional: which tables are entities, which columns are
                                                attributes; pass None to use all tables from table_info)
  * the LLM results                            (entity names, descriptions, attribute meanings)
  * our inferred relationships                 (Auto Ontology finds none when the DB has no foreign keys)
Output
  one YAML file: entities (+ attributes with meanings) and relationships between entities.

When a raw export is provided, entities and attributes are linked to real tables/columns through the ids
inside the export, so no name guessing or dataset-specific string handling is needed.
When no export is provided, all tables in table_info are used as entities (schema-only fallback).
"""
import os

import yaml


def _index_export(raw):
    tables, columns = {}, {}
    for db in raw.get("data_layer", {}).get("databases", []):
        for schema in db.get("schemas", []):
            for table in schema.get("tables", []):
                tables[table["id"]] = table["name"]
                for col in table.get("columns", []):
                    columns[col["id"]] = (table["name"], col["name"], col.get("type"))
    return tables, columns


def _entities_from_export(raw, llm_results, table_info):
    """Build entity list from an Auto Ontology export (the primary path)."""
    table_by_id, column_by_id = _index_export(raw)
    terms = raw.get("semantic_layer", {}).get("terms", [])
    entities, warnings = [], []
    for term in terms:
        represents = term.get("represents") or []
        table = table_by_id.get(represents[0]) if represents else None
        if table is None or table not in llm_results or table not in table_info:
            warnings.append(f"term '{term.get('name')}' could not be matched to a described table; skipped")
            continue
        llm, info = llm_results[table], table_info[table]
        attributes = []
        for attr in term.get("columns_attributes", []):
            _, column, col_type = column_by_id.get(attr.get("column_id"), (None, None, None))
            if column is None or column not in info["columns"]:
                warnings.append(f"attribute '{attr.get('name')}' of '{table}' has no matching column; skipped")
                continue
            attributes.append({
                "name": attr["name"],
                "column": column,
                "type": info["column_types"].get(column, col_type),
                "meaning": llm["attributes"].get(column, ""),
                "is_primary_key": column in info["inferred_pk"],
            })
        entities.append({
            "name": llm["entity_name"],
            "source_table": table,
            "description": llm["description"],
            "primary_key": info["inferred_pk"],
            "row_count": info["row_count"],
            "attributes": attributes,
        })
    return entities, warnings, len(terms)


def _entities_from_schema(llm_results, table_info):
    """Fallback: build entity list directly from table_info (no Auto Ontology export)."""
    entities, warnings = [], []
    for table, info in table_info.items():
        if table not in llm_results:
            warnings.append(f"table '{table}' has no LLM description; skipped")
            continue
        llm = llm_results[table]
        attributes = []
        for column in info["columns"]:
            col_type = info["column_types"].get(column, "")
            attributes.append({
                "name": column,
                "column": column,
                "type": col_type,
                "meaning": llm["attributes"].get(column, ""),
                "is_primary_key": column in info["inferred_pk"],
            })
        entities.append({
            "name": llm["entity_name"],
            "source_table": table,
            "description": llm["description"],
            "primary_key": info["inferred_pk"],
            "row_count": info["row_count"],
            "attributes": attributes,
        })
    return entities, warnings


def _surrogate_key_notes(relationships, table_info):
    """
    Detect one-to-one relationships where the parent table's primary key might be a per-row
    surrogate.  A key is flagged when:
      - the relationship is foreign_key with cardinality one-to-one
      - the parent table has another column whose name suggests a 'unique' or 'uuid' variant
        of the same entity, AND that column has fewer distinct values than the row count
        (i.e. it identifies real-world entities that can appear on multiple rows).
    Returns a list of plain-English notes generated purely from the data.
    """
    notes = []
    for r in relationships:
        if r.get("kind") != "foreign_key" or r.get("cardinality") != "one-to-one":
            continue
        pt = r["parent_table"]
        pc = r["parent_column"]
        if pt not in table_info:
            continue
        info = table_info[pt]
        row_count = info["row_count"]
        # Look for a sibling column whose name contains 'unique' or 'uuid'
        for col in info["columns"]:
            if col == pc:
                continue
            col_lower = col.lower()
            if "unique" not in col_lower and "uuid" not in col_lower:
                continue
            profile = info["profile"].get(col, {})
            distinct = profile.get("distinct", row_count)
            if distinct < row_count:
                notes.append(
                    f"{pt}.{pc} is a per-row surrogate key: {distinct} distinct values in "
                    f"{pt}.{col} vs {row_count} rows, suggesting {col} identifies "
                    f"real-world entities that place multiple records."
                )
                break  # one note per relationship is enough
    return notes


def build_final_ontology(raw_export_path, relationships, llm_results, table_info,
                         sentence_fn, out_path, llm_model=None):
    """
    Build and write the ontology YAML.

    raw_export_path  - path to the NVIDIA Auto Ontology YAML export, or None to use the
                       schema-only fallback (all tables -> entities, all columns -> attributes).
    """
    if raw_export_path is not None:
        with open(raw_export_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
        entities, warnings, n_terms = _entities_from_export(raw, llm_results, table_info)
        semantic = raw.get("semantic_layer", {})
        data_layer = raw.get("data_layer", {})
        ao_rels = len(semantic.get("semantic_fks", []) or []) + len(data_layer.get("foreign_keys", []) or [])
        source_label = os.path.basename(raw_export_path)
    else:
        raw = {}
        entities, warnings = _entities_from_schema(llm_results, table_info)
        n_terms = 0
        ao_rels = 0
        source_label = "none (schema-only fallback)"
        warnings.insert(0, "No Auto Ontology export provided; entities built from inferred schema.")

    # Entity names must be unique so relationships can refer to them unambiguously.
    seen = {}
    entity_of_table = {}
    for e in entities:
        seen[e["name"]] = seen.get(e["name"], 0) + 1
    for e in entities:
        if seen[e["name"]] > 1:
            e["name"] = f'{e["name"]} ({e["source_table"]})'
        entity_of_table[e["source_table"]] = e["name"]

    rels_out = []
    for r in relationships:
        child = entity_of_table.get(r["child_table"])
        parent = entity_of_table.get(r["parent_table"])
        if not child or not parent:
            warnings.append(f"relationship {r['child_table']} -> {r['parent_table']} skipped: entity missing")
            continue
        sentence, source = sentence_fn(r, child, parent)
        rels_out.append({
            "from_entity": child,
            "to_entity": parent,
            "from_column": r["child_column"],
            "to_column": r["parent_column"],
            "kind": r["kind"],
            "cardinality": r["cardinality"],
            "description": sentence,
            "description_source": source,
            "confidence": r["confidence"],
            "tier": r["tier"],
            "evidence": r["evidence"],
        })

    # Generate data-driven notes (e.g. surrogate-key warnings) from the relationships themselves.
    data_notes = _surrogate_key_notes(relationships, table_info)

    ontology = {
        "metadata": {
            "source_export": source_label,
            "llm_model": llm_model,
            "auto_ontology_entities": n_terms,
            "auto_ontology_relationships_found": ao_rels,
            "relationships_inferred_by_this_tool": len(rels_out),
            "warnings": warnings,
            "data_notes": data_notes,
        },
        "entities": entities,
        "relationships": rels_out,
    }
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.dump(ontology, f, sort_keys=False, allow_unicode=True, width=100)
    return ontology

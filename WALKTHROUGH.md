# Walkthrough — 20-Minute Presentation Guide

This document explains every file, class, and key design decision so you can present the project confidently in a 20-minute call or demo session.

---

## Suggested Time Budget

| Segment | Minutes |
|---------|---------|
| Problem and approach | 2 |
| `load_data.py` + `docker-compose.yml` | 2 |
| `db_inspector.py` — schema reading and PK inference | 4 |
| `relationship_finder.py` — confidence-scored relationship discovery | 4 |
| `llm_client.py` — LLM calls, caching, validation | 3 |
| `normalizer.py` — building the final YAML | 2 |
| `validator.py` — cross-checking LLM output | 1 |
| Auto Ontology integration | 2 |
| Q&A | 5 |

---

## The Problem (2 min)

The Olist database has 9 tables and no foreign-key constraints. A reviewer looking at the raw schema cannot tell which tables represent business entities, what their attributes mean, or how the tables relate. The goal is to produce a machine-readable ontology — a YAML file with entity names, plain-English descriptions, attribute meanings, and inferred relationships — without hard-coding anything specific to Olist.

---

## `load_data.py` + `docker-compose.yml` (2 min)

**`docker-compose.yml`** runs PostgreSQL 15 (with pgvector) on port 5432 for the Olist data. This is separate from the Auto Ontology stack (which has its own `docker-compose.yml`).

**`load_data.py`** iterates over every `.csv` file in a given folder, strips and lower-cases the column names, and loads each file into a table named after the file using pandas `to_sql`. It never creates primary or foreign keys — the pipeline's own modules infer those from data.

```python
df.columns = [c.strip().lower().replace(' ', '_') for c in df.columns]
df.to_sql(table_name, engine, if_exists='replace', index=False)
```

**Why it is generic**: The filename becomes the table name. No table or column name is hard-coded.

---

## `db_inspector.py` — Schema Reading and PK Inference (4 min)

### `get_table_info(engine)`

For each table, produces: column names and SQL types, row count, per-column profile
(non-null count, distinct count, `is_unique` flag, 5 sample values), and the inferred
primary key with a confidence score.

### `infer_primary_key`

**Step 1 — Single-column candidates**: any column that is strictly unique (no nulls,
`distinct == row_count`) and of type `int` or `text`.

**Step 2 — Name scoring (`id_name_score`)**: among candidates, the one whose name best
describes the table's own entity wins. `customer_id` in `olist_customers_dataset` scores
0.95 (column mentions the table's entity + ends in `_id`).

**Step 3 — Composite fallback**: if no single column is unique, all 2- and 3-column
combinations of the best candidates are tested. `(order_id, order_item_id)` wins for
`olist_order_items_dataset`.

**Olist results**: 8 of 9 tables get a PK; `olist_geolocation_dataset` correctly gets
none because `geolocation_zip_code_prefix` is not unique.

---

## `relationship_finder.py` — Confidence-Scored Relationship Discovery (4 min)

### Core idea

Three signals combined into one confidence score (0–1):

| Signal | Role |
|--------|------|
| **Name similarity** | token-based Jaccard between column names, with table-prefix removal |
| **Value containment** | fraction of child's distinct values that exist in the parent column |
| **Parent role** | whether the parent column is that table's inferred PK |

### `_containment` SQL

```sql
SELECT COUNT(*), COUNT(b.v)
FROM (SELECT DISTINCT child_col AS v FROM child_table WHERE child_col IS NOT NULL) a
LEFT JOIN (SELECT DISTINCT parent_col AS v FROM parent_table WHERE parent_col IS NOT NULL) b
  ON a.v = b.v
```

### Thresholds

- Minimum containment for a foreign key: **85%**
- Minimum confidence to include: **0.50**; ≥ **0.80** = "high" tier
- Borderline links (0.50–0.80) go to the LLM for a second opinion — the LLM can only
  **lower** a score (by 30%) and cannot create or silently delete links

### Lookup links

Zip codes and city names are non-unique on both sides. These are handled separately:
both columns must have ≥ 100 distinct values, name score ≥ 0.60, and ≥ 90% containment.
Tagged `kind: lookup, cardinality: many-to-many`, capped at confidence 0.75.

**Olist result**: 10 relationships — 7 high-confidence FKs + 3 lookup links to geolocation.

---

## `llm_client.py` — LLM Calls, Caching, Validation (3 min)

### Three tasks

| Function | What it does |
|----------|-------------|
| `describe_table` | Entity name, description, meaning for every column |
| `describe_relationship` | One business sentence per relationship |
| `verify_relationship` | Binary: does child really reference parent? |

### Article-correct sentence generation

`_article(word)` returns "An" before vowel-initial words and "A" otherwise — computed in
code, not left to the LLM. The cardinality is passed explicitly in the prompt so the LLM
uses the right quantifier (one-to-one vs many). A regex check in `check()` rejects any
sentence that still contains "A Order", "A Item", etc., and triggers a self-correction pass.

> **Known limitation**: `_article()` checks only the first character of the word, not its
> pronunciation. It correctly handles "An Order" and "An Item" but would produce "An hour"
> (correct by accident — "h" is not a vowel) and "A university" (wrong — "u" sounds like
> "you"). For entity names drawn from table names this is never triggered in practice, but
> it is worth noting if the tool is extended to handle freeform LLM-generated entity names.

### Output validation and self-correction

Every response goes through a `check(data)` function. For `describe_table`, the check
verifies entity name, description length, and that every column appears with a real meaning.
If problems remain, the client sends one follow-up message listing the issues and asks for
a corrected JSON object.

### Disk cache

Every request is SHA-256-keyed on `(model, system_prompt, user_prompt)`. Cached responses
are loaded on the next run, making the pipeline fully reproducible from `.llm_cache/`
without an API key.

---

## `normalizer.py` — Building the Final YAML (2 min)

### With an Auto Ontology export

The export's `data_layer` indexes tables and columns by UUID. The `semantic_layer.terms`
list maps each term to table UUIDs. `build_final_ontology` looks up each UUID, finds the
LLM result for that table, and assembles entities with attributes.

### Without an export (fallback)

When `raw_export_path` is `None`, every table in `table_info` becomes an entity and every
column an attribute. The LLM results are identical either way.

### Data-driven surrogate-key notes

`_surrogate_key_notes()` scans one-to-one relationships for sibling columns in the parent
table whose name contains "unique" or "uuid" and whose distinct count is below the row count.
When found, it generates a plain-English note from the actual numbers — no hardcoded strings.

> **Known limitation**: the column name check is a simple substring match on `"unique"` or
> `"uuid"`. A surrogate-key column named `cust_identifier` or `real_customer_ref` would be
> missed entirely. This is acceptable for Olist (the column is literally called
> `customer_unique_id`), but a production version would use cardinality heuristics alone,
> without relying on the column name.

---

## `validator.py` — Cross-Checking LLM Output (1 min)

Eight checks run against the real database:

| Check | What it verifies |
|-------|-----------------|
| Entities → real tables | `source_table` exists in `table_info` |
| Attributes → real columns | `column` is in that table's column list |
| Entity descriptions | ≥ 3 words, no template patterns |
| Attribute meanings | ≥ 1 word, no template patterns |
| Coverage | every table maps to at least one entity |
| Unique entity names | relationships can reference them unambiguously |
| Relationships → real entities/columns | both sides exist |
| SQL evidence | fresh containment query matches stored value within 1% |

**Olist result**: All 8 checks passed across 9 entities, 52 attributes, 10 relationships.

---

## Auto Ontology Integration (2 min)

NVIDIA Auto Ontology ([github.com/NVIDIA/auto-ontology](https://github.com/NVIDIA/auto-ontology),
commit `fc43a826`) is an open-source library that runs as a local Docker stack.

**Steps used for Olist**:
1. `git clone https://github.com/NVIDIA/auto-ontology.git && cd auto-ontology`
2. Copy `.env.example` to `.env`, fill in `NVIDIA_API_KEY` and DB credentials
3. `docker compose up --build` (frontend on :3000, backend :3001, ingestion :3002)
4. Open `http://localhost:3000` → Settings → Data Sources → add the Olist DB
5. Click **Compile** (Semantic Compilation) — the LLM proposes entity names per table
6. Click **Export → YAML** → save to `outputs/ontology_olist.yaml`

**What it contributed**: a UUID-based mapping from tables to business term names. All
descriptions were empty; all FK relationships were missing (Olist has none defined).
This pipeline's LLM and relationship-finder steps fill in what Auto Ontology left blank.

---

## Likely Questions

**Q: Why is Order → Customer cardinality `one-to-one`?**
A: `customer_id` in `olist_orders_dataset` is unique — every order gets a fresh
`customer_id`. The statistical reality is 1-to-1, so `cardinality: one-to-one` is correct.
The `data_notes` field (generated from `customer_unique_id` distinct count vs row count)
explains the surrogate-key situation. The relationship sentence says "An Order belongs to one Customer" to match the data.

**Q: What does Auto Ontology actually add if it produces empty descriptions?**
A: The UUID-based entity mapping in the export decouples the ontology from raw table names.
If a table is renamed, the UUID still links the term to the right column. Auto Ontology also
serves as the starting point for human curation in its web UI before exporting.

**Q: What threshold does the relationship finder use?**
A: Minimum containment 85% for a foreign key, 90% for a lookup. Minimum overall confidence
0.50 to be included; ≥ 0.80 for the "high" tier. Borderline links go to the LLM for a
second opinion.

**Q: How do you know it works on an unseen database?**
A: Every function takes generic inputs. There is no `if table == "olist_orders"` anywhere.
The main limitation (documented in NOTES.md) is that per-row surrogate keys look 1-to-1
statistically, so a human review of cardinalities is recommended for production use.

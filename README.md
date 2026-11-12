# Auto-Build an Ontology from Any PostgreSQL Database

A fully generic Python tool that reads any PostgreSQL database and produces a business ontology — entity names, descriptions, attribute meanings, and inferred relationships — using NVIDIA Auto Ontology and the NVIDIA LLM API.  
Demonstrated on the Brazilian e-commerce dataset (Olist).

---

## Table of Contents

1. [How It Works](#how-it-works)
2. [Prerequisites](#prerequisites)
3. [Quick Start](#quick-start)
4. [Auto Ontology Steps](#auto-ontology-steps)
5. [Running the Full Pipeline](#running-the-full-pipeline)
6. [Output Files](#output-files)
7. [How the LLM Output Is Checked](#how-the-llm-output-is-checked)
8. [Project Structure](#project-structure)
9. [Configuration Reference](#configuration-reference)

---

## How It Works

The pipeline has five steps, each in its own module:

```
PostgreSQL DB
     │
     ▼
Step 1 · db_inspector.py      ── columns, types, row counts, 5-row samples,
     │                            inferred primary keys (single or composite)
     ▼
Step 2 · relationship_finder.py ─ name similarity + value-containment scores,
     │                            optional LLM double-check on borderline links
     ▼
Step 3 · llm_client.py        ── NVIDIA LLM: entity names, descriptions,
     │                            attribute meanings, relationship sentences
     ▼
Step 4 · normalizer.py        ── merges Auto Ontology export (or inferred schema)
     │                            with LLM results → ontology_final.yaml
     ▼
Step 5 · validator.py         ── cross-checks every LLM claim against the real
                                 schema and re-runs SQL to confirm relationships
                                 → validation_report.md
```

Nothing in the code is specific to Olist. The only Olist-specific files are the
CSVs in `olist_csvs/` and the cached LLM responses in `.llm_cache/`.

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.11+ | tested on 3.13 |
| Docker + Docker Compose | for the local PostgreSQL instance used by this tool |
| NVIDIA API key | free tier at [build.nvidia.com](https://build.nvidia.com) |
| NVIDIA Auto Ontology | cloned from [github.com/NVIDIA/auto-ontology](https://github.com/NVIDIA/auto-ontology) and run locally |
| Olist CSVs | download from [Kaggle](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce) |

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your keys:

```bash
cp .env.example .env
# edit .env
```

---

## Quick Start

```bash
# 1. Start Postgres (for Olist data)
docker-compose up -d

# 2. Enable pgvector extension in the olist database (required for Auto Ontology)
docker exec -it genaiprotos-postgres-1 psql -U postgres -d olist -c "CREATE EXTENSION IF NOT EXISTS vector;"

# 3. Load Olist CSVs (place them in ./olist_csvs first)
python load_data.py

# 4. Run Auto Ontology against the database (see § Auto Ontology Steps)
#    This produces outputs/ontology_olist.yaml

# 5. Run the full pipeline
python main.py \
  --db "postgresql://postgres:postgres@localhost:5432/olist" \
  --raw-export outputs/ontology_olist.yaml
```

The `--raw-export` flag is optional. If you omit it, the pipeline falls back to
building the ontology from the inferred schema alone — useful when testing on an
unseen database before you have an Auto Ontology export.

---

## Auto Ontology Steps

> **What NVIDIA Auto Ontology does**
> Auto Ontology is an open-source library (Apache 2.0) from NVIDIA that builds a
> semantic layer over relational data. It runs as a local Docker stack (FastAPI
> backend on port 3001, Next.js frontend on port 3000, ingestion worker on port 3002,
> Postgres + pgvector on port 5432). You connect it to any database, trigger
> **Semantic Compilation** to generate business entity names, then export the result
> as a YAML file containing a `data_layer` (raw schema with UUIDs) and a
> `semantic_layer` (LLM-proposed terms and attributes).

The exact steps used to produce `outputs/ontology_olist.yaml`:

1. **Clone the repository** (commit `fc43a826`):
   ```bash
   git clone https://github.com/NVIDIA/auto-ontology.git
   cd auto-ontology
   ```

2. **Configure Auto Ontology**:
   ```bash
   cp .env.example .env
   # In .env set:
   #   NVIDIA_API_KEY=<your key>
   #   POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DATABASE
   ```

3. **Start the full stack**:
   ```bash
   docker compose up --build
   # Services: frontend :3000, backend :3001, ingestion :3002, postgres :5432
   ```

4. **Add the Olist data source** — open `http://localhost:3000`, navigate to
   **Settings → Data Sources**, and add a PostgreSQL connection pointing at the
   Olist database (`localhost:5432 / olist`).

5. **Run Semantic Compilation** — from the data source detail page, click
   **Compile**. Auto Ontology introspects the schema, calls the NVIDIA LLM to
   propose entity names for each table, and writes its results to its internal
   Postgres catalog.

6. **Export the ontology YAML** — click **Export → YAML** (or use the CLI:
   `auto-ontology export --format yaml`). Save the file to
   `outputs/ontology_olist.yaml` in this project.

7. **Run this pipeline**:
   ```bash
   python main.py \
     --db "postgresql://postgres:postgres@localhost:5432/olist" \
     --raw-export outputs/ontology_olist.yaml
   ```

**What Auto Ontology produced for Olist**
The raw export (`outputs/ontology_olist.yaml`) contained:
- 9 table-level terms (one per table, named after the table).
- Placeholder (empty) descriptions for all entities and attributes.
- 0 foreign-key relationships — expected, because Olist has no FK constraints.

This is why the pipeline's own relationship finder and LLM client are essential:
they fill in what Auto Ontology could not detect from the schema alone.

**Running against a new database without an export**
Omit `--raw-export` (or point it at a file that does not exist yet). The pipeline
will warn you and continue using all inferred tables as entities. You can overlay
a real Auto Ontology export and re-run once you have one.

---

## Running the Full Pipeline

```bash
python main.py \
  --db "postgresql+psycopg2://user:pass@host:5432/dbname" \
  --raw-export path/to/auto_ontology_export.yaml \
  --out outputs
```

| Flag | Required | Default | Description |
|------|----------|---------|-------------|
| `--db` | ✅ | — | PostgreSQL connection string |
| `--raw-export` | ❌ | None | Path to Auto Ontology YAML export |
| `--out` | ❌ | `outputs` | Folder for output files |

The script exits with code `0` if validation passes, `1` if there are issues.

---

## Output Files

| File | Description |
|------|-------------|
| `outputs/ontology_final.yaml` | The finished ontology: 9 entities, 52 attributes, 10 relationships |
| `outputs/relationships.json` | Raw relationship list with confidence scores and evidence |
| `outputs/validation_report.md` | Per-check pass/fail table; "All checks passed" when clean |
| `outputs/ontology_olist.yaml` | Raw export from NVIDIA Auto Ontology (checked in for reproducibility) |

---

## How the LLM Output Is Checked

The pipeline never trusts LLM output without verification. Five checks run:

### 1 · Schema grounding (`validator.py`)
Every entity `source_table` is confirmed to exist in the real schema.
Every attribute `column` is confirmed to be a real column of that table.
Hallucinated tables or columns are flagged as issues.

### 2 · Description quality (`validator.py`)
Descriptions shorter than three words are rejected.
Descriptions matching template patterns (e.g. "business entity representing …") are rejected.
Blank or templated attribute meanings are flagged.

### 3 · Coverage (`validator.py`)
Every table in the database must map to at least one entity.
Entity names must be unique so relationships can reference them unambiguously.

### 4 · Relationship evidence (`validator.py` + `relationship_finder.py`)
For every relationship, the validator re-runs the SQL containment query and compares
the result against the stored evidence (tolerance: 1 %).
A foreign-key relationship where less than 85 % of child values exist in the parent is flagged.

### 5 · LLM self-correction + relationship double-check (`llm_client.py`)
When the first LLM response fails any structural check, the client sends one follow-up
message listing the problems and asks for a corrected answer.
Borderline links (confidence 0.50–0.80) are sent to the LLM for a second opinion;
the LLM can only lower a score (by 30 %) and cannot create new links.

---

## Project Structure

```
.
├── main.py                  # CLI entry point; orchestrates all steps
├── db_inspector.py          # Step 1: schema reading and primary-key inference
├── relationship_finder.py   # Step 2: relationship discovery with confidence scores
├── llm_client.py            # Step 3: NVIDIA LLM calls with caching and validation
├── normalizer.py            # Step 4: merges Auto Ontology export with LLM results
├── validator.py             # Step 5: cross-checks every LLM claim against the DB
├── load_data.py             # Utility: loads CSVs into PostgreSQL
├── docker-compose.yml       # Runs PostgreSQL 15 (with pgvector) locally
├── requirements.txt         # Python dependencies
├── .env.example             # Template for secrets (copy to .env)
├── .gitattributes           # Normalises line endings to LF on commit
├── outputs/
│   ├── ontology_olist.yaml  # Raw NVIDIA Auto Ontology export (commit fc43a826)
│   ├── ontology_final.yaml  # Final ontology (9 entities, 52 attributes, 10 relationships)
│   ├── relationships.json   # Inferred relationships with evidence
│   └── validation_report.md # Validation results (8/8 checks passed)
└── .llm_cache/              # Cached LLM responses (SHA-256 keyed; committed for reproducibility)
```

---

## Configuration Reference

All secrets and optional overrides go in `.env` (never committed — see `.gitignore`).

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `NVIDIA_API_KEY` | ✅ | — | Key from [build.nvidia.com](https://build.nvidia.com) |
| `NVIDIA_MODEL` | ❌ | `nvidia/nemotron-3.5-lightning-30b-a3b` | Any chat model on the NVIDIA API |
| `NVIDIA_BASE_URL` | ❌ | `https://integrate.api.nvidia.com/v1` | Override for a self-hosted NIM endpoint |
| `DB_USER` | ❌ | `postgres` | PostgreSQL user (used by `load_data.py`) |
| `DB_PASSWORD` | ❌ | `postgres` | PostgreSQL password |
| `DB_HOST` | ❌ | `localhost` | PostgreSQL host |
| `DB_PORT` | ❌ | `5432` | PostgreSQL port |
| `DB_NAME` | ❌ | `olist` | PostgreSQL database name |
| `LLM_CACHE_DIR` | ❌ | `.llm_cache` | Directory for cached LLM responses |

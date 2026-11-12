"""
End-to-end pipeline.

Usage (with NVIDIA Auto Ontology export):
  python main.py --db postgresql://user:pass@localhost:5432/dbname \\
                 --raw-export outputs/ontology_olist.yaml

Usage (without export - schema-only fallback):
  python main.py --db postgresql://user:pass@localhost:5432/dbname

--raw-export is the YAML exported from NVIDIA Auto Ontology after Semantic Compilation
(see README § Auto Ontology Steps for the exact clone-and-run steps).
When the file is absent the pipeline still completes: it reads the schema, infers
relationships, and uses the NVIDIA LLM for descriptions, then writes the ontology from
the inferred structure alone.
"""
import argparse
import json
import os
import sys

from sqlalchemy import create_engine

import llm_client
from db_inspector import get_table_info
from llm_client import describe_all, describe_relationship, make_relationship_verifier
from normalizer import build_final_ontology
from relationship_finder import find_relationships
from validator import validate_ontology


def run(engine, raw_export, out_dir):
    os.makedirs(out_dir, exist_ok=True)

    if raw_export and not os.path.exists(raw_export):
        # Warn but continue; the pipeline runs without the Auto Ontology export.
        print(
            f"[WARNING] Auto Ontology export not found at '{raw_export}'.\n"
            "  To produce it: clone https://github.com/NVIDIA/auto-ontology, run\n"
            "  'docker compose up --build', open http://localhost:3000, add this\n"
            "  database as a data source, click Compile, then Export -> YAML.\n"
            "  Save the file to that path and re-run with --raw-export.\n"
            "  Continuing WITHOUT the Auto Ontology export — the ontology will be\n"
            "  built from inferred schema structure rather than Auto Ontology terms.",
            file=sys.stderr,
        )
        raw_export = None  # signal to build_final_ontology to skip export-based merging

    print("Step 1: reading the database (columns, types, samples, primary keys)...")
    table_info = get_table_info(engine)

    print("Step 2: inferring relationships (name + value matching, LLM check on borderline links)...")
    relationships = find_relationships(engine, table_info, llm_verifier=make_relationship_verifier(table_info))
    rel_path = os.path.join(out_dir, "relationships.json")
    with open(rel_path, "w", encoding="utf-8") as f:
        json.dump(relationships, f, indent=2)
    print(f"  Saved {len(relationships)} relationships -> {rel_path}")

    print("Step 3: NVIDIA LLM - entity names, descriptions, attribute meanings...")
    descriptions = describe_all(table_info, relationships)

    print("Step 4: building the final ontology...")
    final_path = os.path.join(out_dir, "ontology_final.yaml")
    build_final_ontology(raw_export, relationships, descriptions, table_info,
                         sentence_fn=describe_relationship, out_path=final_path, llm_model=llm_client.MODEL)

    print("Step 5: validating the ontology against the database...")
    result = validate_ontology(final_path, table_info, engine, os.path.join(out_dir, "validation_report.md"))
    print(f"\nDone. Outputs in '{out_dir}': relationships.json, ontology_final.yaml, validation_report.md")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Auto-build an ontology from a PostgreSQL database")
    parser.add_argument("--db", required=True, help="PostgreSQL connection string")
    parser.add_argument(
        "--raw-export", default=None,
        help="YAML exported from NVIDIA Auto Ontology (optional; pipeline runs without it)",
    )
    parser.add_argument("--out", default="outputs", help="output folder (default: outputs)")
    args = parser.parse_args()
    url = args.db
    if url.startswith("postgresql://"):  # force the psycopg2 driver
        url = url.replace("postgresql://", "postgresql+psycopg2://", 1)
    outcome = run(create_engine(url), args.raw_export, args.out)
    sys.exit(1 if outcome["issues"] else 0)

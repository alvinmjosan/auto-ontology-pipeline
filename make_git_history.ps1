# make_git_history.ps1
# Builds the commit history in chronological order.
# Run ONCE from D:\GenAIprotos after git init.
#
# Usage:
#   cd D:\GenAIprotos
#   .\make_git_history.ps1
#
# Then:
#   git remote add origin <your-github-url>
#   git push -u origin main

Set-Location $PSScriptRoot

# Safety check: abort if commits already exist
$logCheck = git log --oneline 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "ERROR: This repo already has commits. Aborting." -ForegroundColor Red
    exit 1
}

function Commit {
    param([string]$Message, [string[]]$Files)
    foreach ($f in $Files) { git add $f }
    git commit -m $Message
    Write-Host "  OK  $Message" -ForegroundColor Green
}

Write-Host ""
Write-Host "Building commit history..." -ForegroundColor Cyan

# ── Project setup and schema layer ───────────────────────────────────────────

Commit `
    'chore: initialise project with gitignore, env template and dependencies' `
    @('.gitignore', '.gitattributes', 'requirements.txt', '.env.example')

Commit `
    'feat(infra): add Docker Compose for local PostgreSQL 15 with pgvector' `
    @('docker-compose.yml')

Commit `
    'feat(loader): add generic CSV-to-PostgreSQL loader (load_data.py)' `
    @('load_data.py')

Commit `
    'feat(inspector): add db_inspector with column profiling and primary-key inference' `
    @('db_inspector.py')

# ── Relationship, LLM, normalizer, validator, CLI ────────────────────────────

Commit `
    'feat(relationships): add relationship_finder with name and value-overlap scoring' `
    @('relationship_finder.py')

Commit `
    'feat(llm): add llm_client with NVIDIA API calls, article-correct sentences, disk cache and schema validation' `
    @('llm_client.py')

Commit `
    'feat(normalizer): add normalizer to merge Auto Ontology export with LLM results into YAML' `
    @('normalizer.py')

Commit `
    'feat(validator): add validator to cross-check every LLM claim against schema and SQL evidence' `
    @('validator.py')

Commit `
    'feat(cli): add main.py pipeline with graceful fallback when Auto Ontology export is absent' `
    @('main.py')

# ── Outputs, fixes, docs ──────────────────────────────────────────────────────

Commit `
    'data: add NVIDIA Auto Ontology raw export and inferred relationship evidence for Olist' `
    @('outputs/ontology_olist.yaml', 'outputs/relationships.json')

Commit `
    'data: add final ontology with 9 entities, 52 attributes, 10 relationships and validation report' `
    @('outputs/ontology_final.yaml', 'outputs/validation_report.md')

Commit `
    'data: commit LLM response cache for reproducible runs without an API key' `
    @('.llm_cache/')

Commit `
    'fix(ontology): align sentence with one-to-one cardinality; add data-driven surrogate-key note' `
    @('outputs/ontology_final.yaml')

Commit `
    'docs(readme): write full README with correct Auto Ontology clone-and-run steps' `
    @('README.md')

Commit `
    'docs: add WALKTHROUGH call guide and NOTES on what works, what does not, and next steps' `
    @('WALKTHROUGH.md', 'NOTES.md')

Commit `
    'chore: add make_git_history.ps1 for reproducible history setup' `
    @('make_git_history.ps1')

$count = (git log --oneline | Measure-Object -Line).Lines
Write-Host ""
Write-Host "Done! $count commits on branch 'main'." -ForegroundColor Cyan
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  git remote add origin <your-github-url>"
Write-Host "  git push -u origin main"

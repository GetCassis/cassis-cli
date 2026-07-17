# Cassis CLI

Run Cassis actions from your CI pipelines:

- `cassis ontology check` validates the ontology files in your repository with the exact same checks as the Cassis GitHub PR check (YAML parsing, round-trip, import validation) — so you can gate merges in any CI system, not just GitHub.
- `cassis ontology upload` uploads the ontology files to a Cassis project (full replace) and, by default, publishes them immediately as a new version — so a merge to your main branch can go live in one CI step.

## Install

```bash
pip install cassis-cli
```

## Setup

1. Create an API key in Cassis under **Organization settings → API keys** (keys start with `sk-k6-`).
2. Store it as a CI secret and expose it as `CASSIS_API_KEY`.
3. For `upload`: find the project ID (UUID) in the project's URL and expose it as `CASSIS_PROJECT_ID` (or pass `--project`).

## Usage

```bash
# From the root of a repository synced with Cassis (contains the ontology export directory, cassis/ by default):
cassis ontology check

# Or point at the checkout explicitly:
cassis ontology check /path/to/checkout

# Upload the ontology to a project and publish it immediately:
cassis ontology upload --project 019f0000-0000-7000-8000-000000000000

# Upload without publishing (the tree becomes the project's unpublished ontology, to review in Cassis):
cassis ontology upload --project ... --no-publish

# Label the published version:
cassis ontology upload --project ... --label "release 1.2"

# Machine-readable output:
cassis ontology check --json
cassis ontology upload --project ... --json
```

Configuration (flags take precedence over env vars):

| Flag        | Env var          | Default                     |
| ----------- | ---------------- | --------------------------- |
| `--api-key` | `CASSIS_API_KEY` | — (required)                |
| `--api-url` | `CASSIS_API_URL` | `https://app.getcassis.com` |
| `--base-path` | `CASSIS_BASE_PATH` | `cassis` — must match the project's git-sync "Path" setting |
| `--project` (upload only) | `CASSIS_PROJECT_ID` | — (required)      |

### Exit codes

| Code | Meaning                                                                        |
| ---- | ------------------------------------------------------------------------------ |
| 0    | Ontology is valid (check) / uploaded (upload)                                  |
| 1    | Validation failed (check: findings printed; upload: nothing imported)          |
| 2    | Usage error (missing API key or project, no ontology directory, unreadable file, tree over the size limits) |
| 3    | Transport/API error (unreachable API, invalid key, inaccessible project, unexpected response) |

Both commands accept up to 2000 YAML files / 5 MB total — far above real
ontologies (a few hundred small files). Beyond that the CLI fails fast with
exit 2 before uploading anything; double-check `--base-path` if you hit it.

`upload` replaces the project's entire ontology with the uploaded tree. A
never-published project always goes live immediately on first upload (even
with `--no-publish`), matching imports from the Cassis app. Publishing is
idempotent: re-uploading content identical to the published version reports
that version instead of creating a new one, so re-running the CI job on
unchanged files is a no-op.

### GitHub Actions example

```yaml
jobs:
  ontology-check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install cassis-cli
      - run: cassis ontology check
        env:
          CASSIS_API_KEY: ${{ secrets.CASSIS_API_KEY }}

  ontology-publish:
    runs-on: ubuntu-latest
    if: github.ref == 'refs/heads/main'
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install cassis-cli
      - run: cassis ontology upload
        env:
          CASSIS_API_KEY: ${{ secrets.CASSIS_API_KEY }}
          CASSIS_PROJECT_ID: ${{ vars.CASSIS_PROJECT_ID }}
```

### GitLab CI example

```yaml
ontology-check:
  image: python:3.12-slim
  script:
    - pip install cassis-cli
    - cassis ontology check
  variables:
    CASSIS_API_KEY: $CASSIS_API_KEY

ontology-publish:
  image: python:3.12-slim
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
  script:
    - pip install cassis-cli
    - cassis ontology upload
  variables:
    CASSIS_API_KEY: $CASSIS_API_KEY
    CASSIS_PROJECT_ID: $CASSIS_PROJECT_ID
```

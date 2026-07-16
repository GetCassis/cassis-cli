# Cassis CLI

Run Cassis actions from your CI pipelines. The first command, `cassis ontology check`, validates the ontology files in your repository with the exact same checks as the Cassis GitHub PR check (YAML parsing, round-trip, import validation) — so you can gate merges in any CI system, not just GitHub.

## Install

```bash
pip install cassis-cli
```

## Setup

1. Create an API key in Cassis under **Organization settings → API keys** (keys start with `sk-k6-`).
2. Store it as a CI secret and expose it as `CASSIS_API_KEY`.

## Usage

```bash
# From the root of a repository synced with Cassis (contains the ontology export directory, cassis/ by default):
cassis ontology check

# Or point at the checkout explicitly:
cassis ontology check /path/to/checkout

# Machine-readable output:
cassis ontology check --json
```

Configuration (flags take precedence over env vars):

| Flag        | Env var          | Default                     |
| ----------- | ---------------- | --------------------------- |
| `--api-key` | `CASSIS_API_KEY` | — (required)                |
| `--api-url` | `CASSIS_API_URL` | `https://app.getcassis.com` |
| `--base-path` | `CASSIS_BASE_PATH` | `cassis` — must match the project's git-sync "Path" setting |

### Exit codes

| Code | Meaning                                                                        |
| ---- | ------------------------------------------------------------------------------ |
| 0    | Ontology is valid                                                              |
| 1    | Validation failed (findings printed)                                           |
| 2    | Usage error (missing API key, no ontology directory, unreadable file, tree over the size limits) |
| 3    | Transport/API error (unreachable API, invalid key, unexpected response)        |

The check accepts up to 2000 YAML files / 5 MB total — far above real
ontologies (a few hundred small files). Beyond that the CLI fails fast with
exit 2 before uploading anything; double-check `--base-path` if you hit it.

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
```

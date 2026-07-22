# Cassis CLI

Run Cassis actions from your CI pipelines:

- `cassis ontology check` validates the ontology files in your repository with the exact same checks as the Cassis GitHub PR check (YAML parsing, round-trip, import validation) — so you can gate merges in any CI system, not just GitHub.
- `cassis ontology fmt` rewrites the ontology files in canonical form (think `black`/`gofmt` for the ontology), so hand or agent edits pass the round-trip check.
- `cassis ontology upload` uploads the ontology files to a Cassis project (full replace) and, by default, publishes them immediately as a new version — so a merge to your main branch can go live in one CI step.
- `cassis ontology pull` downloads the project's unpublished ontology into your repository checkout (full sync — stale local YAML files are pruned), so you can start editing from the current state, or bootstrap a repo that isn't git-synced (e.g. Bitbucket).
- `cassis eval run` runs the project's eval suite against your local ontology files (scored in-memory — nothing is pushed to Cassis) and prints per-question results, so you can test the changes on your git branch before merging.

## Install

```bash
pip install cassis-cli
```

## Setup

1. Create an API key in Cassis under **Organization settings → API keys** (keys start with `sk-k6-`).
2. Store it as a CI secret and expose it as `CASSIS_API_KEY`.
3. For `pull`, `upload` and `eval run`: find the project ID (UUID) in the project's URL and expose it as `CASSIS_PROJECT_ID` (or pass `--project`).

## Usage

```bash
# From the root of a repository synced with Cassis (contains the ontology export directory, cassis/ by default):
cassis ontology check

# Or point at the checkout explicitly:
cassis ontology check /path/to/checkout

# Download the project's unpublished ontology into the checkout (full sync;
# review with git diff — pass --no-prune to keep local files it would delete):
cassis ontology pull --project 019f0000-0000-7000-8000-000000000000

# Upload the ontology to a project and publish it immediately:
cassis ontology upload --project 019f0000-0000-7000-8000-000000000000

# Upload without publishing (the tree becomes the project's unpublished ontology, to review in Cassis):
cassis ontology upload --project ... --no-publish

# Label the published version:
cassis ontology upload --project ... --label "release 1.2"

# Machine-readable output:
cassis ontology check --json
cassis ontology pull --project ... --json
cassis ontology upload --project ... --json
cassis eval run --project ... --json

# Run the eval suite against the local ontology files and wait for results
# (the run is labelled with your git branch name in the Evals page):
cassis eval run --project ...

# Run against an existing Cassis ontology branch, or the unpublished ontology:
cassis eval run --project ... --branch feature-x

# Start the run and return immediately (poll in the webapp):
cassis eval run --project ... --no-wait
```

Configuration (flags take precedence over env vars):

| Flag        | Env var          | Default                     |
| ----------- | ---------------- | --------------------------- |
| `--api-key` | `CASSIS_API_KEY` | — (required)                |
| `--api-url` | `CASSIS_API_URL` | `https://app.getcassis.com` |
| `--base-path` | `CASSIS_BASE_PATH` | `cassis` — must match the project's git-sync "Path" setting |
| `--project` (pull, upload, eval run) | `CASSIS_PROJECT_ID` | — (required)      |

`cassis eval run` also accepts `--label` (run label in the Evals page; defaults
to the branch name from the CI environment or the local git checkout; rejected
with `--branch`, whose runs are labelled with the branch name), `--wait/--no-wait`, `--poll-interval` (5 s),
`--timeout` (30 min — the run keeps going server-side if the CLI stops waiting),
and Ctrl-C cancels the run (exit 130). It prints a deep link to the run's page
in the Evals UI; `--app-url` / `CASSIS_APP_URL` overrides the link's base URL
when the webapp is not served from the API host (defaults to `--api-url`).

### Formatting

```bash
# Rewrite the ontology files in canonical form (in place)
cassis ontology fmt

# CI mode: fail (exit 1) if any file is not canonical, write nothing
cassis ontology fmt --check
```

`fmt` uses the exact serializer the validation round-trip compares against, so a formatted tree cannot fail that stage. Formatting does not run import validation — `check` remains the pass/fail gate for semantic problems (dangling references, incomplete metrics).

**Review the diff before committing**: canonical form keeps exactly the fields Cassis understands. Unknown fields (typos) are dropped — the rewrite makes them visible in `git diff` instead of losing them silently at sync time. Files with duplicate YAML keys are rejected (fix them by hand: the formatter can't know which value you meant).

### Exit codes

| Code | Meaning                                                                        |
| ---- | ------------------------------------------------------------------------------ |
| 0    | Ontology is valid (check) / pulled (pull) / uploaded (upload) / eval run completed all-passed (eval run) |
| 1    | Validation failed (check: findings printed; upload: nothing imported; eval run: invalid tree, failed cases, or failed/cancelled run) |
| 2    | Usage error (missing API key or project, no ontology directory, unreadable file, tree over the size limits) |
| 3    | Transport/API error (unreachable API, invalid key, inaccessible project, unexpected response), another run already active, or `--timeout` reached |

Commands that send the local tree (`check`, `upload`, `eval run`) accept up to
2000 YAML files / 5 MB total — far above real ontologies (a few hundred small
files). Beyond that the CLI fails fast with exit 2 before uploading anything;
double-check `--base-path` if you hit it.

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

  ontology-eval:
    runs-on: ubuntu-latest
    if: github.event_name == 'pull_request'
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install cassis-cli
      - run: cassis eval run
        env:
          CASSIS_API_KEY: ${{ secrets.CASSIS_API_KEY }}
          CASSIS_PROJECT_ID: ${{ vars.CASSIS_PROJECT_ID }}

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

ontology-eval:
  image: python:3.12-slim
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script:
    - pip install cassis-cli
    - cassis eval run
  variables:
    CASSIS_API_KEY: $CASSIS_API_KEY
    CASSIS_PROJECT_ID: $CASSIS_PROJECT_ID

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

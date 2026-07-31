# Cassis CLI

Run Cassis actions from your CI pipelines:

- `cassis ontology check` validates the ontology files in your repository with the exact same checks as the Cassis GitHub PR check (YAML parsing, round-trip, import validation) — so you can gate merges in any CI system, not just GitHub. In a checkout bound to a project (`project.yml`, `--project`, or `CASSIS_PROJECT_ID`), it also cross-checks the tree against the project's source schema: references to tables or columns the warehouse doesn't have print as **warnings** — advisory only (the object may simply not be built or synced yet), never a failed check.
- `cassis schema pull` downloads the data source's full source schema (as Cassis last introspected it) into `<base-path>/.schema.json` — a **gitignored** local snapshot (the command maintains the ignore entry) with a `pulled_at` stamp. The warehouse stays authoritative; the snapshot is a cache for offline/bulk work — e.g. a coding agent grepping table and column names during a modeling pass instead of paging through the MCP `get_source_schema` tool. Re-run to refresh.
- `cassis ontology fmt` rewrites the ontology files in canonical form (think `black`/`gofmt` for the ontology), so hand or agent edits pass the round-trip check.
- `cassis ontology upload` uploads the ontology files to a Cassis project (full replace) and, by default, publishes them immediately as a new version — so a merge to your main branch can go live in one CI step.
- `cassis ontology pull` downloads the project's unpublished ontology into your repository checkout (full sync — stale local ontology files are pruned), so you can start editing from the current state, or bootstrap a repo that isn't git-synced (e.g. Bitbucket).
- `cassis ontology pull` and `cassis ontology fmt` also write `<base-path>/AGENTS.md`, the Cassis ontology modeling guide, into the checkout (default `cassis/AGENTS.md`) — a managed file (generated banner; the CLI overwrites local edits) so a repo-aware coding agent loads current Cassis modeling doctrine by convention. It sits inside the ontology directory but is not part of the ontology tree (which is the YAML files plus the domain Markdown files `domains/**/README.md`), so it is never uploaded, validated, or pruned. Commit it alongside your ontology changes. The guide text ships inside the CLI package, so its version tracks the **installed cassis-cli version** — upgrade the CLI (`pip install -U cassis-cli`) and re-run `fmt` to pick up doctrine updates; an unpinned `pip install cassis-cli` in CI gets them automatically. The banner stamps a doctrine version, and the CLI never *downgrades* the file: if the checkout's `AGENTS.md` was written by a newer doctrine (a newer CLI, or Cassis itself on a publish), `fmt`/`pull` leave it in place, print an upgrade notice, and `fmt --check` still passes.
- The CLI identifies itself to the API (`User-Agent: cassis-cli/<version>`), and successful API responses advertise the newest published version — when you are behind, commands print a one-line upgrade notice on stderr (purely informational; output and exit codes are unchanged).
- `cassis eval run` runs the project's eval suite against your local ontology files (scored in-memory — nothing is pushed to Cassis) and prints per-question results, so you can test the changes on your git branch before merging.
- `cassis ontology test` runs individual questions through the text-to-SQL agent using your local ontology files, so you can check that a change actually works (e.g. a new column gets picked) — where `eval run` only checks for regressions on existing eval cases.
- `cassis eval add-case` adds a gold question/SQL case to the project's eval suite — after fixing an ontology issue, add the question users were failing on so `eval run` guards it from regressing.
- `cassis eval list-cases` and `cassis eval delete-case` maintain the suite: list the current cases with their ids, and prune one that is stale or wrong (e.g. its gold SQL encodes a definition the ontology has since changed).

## Install

```bash
pip install cassis-cli
```

## Ontology file format

The ontology tree under `<base-path>` (default `cassis/`) is:

- **Project identity** — `project.yml`: the Cassis project id and format version. Written by `pull` and by server-side publish (the contexts that know the id); a local `fmt` won't create it.
- **Domains** — Markdown files: every domain is the `README.md` of its folder — `domains/README.md` for the root, `domains/<path>/README.md` for each sub-domain. Each has a small YAML frontmatter block (`type`, `title`, `description`) and a Markdown body carrying the domain's `context_md`; a generated section at the bottom links the domain's tables and metrics (kept current by `fmt`/`pull` — edit your prose above it, and the PR check fails if the links are stale, so re-run `fmt`). The layout is a Cassis profile inspired by [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf): the files render on GitHub and read in any Markdown editor, but Cassis validates them strictly (unknown keys are flagged, not preserved).
- **Tables, joins, metrics** — YAML, unchanged: `tables/<schema>/<table>.yml`, `joins.yml`, `metrics/<name>.yml`.

**Migrating an existing repo** (domains were YAML `_project.yml` / `_domain.yml` before cassis-cli 1.1.0): upgrade and run `cassis ontology fmt` (or `cassis ontology pull` if you have no local edits) — it rewrites the domain files to Markdown and removes the old ones. Review the diff and commit. Cassis reads the old YAML domain files too, so an un-migrated repo keeps working until you convert it. **Uploading requires cassis-cli ≥ 1.1.0** — the server rejects an older CLI (which would drop the Markdown domain files) with a clear upgrade error.

## Setup

1. Create an API key in Cassis under **Organization settings → API keys** (keys start with `sk-k6-`).
2. Store it as a CI secret and expose it as `CASSIS_API_KEY`.
3. For `pull`, `upload`, `schema pull`, `eval run`, `ontology test`, and the `eval` case commands (`add-case`, `list-cases`, `delete-case`): the project ID (UUID) is taken from `<base-path>/project.yml` in the checkout (written by `pull` and by publishing) — so once a repo is pulled you don't need to pass it. To override, or before the first pull, set `CASSIS_PROJECT_ID` or pass `--project` (find the UUID in the project's URL). `ontology check` uses the same resolution but treats it as optional: unbound checkouts get the project-less validation (no schema reference warnings).

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

# Probe questions through the text-to-SQL agent using the local ontology files
# (one full agent run per question, expect ~30-90s each; repeat -q for several):
cassis ontology test --project ... -q "How much was refunded last month?" -q "Net revenue in Q1?"

# Add a gold case to the eval suite (rejected if the exact question already exists):
cassis eval add-case --project ... -q "How much was refunded last month?" \
  --gold-sql "SELECT SUM(refunded_cents) / 100.0 FROM public.orders WHERE ..."

# List the suite's cases (id + question; --json adds the gold SQL), then prune one:
cassis eval list-cases --project ...
cassis eval delete-case 019f0000-0000-7000-8000-0000000000ca --project ...

# Pull the source schema into <base-path>/.schema.json (gitignored local snapshot):
cassis schema pull
```

Configuration (flags take precedence over env vars):

| Flag        | Env var          | Default                     |
| ----------- | ---------------- | --------------------------- |
| `--api-key` | `CASSIS_API_KEY` | — (required)                |
| `--api-url` | `CASSIS_API_URL` | `https://app.getcassis.com` |
| `--base-path` | `CASSIS_BASE_PATH` | `cassis` — must match the project's git-sync "Path" setting |
| `--project` (check, pull, upload, schema pull, eval run, eval add-case, eval list-cases, eval delete-case, test) | `CASSIS_PROJECT_ID` | the id in `<base-path>/project.yml` (required before the first pull; `check` alone falls back to the project-less validation when unbound) |

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
| 0    | Ontology is valid (check) / pulled (pull) / uploaded (upload) / eval run completed all-passed (eval run) / every probe completed (test — whatever its outcome; probes are informational, don't gate CI on them) |
| 1    | Validation failed (check: findings printed; upload: nothing imported; eval run: invalid tree, failed cases, or failed/cancelled run; test: invalid tree or a probe failed; add-case: duplicate question or gold SQL that does not run; delete-case: no such case in the project) |
| 2    | Usage error (missing API key or project, no ontology directory, unreadable file, tree over the size limits) |
| 3    | Transport/API error (unreachable API, invalid key, inaccessible project, unexpected response), another run already active, out of credits, or `--timeout` reached |

Commands that send the local tree (`check`, `fmt`, `upload`, `eval run`, `test`) accept up to
2000 ontology files / 5 MB total — far above real ontologies (a few hundred small
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

## About this repository

[github.com/GetCassis/cassis-cli](https://github.com/GetCassis/cassis-cli) is a
read-only mirror, synced automatically from the Cassis monorepo where the CLI is
developed. Issues are welcome and watched; pull requests can't be merged here, so
open an issue (or mail tech.admin@getcassis.com) and we'll port the patch upstream
with credit.

Only the CLI is open source. The Cassis backend it talks to is proprietary and
requires an account.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

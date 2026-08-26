# Changelog

Versions match the releases on [PyPI](https://pypi.org/project/cassis-cli/); dates are the
PyPI upload date.

## 1.7.0 (2026-08-26)

### Added

- `cassis issues analyze`: analyze the project's conversations that nobody has analyzed yet,
  turning what went wrong into issues — the same pass as the webapp's "Analyze conversations"
  button, started on demand from a checkout or a CI job instead of waiting for the nightly one.
  Waits for the run and prints its summary by default (`--no-wait` prints the run id and
  returns; `--poll-interval`, `--timeout`, `--json`). When every conversation is already
  analyzed it is a no-op that exits 0, so a job re-running it on a quiet project stays green.
  Exit codes follow `eval run`: 1 when the run fails or is cancelled, 3 on transport errors, an
  already-running analysis, or `--timeout` (the run keeps going server-side); Ctrl-C cancels
  the run. Needs the matching server-side support, which ships with the Cassis release this
  version accompanies.

### Fixed

- `cassis eval run --json` (with `--wait`) and `cassis schema push --json` mixed human-readable
  lines into stdout around the JSON record — the "run started" line, `eval run`'s `n/m cases
  done` progress, `schema push`'s "Detection completed" summary — breaking `| jq`. Those lines
  now go to stderr; stdout is the JSON record alone.

## 1.6.0 (2026-08-21)

### Added

- `cassis source-changes list` and `cassis source-changes show`: read the project's Data
  source review queue (schema drift Cassis detected — tables/columns added, removed,
  renamed, retyped) from a checkout. `list` is paginated (`--limit`/`--offset`, `--status`,
  `--json` prints the `{items, total}` page); `show` prints one change's impact references
  and suggested edit. Read-only: reviewing stays in the webapp — fix headlessly by editing
  the ontology files and opening a pull request. Needs the matching server-side support,
  which ships with the Cassis release this version accompanies.
- `cassis status` now reports pending Data source review items ("Source changes pending
  review: N, M breaking") when the server provides them.

### Changed

- `ontology upload` now reports a server-side import failure with the server's
  error message and a validation-failure exit code. The server runs the import
  as a background job and streams keepalives while it runs; a failure after the
  stream starts arrives in the response body rather than as an HTTP error.
- `schema push` now treats the DDL file as a statement about only the schemas it
  contains: a partial export (for example Snowflake's per-schema `GET_DDL`) adds or
  updates those schemas without marking every table of the others as removed, and the
  stored schema keeps carrying the untouched schemas. Pass the new `--complete` flag
  when the file is the project's complete source schema, so schemas absent from it are
  treated as dropped (the previous behavior). The scoping takes effect once the matching
  server-side support is live — it ships with the Cassis release this version accompanies;
  until then the server ignores it and keeps whole-source semantics. Note that the new
  default then applies server-side to **all** clients: pushes from older CLI versions
  (which cannot send `--complete`) become scoped too, so a workflow that relied on
  omission to signal dropped schemas must upgrade and pass `--complete` to keep detecting
  whole-schema drops.

### Fixed

- `schema push` now reports the number of changes the run queued for review. It read a
  count field the server never sent, so every successful push printed "no changes" even
  when the run created cards. It also prints a note when the server flags the upload as a
  suspected partial export (the file drops most tables of a schema it contains).
- `ontology pull` and `eval run` no longer time out client-side on very large
  ontologies: both move whole ontology trees, but used the default 60-second request
  budget instead of the 5-minute one the other tree endpoints get. Pulls of
  multi-thousand-table projects were measured seconds from the cutoff.

## 1.5.1 (2026-08-17)

### Changed

- Raise the tree ceilings on `check`, `fmt`, `upload`, `eval run` and `test` from
  2,000 files / 5 MB to 20,000 files / 100 MB, sized for ontologies of roughly
  10,000 modeled tables (the old limits rejected trees Cassis itself exported above
  ~1,400 tables). The size gate now counts path bytes plus content bytes, matching
  the server exactly. The new limits apply end to end once the server ships the
  matching raise (the server's 422 remains the backstop against older servers).

### Fixed

- `ontology pull` no longer deletes untracked or locally modified files. Pruning is now
  restricted to files that git can restore (tracked and unmodified); anything else is kept
  and listed with the reason, and every deleted path is printed before deletion. Outside a
  git repository, pruning is skipped entirely. The `--json` summary gains a `kept` list
  (`[{"path", "reason"}]`).

## 1.5.0 (2026-08-11)

### Added

- `cassis issues`: triage the issues Cassis raised on the project from the terminal.
  `issues list` (filter with `--status`, `--impact`, `--cause`), `issues show <id>` for the
  diagnosis, suggested action and occurrences, `issues evidence <id> <occurrence-id>` for
  what the agent saw, and `issues resolve` / `dismiss` / `reopen` to change the status.

### Changed

- `schema push` always waits for the detection run and exits 0 only when the run
  completed — i.e. the DDL parsed and the schema was applied. The `--no-wait` flag is
  removed: the server now parses the DDL inside the detection run (large files no longer
  time out the upload request), so a fire-and-forget push could report success for a
  schema that never parsed. A parse error surfaces as a failed run (exit 1) with the
  parser's message.

## 1.4.1 (2026-08-06)

### Fixed

- The commands that send the whole ontology tree (`ontology check`, `ontology fmt`,
  `ontology upload`) now wait up to 300s instead of 60s. On a large ontology the CLI could
  report a timeout while the server completed the request normally.

## 1.4.0 (2026-08-05)

### Added

- `ontology check` prints advisory **ontology quality warnings** for a tree that parses:
  tables not assigned to any domain, joins and metrics pointing at unknown tables or
  columns, missing table and column descriptions. Same findings as `ontology test`, without
  the agent run. Warnings never fail the check.

## 1.3.0 (2026-08-04)

### Added

- `cassis verify`: the full local gate in one command (`ontology fmt --check`,
  `ontology check`, `eval run`), stopping at the first failure. `--no-eval` skips the evals.
- `cassis status`: published version, pending unpublished changes, git-sync binding, and how
  local HEAD relates to the published commit. `--watch` polls until they match.
- `cassis projects list`: projects the API key can reach, with id, name, published version
  and data-source dialect.
- `cassis schema push`: upload a DDL file to detect source-schema changes on a DDL-only
  project. Waits for completion unless `--no-wait`.
- `eval run --case` runs a single case; `eval add-case --gold-sql-file` reads gold SQL from
  a file.

## 1.2.0 (2026-07-31)

### Added

- `cassis schema pull`: writes the source schema, as Cassis last introspected it, to
  `<base-path>/.schema.json`, a gitignored snapshot the command keeps ignored. For offline
  or bulk modeling work.
- `eval list-cases` and `eval delete-case`.
- In a checkout bound to a project, `ontology check` cross-checks the tree against the
  source schema. References to tables or columns the warehouse doesn't have print as
  warnings, never failures: the object may not be built or synced yet.

## 1.1.1 (2026-07-30)

### Changed

- The CLI is Apache-2.0 licensed, source published at
  [github.com/GetCassis/cassis-cli](https://github.com/GetCassis/cassis-cli).
- Bundled modeling guide: an extension pass now sets structure before content, and hedged
  guesses and invented contrasts between sibling columns are no longer allowed.

## 1.1.0 (2026-07-30)

### Changed

- **Domains are now Markdown.** Every domain is the `README.md` of its folder
  (`domains/README.md` for the root, `domains/<path>/README.md` per sub-domain), with YAML
  frontmatter and a Markdown body. Tables, joins and metrics stay YAML.
- **Migrating**: run `ontology fmt`, or `ontology pull` if you have no local edits. It
  rewrites the old `_project.yml` / `_domain.yml` files to Markdown and removes them.
  Review the diff and commit. Cassis still reads the old files, so an unconverted repo
  keeps working.
- **Uploading requires 1.1.0 or newer.** The server rejects older CLIs, which would drop
  the Markdown domain files.

### Added

- `--project` now defaults to the id recorded in the checkout's `<base-path>/project.yml`,
  so a pulled repo no longer needs the flag or `CASSIS_PROJECT_ID`.

## 1.0.0 (2026-07-28)

### Added

- `ontology test -q "..."`: runs a question through the text-to-SQL agent against your local
  ontology files, so you can check a change works (a new column gets picked) rather than
  only checking for regressions.
- `eval add-case`: adds a gold question and SQL to the project's eval suite.
- `ontology fmt` and `ontology pull` write `<base-path>/AGENTS.md`, the bundled Cassis
  modeling guide, into the checkout. Managed file: the CLI overwrites local edits, and it is
  never uploaded or validated. Commit it with your ontology changes.
- The CLI sends `User-Agent: cassis-cli/<version>` and prints a one-line upgrade notice on
  stderr when a newer version exists. Informational: output and exit codes are unchanged.

## 0.4.0 (2026-07-22)

### Added

- `ontology fmt` rewrites the ontology in canonical form, so hand or agent edits pass the
  round-trip check, and any field Cassis would drop shows up in the diff.

## 0.3.0 (2026-07-21)

### Added

- `eval run` scores the project's eval suite against local ontology files, per question, so
  you can test a change on your branch before merging.
- `ontology pull` downloads a project's unpublished ontology into a checkout.

## 0.2.0 (2026-07-17)

### Added

- `ontology upload` uploads the ontology to a project (full replace) and publishes it by
  default, so a merge to main can go live in one CI step.

## 0.1.0 (2026-07-16)

First release: `ontology check` validates a repository's ontology files with the same checks
as the Cassis PR check, so merges can be gated in any CI system.

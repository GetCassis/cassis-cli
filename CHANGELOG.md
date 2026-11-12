# Changelog

Versions match the releases on [PyPI](https://pypi.org/project/cassis-cli/); dates are the
PyPI upload date.

## 3.6.1 (unreleased)

### Fixed

- `cassis chats show` and `--analysis-only` display saved observations and their next steps without requiring the removed destination field.

## 3.6.0 (2026-11-10)

### Added

- `cassis issues show` explains when some source conversations, evidence or evaluation drafts are restricted by your access.

- `cassis context test --save-case` keeps each successful probe as an eval case, its question and the SQL it produced, as `eval add-case --pending-change` would: Cassis checks it after the change is published. Probing the same question again updates a pending case with the new SQL, and reports a case already checked as guarding it. A probe that failed, asked a clarification or wrote no SQL is skipped with a line saying so; a clarifying probe is told to put the missing detail in the question. Without `--save-case`, a successful probe suggests it. `--json` carries the created case under `saved_case`.
- `cassis eval add-case --issue <id>` ties a case to the issue it guards: its check waits for that issue's resolution (the pull request mentioning it merged and published), or runs at once when it already is. `cassis context test --save-case --issue <id>` does the same.
- `cassis eval add-case --pending-change` marks a case that guards a change still in a pull request: its three-attempt check waits for the next publication, and a gold table the current context lacks is noted as expected instead of flagged.
- `cassis eval run --changed` and `--dry-run` name the changed tables and new domains no case reads, warn when a change-scoped run selects nothing, and point at `--save-case` and `add-case`; `cassis context check` warns about the same gaps (`[eval_coverage]`).
- `cassis eval add-case --expect cannot-answer` needs no `--gold-sql`; any other case without one is a usage error (exit 2), and so is a `replace-case` the server refuses for leaving an answer case without a gold SQL. `cassis eval replace-case --no-gold-sql` removes the gold SQL of a case that expects a cannot-answer.
- `cassis eval add-case`, `replace-case` and `list-cases` warn once on stderr when a case sets result comparison rules on a metadata-only source, where the judge decides and the rules are never applied.
- Case history links to issues supported by that case, and issue details list their supporting eval cases. Deleting or replacing a case detaches its evidence; an unsupported issue is dismissed, never marked fixed.
- `cassis eval health` shows current case labels, evidence and review recommendations; `--attention` narrows the list. Case history includes run counts, verdict transitions by context, and recorded tokens and duration.
- `cassis eval runs` lists the project's recent eval runs, newest first: id, status, the context scored, where the run came from, the model, the pass count, and why a skipped run did not start. `--limit` (default 20); `--json` prints the server's list. Requires a server that lists eval runs; an older one exits 3.
- `cassis eval history` counts a repeat run as one run with its attempts, and prints each result's error, why result comparison did not apply, and the comparison rule that decided it. `cassis eval run` prints the same note and rule under each case, and says when a case's default comparison rules were applied (1% numeric tolerance, unordered rows, columns by value, empty rejected).
- `cassis eval run` names what a run scores and the model: `3 cases · label bt-s2b · unpublished context from local files · <model>` for local files, `· branch <name>` with `--branch`, `· v3` for a published version, and the context as unknown on a server too old to say.
- `cassis eval history` prints a result's timing as `plan 11.4s · exec 0.3s` (only the phases recorded), leaves empty columns out instead of printing dashes, and names the columns above each group's rows.
- `cassis eval run --changed` selects affected cases conservatively; `--base <version-id>` chooses its base, `--dry-run` previews the selection. Exclusions have reasons. Full runs report shadow measurements, and affected-only runs cannot stand in for a full-suite check. The selection names its base (`Selection: 3/15 cases affected since base <id>`), lists the selected cases with their reasons and counts the skipped ones per reason (`12 skipped: No changed dependency`); `--verbose` lists every skipped case.
- Eval runs display their frozen SQL dialect and query guidance; source-setting changes are kept separate when measuring noise.
- Eval cases can specify result comparison with `--compare` on `eval add-case` and `eval replace-case`: `default`, or any of `exact` (numbers match exactly), `ordered` (row order matters) and `allow-empty` (an empty result is valid), separated by commas. Changes create a replacement case. `list-cases` and `run` show explicit rules and whether they were applied; metadata-only sources retain SQL judging. Cases without settings retain the default comparison. Requires a server supporting comparison expectations.
- `cassis eval replace-case <id>` replaces an eval case's definition (`--question`, `--gold-sql` or `--gold-sql-file`; a field left out keeps its value) with a new case. A definition never changes in place: the old case stays read-only with its results and historical evidence, the replacement gets its own three-attempt check, and the command prints what changed and the new id. Exits 1 when the case was already replaced, the question belongs to another case or the gold SQL does not run.
- `cassis eval list-cases --include-replaced` also lists the cases a replacement superseded, each marked with the id that replaced it. `cassis eval history` says when a case was replaced, or which case it replaces.
- `cassis eval add-case --expect` says what a correct response to the case does: `answer` (the default, so asking or refusing fails: write a question that needs a clarification with the detail it lacks) or `cannot-answer` (the agent must say the context cannot ground the question). `replace-case` takes the same options, and `list-cases` shows a case's expectation when it is not the default. Requires a server at 8.30.0 or later; an older server ignores the option and the command says so.
- `cassis eval run` prints, under each case it details, what the agent expected and did on the way to its answer: turns, tables read against the gold SQL's (recall and precision), failed SQL attempts, context gaps reported, and the clarifications it asked. `--json` carries the record under `path_evidence` and the expectation under `expectation`.
- `cassis eval run` records where the run came from: the commit the local files come from (`git rev-parse HEAD`, or the CI's own variable) and, in GitHub Actions, GitLab CI or Bitbucket Pipelines, the pipeline and the merge or pull request, so the webapp's runs list says "CI · abc1234 · !7" instead of "CLI run". A `--branch` run sends no commit. An older server ignores the fields.
- `cassis eval run` warns, before starting, when the project's automatic evaluations are paused because their spending window is spent; the run goes ahead regardless and counts toward the window. `cassis eval history` names where a configuration's runs came from (CI, scheduled, Evals page, ...) in each group's header.

### Changed

- `cassis dbt push` distinguishes findings saved for issue curation from published issues, and prints their source observation for follow-up.

- `cassis eval run` exits 1 only on a regression: a case whose answer fails now and passed on the base run. A case already failing on the base, or pending the change it guards, is listed and does not fail the command; against a server that does not split failures, every failed answer still exits 1. It exits 4 when the run reached no verdict: it failed or was cancelled on Cassis's side, or the cases that did not pass errored instead of failing.
- `cassis eval show <run-id>` prints any run by id: its results and verdict, or how a repeat run's attempts compare. It replaces `cassis eval compare`, which is removed.
- The bundled context design guide (`cassis/AGENTS.md`) moves to doctrine v12: every fix and every new domain gets an eval case in the same pull request.
- Automatic-eval spending warnings use monetary limits and report incomplete cost records.
- Eval runs, and `cassis context test`, run the production agent: the same prompts and tools as a chat. A question the agent asks gets "no answer available" and the run goes on; an answerable case the agent asked about fails naming the question. `cassis context test` prints the questions the agent asked on the way. The dependency diff's "used" side is now the generated SQL's own tables, resolved like the gold SQL's.
- The repeat comparison (`cassis eval show`, `cassis eval run --repeat`) counts one query written several ways as one SQL: table aliases, CTE names, join spelling, the order of a condition's operands and a default `ASC` no longer split attempts, so a judge flip across them is attributed to the judge. Requires a server at 8.30.0 or later; older servers keep the previous grouping.

### Fixed

- Distinguish unverified source catalogs from tables missing in the context when explaining case footprints.
- Links printed by `cassis dbt push` open the Issues tab using `tab=issues`.
- `cassis eval run --base` with a version the project does not have, or without `--changed`, is a usage error (exit 2) naming the problem; `--dry-run --case` naming no current case exits 2 with the ids; `--dry-run` against a server without selection previews says the server is too old (exit 3). `cassis eval show` on an unknown run says so and exits 1.
- `cassis eval add-case` and `replace-case` warn on stderr under `--json` too when the server ignores `--expect`; `--expect answer`, the default, never warns.
- `cassis eval run` prints the spending-window warning only once the run has started: never before a usage error, never under `--dry-run`. A skipped automatic run names its spending limit in dollars.
- A change-scoped run never reads like a full green suite: `cassis eval run`'s summary, `cassis eval runs`, `cassis eval show` and `cassis eval history` say how much of the suite it covered (`3/3 passed · 3 of 15 cases (affected by a context change)`). A repeat run counts attempts, not cases (`25 cases × 3 attempts · 75/75 attempts passed`).
- When result comparison did not apply, `cassis eval run` and `cassis eval history` say so as `Result comparison not applied: <why>`, adding `(metadata-only source: the judge decided)` when results were not executed.

## 3.5.0 (2026-11-02)

### Added

- `cassis chats list` lists conversations you can access, with feedback, saved analysis status and links. Filter by `--feedback`, `--review needs-attention`, `--since` or `--search`; page with `--limit` and `--offset`.
- `cassis chats show <id>` displays a conversation and its saved analysis, including observations, supporting quotes and suggested next steps. `--trace` shows compact execution previews; `--trace-entry trace:<message-id>:<index>` prints one full entry. `--analysis-only` shows just the report. `--json` returns structured data, with complete traces for `show`. Reading never starts an analysis or a model call. Requires a server supporting conversation reads through API keys. Members can read their own conversations; administrators can read conversations in accessible projects. Project-scoped keys retain their project restriction.

## 3.4.0 (2026-10-30)

### Added

- `cassis eval run --repeat N` (2 to 5) runs every case N times on the same context and model as one diagnostic run, then prints how the attempts compare per case instead of the per-case list: how many cases passed every attempt, failed every attempt or flipped, and for each flipping case the attempts that passed, how many distinct SQLs the agent wrote and the cause (`generation`, `judge`, `both` or `unattributed`). A repeat run opens no issue and is not the run the webapp shows before publishing. It exits 1 only when a case failed every attempt: a case that flipped is the noise being measured, not a failure. `--json` adds the comparison under `comparison`. An older server ignores the option; the command then says so and runs each case once.
- `cassis eval compare <run-id>` prints the same comparison for a run started earlier, `--json` the server's record as is. Exits 3 on a server too old to compare runs.
- `cassis eval add-case` says the new case is being checked (three attempts in the background), and `cassis eval list-cases` flags a case whose check found it flaky (the attempts disagree) or failing (none passed), or that is still being checked. `--json` carries the check under `entry_check`. Older servers return none and the commands print as before.

### Fixed

- `cassis issues list` prints each issue's priority (`P1`, `P2`, `P3`, or `-` when unranked) as its first column: the list was already sorted by it but never showed it. The command's help also lists every column it prints, the origin included.
- The commands that wait on Cassis print a sub-second `--timeout` as given (`Timed out after 0.5s`) instead of rounding it to `0s`, and the help of `cassis dbt push` and `cassis dbt check` says that a timeout exits 3, like a transport error.

## 3.3.0 (2026-10-27)

### Added

- `cassis eval history <case-id>` shows how an eval case has behaved across runs: its health (a verdict: noisy, failing, passing or changed across configurations; pass rate, how many configurations its repeated runs disagree on, average plan and execution time, the judge's divergence categories and the footprint states seen), then every result grouped by the configuration it ran under (context version or branch, model, Cassis version), newest first, each with its verdict, divergence, footprint state and timings. Verdicts that differ inside one configuration are flagged as noise to act on. `--json` prints the same record the webapp's case page reads. Exits 1 when the case does not exist in the project. Needs a server that serves case history.
- `cassis eval list-cases` flags every case whose gold SQL does not fully resolve against the context (a table the context lacks, a bare name matching several tables, SQL the dialect cannot parse), with the reason under its line, and takes `--branch <name>` to resolve against a context branch instead of the unpublished context (exit 2 for a branch the project lacks). `cassis eval add-case` prints the same warning right after adding a case. `--json` carries the footprint (`state`, `tables`, `unresolved`, `ambiguous`, `shadowed`, `error`). Older servers return no footprint and the commands print as before.
- `cassis context check`, in a checkout bound to a project, also resolves the project's eval cases against the files being checked and prints the cases whose gold SQL reads tables these files lack, as advisory warnings in their own section (`evals` stage in `--json`); `✓ Eval cases resolve against this context.` when the stage ran clean. Needs a server with the stage; older ones print as before.
- `cassis dbt push [target]` sends the artifacts dbt writes to `target/` to Cassis: `manifest.json`, and `catalog.json` when present, gzip-compressed, with the commit they were built from (`--commit`, default `HEAD`) and its message. On GitLab CI and GitHub Actions it also sends the merge request or pull request title and description (`--pr-title`, `--pr-description` to set them). Run it after `dbt docs generate` on your production target. It waits for Cassis to check the files and prints how many models and sources match tables of the project's data source, and how many tests they declare. Cassis compares the upload with the previous baseline and files each part of the context the change puts in doubt as an issue; the command lists them with links (`--app-url` sets the webapp's address). Exits 0 when the upload is accepted as the project's dbt baseline, or kept behind a newer one; 1 when it is rejected (invalid files, or no relation matches the data source); 2 on usage errors; 3 on transport errors or when Cassis fails on its side while processing it. Needs a server that accepts dbt uploads.
- `cassis dbt check [target]` uploads a pull request's dbt artifacts (after `dbt compile` or `dbt docs generate`) with the same options as `push`, as a check: Cassis compares them with the project's dbt baseline and the command prints the context tables, columns, joins and metrics the change puts in doubt, grouped by context object, then the tables and columns dbt no longer builds. Each object carries its reach (the chat answers that used its tables in the last 30 days) and, within each section, the most-reached print first; `cassis dbt push` prints the reach next to each filed issue too. Nothing is filed and the baseline doesn't move. Exits 0 with or without findings; 1 when the upload is rejected, including when there is no baseline yet (run `cassis dbt push` on the production branch first); 2 on usage errors; 3 on transport errors or when Cassis fails on its side while processing it.
- `cassis issues list --impact` accepts `at_risk`, the impact of issues filed from dbt changes; `cassis issues list` shows every issue's origin (`conversation`, `eval` or `dbt`) and reach (`reach 12`, the chat answers that used its context tables in the last 30 days; `reach -` when the issue names no table of the context) in fixed columns, and lists issues by priority first, and `cassis issues show` prints a dbt issue's origin (model, column, commit, pull request) and every issue's reach (`not measured` when it names no table of the context) and evidence (removed from dbt, an AI review suggestion, user feedback, a failed eval, or the chat analysis).
- After a merge, `cassis dbt push` finds the pull request's title for the issues it files: on a GitHub `push` event it asks the GitHub API which pull request merged the commit (set `GITHUB_TOKEN`; a failure is ignored), and on GitLab it reads the merge request's title from the merge commit.
- `cassis dbt check` prints the review's confidence with two decimals, and `cassis dbt push` says once when an upload is kept but is not the baseline.

### Changed

- Bundled context design guide at doctrine v11. Where a table or column has no `description`, the agent now reads the warehouse's own comment on it (including dbt descriptions written by `persist_docs`), so the guide asks you not to copy those comments into the context: write a `description` only when the comment is missing, wrong or incomplete. Run `cassis context fmt` to refresh `cassis/AGENTS.md` in a checkout.
- `cassis eval run --case` prints the generated SQL, gold SQL, row counts and judge verdict for passed cases too, not only failed ones, so you can check that a context change makes the case pass for the right reason. Runs of the whole suite still list passed cases on one line.

## 3.2.0 (2026-10-21)

### Changed

- Bundled context design guide at doctrine v10: a column's stored values go in its new `enum_values` field instead of its description. Run `cassis context fmt` to refresh `cassis/AGENTS.md` in a checkout.

## 3.1.1 (2026-10-07)

### Fixed

- `cassis status` shows the checkout in sync after an upload that changes nothing, such as the first upload after `cassis context pull` or one from a commit that only refreshed `cassis/AGENTS.md`. `cassis status --watch` stops waiting there instead of timing out. The upload itself now says nothing changed, instead of "published as vN". This needs a server that records the commit of such an upload; with an older one, `cassis status` behaves as before.

## 3.1.0 (2026-10-07)

### Added

- `cassis context` is the new name of the command group for your context files: `cassis context check`, `pull`, `upload`, `fmt` and `test` take the same options and return the same exit codes as before.

### Changed

- Help, messages and hints now say "context" instead of "ontology", in line with the Cassis app.
- `cassis ontology` still works exactly as before, so existing scripts and CI jobs need no change, but it no longer appears in `cassis --help`. Switch to `cassis context` when convenient.
- Bundled context design guide at doctrine v9: same modeling rules, new vocabulary. Run `cassis context fmt` to refresh `cassis/AGENTS.md` in a checkout (an older CLI leaves the newer file alone).

## 3.0.0 (2026-10-05)

### Changed

- `cassis ontology upload` and `cassis schema push` run only from a git checkout whose ontology files match `HEAD`, and send that commit with the upload. They exit 2 outside a git checkout, when `git` is not installed, or when an ontology file under the base path is modified, staged, deleted or untracked; commit the changes first. Line endings do not count: a file committed with CRLF uploads. A published version then records the commit it came from, so `cassis status` shows whether the checkout is in sync; an older server ignores the commit. CI images need `git`: the GitLab recipe's `python:3.12-slim` does not have it.
- The out-of-credits error now tells you to contact your Cassis administrator.

## 2.4.0 (2026-09-18)

### Added

- Schema plans show structured extraction diagnostics, including object names and statement locations, plus an inventory of extracted objects when an upload is incomplete. Metadata changes, including view SQL definitions and constraints, show their before/after values.

### Changed

- Issues list defaults to open issues. Use `--status all` to include resolved and dismissed issues.
- Dismiss requires `--reason invalid|irrelevant|duplicate|declined`, with optional `--detail`.
- Resolve requires confirmation that the fix is published: pass `--published`, or confirm in an interactive terminal.
- Show includes the status history, dismissal reason, and fixes awaiting publication.
- These lifecycle options need a server with the issue-lifecycle support (8.9+); an older one answers with the upgrade hint.
- Bundled ontology guide at doctrine v8: a change that fixes Cassis issues now carries `Resolves <id>` in the
  pull request description, and resolving by hand is reserved for outcomes that never go through one. Run
  `cassis ontology fmt` to refresh `cassis/AGENTS.md` in a checkout (an older CLI leaves the newer file alone).
- Incomplete extraction exits 1 from `schema plan` and `--dry-run`. `schema apply`, `schema push`, and `--write-checkout` refuse the incomplete result before changing local files or the app. `--json` retains the diagnostic data. Requires the corresponding server support; older server responses remain supported.
- `cassis status` identifies a ready schema plan that is blocked by incomplete extraction instead of suggesting `schema apply`.

## 2.3.0 (2026-09-14)

### Added

- `cassis issues show` prints a `PR mention:` line on open issues (`Resolves <id>`): write it in the
  description of the pull request that fixes the issue and Cassis resolves the issue when the PR
  merges (`Closes` / `Fixes` and the id's first 13+ characters also work). Once closed, the issue
  shows `Resolved via:` with how it was closed (`manual`, `fix_proposal`, `fix_chat_merge`,
  `pr_mention`) and the PR when there was one; `--json` carries the same as `resolved_via` /
  `resolved_ref`. Needs a server with the mention support (8.8+).

### Changed

- `cassis issues resolve --help` now points to the PR mention for fixes that ship through a pull
  request; the command stays for outcomes that never go through one.

## 2.2.0 (2026-09-10)

### Added

- `--domain` on `cassis issues list`: triage one ontology domain at a time. A domain path also
  covers the domains nested under it, so `--domain sales` includes `sales/pipeline`.
- `cassis schema plan <ddl> --dry-run`: compute the plan in the request and keep nothing
  server-side (no plan to apply or resume, the project's current plan untouched), for a schema
  change that is still a PR. `--write-checkout` also writes the ontology files the plan would
  produce under `<path>/<base-path>`, to commit next to the schema change; `--warehouse` and
  `--json` work as for a regular plan. Needs a server with the preview routes (8.7+); an older
  one answers with the upgrade hint.

### Changed

- `cassis issues list` lines now carry the issue's primary ontology domain between the status and
  the title (`-` when Cassis could not attach one), and `cassis issues show` prints a `Domains:`
  field. Scripts that parse the list output need updating; `--json` is the stable shape. Against
  an older server the domain reads `-` and `--domain` is ignored.

## 2.1.0 (2026-09-10)

### Added

- `eval run` prints the evidence for a failed case: the generated SQL, the gold SQL it was
  compared against, how many rows each side returned, the missing concepts, and the judge's
  verdict and reasoning. Which of those appears follows the failure — a missing-concept
  failure has no error text of its own, and used to print nothing at all. Expected and actual
  row *values* are not printed: `verify` output is a CI job log. They are in `--json`. The
  generated SQL is printed as the agent wrote it, so a value the agent read while planning can
  appear in a SQL literal — worth knowing when deciding who can read your CI logs.
- Long error text and judge reasoning now say when they were truncated, instead of being cut
  silently, and SQL is capped at 20 lines and 2000 characters with the remainder counted.
- `cassis schema plan <ddl> --dry-run`: compute the plan in the request and keep nothing
  server-side (no plan to apply or resume, the project's current plan untouched), for a schema
  change that is still a PR. `--write-checkout` also writes the ontology files the plan would
  produce under `<path>/<base-path>`, to commit next to the schema change; `--warehouse` and
  `--json` work as for a regular plan. Needs a server with the preview routes (8.6+); an older
  one answers with the upgrade hint.

### Changed

- `eval run --branch` with a name the project doesn't have now explains that `--branch` takes an
  ontology branch that already exists in Cassis and points at `--label` for naming a run after
  the local checkout's branch. It exits **2** (usage) rather than 3 (transport), since the API
  key and project are fine.

- Doctrine v7 in the bundled modeling guide (`cassis ontology pull` / `fmt` write it as
  `cassis/AGENTS.md`). A metric's definition (columns, thresholds, time windows, filters)
  lives only in its structured fields; every prose field, the metric's own `description`
  and `notes` included, says what the number means, not how it is computed. Column
  descriptions state facts, not SQL fragments. Prose never records a measurement
  (counts, shares, "as of" statements): sampling verifies a stable fact and only that
  fact is written. A prose field may stay short or empty when the structured fields say
  it all. `fmt` rewrites an older `AGENTS.md` in place.

## 2.0.0 (2026-09-07)

### Removed

- `cassis source-changes` (`list` / `show` / `detect`). The Data source review queue it read is
  gone from the server: schema drift is now previewed and applied with `cassis schema plan` /
  `apply` / `push` (including `--warehouse` for a project connected to a warehouse).
- `cassis status` no longer prints the "Source changes pending review" line.

### Added

- `--warehouse` on `cassis schema plan` / `apply` / `push`: for a project connected to a
  warehouse, the server introspects it instead of parsing a DDL file. Same plan, same review,
  same push. Starting a plan replaces the project's previous one.
- `cassis schema plan <ddl>`: preview what a DDL update would change before anything is
  applied. Prints the source schema diff, the ontology changes Cassis will make (every change on
  a table placed in the ontology, with everything a drop takes with it: joins, metrics, virtual
  tables) and warnings, terraform-style. Exit 0 when the plan is ready, 1 when the DDL is
  unparseable or looks truncated, 3 on transport errors. `--json` prints the plan record on
  stdout (the rendering goes to stderr), `--out` writes it to a file.
- `cassis schema apply <ddl>` / `cassis schema apply --plan <id>`: plan, then write the
  resulting ontology files into the local checkout (renamed tables, dropped columns, rewritten
  joins and metrics) for review with `git diff`. The app is not modified.
- `cassis schema push <ddl> [--publish]`: push the new schema and the local ontology to the app
  in that order (schema version stored, tracked schema updated, then the local tree replaces the
  unpublished ontology so hand edits land too). `--yes` skips the prompt (required without a
  TTY). A plan whose schema or ontology moved since it was computed is refused as stale: plan again.
- `cassis status` shows the schema plan waiting to be applied.

### Changed

- `cassis schema push` no longer queues review items in Ontology → Review → Data source: it
  applies the schema and uploads the local ontology (see above). Needs the matching server-side
  support, which ships with the Cassis release this version accompanies (older servers answer
  "upgrade the server first").

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

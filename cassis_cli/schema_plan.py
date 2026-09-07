"""Terminal rendering of a schema plan (`cassis schema plan` / `schema apply`).

Pure functions over the plan record the server returns; the command modules
own the I/O. Everything goes through `typer.secho` with `err=` so `--json`
keeps stdout to the JSON record alone.
"""

from __future__ import annotations

from typing import Any

import typer

_MARK = {"added": "+", "removed": "-", "renamed": "~", "modified": "~", "retyped": "~", "nullability": "~"}
_COLOR = {"+": typer.colors.GREEN, "-": typer.colors.RED, "~": typer.colors.YELLOW}
_OP_LABEL = {
    "add_column": ("+", "add column"),
    "drop_column": ("-", "drop column"),
    "drop_table": ("-", "drop table"),
    "retype": ("~", "retype"),
    "rename_column": ("~", "rename column"),
    "rename_table": ("~", "rename table"),
}


def _line(text: str, *, err: bool, mark: str | None = None, bold: bool = False) -> None:
    typer.secho(text, fg=_COLOR.get(mark or ""), bold=bold, err=err)


def render_plan(plan: dict[str, Any], *, err: bool) -> None:
    """Print the plan: schema diff, ontology changes with their cascade, warnings, footer."""
    document = plan.get("document") or {}
    diff = document.get("schema_diff") or {}
    tables = diff.get("tables") or []
    _line(f"Schema diff ({len(tables)} table{'s' if len(tables) != 1 else ''})", err=err, bold=True)
    for t in tables:
        mark = _MARK.get(t.get("kind", ""), "~")
        name = f"{t.get('schema_name')}.{t.get('table_name')}"
        suffix = ""
        if t.get("kind") == "renamed":
            suffix = f"  -> {t.get('new_name')}"
        elif t.get("column_count") is not None:
            suffix = f"  ({t['column_count']} columns)"
        if t.get("in_ontology"):
            suffix += "  (in ontology)"
        _line(f"  {mark} {name}{suffix}", err=err, mark=mark)
        for c in t.get("columns") or []:
            cmark = _MARK.get(c.get("kind", ""), "~")
            if c.get("kind") == "renamed":
                detail = f"{c.get('name')} -> {c.get('new_name')}"
            elif c.get("kind") == "retyped":
                detail = f"{c.get('name')}  {c.get('old_type')} -> {c.get('new_type')}"
                if c.get("same_type_family"):
                    detail += "  (same type family)"
            elif c.get("kind") == "nullability":
                detail = f"{c.get('name')}  nullable {c.get('old_nullable')} -> {c.get('new_nullable')}"
            else:
                detail = f"{c.get('name')}  {c.get('new_type') or c.get('old_type') or ''}".rstrip()
            _line(f"      {cmark} {detail}", err=err, mark=cmark)
    if diff.get("truncated"):
        _line("  … list capped; the summary counts are exact", err=err)

    changes = document.get("ontology_changes") or []
    typer.echo("", err=err)
    _line(f"Ontology changes ({len(changes)})", err=err, bold=True)
    if not changes:
        _line("  none: no table in the ontology is affected", err=err)
    for c in changes:
        mark, label = _OP_LABEL.get(c.get("op", ""), ("~", c.get("op", "?")))
        target = f"{c.get('schema_name')}.{c.get('table_name')}"
        if c.get("column_name"):
            target += f".{c['column_name']}"
        head = f"  {mark} {label} {target}"
        if c.get("new_name"):
            head += f" -> {c['new_name']}"
        if c.get("old_type") and c.get("new_type") and c.get("op") == "retype":
            head += f"  {c['old_type']} -> {c['new_type']}"
        if c.get("loses_curation"):
            head += "   loses description"
        _line(head, err=err, mark=mark)
        cascade = c.get("cascade") or []
        if cascade:
            _line(
                "      also removes: " + ", ".join(f"{r.get('kind')} {r.get('object_label')}" for r in cascade),
                err=err,
                mark="-",
            )
        rewrites = c.get("rewrites") or []
        if rewrites:
            _line(
                "      rewrites: " + ", ".join(f"{r.get('kind')} {r.get('object_label')}" for r in rewrites),
                err=err,
                mark="~",
            )
        affects = c.get("affects") or []
        if affects:
            _line("      affects: " + ", ".join(f"{r.get('kind')} {r.get('object_label')}" for r in affects), err=err)
        if c.get("note"):
            _line(f"      note: {c['note']}", err=err, mark="~")

    warnings = document.get("warnings") or []
    if warnings:
        typer.echo("", err=err)
        _line(f"Warnings ({len(warnings)})", err=err, bold=True)
        for w in warnings:
            _line(f"  - {w}", err=err, mark="~")

    add, change, remove, cascaded = plan_counts(plan)
    typer.echo("", err=err)
    _line(
        f"Plan: {add} to add, {change} to change, {remove} to remove in the ontology; "
        f"{cascaded} dependent object{'s' if cascaded != 1 else ''} removed.",
        err=err,
        bold=True,
    )


def plan_counts(plan: dict[str, Any]) -> tuple[int, int, int, int]:
    """(add, change, remove, cascaded) over the ontology changes."""
    summary = plan.get("summary") or (plan.get("document") or {}).get("summary") or {}
    add = summary.get("ontology_add_column", 0)
    change = sum(summary.get(k, 0) for k in ("ontology_retype", "ontology_rename_column", "ontology_rename_table"))
    remove = summary.get("ontology_drop_column", 0) + summary.get("ontology_drop_table", 0)
    return add, change, remove, summary.get("cascaded_objects", 0)


def plan_is_empty(plan: dict[str, Any]) -> bool:
    document = plan.get("document") or {}
    diff = document.get("schema_diff") or {}
    return not (diff.get("tables") or document.get("ontology_changes") or document.get("warnings"))

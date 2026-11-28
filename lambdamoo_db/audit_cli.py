"""CLI registration for ``moodb audit``."""

from __future__ import annotations

import datetime as dt
import json

import click

from .audit import AuditRuleError, audit_database, format_audit_text, load_rules
from .inspection import LookupFailed, resolve_object
from .lazy import open_indexed
from .reader import load


def register_audit(moodb: click.Group, get_db) -> None:
    @moodb.command("audit")
    @click.argument("old", required=False, type=click.Path(exists=True, dir_okay=False))
    @click.option("--rules", "rules_path", envvar="MOODB_AUDIT_RULES",
                  type=click.Path(exists=True, dir_okay=False), default=None,
                  help="Declarative JSON rules file (or set MOODB_AUDIT_RULES). Without it, runs universal MOO checks only.")
    @click.option("-o", "--object", "objs", multiple=True, metavar="REF",
                  help="Audit only these objects (repeatable).")
    @click.option("--descendants", is_flag=True,
                  help="With -o, include every descendant of the given objects.")
    @click.option("--existing", is_flag=True,
                  help="With OLD: only objects OLD already had (skips new and reused numbers).")
    @click.option("--identity", default="creation_time", show_default=True, metavar="PROP",
                  help="Identity property for --existing.")
    @click.option("--author", default=None, metavar="TEXT",
                  help="Only verbs whose Last-modified author or owner matches TEXT (case-insensitive).")
    @click.option("--since", default=None, metavar="YYYY-MM-DD",
                  help="Only verbs whose Last-modified UTC date is on or after YYYY-MM-DD.")
    @click.option("--check", "checks", multiple=True, metavar="NAME",
                  help="Run only selected built-in check, rule id, or rule category (repeatable; default: all).")
    @click.option("--doc-lines", type=click.IntRange(min=1), default=None,
                  help="Minimum leading docstring lines (overrides rules file; default 3).")
    @click.option("--limit", type=click.IntRange(min=0), default=20, show_default=True,
                  help="Maximum example rows shown per section in text output (0 for counts only).")
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text", show_default=True)
    @click.pass_context
    def audit(
        ctx: click.Context,
        old: str | None,
        rules_path: str | None,
        objs: tuple[str, ...],
        descendants: bool,
        existing: bool,
        identity: str,
        author: str | None,
        since: str | None,
        checks: tuple[str, ...],
        doc_lines: int | None,
        limit: int,
        fmt: str,
    ) -> None:
        """Audit MOO verb code using universal checks plus declarative JSON rules.

        With OLD, audits verbs added or changed between OLD and --db (skipping
        Last-modified-only saves). Without OLD, audits verbs in --db directly
        (useful with -o, --author or --since). Pass --rules FILE (or set
        MOODB_AUDIT_RULES) to layer project-specific rules on top of the
        built-in checks (raw-refs, catches, docstrings, security, duplicates).
        """
        if descendants and not objs:
            raise click.UsageError("--descendants requires at least one -o/--object")
        if existing and not old:
            raise click.UsageError("--existing requires an OLD dump")

        since_date: dt.date | None = None
        if since is not None:
            try:
                since_date = dt.date.fromisoformat(since)
            except ValueError as exc:
                raise click.UsageError(f"invalid --since date {since!r}: expected YYYY-MM-DD") from exc

        try:
            rules = load_rules(rules_path) if rules_path is not None else None
        except (AuditRuleError, OSError, json.JSONDecodeError) as exc:
            raise click.ClickException(f"failed loading rules: {exc}") from exc

        db, _ = get_db(ctx)
        cache = ctx.obj["cache"]
        old_db = (load(old) if cache is None else open_indexed(old, cache)) if old is not None else None

        object_ids: tuple[int, ...] | None = None
        if objs:
            resolved: list[int] = []
            for ref in objs:
                try:
                    resolved.append(resolve_object(db, ref).id)
                except LookupFailed as exc:
                    raise click.ClickException(str(exc)) from exc
            object_ids = tuple(resolved)

        try:
            report = audit_database(
                db,
                old_db,
                rules=rules,
                object_ids=object_ids,
                include_descendants=descendants,
                existing_only=existing,
                identity=identity,
                author=author,
                since=since_date,
                checks=checks or None,
                doc_lines=doc_lines,
            )
        except AuditRuleError as exc:
            raise click.ClickException(str(exc)) from exc

        if fmt == "json":
            click.echo(json.dumps(report, ensure_ascii=True, indent=2))
        else:
            click.echo(format_audit_text(report, limit=limit))

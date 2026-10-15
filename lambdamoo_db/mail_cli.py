"""CLI for deliberate, read-only inspection of mailbox content."""

import json
from datetime import datetime, timezone

import click

from .inspection import LookupFailed, descendants, resolve_object
from .mail import read_mailbox, select_messages


def _date(stamp):
    try:
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return str(stamp)


def register_mail(moodb, get_db):
    @moodb.group()
    def mail():
        """Browse LambdaCore mailing lists/mailboxes stored in this snapshot."""

    @mail.command("lists")
    @click.argument("pattern", default="")
    @click.option("--include-players", is_flag=True, help="Also list player mailboxes.")
    @click.option("--root", default="$mail_recipient", show_default=True)
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
    @click.pass_context
    def lists(ctx, pattern, include_players, root, fmt):
        """Find recipients by name; no mail contents are printed."""
        db, _ = get_db(ctx)
        try:
            obj = resolve_object(db, root)
            players = {int(n) for n in db.players}
            rows = [{"object": o.id, "name": o.name} for o in [obj, *descendants(db, obj)]
                    if (include_players or o.id not in players) and pattern.casefold() in o.name.casefold()]
        except LookupFailed as error:
            raise click.ClickException(str(error)) from error
        if fmt == "json":
            click.echo(json.dumps(rows, ensure_ascii=True))
        else:
            for row in rows:
                click.echo(f"#{row['object']} {row['name']}")

    def options(func):
        for decorate in [
            click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text"),
            click.option("--tail", type=click.IntRange(min=1), help="Last N matches, in stored order."),
            click.option("--search", help="Case-insensitive literal search of headers and body."),
            click.argument("sequence", default="all"),
            click.argument("ref"),
        ]:
            func = decorate(func)
        return func

    def show(ctx, ref, sequence, search, tail, fmt, full):
        db, _ = get_db(ctx)
        try:
            obj = resolve_object(db, ref)
            messages = select_messages(read_mailbox(db, obj), sequence, search=search, tail=tail)
        except (LookupFailed, RecursionError) as error:
            raise click.ClickException(str(error) or "Mail tree is too deep") from error
        if fmt == "json":
            rows = [m.as_dict() for m in messages]
            if not full:
                rows = [{k: v for k, v in row.items() if k != "body"} for row in rows]
            click.echo(json.dumps({"object": obj.id, "count": len(rows), "messages": rows}, ensure_ascii=True))
            return
        click.echo(f"#{obj.id} {obj.name}: {len(messages)} messages selected")
        for m in messages:
            click.echo(f"[{m.number}] {_date(m.date)} | {m.sender} | {m.subject}")
            if full:
                click.echo(f"To: {m.recipients}")
                for line in m.headers:
                    click.echo(line)
                click.echo()
                for line in m.body:
                    click.echo(line)
                click.echo()

    @mail.command()
    @options
    @click.pass_context
    def headers(ctx, ref, sequence, search, tail, fmt):
        """List headers. SEQUENCE: all, 123, 100-120, or 1,4-9 (stored numbers)."""
        show(ctx, ref, sequence, search, tail, fmt, False)

    @mail.command()
    @options
    @click.pass_context
    def read(ctx, ref, sequence, search, tail, fmt):
        """Read complete messages with additional headers and intact body lines."""
        show(ctx, ref, sequence, search, tail, fmt, True)

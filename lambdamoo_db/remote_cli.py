"""CLI for reading a live MOO over telnet: census its objects, then rank what is worth reading."""

import json
import os
import time

import click

from .remote import RemoteError, Session, build_database, census, rank, read_census, read_object
from .writer import Writer


def register_remote(moodb):
    @moodb.group()
    def remote():
        """Read a live MOO through one telnet connection as an ordinary programmer character."""

    @remote.command("census")
    @click.argument("out", type=click.Path(dir_okay=False))
    @click.option("--host", required=True)
    @click.option("--port", type=int, required=True)
    @click.option("--user", required=True, help="Character to connect as (needs the programmer bit).")
    @click.option("--password-file", type=click.Path(exists=True, dir_okay=False), required=True,
                  help="File holding only the password; it is never printed.")
    @click.option("--first", type=click.IntRange(min=0), default=0, show_default=True)
    @click.option("--last", type=int, help="Last object number (default: the server's max_object()).")
    @click.option("--block", type=click.IntRange(min=1), default=250, show_default=True,
                  help="Objects per eval. Large blocks can overflow the server's output buffer, which is reported.")
    @click.option("--pause", type=float, default=1.0, show_default=True, help="Seconds between evals.")
    def census_cmd(out, host, port, user, password_file, first, last, block, pause):
        """Append one JSON line per valid object to OUT; rerunning continues after the last finished block."""
        if os.path.exists(out):
            _, blocks = read_census(out)
            if blocks:
                first = max(first, max(end for _, end in blocks) + 1)
        with open(password_file, encoding="utf-8") as handle:
            password = handle.read().strip()
        session = Session(host, port)
        try:
            session.login(user, password)
            if last is None:
                last = session.eval_rows("__emit(max_object());")[0].id
            if first > last:
                click.echo(f"{out} already covers #{last}")
                return
            click.echo(f"census #{first}..#{last} in blocks of {block}", err=True)
            with open(out, "a", encoding="ascii") as handle:
                census(session, first, last, block, handle, pause=pause,
                       progress=lambda a, b, n: click.echo(f"#{a}..#{b}: {n} objects", err=True))
        except RemoteError as error:
            raise click.ClickException(str(error)) from error
        finally:
            session.close()

    @remote.command("read")
    @click.argument("records_dir", type=click.Path(file_okay=False))
    @click.argument("objects", type=int, nargs=-1)
    @click.option("--census", "census_file", type=click.Path(exists=True, dir_okay=False), required=True,
                  help="Census file; every object's ancestors are read too, as it records them.")
    @click.option("--top", type=click.IntRange(min=0), default=0, help="Also read the first N of `remote rank`.")
    @click.option("--fertile", is_flag=True, help="With --top: rank only fertile objects.")
    @click.option("--host", required=True)
    @click.option("--port", type=int, required=True)
    @click.option("--user", required=True)
    @click.option("--password-file", type=click.Path(exists=True, dir_okay=False), required=True)
    @click.option("--pause", type=float, default=1.0, show_default=True, help="Seconds between objects.")
    def read_cmd(records_dir, objects, census_file, top, fertile, host, port, user, password_file, pause):
        """Read OBJECTS (and the top ranked) whole into RECORDS_DIR, one JSON file each; existing files are kept."""
        rows, _ = read_census(census_file)
        # #0 always: moodb resolves $names through it
        wanted = [0, *objects, *(r["object"] for r in rank(rows, fertile_only=fertile)[:top])]
        order = []
        for n in wanted:
            while n >= 0 and n not in order:
                order.append(n)
                n = rows[n]["parent"] if n in rows else -1
        os.makedirs(records_dir, exist_ok=True)
        todo = [n for n in order if not os.path.exists(os.path.join(records_dir, f"{n}.json"))]
        click.echo(f"{len(order)} objects with ancestors, {len(todo)} to read", err=True)
        if not todo:
            return
        with open(password_file, encoding="utf-8") as handle:
            password = handle.read().strip()
        session = Session(host, port)
        try:
            session.login(user, password)
            for count, n in enumerate(todo, 1):
                record = read_object(session, n)
                path = os.path.join(records_dir, f"{n}.json")
                with open(path + ".part", "w", encoding="ascii") as out:
                    json.dump(record, out, ensure_ascii=True)
                os.replace(path + ".part", path)
                click.echo(f"{count}/{len(todo)} #{n} {record['name']}: {len(record['verbs'])} verbs", err=True)
                time.sleep(pause)
        except RemoteError as error:
            raise click.ClickException(str(error)) from error
        finally:
            session.close()

    @remote.command("build")
    @click.argument("records_dir", type=click.Path(exists=True, file_okay=False))
    @click.argument("out", type=click.Path(dir_okay=False))
    def build_cmd(records_dir, out):
        """Write the objects in RECORDS_DIR as a version 17 database OUT that every moodb command can read."""
        records = {}
        for name in os.listdir(records_dir):
            if name.endswith(".json"):
                with open(os.path.join(records_dir, name), encoding="ascii") as handle:
                    record = json.load(handle)
                records[record["object"]] = record
        db, skipped = build_database(records)
        with open(out, "w", encoding="latin-1", newline="\n") as handle:
            Writer(db=db, output_file=handle).writeDatabase()
        click.echo(f"{out}: {len(db.objects)} objects")
        for n, why in sorted(skipped.items()):
            click.echo(f"left out #{n}: property names of #{why} not read", err=True)

    @remote.command("rank")
    @click.argument("census_file", type=click.Path(exists=True, dir_okay=False))
    @click.option("--fertile", is_flag=True, help="Only fertile objects (their authors invite children).")
    @click.option("--players", is_flag=True, help="Include player objects.")
    @click.option("--min-verbs", type=click.IntRange(min=0), default=1, show_default=True,
                  help="Least number of readable verbs.")
    @click.option("--owner", "owners", type=int, multiple=True, help="Only objects owned by this object number.")
    @click.option("--name", "name_part", help="Only objects whose name contains this text (any case).")
    @click.option("--top", type=click.IntRange(min=1), default=50, show_default=True)
    @click.option("--format", "fmt", type=click.Choice(["text", "json"]), default="text")
    def rank_cmd(census_file, fertile, players, min_verbs, owners, name_part, top, fmt):
        """Objects worth reading, best first: fertile, then most readable verbs, then most children."""
        rows, _ = read_census(census_file)
        ranked = rank(rows, fertile_only=fertile, include_players=players, min_verbs=min_verbs)
        if owners:
            ranked = [r for r in ranked if r["owner"] in owners]
        if name_part:
            ranked = [r for r in ranked if name_part.casefold() in r["name"].casefold()]
        names = {n: r["name"] for n, r in rows.items()}
        ranked = ranked[:top]
        if fmt == "json":
            click.echo(json.dumps(ranked, ensure_ascii=True))
            return
        for r in ranked:
            owner = f"#{r['owner']} {names.get(r['owner'], '?')}"
            click.echo(f"#{r['object']:<7} {'f' if r['fertile'] else ' '} "
                       f"verbs {r['readable_verbs']:>3}/{r['verbs']:<3} kids {r['children']:>5}  "
                       f"{r['name']}  (owner {owner}, parent #{r['parent']})")

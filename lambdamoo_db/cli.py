import json
import os
import re
import shlex
import sys
from collections import Counter
from fnmatch import fnmatchcase
from itertools import groupby
from pathlib import Path

import click
from .database import ObjNum
from .inspection import (
    BUILTIN_PROPS,
    LookupFailed,
    added_property_definitions,
    added_verb_lines,
    all_objects,
    all_properties,
    answers_call,
    builtin_value,
    code_fingerprint,
    defined_property_names,
    descendants,
    dollar_names,
    find_references,
    find_slot,
    find_strings,
    find_verb,
    format_time,
    format_value,
    frame_text,
    grep_verbs,
    label,
    lookup_property,
    moo_string,
    object_heads,
    object_continuity,
    object_flags,
    own_properties,
    property_perms,
    resolve_object,
    split_ref,
    stored_properties,
    tasks as list_tasks,
    tree_summary,
    value_holders,
    verb_summary,
    verbcasecmp,
)
from .lazy import LazyDatabase, default_cache_dir, open_indexed
from .reader import load
from .split import DEFAULT_MAX_PIECE_BYTES, SplitError, first_difference, join_dir, write_split


@click.command()
@click.argument("dbfile")
@click.argument("dir")
def moodb2flat(dbfile: str, dir: str) -> None:
    from .exporter import to_moo_files  # imported here: only this command needs cattrs

    db = load(dbfile)
    to_moo_files(db, dir, True)


@click.command()
@click.argument("dbfile", type=click.Path(exists=True, dir_okay=False))
@click.argument("outdir", type=click.Path(file_okay=False))
@click.option("--max-piece-bytes", default=DEFAULT_MAX_PIECE_BYTES, show_default=True, help="Fail if any piece is larger than this.")
def moodb_split(dbfile: str, outdir: str, max_piece_bytes: int) -> None:
    """Split DBFILE into per-object pieces in OUTDIR and verify they rejoin byte for byte."""
    data = Path(dbfile).read_bytes()
    try:
        stats = write_split(data, outdir, max_piece_bytes=max_piece_bytes)
    except SplitError as e:
        where = first_difference(outdir, data) if (Path(outdir) / "MANIFEST").exists() else None
        raise click.ClickException(f"{e}" + (f"; first difference at {where}" if where else ""))
    click.echo(
        f"split ok: {stats['pieces']} pieces, {stats['written']} written, " f"{stats['unchanged']} unchanged, {stats['removed']} removed"
    )


@click.command()
@click.argument("indir", type=click.Path(exists=True, file_okay=False))
@click.argument("outfile", type=click.Path(dir_okay=False), required=False)
@click.option("--compare", type=click.Path(exists=True, dir_okay=False), help="Report where INDIR first differs from this db file.")
def moodb_join(indir: str, outfile: str | None, compare: str | None) -> None:
    """Rebuild a db file from INDIR, verifying it against the MANIFEST. Without OUTFILE, only verify."""
    if compare:
        where = first_difference(indir, Path(compare).read_bytes())
        if where:
            raise click.ClickException(f"differs from {compare} at {where}")
        click.echo(f"identical to {compare}")
    try:
        data = join_dir(indir)
    except SplitError as e:
        raise click.ClickException(str(e))
    if outfile:
        tmp = Path(outfile + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, outfile)
    click.echo(f"join ok: {len(data)} bytes")


# --------------------------------------------------------------------------
# moodb: read-only inspection


class _LiteralArgs(click.Group):
    """Arguments are patterns and MOO references, never file globs: on Windows click would expand `*_msg` itself."""

    def main(self, *args, **kwargs):
        kwargs.setdefault("windows_expand_args", False)
        return super().main(*args, **kwargs)


@click.group(cls=_LiteralArgs, context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--db", "db_path", envvar="MOODB", type=str,
              help="Textdump to read (or set MOODB).")
@click.option("--no-cache", is_flag=True, help="Parse the whole dump, without reading or writing its offset index.")
@click.option("--cache-dir", type=click.Path(file_okay=False), default=None,
              help="Index directory (default: lambdamoo-db under $XDG_CACHE_HOME, %LOCALAPPDATA% or ~/.cache).")
@click.pass_context
def moodb(ctx: click.Context, db_path: str | None, no_cache: bool, cache_dir: str | None) -> None:
    """Read-only inspection of a LambdaMOO/ToastStunt textdump.

    \b
    Object references: #N, N, or $name, optionally followed by .object_property.
    Verbs:             REF:NAME (MOO name matching, inherited) or REF:N (0-based index on REF).
    Properties:        REF.NAME (inherited, clear values followed).

    The first query on a new dump parses all of it and saves an index of where
    each record is. Later queries read only the records they need. `batch`
    answers several queries in one run.
    """
    if ctx.invoked_subcommand in ('history', 'diff-read', 'history-read'):
        if ctx.get_parameter_source('db_path') == click.core.ParameterSource.COMMANDLINE:
            raise click.UsageError('--db is not used by history, diff-read or history-read')
        db_path = None
    elif ctx.invoked_subcommand != 'diff' and db_path is not None:
        db_path = click.Path(exists=True, dir_okay=False).convert(db_path, None, ctx)
    cache = None if no_cache else Path(cache_dir) if cache_dir else default_cache_dir()
    ctx.obj = {"path": db_path, "cache": cache}


def _db(ctx: click.Context):
    if "db" not in ctx.obj:
        if ctx.obj["path"] is None:
            raise click.UsageError("no dump given: pass --db DUMP or set MOODB")
        path, cache = ctx.obj["path"], ctx.obj["cache"]
        ctx.obj["db"] = load(path) if cache is None else open_indexed(path, cache)
    if "names" not in ctx.obj:
        ctx.obj["names"] = dollar_names(ctx.obj["db"])
    return ctx.obj["db"], ctx.obj["names"]


@moodb.command()
@click.argument("query", nargs=-1)
@click.pass_context
def batch(ctx: click.Context, query: tuple[str, ...]) -> None:
    """Answer several queries in one run: `batch 'obj 842' 'code 842:0 842:1' 'prop 842.todos'`.

    Each QUERY is one moodb command with its arguments, quoted as in a POSIX
    shell. With no QUERY, queries are read from standard input, one per line.
    Each answer follows a `=== QUERY` line. The run stops at the first query
    that fails.
    """
    queries = query or tuple(line.strip() for line in iter(sys.stdin.readline, "") if line.strip())
    for q in queries:
        name, *arguments = shlex.split(q)
        command = moodb.get_command(ctx, name)
        if command is None:
            raise click.UsageError(f"no such command {name!r} in {q!r}")
        click.echo(f"=== {q}")
        with command.make_context(name, arguments, parent=ctx) as sub:
            command.invoke(sub)


def _object(db, ref: str):
    try:
        return resolve_object(db, ref)
    except LookupFailed as e:
        raise click.ClickException(str(e))


def _alias(verb) -> str:
    """The first name of a verb, for one-line output."""
    return (verb.name.split() or [""])[0]


def _added_pattern(pattern: str | None, ignore_case: bool, fixed_strings: bool, invert_match: bool) -> re.Pattern[str] | None:
    """PATTERN of `added` and `added-props`, compiled with their -i, -F and -v options."""
    if invert_match and pattern is None:
        raise click.UsageError("-v needs a PATTERN")
    if pattern is None:
        return None
    try:
        return re.compile(re.escape(pattern) if fixed_strings else pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise click.BadParameter(str(e), param_hint="PATTERN")


def _left_out(continuity: dict[int, str], old_db, db, num: int, identity: str) -> bool:
    """Does --existing leave object #num out? Its continuity is worked out once and kept for the summary."""
    if num not in continuity:
        continuity[num] = object_continuity(old_db, db, num, identity)
    return continuity[num] in ("absent", "reused")


def _echo_existing_summary(continuity: dict[int, str], identity: str) -> None:
    count = Counter(continuity.values())
    unproven = [f"#{num}" for num, state in continuity.items() if state == "unproven"]
    which = f": {' '.join(unproven[:20])}{' ...' if len(unproven) > 20 else ''}" if unproven else ""
    click.echo(f"--existing left out {count['absent']} objects OLD lacks and {count['reused']} reused numbers; "
               f"kept {count['unproven']} on number alone (no .{identity} to compare){which}")


@moodb.command()
@click.pass_context
def info(ctx: click.Context) -> None:
    """Summarise the dump: format, object, verb, player and task counts."""
    db, names = _db(ctx)
    objs = list(db.objects.values())
    anon = sum(1 for o in objs if o.anon)
    verbs = [v for o in objs for v in o.verbs]
    click.echo(f"dump      {ctx.obj['path']}")
    click.echo(f"format    {db.versionstring.strip()}")
    click.echo(f"objects   {len(objs) - anon} (+{anon} anonymous), highest #{max(db.objects, default=-1)}, {len(db.recycled_objects)} recycled")
    click.echo(f"verbs     {len(verbs)} ({sum(1 for v in verbs if v.code)} with code)")
    click.echo(f"players   {len(db.players)}")
    click.echo(f"$names    {len(names)}")
    click.echo(f"tasks     {len(db.queuedTasks)} queued, {len(db.suspendedTasks)} suspended, {len(db.interruptedTasks)} interrupted")
    click.echo(f"waifs     {len(db.waifs)}")


_OBJECT_HEADER = re.compile(rb"#-?\d+( recycled)?")


@moodb.command()
@click.option("--max-diffs", default=20, show_default=True, help="Stop listing after this many differing lines.")
@click.option("--show", default=0, show_default=True,
              help="Print this many lines of both texts from the first difference on. They may hold secrets.")
@click.option("--remap", is_flag=True, help="Pass every object number through the renumbering walk, unchanged, first.")
@click.pass_context
def roundtrip(ctx: click.Context, max_diffs: int, show: int, remap: bool) -> None:
    """Write the dump back out in memory and check it reproduces the file byte for byte.

    A differing line is reported by its number, its two lengths and the nearest
    object header before it, never by its text unless --show asks: dumps hold
    secrets. Exits 1 when the bytes differ.
    """
    from io import StringIO

    from .objnums import remap as remap_numbers
    from .writer import Writer

    if ctx.obj["path"] is None:
        raise click.UsageError("no dump given: pass --db DUMP or set MOODB")
    original = Path(ctx.obj["path"]).read_bytes()
    db = load(ctx.obj["path"])
    if remap:
        remap_numbers(db, lambda kind, number, where: number)
    output = StringIO()
    Writer(db=db, output_file=output).writeDatabase()
    written = output.getvalue().encode("latin-1")
    if written == original:
        click.echo(f"identical: {len(original)} bytes")
        return
    old_lines, new_lines = original.split(b"\n"), written.split(b"\n")
    click.echo(f"differs: original {len(original)} bytes, {len(old_lines)} lines; "
               f"written {len(written)} bytes, {len(new_lines)} lines")
    header, shown, total = None, 0, 0
    for number, (old, new) in enumerate(zip(old_lines, new_lines), 1):
        if _OBJECT_HEADER.fullmatch(old):
            header = old.decode("latin-1")
        if old != new:
            if total == 0 and show:
                for side, lines in (("original", old_lines), ("written", new_lines)):
                    click.echo(f"{side} from line {number}:")
                    for line in lines[number - 1:number - 1 + show]:
                        click.echo(f"  {line.decode('latin-1')}")
            total += 1
            if shown < max_diffs:
                shown += 1
                click.echo(f"line {number}: original {len(old)} bytes, written {len(new)} bytes"
                           + (f", after header {header}" if header else ""))
    click.echo(f"{total} differing lines among the first {min(len(old_lines), len(new_lines))}")
    ctx.exit(1)


def _slot_label(where: tuple) -> str:
    """The field an object-number slot is in: `object.owner`, `object.property.value`, `queuedTasks.activation.rtEnv`."""
    head, rest = where[0], list(where[1:])
    if isinstance(head, str) and head.startswith(("#", "anon ")):
        label = "anon" if head.startswith("anon ") else "object"
        if rest and isinstance(rest[0], str) and rest[0][:1] in ".:":
            label += ".property" if rest.pop(0)[0] == "." else ".verb"
        return f"{label}.{rest[0]}" if rest else label
    return ".".join(s for s in where if isinstance(s, str) and s not in ("key", "value"))


def _missing_key(db, by: str, number: int, where: tuple) -> str:
    """The group a slot naming missing object #number falls in, for `slots --missing-by`."""
    if by == "target":
        return f"#{number}"
    head = where[0]
    if head == "waifs":
        cls = int(db.waifs[where[1]].waif_class)
        return f"waifs of class #{cls} {db.name_of(cls) or ''}".rstrip()
    if by == "property" and isinstance(head, str) and head.startswith(("#", "anon ")):
        return f"{where[1]}" if str(where[1])[:1] == "." else _slot_label(where)
    if by == "holder" and isinstance(head, str) and head.startswith("#"):
        return f"{head} {db.name_of(int(head[1:])) or ''}".rstrip()
    return _slot_label(where)


@moodb.command()
@click.option("--missing-by", type=click.Choice(["holder", "property", "target"]),
              help="Instead of the field table, group the slots naming a missing object by the object holding them, "
                   "the property they are in, or the missing number.")
@click.option("--limit", default=40, show_default=True, help="Groups to show with --missing-by.")
@click.pass_context
def slots(ctx: click.Context, missing_by: str | None, limit: int) -> None:
    """Count every slot holding an object number, by field.

    Columns: slots; how many hold a negative number (#-1 and the like); how many
    name a permanent object that does not exist (recycled or past the end).
    The fields are those the renumbering walk rewrites. Waif slots are grouped
    by the waif's class.
    """
    from .objnums import remap as remap_numbers

    if ctx.obj["path"] is None:
        raise click.UsageError("no dump given: pass --db DUMP or set MOODB")
    db = load(ctx.obj["path"])
    permanent = {oid for oid, o in db.objects.items() if not o.anon}
    counts: dict[tuple[str, str], list[int]] = {}
    missing: Counter[str] = Counter()

    def count(kind: str, number: int, where: tuple) -> int:
        row = counts.setdefault((kind, _slot_label(where)), [0, 0, 0])
        row[0] += 1
        row[1] += number < 0
        if kind == "object" and number >= 0 and number not in permanent:
            row[2] += 1
            if missing_by:
                missing[_missing_key(db, missing_by, number, where)] += 1
        return number

    remap_numbers(db, count)
    if missing_by:
        click.echo(f"{sum(missing.values())} slots name a missing object, in {len(missing)} groups by {missing_by}")
        for key, n in missing.most_common(limit):
            click.echo(f"{n:>10}  {key}")
        return
    click.echo(f"{'kind':<7} {'field':<44} {'slots':>10} {'negative':>9} {'missing':>8}")
    for (kind, label), (total, negative, missing_count) in sorted(counts.items()):
        click.echo(f"{kind:<7} {label:<44} {total:>10} {negative:>9} {missing_count:>8}")


def _indexed(ctx: click.Context, path: str) -> LazyDatabase:
    """``path`` opened through its offset index, built first if there is none."""
    if ctx.obj["cache"] is None:
        raise click.UsageError("this command reads the offset index; drop --no-cache")
    db = open_indexed(path, ctx.obj["cache"])
    if not isinstance(db, LazyDatabase):
        db = open_indexed(path, ctx.obj["cache"])  # the first call built the index and parsed the whole dump
    if not isinstance(db, LazyDatabase):
        raise click.UsageError(f"{path} is older than format 17 and has no offset index")
    return db


@moodb.command()
@click.option("--since", "old", type=click.Path(exists=True, dir_okay=False),
              help="Only objects this older dump does not have (by number).")
@click.option("--by", "field", type=click.Choice(["owner", "parent", "location", "top", "object"]), default="owner",
              show_default=True, help="`top` is the outermost location (what a ship or area holds, however deep); "
                                      "`object` lists single objects, largest first.")
@click.option("--name", "name_pattern", help="Only objects whose name matches this regex (case-insensitive).")
@click.option("--limit", default=40, show_default=True, help="Groups to show.")
@click.pass_context
def census(ctx: click.Context, old: str | None, field: str, name_pattern: str | None, limit: int) -> None:
    """Group objects by owner, first parent or location, with their size in the dump.

    Bytes are the object's record (which holds the waifs first written there)
    plus its verb programs. Each group shows up to three example objects.
    """
    if ctx.obj["path"] is None:
        raise click.UsageError("no dump given: pass --db DUMP or set MOODB")
    db = _indexed(ctx, ctx.obj["path"])
    index = db.index
    skip = set(_indexed(ctx, old).index.records) if old else set()
    named = re.compile(name_pattern, re.I) if name_pattern else None
    groups: dict[int, list] = {}
    heads = list(db.object_heads())
    where = {o.id: int(o.location) for o in heads}

    def top(oid: int) -> int:
        seen = {oid}
        while where.get(oid, -1) in where and where[oid] not in seen:
            oid = where[oid]
            seen.add(oid)
        return oid

    for o in heads:
        if o.id in skip or (named and not named.search(o.name)):
            continue
        start, end, anon = index.records[o.id]
        size = end - start + sum(e - s for s, e in index.programs.get(o.id, ()))
        key = {"owner": lambda: int(o.owner), "parent": lambda: int(o.parents[0]) if o.parents else -1,
               "location": lambda: int(o.location), "top": lambda: top(o.id), "object": lambda: o.id}[field]()
        row = groups.setdefault(key, [0, 0, []])
        row[0] += 1
        row[1] += size
        if len(row[2]) < 3:
            row[2].append(f"#{o.id} {o.name!r}")
    objects, size = sum(r[0] for r in groups.values()), sum(r[1] for r in groups.values())
    click.echo(f"{objects} objects, {size} bytes, in {len(groups)} groups by {field}"
               + (f"; only objects {old} does not have" if old else ""))
    players = {int(p) for p in db.players}
    for key, (n, b, examples) in sorted(groups.items(), key=lambda kv: -kv[1][1])[:limit]:
        kind = f"[{_owner_kind(db, key, players)}] " if field == "owner" else ""
        click.echo(f"{n:>7} {b:>12}  #{key} {kind}{db.name_of(key) or ''}  e.g. {', '.join(examples)}")


SET_OPTIONS = ("--owned-by", "--name", "--empty")
SET_FLAGS = ("--with-contents", "--keep-numbers")


def _read_set(path: str | None) -> dict[str, list]:
    """A removal set kept in a file, one entry per line: a REF, `--owned-by REF`, `--name REGEX` (the rest
    of the line, spaces included), `--empty REF.NAME`, `--with-contents` or `--keep-numbers`. Blank lines
    and lines starting with `;` are skipped."""
    found: dict[str, list] = {"refs": [], **{k: [] for k in SET_OPTIONS + SET_FLAGS}}
    if path is None:
        return found
    for n, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        word, _, rest = line.partition(" ")
        if word in SET_FLAGS and not rest:
            found[word].append(True)
        elif word in SET_OPTIONS and rest.strip():
            found[word].append(rest.strip())
        elif not word.startswith("-") and not rest:
            found["refs"].append(word)
        else:
            raise click.UsageError(f"{path}:{n}: not a set entry: {line!r}")
    return found


def _chosen(db, refs, owned_by, names_rx) -> tuple[set[int], dict[str, set[int]]]:
    """Objects named by REFS, --owned-by (the owner and what it owns) and --name (by regex)."""
    chosen = {_object(db, r).id for r in refs}
    for r in owned_by:
        owner = _object(db, r).id
        chosen |= {owner} | {oid for oid, o in db.objects.items() if int(o.owner) == owner and not o.anon}
    by_name = {}
    for rx in names_rx:
        pattern = re.compile(rx, re.I)
        by_name[rx] = {oid for oid, o in db.objects.items() if not o.anon and pattern.search(o.name)}
        chosen |= by_name[rx]
    return chosen, by_name


@moodb.command()
@click.argument("refs", nargs=-1)
@click.option("--owned-by", multiple=True, help="Also remove this object and everything it owns (repeatable).")
@click.option("--with-contents", is_flag=True, help="Also remove everything located in a removed object, except players.")
@click.option("--name", "names_rx", multiple=True, metavar="REGEX",
              help="Also remove every object whose name matches (case-insensitive, repeatable).")
@click.option("--limit", default=40, show_default=True, help="Inbound references to list (0 = all).")
@click.option("--field", "fields", multiple=True,
              help="List only the inbound references in this field, as the counts name it (repeatable).")
@click.option("--set", "set_file", type=click.Path(exists=True, dir_okay=False),
              help="Read more of the set from FILE: one REF, --owned-by REF, --name REGEX or --with-contents "
                   "per line (--empty lines are for rewrite and ignored here); ; starts a comment.")
@click.pass_context
def removal(ctx: click.Context, refs: tuple[str, ...], owned_by: tuple[str, ...], with_contents: bool,
            names_rx: tuple[str, ...], limit: int | None, fields: tuple[str, ...], set_file: str | None) -> None:
    """What removing objects would leave pointing at nothing.

    The set is closed over descendants (and contents, with --with-contents). Every slot outside the set that names
    a removed object is listed by field, holder and target; entries in
    .owned_objects/.lost_souls and in children/contents lists are only
    counted, since the rewrite drops them. Also lists objects located inside
    the set that stay, and players in the set.
    """
    from .removal import plan

    if ctx.obj["path"] is None:
        raise click.UsageError("no dump given: pass --db DUMP or set MOODB")
    kept = _read_set(set_file)
    with_contents = with_contents or bool(kept["--with-contents"])
    db = load(ctx.obj["path"])
    chosen, by_name = _chosen(db, (*refs, *kept["refs"]), (*owned_by, *kept["--owned-by"]),
                              (*names_rx, *kept["--name"]))
    if not chosen:
        raise click.UsageError("name objects to remove, or --owned-by / --name")
    p = plan(db, chosen, with_contents)
    limit = limit or None
    click.echo(f"remove {len(p.removed)} objects: {len(p.chosen)} chosen, {len(p.descendants)} descendants"
               + (f", {len(p.contents)} contents" if with_contents else ""))
    for rx, found in by_name.items():
        click.echo(f"--name {rx}: {len(found)}: " + ", ".join(label(db, x) for x in sorted(found)[:limit])
                   + (" ..." if limit and len(found) > limit else ""))
    if p.players:
        click.echo("players removed: " + ", ".join(label(db, x) for x in sorted(p.players)))
    click.echo(f"inbound references from outside: {sum(p.inbound.values())} slots")
    by_field = Counter()
    for (field, _, _), n in p.inbound.items():
        by_field[field] += n
    for field, n in by_field.most_common():
        click.echo(f"{n:>8}  {field}")
    by_holder = Counter()
    for (_, holder, _), n in p.inbound.items():
        by_holder[holder] += n
    click.echo(f"by holder ({len(by_holder)}):")
    for holder, n in by_holder.most_common(limit):
        name = db.name_of(int(holder[1:])) if holder[:1] == "#" else None
        click.echo(f"{n:>8}  {holder}" + (f" {moo_string(name)}" if name is not None else ""))
    listed = [(k, n) for k, n in p.inbound.most_common() if not fields or k[0] in fields]
    for (field, holder, target), n in listed[:limit]:
        click.echo(f"    {field}  {holder} -> {label(db, target)}" + (f"  x{n}" if n > 1 else ""))
    click.echo("dropped by the rewrite: " + (", ".join(f"{f} {n}" for f, n in p.dropped.most_common()) or "nothing"))
    click.echo(f"staying objects located inside the set: {len(p.stranded)}")
    for oid, where in sorted(p.stranded.items())[:limit]:
        click.echo(f"    {label(db, oid)} in #{where}")


@moodb.command()
@click.option("--limit", default=40, show_default=True, help="Places to list (0 = all).")
@click.pass_context
def mentions(ctx: click.Context, limit: int) -> None:
    """Where existing objects' numbers are spelled `#N` inside strings, by property or verb.

    A renumbering rewrites object values and code literals, not text: these strings keep the old numbers.
    Code that builds keys with tostr(obj) or reads them back with toobj() then meets the wrong object.
    """
    from .liveness import mentions as find

    if ctx.obj["path"] is None:
        raise click.UsageError("no dump given: pass --db DUMP or set MOODB")
    db = load(ctx.obj["path"])
    found = find(db, {oid for oid, o in db.objects.items() if not o.anon})
    by_place: Counter = Counter()
    for where in found.values():
        for w in where:
            by_place[w] += 1
    click.echo(f"{len(found)} objects are spelled #N in strings, in {len(by_place)} places")
    for w, n in by_place.most_common(limit or None):
        click.echo(f"{n:>8}  {w}")


@moodb.command()
@click.option("--skip", multiple=True, metavar="NAME",
              help="A property whose values keep nothing alive (bookkeeping); repeatable, case-insensitive.")
@click.option("--limit", default=40, show_default=True, help="Groups to list (0 = all).")
@click.option("--set", "set_file", type=click.Path(exists=True, dir_okay=False),
              help="Treat this removal set (as `removal --set` reads it) as already gone.")
@click.pass_context
def unreachable(ctx: click.Context, skip: tuple[str, ...], limit: int, set_file: str | None) -> None:
    """Objects nothing live reaches, grouped under the outermost unreachable object that holds them.

    Roots: #0 (every $name), players, tasks and other slots no object holds. Followed: every number a
    reached object holds in properties, verb code, location, contents, parents and waifs. Not followed:
    children, owners, owned lists, last_move, lost_souls and --skip properties.
    """
    from .liveness import mentions
    from .liveness import unreachable as find

    if ctx.obj["path"] is None:
        raise click.UsageError("no dump given: pass --db DUMP or set MOODB")
    db = load(ctx.obj["path"])
    removed: set[int] = set()
    if set_file:
        from .removal import plan

        kept = _read_set(set_file)
        chosen, _ = _chosen(db, kept["refs"], kept["--owned-by"], kept["--name"])
        removed = plan(db, chosen, bool(kept["--with-contents"])).removed
    gone = find(db, skip, removed)
    players = {int(p) for p in db.players}
    owners = Counter(int(db.objects[oid].owner) for oid in gone)
    spelled = mentions(db, gone, ignore=gone | removed)

    def top(oid: int) -> int:
        seen = {oid}
        while int(db.objects[oid].location) in gone and int(db.objects[oid].location) not in seen:
            oid = int(db.objects[oid].location)
            seen.add(oid)
        return oid

    groups: dict[int, list[int]] = {}
    for oid in sorted(gone):
        groups.setdefault(top(oid), []).append(oid)
    click.echo(f"unreachable: {len(gone)} objects in {len(groups)} groups"
               + (f" beside the {len(removed)} the set removes" if set_file else "")
               + (f" (skipping {', '.join(skip)})" if skip else ""))
    for root, members in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:limit or None]:
        o = db.objects[root]
        click.echo(f"{len(members):>7}  {label(db, root)} owner #{int(o.owner)} parent "
                   f"#{int(o.parents[0]) if o.parents else -1}"
                   + (f"  e.g. {', '.join(label(db, m) for m in members[1:4])}" if len(members) > 1 else ""))
    click.echo(f"by owner ({len(owners)}):")
    for owner, n in owners.most_common(limit or None):
        kind = ("player" if owner in players else "nothing" if owner == -1
                else "object" if owner in db.objects else "missing")
        click.echo(f"{n:>7}  {label(db, owner) if owner in db.objects else f'#{owner}'} [{kind}]")
    click.echo(f"spelled as #N in a string elsewhere (the sweep cannot follow these): {len(spelled)}")
    for oid, where in sorted(spelled.items())[:limit or None]:
        click.echo(f"    {label(db, oid)}  in {', '.join(sorted(where)[:4])}" + (" ..." if len(where) > 4 else ""))


@moodb.command()
@click.argument("out", type=click.Path(dir_okay=False))
@click.argument("refs", nargs=-1)
@click.option("--owned-by", multiple=True, help="Also remove this object and everything it owns (repeatable).")
@click.option("--owner-to", help="Give surviving objects, verbs and properties owned by removed objects to this object.")
@click.option("--strand-to", help="Move surviving objects located in removed ones here (#-1 for nowhere).")
@click.option("--with-contents", is_flag=True, help="Also remove everything located in a removed object, except players.")
@click.option("--name", "names_rx", multiple=True, metavar="REGEX",
              help="Also remove every object whose name matches (case-insensitive, repeatable).")
@click.option("--empty", "empties", multiple=True, metavar="REF.NAME",
              help="Empty this property's value (same type) and clear descendants' own values; repeatable.")
@click.option("--set", "set_file", type=click.Path(exists=True, dir_okay=False),
              help="Read more of the set from FILE: one REF, --owned-by REF, --name REGEX, --empty REF.NAME, "
                   "--with-contents or --keep-numbers per line; ; starts a comment.")
@click.option("--keep-numbers", is_flag=True,
              help="Renumber nothing: removed objects become recycled slots and survivors keep their numbers.")
@click.pass_context
def rewrite(ctx: click.Context, out: str, refs: tuple[str, ...], owned_by: tuple[str, ...],
            owner_to: str | None, strand_to: str | None, with_contents: bool, names_rx: tuple[str, ...],
            empties: tuple[str, ...], set_file: str | None, keep_numbers: bool) -> None:
    """Write OUT: the dump without the named objects, numbered densely unless --keep-numbers.

    Removes the objects (and their descendants), numbers the rest #0.. in their
    old order with no recycled slots, and rewrites every reference: values,
    the object table, verb code literals, tasks. A reference to a removed or
    missing object becomes a distinct negative number (base - old). Numbers
    spelled inside strings are NOT rewritten, so dense numbering breaks code
    that keys data by tostr(obj) and outside URLs that carry object numbers;
    --keep-numbers avoids that (only references to removed objects change).
    Writes OUT.map.json (old -> new) and prints what changed. Refuses while
    anything surviving is owned by, or located in, a removed object, unless
    --owner-to or --strand-to says where it goes.
    """
    from .rewrite import RewriteRefused, empty_property
    from .rewrite import rewrite as rewrite_db
    from .writer import Writer

    if ctx.obj["path"] is None:
        raise click.UsageError("no dump given: pass --db DUMP or set MOODB")
    if Path(out).resolve() == Path(ctx.obj["path"]).resolve():
        raise click.UsageError("OUT must not be the dump being read")
    kept = _read_set(set_file)
    with_contents = with_contents or bool(kept["--with-contents"])
    keep_numbers = keep_numbers or bool(kept["--keep-numbers"])
    empties = (*empties, *kept["--empty"])
    db = load(ctx.obj["path"])
    chosen, _ = _chosen(db, (*refs, *kept["refs"]), (*owned_by, *kept["--owned-by"]), (*names_rx, *kept["--name"]))
    target = {"owner_to": owner_to, "strand_to": strand_to}
    resolved = {k: (int(v[1:]) if v.startswith("#-") else _object(db, v).id) if v else None for k, v in target.items()}
    emptied = []
    try:
        for spec in empties:
            ref, _, name = spec.rpartition(".")
            if not ref or not name:
                raise click.UsageError(f"--empty wants REF.NAME, got {spec!r}")
            emptied.append(f"{spec} {empty_property(db, _object(db, ref).id, name)}")
        report = rewrite_db(db, chosen, **resolved, with_contents=with_contents, keep_numbers=keep_numbers)
    except RewriteRefused as e:
        raise click.ClickException(str(e)) from e
    with open(out, "w", encoding="latin-1", newline="") as f:
        Writer(db=db, output_file=f).writeDatabase()
    Path(out + ".map.json").write_text(json.dumps({"base": report.base, "map": report.renumbered}))
    p = report.plan
    click.echo(f"removed {len(p.removed)} objects ({len(p.chosen)} chosen, {len(p.descendants)} descendants, "
               f"{len(p.contents)} contents); "
               + (f"numbers kept: {sum(1 for o in db.objects.values() if not o.anon)} objects in "
                  f"#0..#{db.total_objects - 1}, {len(db.recycled_objects)} recycled slots" if keep_numbers else
                  f"{db.total_objects} permanent objects now, highest #{db.total_objects - 1}"))
    click.echo(f"dead numbers: base {report.base}; {sum(report.dead_slots.values())} slots")
    for field, n in report.dead_slots.most_common():
        click.echo(f"{n:>10}  {field}")
    click.echo("dropped: " + (", ".join(f"{k} {n}" for k, n in report.dropped.most_common()) or "nothing"))
    click.echo("reassigned: " + (", ".join(f"{k} {n}" for k, n in report.reassigned.items()) or "nothing"))
    click.echo("tasks dropped: " + (", ".join(f"{s} {t}" for s, t in report.tasks_dropped) or "none"))
    click.echo("emptied (slots changed): " + (", ".join(emptied) or "nothing"))
    click.echo(f"wrote {out} and {out}.map.json")


def _object_kind(db: LazyDatabase, oid: int, players: set[int]) -> str:
    """What a number names: nothing (negative), a missing object, a player or another object."""
    if oid < 0:
        return "nothing"
    if oid not in db.index.records:
        return "missing"
    return "player" if oid in players else "object"


def _owner_kind(db: LazyDatabase, oid: int, players: set[int]) -> str:
    """What an owner is: a player, an object that owns itself, another object, nothing (#-1) or missing."""
    kind = _object_kind(db, oid, players)
    if kind == "object" and int(db.read_head(oid).owner) == oid:
        return "self-owned"
    return kind


@moodb.command()
@click.argument("ref")
@click.pass_context
def obj(ctx: click.Context, ref: str) -> None:
    """Show an object: header, parents, verbs and its own properties."""
    db, names = _db(ctx)
    o = _object(db, ref)
    click.echo(label(db, o.id, names))
    click.echo(f"  owner     {label(db, o.owner, names)}")
    click.echo(f"  location  {label(db, o.location, names)}")
    click.echo(f"  flags     {object_flags(o)}")
    click.echo("  parents   " + (", ".join(label(db, int(p), names) for p in o.parents) or "none"))
    click.echo("  ancestry  " + " > ".join(f"#{a.id}" for a in db.ancestors(o)))
    click.echo(f"  children  {len(o.children)}  contents {len(o.contents)}")
    click.echo(f"verbs ({len(o.verbs)}):")
    for idx, v in enumerate(o.verbs):
        click.echo(f"  [{idx}] {verb_summary(v)}  {len(v.code or [])} lines")
    props = own_properties(o)
    click.echo(f"own properties ({len(props)}, {len(o.properties) - len(props)} inherited; see `props`):")
    for p in props:
        click.echo(f"  .{p.propertyName}  {property_perms(p)}  owner #{int(p.owner)}  = {format_value(p.value, 100)}")


@moodb.command()
@click.argument("ref")
@click.option("--full", is_flag=True, help="Do not truncate long values.")
@click.option("--builtin", is_flag=True, help="Also show builtin properties (name, owner, location, ...).")
@click.option("--name", "globs", multiple=True, metavar="GLOB",
              help="Only properties whose name matches (case-insensitive, repeatable): --name '*_msg'.")
@click.option("-r", "--descendants", "recursive", is_flag=True,
              help="One line per value: REF's effective values, then every descendant's own (not clear) values.")
@click.option("--group", is_flag=True,
              help="With -r: one line per distinct name and value, with how many objects hold it and the first five.")
@click.option("--largest", type=int, metavar="N",
              help="Only the N stored (not clear) values taking the most bytes in the dump, largest first.")
@click.pass_context
def props(ctx: click.Context, ref: str, full: bool, builtin: bool, globs: tuple[str, ...], recursive: bool,
          group: bool, largest: int | None) -> None:
    """List every property of an object, own and inherited, with effective values."""
    db, names = _db(ctx)
    o = _object(db, ref)
    limit = None if full else 100
    patterns = [g.lower() for g in globs]

    def wanted(name: str) -> bool:
        return not patterns or any(fnmatchcase(name.lower(), g) for g in patterns)

    if largest is not None:
        from io import StringIO

        from .writer import Writer

        def size(value) -> int:
            out = StringIO()
            Writer(db=db, output_file=out).writeValue(value)
            return len(out.getvalue())

        sized = sorted(((size(v), n, v) for n, v in stored_properties(db, o) if wanted(n)), key=lambda r: -r[0])
        for nbytes, name, value in sized[:largest]:
            click.echo(f"{nbytes:>10}  .{name} = {format_value(value, limit or 60)}")
        return
    if group and not recursive:
        raise click.UsageError("--group needs -r")
    if recursive:
        rows = [(o, h.name, h.value) for h in all_properties(db, o) if wanted(h.name)]
        rows += [(d, name, value) for d in descendants(db, o) for name, value in stored_properties(db, d) if wanted(name)]
        if group:
            holders: dict[tuple[str, str], list[int]] = {}
            for holder, name, value in rows:
                holders.setdefault((name.lower(), format_value(value, limit)), []).append(holder.id)
            for (name, text), ids in sorted(holders.items(), key=lambda kv: (kv[0][0], -len(kv[1]), kv[0][1])):
                shown = " ".join(f"#{i}" for i in ids[:5]) + (" ..." if len(ids) > 5 else "")
                click.echo(f".{name} = {text}  x{len(ids)}  {shown}")
            return
        for holder, name, value in rows:
            click.echo(f"{label(db, holder.id, names)}.{name} = {format_value(value, limit)}")
        return
    if builtin:
        click.echo("== builtin")
        for n in BUILTIN_PROPS:
            if wanted(n):
                click.echo(f"  .{n}  = {format_value(builtin_value(o, n), limit)}")
    for definer, hits in groupby((h for h in all_properties(db, o) if wanted(h.name)), key=lambda h: h.definer.id):
        click.echo(f"== {label(db, definer, names)}")
        for h in hits:
            source = "" if h.value_from is o else f"  (clear, from #{h.value_from.id})"
            click.echo(f"  .{h.name}  {property_perms(h.slot)}  owner #{int(h.slot.owner)}  = {format_value(h.value, limit)}{source}")


@moodb.command()
@click.argument("ref")
@click.option("-i", "--inherited", is_flag=True, help="Also list verbs of every ancestor.")
@click.pass_context
def verbs(ctx: click.Context, ref: str, inherited: bool) -> None:
    """List verbs as #OBJ:[index] "names" args perms owner."""
    db, names = _db(ctx)
    o = _object(db, ref)
    for a in db.ancestors(o) if inherited else [o]:
        if inherited:
            click.echo(f"== {label(db, a.id, names)}")
        for idx, v in enumerate(a.verbs):
            click.echo(f"#{a.id}:[{idx}] {verb_summary(v)}")


def _print_program(db, names, o, idx: int, v, line_numbers: bool) -> None:
    click.echo(f"@program {label(db, o.id, names)}:[{idx}] {verb_summary(v)}")
    if v.code is None:
        click.echo("  (no program)")
    for n, line in enumerate(v.code or [], 1):
        click.echo(f"{n:4}  {line}" if line_numbers else line)
    click.echo(".")


@moodb.command()
@click.argument("spec", nargs=-1, required=True)
@click.option("-n", "--line-numbers", is_flag=True, help="Number the code lines.")
@click.option("--fingerprint", is_flag=True, help="Print JSON hashes, line counts and recognized UTC stamp dates, without source text.")
@click.pass_context
def code(ctx: click.Context, spec: tuple[str, ...], line_numbers: bool, fingerprint: bool) -> None:
    """Print verb code: `code '$httpd:GET' 852:12`. A bare REF prints every verb on it.

    Verbs are found on the object or its ancestors; the header names the
    object that defines the verb.
    """
    if fingerprint and line_numbers:
        raise click.UsageError("--fingerprint cannot be combined with --line-numbers")
    db, names = _db(ctx)
    fingerprints = []

    def emit(o, idx, v):
        if fingerprint:
            fingerprints.append({"object": o.id, "index": idx, "native_slot": idx + 1,
                                 **code_fingerprint(v)})
        else:
            _print_program(db, names, o, idx, v, line_numbers)

    for s in spec:
        try:
            ref, separator, wanted = s.partition(":")
            o = resolve_object(db, ref)
            if not separator:
                for idx, v in enumerate(o.verbs):
                    emit(o, idx, v)
                continue
            if not wanted:
                raise LookupFailed(f"expected REF or REF:VERB, got {s!r}")
            hit = find_verb(db, o, wanted)
        except LookupFailed as e:
            raise click.ClickException("Unresolved code reference" if fingerprint else str(e))
        emit(hit.obj, hit.index, hit.verb)
    if fingerprint:
        click.echo(json.dumps(fingerprints, sort_keys=True))


def _program_body(v) -> list[str]:
    """A verb's code without the trailing `"Last modified ..."` stamp the verb editor adds."""
    lines = list(v.code or [])
    while lines and lines[-1].startswith('"Last modified '):
        lines.pop()
    return lines


@moodb.command("compare-verbs")
@click.argument("a")
@click.argument("b")
@click.option("-q", "--quiet", is_flag=True, help="Only the one-line verdict for each verb, no diff.")
@click.pass_context
def compare_verbs(ctx: click.Context, a: str, b: str, quiet: bool) -> None:
    """Compare the verbs A defines with B's verbs of the same names, in one dump.

    For checking a move or copy of verbs from one object to another. Each
    verb A defines is `same`, `differs` (with a unified diff) or `missing`
    on B; verbs only B defines are listed last. The verb editor's trailing
    "Last modified" line is ignored. Ends with the counts.
    """
    import difflib

    db, _ = _db(ctx)
    oa, ob = _object(db, a), _object(db, b)
    theirs: dict[str, list] = {}
    for v in ob.verbs:
        theirs.setdefault(v.name, []).append(v)
    counts: Counter[str] = Counter()
    for idx, v in enumerate(oa.verbs):
        other = theirs.get(v.name, [])
        match = other.pop(0) if other else None
        first = v.name.split(" ")[0]
        if match is None:
            verdict = "missing"
        elif _program_body(v) == _program_body(match):
            verdict = "same"
        else:
            verdict = "differs"
        counts[verdict] += 1
        click.echo(f"{verdict:8} #{oa.id}:[{idx}] {first}")
        if verdict == "differs" and not quiet:
            for line in difflib.unified_diff(_program_body(v), _program_body(match), f"#{oa.id}:{first}", f"#{ob.id}:{first}", lineterm="", n=1):
                click.echo("    " + line)
    for name, left in theirs.items():
        for _v in left:
            counts["only-b"] += 1
            click.echo(f"only-b   #{ob.id}:{name.split(' ')[0]}")
    click.echo("  ".join(f"{k} {counts[k]}" for k in ("same", "differs", "missing", "only-b")))


@moodb.command()
@click.argument("spec", nargs=-1, required=True)
@click.option("--full", is_flag=True, help="Do not truncate long values.")
@click.pass_context
def prop(ctx: click.Context, spec: tuple[str, ...], full: bool) -> None:
    """Print effective property values: `prop '$httpd.port' 0.httpd`.

    Names are case-insensitive. Builtins (name, owner, location, contents,
    last_move, programmer, wizard, r, w, f, a) work too.
    """
    db, _ = _db(ctx)
    for s in spec:
        try:
            ref, rest = split_ref(s)
            if not rest.startswith(".") or len(rest) < 2:
                raise LookupFailed(f"expected REF.PROP, got {s!r}")
            o = resolve_object(db, ref)
            name = rest[1:]
            # A literal property wins at each object, preserving dotted names.
            while name.lower() not in BUILTIN_PROPS and find_slot(db, o, name) is None:
                part, separator, remainder = name.partition(".")
                if not separator:
                    break
                o = resolve_object(db, f"#{o.id}.{part}")
                name = remainder
                if not name:
                    raise LookupFailed(f"expected REF.PROP, got {s!r}")
            if name.lower() in BUILTIN_PROPS:
                click.echo(f"#{o.id}.{name.lower()} = {format_value(builtin_value(o, name), None if full else 2000)}")
                continue
            hit = lookup_property(db, o, name)
        except LookupFailed as e:
            raise click.ClickException(str(e))
        notes = []
        if hit.definer is not o:
            notes.append(f"defined on #{hit.definer.id}")
        if hit.value_from is not o:
            notes.append(f"clear, value from #{hit.value_from.id}")
        where = f" ({'; '.join(notes)})" if notes else ""
        click.echo(f"#{o.id}.{hit.name}{where} = {format_value(hit.value, None if full else 2000)}")


@moodb.command()
@click.argument("spec")
@click.pass_context
def keys(ctx: click.Context, spec: str) -> None:
    """Group a map property's keys, or a list property's elements, by what they name.

    Object numbers are a player, another object, a missing object (recycled or past
    the end) or nothing (negative); anything else is grouped by its type. Bytes are
    the entries' size in the dump (key and value for a map).
    """
    from io import StringIO

    from .writer import Writer

    if ctx.obj["path"] is None:
        raise click.UsageError("no dump given: pass --db DUMP or set MOODB")
    db = _indexed(ctx, ctx.obj["path"])
    try:
        ref, rest = split_ref(spec)
        if not rest.startswith(".") or len(rest) < 2:
            raise LookupFailed(f"expected REF.PROP, got {spec!r}")
        o = resolve_object(db, ref)
        hit = lookup_property(db, o, rest[1:])
    except LookupFailed as e:
        raise click.ClickException(str(e))
    value = hit.value
    if isinstance(value, dict):
        entries, what = list(value.items()), f"map of {len(value)} keys"
    elif isinstance(value, list):
        entries, what = [(e, None) for e in value], f"list of {len(value)} elements"
    else:
        raise click.ClickException(f"#{o.id}.{hit.name} is neither a map nor a list")

    def size(*values) -> int:
        out = StringIO()
        writer = Writer(db=db, output_file=out)
        for v in values:
            if v is not None:
                writer.writeValue(v)
        return len(out.getvalue())

    players = {int(p) for p in db.players}
    groups: dict[str, list] = {}
    for key, entry in entries:
        kind = _object_kind(db, int(key), players) if isinstance(key, ObjNum) else type(key).__name__.upper()
        row = groups.setdefault(kind, [0, 0, []])
        row[0] += 1
        row[1] += size(key, entry)
        if len(row[2]) < 3:
            row[2].append(format_value(key, 40))
    click.echo(f"#{o.id}.{hit.name}: {what}")
    for kind, (n, nbytes, examples) in sorted(groups.items(), key=lambda kv: (-kv[1][0], kv[0])):
        click.echo(f"{kind:<8} {n:>8} {nbytes:>12}  e.g. {', '.join(examples)}")


def _clip(text: str, width: int, rx: re.Pattern[str] | None = None) -> str:
    """``text`` cut to ``width`` characters around the first match of ``rx``; 0 keeps it whole."""
    if not width or len(text) <= width:
        return text
    m = rx.search(text) if rx else None
    start = max(0, min(m.start() - width // 2, len(text) - width)) if m else 0
    return ("..." if start else "") + text[start:start + width] + f"... [{len(text)} chars]"


WIDTH = click.option("--width", default=400, show_default=True, metavar="N",
                     help="Cut code lines longer than N characters around the match; 0 prints them whole.")


@moodb.command()
@click.argument("pattern")
@click.option("-o", "--obj", "objs", multiple=True, help="Only search these objects (repeatable).")
@click.option("-i", "--ignore-case", is_flag=True)
@click.option("-F", "--fixed-strings", is_flag=True, help="PATTERN is a literal string.")
@click.option("-l", "--verbs-only", is_flag=True, help="Print each matching verb once.")
@click.option("-C", "--context", default=0, metavar="N", help="Show N lines of context around each match.")
@click.option("--verb", "verb_names", multiple=True, metavar="NAME",
              help="Only verbs NAME would call, by MOO name matching (repeatable).")
@click.option("-L", "--verbs-without-match", is_flag=True,
              help="Print each verb with a program and no matching line, once.")
@WIDTH
@click.pass_context
def grep(ctx: click.Context, pattern: str, objs: tuple[str, ...], ignore_case: bool, fixed_strings: bool, verbs_only: bool,
         context: int, verb_names: tuple[str, ...], verbs_without_match: bool, width: int) -> None:
    """Search verb code (Python regex): #OBJ:[index] name:LINE: text."""
    db, names = _db(ctx)
    selected = [_object(db, r) for r in objs] or None
    try:
        rx = re.compile(re.escape(pattern) if fixed_strings else pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise click.BadParameter(str(e), param_hint="PATTERN")

    def wanted(v) -> bool:
        return not verb_names or any(verbcasecmp(v.name, name) for name in verb_names)

    if verbs_without_match:
        if selected is None:
            selected = db.objects_with_programs() if isinstance(db, LazyDatabase) else list(all_objects(db))
        for o in selected:
            for idx, v in enumerate(o.verbs):
                if v.code and wanted(v) and not any(rx.search(line) for line in v.code):
                    click.echo(f"{label(db, o.id, names)}:[{idx}] {moo_string(v.name)}")
        return
    by_verb = groupby((h for h in grep_verbs(db, rx, selected) if wanted(h.verb)), key=lambda h: (h.obj.id, h.index))
    for n_group, (_, group) in enumerate(by_verb):
        hits = list(group)
        o, idx, v = hits[0].obj, hits[0].index, hits[0].verb
        if verbs_only:
            click.echo(f"{label(db, o.id, names)}:[{idx}] {moo_string(v.name)}")
            continue
        if not context:
            for h in hits:
                click.echo(f"#{o.id}:[{idx}] {_alias(v)}:{h.lineno}: {_clip(h.line.strip(), width, rx)}")
            continue
        if n_group:
            click.echo("--")
        matched = {h.lineno for h in hits}
        code_lines = v.code or []
        shown = sorted({n for m in matched for n in range(max(1, m - context), min(len(code_lines), m + context) + 1)})
        for prev, n in zip([None] + shown, shown):
            if prev is not None and n != prev + 1:
                click.echo("--")
            sep = ":" if n in matched else "-"
            click.echo(f"#{o.id}:[{idx}] {_alias(v)}{sep}{n}{sep} {_clip(code_lines[n - 1].strip(), width, rx)}")


@moodb.command()
@click.argument("old", type=click.Path(exists=True, dir_okay=False))
@click.argument("pattern", required=False)
@click.option("-i", "--ignore-case", is_flag=True)
@click.option("-F", "--fixed-strings", is_flag=True, help="PATTERN is a literal string.")
@click.option("-l", "--verbs-only", is_flag=True, help="Print each verb once, with its count of added lines.")
@click.option("-o", "--obj", "objs", multiple=True, help="Only these objects (repeatable).")
@click.option("--verb", "verb_names", multiple=True, metavar="NAME",
              help="Only verbs NAME would call, by MOO name matching (repeatable).")
@click.option("-v", "--invert-match", is_flag=True, help="Only added lines that do NOT match PATTERN.")
@click.option("--existing", is_flag=True,
              help="Only objects OLD already had: leaves out new numbers, and numbers recycled and reused since.")
@click.option("--identity", default="creation_time", show_default=True, metavar="PROP",
              help="The property --existing compares to tell a reused number from the same object.")
@click.pass_context
def added(ctx: click.Context, old: str, pattern: str | None, ignore_case: bool, fixed_strings: bool, verbs_only: bool,
          objs: tuple[str, ...], verb_names: tuple[str, ...], invert_match: bool, existing: bool,
          identity: str) -> None:
    """Verb code lines this dump has that the OLD dump's same verb does not: #OBJ:[index] name:LINE: text.

    With PATTERN (Python regex), only added lines matching it, or with -v
    only those that do not. A verb that OLD does not have is marked "new
    verb". --existing answers "what did this work add to objects it did not
    create"; a second summary line counts the objects it left out.
    """
    rx = _added_pattern(pattern, ignore_case, fixed_strings, invert_match)
    db, names = _db(ctx)
    cache = ctx.obj["cache"]
    old_db = load(old) if cache is None else open_indexed(old, cache)
    selected = [_object(db, r) for r in objs] or None
    lines = verbs = 0
    continuity: dict[int, str] = {}
    found = added_verb_lines(old_db, db, rx, selected, invert=invert_match)
    for _, group in groupby(found, key=lambda h: (h.obj.id, h.index)):
        hits = list(group)
        o, idx, v = hits[0].obj, hits[0].index, hits[0].verb
        if verb_names and not any(verbcasecmp(v.name, name) for name in verb_names):
            continue
        if existing and _left_out(continuity, old_db, db, o.id, identity):
            continue
        lines += len(hits)
        verbs += 1
        new = "  (new verb)" if hits[0].is_new else ""
        if verbs_only:
            click.echo(f"{len(hits):4d}  {label(db, o.id, names)}:[{idx}] {moo_string(v.name)}{new}")
            continue
        for h in hits:
            click.echo(f"#{o.id}:[{idx}] {_alias(v)}:{h.lineno}: {h.line}{new}")
    click.echo(f"{lines} added lines in {verbs} verbs")
    if existing:
        _echo_existing_summary(continuity, identity)


@moodb.command("added-props")
@click.argument("old", type=click.Path(exists=True, dir_okay=False))
@click.argument("pattern", required=False)
@click.option("-i", "--ignore-case", is_flag=True)
@click.option("-F", "--fixed-strings", is_flag=True, help="PATTERN is a literal string.")
@click.option("-c", "--count", is_flag=True, help="Print each object once, with its count of added definitions.")
@click.option("--holders", is_flag=True,
              help="After each definition, how many of the object and its descendants hold a value that is not clear.")
@click.option("-o", "--obj", "objs", multiple=True, help="Only these objects (repeatable).")
@click.option("-v", "--invert-match", is_flag=True, help="Only added definitions whose name does NOT match PATTERN.")
@click.option("--existing", is_flag=True,
              help="Only objects OLD already had: leaves out new numbers, and numbers recycled and reused since.")
@click.option("--identity", default="creation_time", show_default=True, metavar="PROP",
              help="The property --existing compares to tell a reused number from the same object.")
@click.pass_context
def added_props(ctx: click.Context, old: str, pattern: str | None, ignore_case: bool, fixed_strings: bool,
                count: bool, holders: bool, objs: tuple[str, ...], invert_match: bool, existing: bool,
                identity: str) -> None:
    """Property definitions this dump has that the OLD dump's same object does not: #OBJ.name  perms  owner.

    A definition is a property the object itself defines, not one it
    inherits. Names compare without regard to letter case. With PATTERN
    (Python regex on the property name), only definitions matching it, or
    with -v only those that do not. --existing answers "which properties did
    this work define on objects it did not create"; a second summary line
    counts the objects it left out.
    """
    rx = _added_pattern(pattern, ignore_case, fixed_strings, invert_match)
    db, names = _db(ctx)
    cache = ctx.obj["cache"]
    old_db = load(old) if cache is None else open_indexed(old, cache)
    selected = [_object(db, r) for r in objs] or None
    definitions = objects = 0
    continuity: dict[int, str] = {}
    found = added_property_definitions(old_db, db, rx, selected, invert=invert_match)
    for num, group in groupby(found, key=lambda h: h.obj.id):
        if existing and _left_out(continuity, old_db, db, num, identity):
            continue
        hits = list(group)
        definitions += len(hits)
        objects += 1
        where = label(db, num, names)
        if count:
            click.echo(f"{len(hits):4d}  {where}")
            continue
        for h in hits:
            held = "  value held by {} of {}".format(*value_holders(db, h.obj, h.prop.propertyName)) if holders else ""
            click.echo(f"{where}.{h.prop.propertyName}  {property_perms(h.prop)}  owner #{int(h.prop.owner)}{held}")
    click.echo(f"{definitions} added property definitions on {objects} objects")
    if existing:
        _echo_existing_summary(continuity, identity)


@moodb.command()
@click.argument("text", nargs=-1, required=True)
@click.option("--verb", "mode", flag_value="verb", help="Find objects defining a verb that TEXT would call (MOO name matching), waif class verbs (`:TEXT`) included.")
@click.option("--prop", "mode", flag_value="prop", help="Find objects defining a property named TEXT (case-insensitive).")
@click.option("--glob", "glob", is_flag=True,
              help="With --verb or --prop: TEXT is a shell pattern (`event_*`) matched against each verb name or the property name.")
@click.option("-c", "--count", is_flag=True, help="Print only the number of hits for each TEXT.")
@click.pass_context
def find(ctx: click.Context, text: tuple[str, ...], mode: str | None, glob: bool, count: bool) -> None:
    """Find objects whose name or $name contains TEXT (case-insensitive).

    With --verb or --prop, find where a verb or property is defined instead.
    Several TEXTs are answered in one run, each under an `== TEXT` line.
    """
    if glob and mode is None:
        raise click.UsageError("--glob needs --verb or --prop")
    db, names = _db(ctx)
    hits: dict[str, list[str]] = {t: [] for t in text}
    if mode == "verb":
        for o in object_heads(db):
            for idx, v in enumerate(o.verbs):
                verb_names = v.name.lower().split()
                verb_names += [n[1:] for n in verb_names if n.startswith(":")]  # a waif class verb, as it is called
                for t in hits:
                    if any(fnmatchcase(n, t.lower()) for n in verb_names) if glob else answers_call(v.name, t):
                        hits[t].append(f"{label(db, o.id, names)}:[{idx}] {verb_summary(v)}")
    elif mode == "prop":
        for num, defined in defined_property_names(db):
            for name in defined:
                if not isinstance(name, str):
                    continue
                for t in hits:
                    if fnmatchcase(name.lower(), t.lower()) if glob else name.lower() == t.lower():
                        hits[t].append(f"{label(db, num, names)}.{name}")
    else:
        for num, name in sorted(db.object_names().items()):
            for t in hits:
                needle = t.lower().lstrip("$")
                if needle in name.lower() or needle in names.get(num, "").lower():
                    hits[t].append(label(db, num, names))
    for t, lines in hits.items():
        if count:
            click.echo(f"{len(lines):6d}  {t}")
            continue
        if len(hits) > 1:
            click.echo(f"== {t}")
        for line in lines:
            click.echo(line)


@moodb.command()
@click.argument("ref", nargs=-1, required=True)
@click.option("--code", "code_only", is_flag=True, help="Only verb code naming it; leave out property values holding it.")
@click.option("--skip", "skipped", multiple=True, metavar="NAME",
              help="Leave out references held in properties with this name (case-insensitive, repeatable).")
@WIDTH
@click.pass_context
def refs(ctx: click.Context, ref: tuple[str, ...], code_only: bool, skipped: tuple[str, ...], width: int) -> None:
    """Find references to an object: property values holding it, and verb code naming #N or its $name.

    Several REFs are answered in one run, each under an `== REF` line.
    """
    db, names = _db(ctx)
    targets = [_object(db, one) for one in ref]
    for one, target in zip(ref, targets):
        if len(ref) > 1:
            click.echo(f"== {one}")
        number = re.compile(rf"#{target.id}(?!\d)")
        skip = re.compile(r"\.(" + "|".join(re.escape(s) for s in skipped) + r")(?=[\[.]|$)", re.I) if skipped else None
        for r in find_references(db, target, names):
            if code_only and not r.where.startswith(":["):
                continue
            if skip and skip.match(r.where):
                continue
            click.echo(f"{label(db, r.obj.id, names)}{r.where}" + (f": {_clip(r.text, width, number)}" if r.text else ""))


@moodb.command()
@click.argument("pattern")
@click.option("-i", "--ignore-case", is_flag=True, help="Case-insensitive match.")
@click.option("-F", "--fixed-strings", is_flag=True, help="PATTERN is a literal string.")
@click.option("--in-waif", "waif_class", metavar="REF", help="Only strings stored inside a waif of class REF or a descendant.")
@click.pass_context
def values(ctx: click.Context, pattern: str, ignore_case: bool, fixed_strings: bool, waif_class: str | None) -> None:
    """Search stored string values (Python regex): OBJ.prop[i]<waif #C>.slot = "text".

    Looks in every object's own property values, anonymous objects included,
    through lists and maps and into waifs, naming each waif slot. Inherited
    (clear) slots are not repeated on descendants.
    """
    db, names = _db(ctx)
    try:
        rx = re.compile(re.escape(pattern) if fixed_strings else pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise click.BadParameter(str(e), param_hint="PATTERN")
    wanted = None
    if waif_class is not None:
        cls = _object(db, waif_class)
        wanted = {cls.id, *(d.id for d in descendants(db, cls))}
    for hit in find_strings(db, rx):
        if wanted is not None and not wanted.intersection(hit.waif_classes):
            continue
        click.echo(f"{label(db, hit.obj.id, names)}{hit.where} = {moo_string(hit.value)}")


def _tree(ctx: click.Context, ref: str, field: str, recursive: bool, summary: bool, output_format: str) -> None:
    if output_format == "json" and not summary:
        raise click.UsageError("--format json requires --summary")
    db, names = _db(ctx)
    o = _object(db, ref)
    if summary:
        counts = tree_summary(db, o, field)
        if output_format == "json":
            click.echo(json.dumps(counts))
        else:
            click.echo(f"#{o.id} {field}")
            for key, value in counts.items():
                if key not in ("root", "relation"):
                    click.echo(f"  {key} {value}")
        return
    if recursive:
        found = list(descendants(db, o, field))
    else:
        found = [db.objects[int(c)] for c in getattr(o, field) if int(c) in db.objects]
    for c in found:
        click.echo(label(db, c.id, names))


@moodb.command()
@click.argument("ref")
@click.option("-r", "--recursive", is_flag=True, help="All descendants, depth-first.")
@click.option("--summary", is_flag=True, help="Count the entire reachable graph; does not require -r.")
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text", show_default=True,
              help="Summary output format. JSON requires --summary.")
@click.pass_context
def children(ctx: click.Context, ref: str, recursive: bool, summary: bool, output_format: str) -> None:
    """List an object's children."""
    _tree(ctx, ref, "children", recursive, summary, output_format)


@moodb.command()
@click.argument("ref")
@click.option("-r", "--recursive", is_flag=True, help="Everything inside, depth-first.")
@click.option("--summary", is_flag=True, help="Count the entire reachable graph; does not require -r.")
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text", show_default=True,
              help="Summary output format. JSON requires --summary.")
@click.pass_context
def contents(ctx: click.Context, ref: str, recursive: bool, summary: bool, output_format: str) -> None:
    """List what an object contains."""
    _tree(ctx, ref, "contents", recursive, summary, output_format)


@moodb.command()
@click.pass_context
def players(ctx: click.Context) -> None:
    """List player objects with their flags."""
    db, names = _db(ctx)
    for num in db.players:
        o = db.objects.get(int(num))
        click.echo(f"{label(db, int(num), names)}  {object_flags(o) if o else '(missing)'}")


@moodb.command()
@click.option("-v", "--verbose", is_flag=True, help="Show every stack frame, outermost first.")
@click.pass_context
def tasks(ctx: click.Context, verbose: bool) -> None:
    """List queued (forked), suspended and interrupted tasks saved in the dump.

    Each line shows the task id, when it is due to run (UTC), and its
    innermost frame as #DEFINER:verb.
    """
    db, _ = _db(ctx)
    for t in list_tasks(db):
        top = frame_text(t.frames[-1]) if t.frames else "(no frames)"
        more = f"  (+{len(t.frames) - 1} frames)" if len(t.frames) > 1 and not verbose else ""
        click.echo(f"{t.kind} {t.id}  {format_time(t.when)}  {top}{more}")
        if verbose:
            for depth, a in enumerate(t.frames):
                click.echo(f"    {depth}: {frame_text(a)}")


def _report_options(function):
    """Identical semantic and presentation options for pairs and history edges."""
    from .diff_types import SECTIONS, KINDS
    decorators = [
        click.option('--view', type=click.Choice(['world', 'checkpoint', 'code']), default='world', show_default=True),
        click.option('-o', '--object', 'references', multiple=True),
        click.option('--section', 'sections', multiple=True, type=click.Choice(SECTIONS)),
        click.option('--kind', 'kinds', multiple=True, type=click.Choice(KINDS)),
        click.option('--context', type=click.IntRange(min=0), default=3, show_default=True),
        click.option('--max-events', type=click.IntRange(min=0), default=None),
        click.option('--stop-after', type=click.IntRange(min=1), default=None),
        click.option('--redact', type=click.Path(dir_okay=False), default=None),
        click.option('--redact-strict', is_flag=True),
    ]
    for decorator in reversed(decorators):
        function = decorator(function)
    return function


def _validate_report_options(view, sections, stop_after, context, redact, redact_strict):
    from .diff_types import ReportOptions
    if stop_after is not None and redact:
        raise ValueError('--stop-after cannot be combined with --redact')
    if redact_strict and not redact:
        raise ValueError('--redact-strict requires --redact')
    options = ReportOptions(view=view, sections=sections or None, stop_after=stop_after, context=context)
    if redact:
        from .diff_redaction import RedactionPolicy
        policy = RedactionPolicy.from_file(redact)
    else:
        policy = None
    return options, policy


def _report_failure(ctx, error, *, jsonl=False):
    """Machine failures still end with a footer, including before parsing starts."""
    if jsonl:
        import json
        click.echo(json.dumps({'record': 'header', 'schema_version': 1}))
        click.echo(json.dumps({'record': 'footer', 'status': 'error', 'scan_complete': False,
                               'identity_complete': False, 'details_complete': False, 'payloads_complete': False,
                               'errors': [str(error)]}))
    else:
        click.echo(f'Error: {error}', err=True)
    ctx.exit(2)


_PLAIN_ONLY = {'excluded': '--exclude', 'existing': '--existing', 'identity': '--identity', 'full': '--full'}
_NOT_WITH_PLAIN = {'output_format': '--format', 'sections': '--section', 'kinds': '--kind', 'max_events': '--max-events',
                   'stop_after': '--stop-after', 'redact': '--redact', 'redact_strict': '--redact-strict'}


def _plain_diff(ctx, old_path, output_dir, references, excluded, existing, identity, context, full):
    """`diff --plain`: the verb changes as source text, one file per object and an index."""
    from .diff_sources import select_objects, SourceError
    from .verb_changes import changed_objects, render_index, render_object, write_plain
    if not ctx.obj['path']:
        raise click.UsageError('No NEW dump given: pass --db DUMP or set MOODB')
    exists = click.Path(exists=True, dir_okay=False)
    old_path, new_path = exists.convert(old_path, None, ctx), exists.convert(ctx.obj['path'], None, ctx)
    if output_dir and (Path(output_dir).exists() or Path(output_dir).is_symlink()):
        _report_failure(ctx, 'output directory already exists')
    cache = ctx.obj['cache']
    old, new = (load(path) if cache is None else open_indexed(path, cache) for path in (old_path, new_path))
    try:
        selected, dropped = select_objects(old, new, references), set(select_objects(old, new, excluded) or ())
    except SourceError as error:
        _report_failure(ctx, error)
    found = changed_objects(old, new, selected, identity, dollar_names(old), dollar_names(new))
    left_out = {'--exclude': [entry for entry in found if entry.num in dropped], '--existing': []}
    kept = []
    for entry in found:
        if entry.num in dropped:
            continue
        # As on `added`: an object OLD lacks, or whose number was reused since, is not one OLD already had.
        (left_out['--existing'] if existing and entry.state in ('absent', 'reused') else kept).append(entry)
    scope = [f"Context: {'whole verbs, changed lines marked' if full else f'{context} lines'}",
             'Objects: ' + (' '.join(f'#{num}' for num in selected) if selected is not None else 'all')]
    if dropped:
        scope.append('Excluded: ' + ' '.join(f'#{num}' for num in sorted(dropped)))
    if existing:
        scope.append(f'Only objects OLD already had (--existing, told apart by .{identity})')
    labels = Path(old_path).name, Path(new_path).name
    files = {'index.txt': render_index(kept, left_out, *labels, scope)}
    for entry in kept:
        files[entry.path] = render_object(entry, old, new, *labels, identity, context, full)
    if output_dir:
        write_plain(files, output_dir)
        click.echo(f"{'different' if kept else 'equal'}: plain {output_dir}")
    else:
        click.echo('\n'.join(files.values()), nl=False)
    ctx.exit(1 if kept else 0)


@moodb.command('diff')
@click.argument('old_path', type=click.Path(dir_okay=False))
@click.option('--format', 'output_format', type=click.Choice(['text', 'jsonl']), default='text')
@click.option('--output-dir', type=click.Path(file_okay=False), default=None)
@click.option('--plain', is_flag=True,
              help='Verb changes as plain source text: an index and one file per object in --output-dir, '
                   'or all of it on standard output.')
@click.option('--exclude', 'excluded', multiple=True, metavar='REF', help='With --plain: leave these objects out (repeatable).')
@click.option('--existing', is_flag=True,
              help='With --plain: only objects OLD already had; leaves out new numbers, and numbers recycled and reused since.')
@click.option('--identity', default='creation_time', show_default=True, metavar='PROP',
              help='With --plain: the property that tells a reused number from the same object.')
@click.option('--full', is_flag=True, help='With --plain: show each changed verb whole, its changed lines marked.')
@_report_options
@click.pass_context
def diff(ctx, old_path, output_format, output_dir, plain, excluded, existing, identity, full, view, references, sections,
         kinds, context, max_events, stop_after, redact, redact_strict):
    """Compare OLD_PATH to --db NEW with typed, exact evidence.

    --output-dir writes a bundle and prints its location. Give --format as
    well to also get that report on standard output, from the same
    comparison; the bundle location then goes to standard error.

    --plain writes the verb changes for reading instead: every added or
    removed verb's whole source and a line diff of every changed one, with
    nothing escaped. -o, --exclude and --existing choose the objects;
    --context and --full choose how much of a changed verb is shown.
    """
    from dataclasses import replace
    from .compare import compare_snapshots
    from .diff_sources import freeze_source, select_objects, SourceError
    given = {name for name in ctx.params if ctx.get_parameter_source(name) == click.core.ParameterSource.COMMANDLINE}
    if plain:
        unusable = [option for name, option in _NOT_WITH_PLAIN.items() if name in given]
        if 'view' in given and view != 'code':
            unusable.append(f'--view {view}')
        if unusable:
            raise click.UsageError(f"--plain shows verb changes only and cannot be combined with {', '.join(unusable)}")
        _plain_diff(ctx, old_path, output_dir, references, excluded, existing, identity, context, full)
    needs_plain = [option for name, option in _PLAIN_ONLY.items() if name in given]
    if needs_plain:
        raise click.UsageError(f"{', '.join(needs_plain)} needs --plain")
    jsonl_started = False
    # Standard output carries the formatted report unless a bundle was asked
    # for without naming a format.
    stream = not output_dir or ctx.get_parameter_source('output_format') == click.core.ParameterSource.COMMANDLINE
    try:
        options, policy = _validate_report_options(view, sections, stop_after, context, redact, redact_strict)
        if not ctx.obj['path']:
            raise ValueError('No NEW dump given: pass --db DUMP or set MOODB')
        old, new = freeze_source(old_path), freeze_source(ctx.obj['path'])
        options = replace(options, object_ids=select_objects(old.db, new.db, references))
        comparison = compare_snapshots(old.db, new.db, options)
        comparison.header['sources'] = {'old': old.metadata, 'new': new.metadata}
        if policy:
            policy.bind(old.db, new.db, options)
        from .diff_report import prepare_report, render_text, iter_jsonl, write_bundle
        # Each sink keeps its own default limit: text shows 200 events, JSON
        # Lines and bundles show all.
        stream_limit = 200 if max_events is None and output_format == 'text' else max_events
        report = prepare_report(comparison, kinds=kinds, max_events=max_events if output_dir else stream_limit,
                                redaction=policy, redact_strict=redact_strict)
        if output_dir:
            # The bundle is published before any report line is printed, so a
            # refused destination cannot follow a finished-looking stream.
            write_bundle(report, output_dir)
            click.echo(f'{report.footer["status"]}: bundle {output_dir}', err=stream)
            if stream and stream_limit != max_events:
                # Re-limit the already redacted and identified events; the
                # dumps are not compared again and the policy is not reapplied.
                report = prepare_report(report, kinds=kinds, max_events=stream_limit)
        if stream and output_format == 'jsonl':
            for line in iter_jsonl(report):
                jsonl_started = True
                click.echo(line, nl=False)
        elif stream:
            click.echo(render_text(report, context=context))
    except (SourceError, ValueError, OSError) as error:
        if jsonl_started:
            # iter_jsonl owns its partial-stream error footer. A second header
            # or footer here would corrupt the machine record sequence.
            ctx.exit(2)
        _report_failure(ctx, error, jsonl=output_format == 'jsonl' and stream)
    ctx.exit(report.exit_code)


@moodb.command('diff-read')
@click.argument('bundle', type=click.Path(file_okay=False))
@click.option('--event', default=None)
@click.option('--object', 'object_id', default=None)
@click.option('--category', type=click.Choice(['code','definitions','values','heap','runtime','serialization']), default=None)
@click.option('--offset', type=click.IntRange(min=0), default=0)
@click.option('--limit', type=click.IntRange(min=1), default=50)
@click.option('--side', type=click.Choice(['old','new']), default=None)
@click.option('--range', 'value_range', default=None)
@click.option('--edge', default=None)
@click.pass_context
def diff_read(ctx, bundle, event, object_id, category, offset, limit, side, value_range, edge):
    """Retrieve verified evidence without reopening either source dump."""
    from .diff_types import canonical_json
    try:
        if value_range:
            match = re.fullmatch(r'(\d+):(\d+)', value_range)
            if not match or int(match[1]) > int(match[2]):
                raise ValueError('--range requires ordered START:END bounds')
            value_range = tuple(map(int, match.groups()))
            if not event or not side:
                raise ValueError('--range requires --event and --side')
        import json
        root = Path(bundle)
        manifest = json.loads((root / 'manifest.json').read_bytes())
        if not isinstance(manifest, dict):
            raise ValueError('Invalid bundle manifest structure')
        if manifest.get('report_type') == 'history':
            from .diff_history import read_history
            result = read_history(root, edge=edge, event=event, object_id=object_id, category=category,
                                  offset=offset, limit=limit, side=side, value_range=value_range)
        else:
            if edge:
                raise ValueError('--edge requires a history bundle')
            from .diff_report import read_bundle
            result = read_bundle(root, event=event, object_id=object_id, category=category,
                                 offset=offset, limit=limit, side=side, value_range=value_range)
        click.echo(canonical_json(result).decode('utf-8'))
    except (ValueError, OSError) as error:
        _report_failure(ctx, error)


@moodb.command('history-read')
@click.argument('bundle', type=click.Path(file_okay=False))
@click.option('--object', 'object_id', default=None, help='Permanent numeric slot, e.g. #852.')
@click.option('--target', default=None, help='Permanent numeric reference target; tagged object values only.')
@click.option('--category', type=click.Choice(['code','definitions','values','heap','runtime','serialization']), default=None)
@click.option('--section', 'sections', multiple=True,
              type=click.Choice(['objects','properties','verbs','heap','players','recycled','runtime','serialization']))
@click.option('--kind', 'kinds', multiple=True,
              type=click.Choice(['added','removed','changed','type_changed','order_changed','identity_conflict','identity_unresolved']))
@click.option('--property', 'property_name', default=None, help='Case-insensitive stored property name in either path.')
@click.option('--path-prefix', default=None, help='JSON array of typed path steps, matched on either side.')
@click.option('--direction', type=click.Choice(['forward','backward']), default='forward', show_default=True)
@click.option('--offset', type=click.IntRange(min=0), default=0, show_default=True)
@click.option('--limit', type=click.IntRange(min=1), default=50, show_default=True)
@click.option('--max-scan-events', type=click.IntRange(min=1), default=100000, show_default=True)
@click.option('--max-scan-edges', type=click.IntRange(min=1), default=1000, show_default=True)
@click.option('--format', 'output_format', type=click.Choice(['text','json']), default='text', show_default=True)
@click.pass_context
def history_read(ctx, bundle, object_id, target, category, sections, kinds, property_name, path_prefix,
                 direction, offset, limit, max_scan_events, max_scan_edges, output_format):
    """Trace stored transition evidence and coverage without reopening dumps.

    Text prints locators and coverage. JSON includes the stored typed events.
    Search limits bound interrogation; authentication still checks all files.
    """
    from .diff_types import canonical_json
    from .diff_report import _json
    from .history_read import query_history, render_history_query
    try:
        result = query_history(bundle, object_id=object_id, target=target, category=category,
                               sections=sections, kinds=kinds, property_name=property_name,
                               path_prefix=_json(path_prefix) if path_prefix is not None else None,
                               direction=direction, offset=offset, limit=limit,
                               max_scan_events=max_scan_events, max_scan_edges=max_scan_edges)
        click.echo(canonical_json(result).decode('utf-8') if output_format == 'json' else render_history_query(result))
    except (ValueError, OSError, KeyError, TypeError, RecursionError) as error:
        _report_failure(ctx, error)
    if not result['coverage']['query_scan_complete']:
        ctx.exit(2)


@moodb.command('history')
@click.option('--repo', required=True, type=click.Path(file_okay=False))
@click.option('--from', 'start', default=None)
@click.option('--to', 'end', default=None)
@click.option('--from-date', default=None)
@click.option('--to-date', default=None)
@click.option('--branch', default=None)
@click.option('--mode', type=click.Choice(['endpoints','adjacent']), default='endpoints', show_default=True)
@click.option('--output-dir', required=True, type=click.Path(file_okay=False))
@click.option('--max-edges', type=click.IntRange(min=1), default=100, show_default=True)
@click.option('--keep-going', is_flag=True)
@click.option('--split-prefix', default='db', show_default=True)
@click.option('--legacy-path', default='mongoose.db.new', show_default=True)
@click.option('--source-layout', type=click.Choice(['auto','legacy']), default='auto')
@click.option('--resume-from', type=click.Path(file_okay=False), default=None, help='Reuse verified matching edges from a previous report; output goes to a new directory.')
@_report_options
@click.pass_context
def history(ctx, repo, start, end, from_date, to_date, branch, mode, output_dir, max_edges, keep_going,
            split_prefix, legacy_path, source_layout, resume_from, view, references, sections, kinds,
            context, max_events, stop_after, redact, redact_strict):
    """Compare immutable local Git checkpoints, as endpoints or adjacent edges."""
    from .diff_git import GitRepository
    from .diff_history import build_history
    from .diff_sources import SourceError
    try:
        options, policy = _validate_report_options(view, sections, stop_after, context, redact, redact_strict)
        if bool(start) == bool(from_date) or bool(end) == bool(to_date):
            raise ValueError('Choose exactly one of --from/--from-date and one of --to/--to-date')
        if (from_date or to_date) and not branch:
            raise ValueError('Date selection requires --branch')
        repository = GitRepository(repo)
        selection = {'from_date': from_date, 'to_date': to_date}
        if from_date or to_date:
            tip = repository.resolve(branch)
            selection['branch_tip'] = tip
            start = repository.resolve_date(tip, from_date) if from_date else repository.resolve(start)
            end = repository.resolve_date(tip, to_date) if to_date else repository.resolve(end)
        manifest = build_history(repo, start, end, output_dir, mode=mode, options=options,
                                 references=references, kinds=kinds, max_events=max_events,
                                 max_edges=max_edges, keep_going=keep_going, split_prefix=split_prefix,
                                 legacy_path=legacy_path, source_layout=source_layout, redaction=policy,
                                 redact_strict=redact_strict, resume_from=resume_from, selection=selection)
        click.echo(f'{manifest["status"]}: {manifest["edge_count"]} edges; bundle {output_dir}')
    except (SourceError, ValueError, OSError) as error:
        _report_failure(ctx, error)
    ctx.exit({'equal':0, 'different':1, 'error':2, 'unknown':3}[manifest['status']])


from .graph_cli import register_graph

register_graph(moodb)

from .mail_cli import register_mail

register_mail(moodb, _db)

import os
import re
from itertools import groupby
from pathlib import Path

import click
from .exporter import to_moo_files
from .inspection import (
    BUILTIN_PROPS,
    LookupFailed,
    all_objects,
    all_properties,
    builtin_value,
    default_cache_dir,
    descendants,
    dollar_names,
    find_references,
    find_strings,
    find_verb,
    format_time,
    format_value,
    frame_text,
    grep_verbs,
    label,
    load_cached,
    lookup_property,
    moo_string,
    object_flags,
    own_properties,
    property_perms,
    resolve_object,
    split_ref,
    tasks as list_tasks,
    verb_summary,
    verbcasecmp,
)
from .reader import load
from .split import DEFAULT_MAX_PIECE_BYTES, SplitError, first_difference, join_dir, write_split


@click.command()
@click.argument("dbfile")
@click.argument("dir")
def moodb2flat(dbfile: str, dir: str) -> None:
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


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--db", "db_path", envvar="MOODB", type=click.Path(exists=True, dir_okay=False),
              help="Textdump to read (or set MOODB).")
@click.option("--no-cache", is_flag=True, help="Parse the dump without reading or writing the pickle cache.")
@click.option("--cache-dir", type=click.Path(file_okay=False), default=None,
              help="Pickle cache directory (default: lambdamoo-db under $XDG_CACHE_HOME, %LOCALAPPDATA% or ~/.cache).")
@click.pass_context
def moodb(ctx: click.Context, db_path: str | None, no_cache: bool, cache_dir: str | None) -> None:
    """Read-only inspection of a LambdaMOO/ToastStunt textdump.

    \b
    Object references: #N, N, or $name (the object in #0.name).
    Verbs:             REF:NAME (MOO name matching, inherited) or REF:N (0-based index on REF).
    Properties:        REF.NAME (inherited, clear values followed).

    The parsed dump is cached as a pickle, so only the first query on a new dump is slow.
    """
    cache = None if no_cache else Path(cache_dir) if cache_dir else default_cache_dir()
    ctx.obj = {"path": db_path, "cache": cache}


def _db(ctx: click.Context):
    if "db" not in ctx.obj:
        if ctx.obj["path"] is None:
            raise click.UsageError("no dump given: pass --db DUMP or set MOODB")
        ctx.obj["db"] = load_cached(ctx.obj["path"], ctx.obj["cache"])
        ctx.obj["names"] = dollar_names(ctx.obj["db"])
    return ctx.obj["db"], ctx.obj["names"]


def _object(db, ref: str):
    try:
        ref, rest = split_ref(ref)
        if rest:
            raise LookupFailed(f"expected an object reference, got {ref + rest!r}")
        return resolve_object(db, ref)
    except LookupFailed as e:
        raise click.ClickException(str(e))


def _alias(verb) -> str:
    """The first name of a verb, for one-line output."""
    return (verb.name.split() or [""])[0]


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
@click.pass_context
def props(ctx: click.Context, ref: str, full: bool, builtin: bool) -> None:
    """List every property of an object, own and inherited, with effective values."""
    db, names = _db(ctx)
    o = _object(db, ref)
    limit = None if full else 100
    if builtin:
        click.echo("== builtin")
        for n in BUILTIN_PROPS:
            click.echo(f"  .{n}  = {format_value(builtin_value(o, n), limit)}")
    for definer, hits in groupby(all_properties(db, o), key=lambda h: h.definer.id):
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
@click.pass_context
def code(ctx: click.Context, spec: tuple[str, ...], line_numbers: bool) -> None:
    """Print verb code: `code '$httpd:GET' 852:12`. A bare REF prints every verb on it.

    Verbs are found on the object or its ancestors; the header names the
    object that defines the verb.
    """
    db, names = _db(ctx)
    for s in spec:
        try:
            ref, rest = split_ref(s)
            o = resolve_object(db, ref)
            if rest == "":
                for idx, v in enumerate(o.verbs):
                    _print_program(db, names, o, idx, v, line_numbers)
                continue
            if not rest.startswith(":") or len(rest) < 2:
                raise LookupFailed(f"expected REF or REF:VERB, got {s!r}")
            hit = find_verb(db, o, rest[1:])
        except LookupFailed as e:
            raise click.ClickException(str(e))
        _print_program(db, names, hit.obj, hit.index, hit.verb, line_numbers)


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
@click.argument("pattern")
@click.option("-o", "--obj", "objs", multiple=True, help="Only search these objects (repeatable).")
@click.option("-i", "--ignore-case", is_flag=True)
@click.option("-F", "--fixed-strings", is_flag=True, help="PATTERN is a literal string.")
@click.option("-l", "--verbs-only", is_flag=True, help="Print each matching verb once.")
@click.option("-C", "--context", default=0, metavar="N", help="Show N lines of context around each match.")
@click.pass_context
def grep(ctx: click.Context, pattern: str, objs: tuple[str, ...], ignore_case: bool, fixed_strings: bool, verbs_only: bool, context: int) -> None:
    """Search verb code (Python regex): #OBJ:[index] name:LINE: text."""
    db, names = _db(ctx)
    selected = [_object(db, r) for r in objs] or None
    try:
        rx = re.compile(re.escape(pattern) if fixed_strings else pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise click.BadParameter(str(e), param_hint="PATTERN")
    by_verb = groupby(grep_verbs(db, rx, selected), key=lambda h: (h.obj.id, h.index))
    for n_group, (_, group) in enumerate(by_verb):
        hits = list(group)
        o, idx, v = hits[0].obj, hits[0].index, hits[0].verb
        if verbs_only:
            click.echo(f"{label(db, o.id, names)}:[{idx}] {moo_string(v.name)}")
            continue
        if not context:
            for h in hits:
                click.echo(f"#{o.id}:[{idx}] {_alias(v)}:{h.lineno}: {h.line.strip()}")
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
            click.echo(f"#{o.id}:[{idx}] {_alias(v)}{sep}{n}{sep} {code_lines[n - 1].strip()}")


@moodb.command()
@click.argument("text")
@click.option("--verb", "mode", flag_value="verb", help="Find objects defining a verb that TEXT would call (MOO name matching).")
@click.option("--prop", "mode", flag_value="prop", help="Find objects defining a property named TEXT (case-insensitive).")
@click.pass_context
def find(ctx: click.Context, text: str, mode: str | None) -> None:
    """Find objects whose name or $name contains TEXT (case-insensitive).

    With --verb or --prop, find where a verb or property is defined instead.
    """
    db, names = _db(ctx)
    if mode == "verb":
        for o in all_objects(db):
            for idx, v in enumerate(o.verbs):
                if verbcasecmp(v.name, text):
                    click.echo(f"{label(db, o.id, names)}:[{idx}] {verb_summary(v)}")
        return
    if mode == "prop":
        for o in all_objects(db):
            for p in own_properties(o):
                if isinstance(p.propertyName, str) and p.propertyName.lower() == text.lower():
                    click.echo(f"{label(db, o.id, names)}.{p.propertyName}")
        return
    needle = text.lower().lstrip("$")
    for o in all_objects(db):
        if needle in o.name.lower() or needle in names.get(o.id, "").lower():
            click.echo(label(db, o.id, names))


@moodb.command()
@click.argument("ref")
@click.pass_context
def refs(ctx: click.Context, ref: str) -> None:
    """Find references to an object: property values holding it, and verb code naming #N or its $name."""
    db, names = _db(ctx)
    target = _object(db, ref)
    for r in find_references(db, target, names):
        click.echo(f"{label(db, r.obj.id, names)}{r.where}" + (f": {r.text}" if r.text else ""))


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


def _tree(ctx: click.Context, ref: str, field: str, recursive: bool) -> None:
    db, names = _db(ctx)
    o = _object(db, ref)
    if recursive:
        found = list(descendants(db, o, field))
    else:
        found = [db.objects[int(c)] for c in getattr(o, field) if int(c) in db.objects]
    for c in found:
        click.echo(label(db, c.id, names))


@moodb.command()
@click.argument("ref")
@click.option("-r", "--recursive", is_flag=True, help="All descendants, depth-first.")
@click.pass_context
def children(ctx: click.Context, ref: str, recursive: bool) -> None:
    """List an object's children."""
    _tree(ctx, ref, "children", recursive)


@moodb.command()
@click.argument("ref")
@click.option("-r", "--recursive", is_flag=True, help="Everything inside, depth-first.")
@click.pass_context
def contents(ctx: click.Context, ref: str, recursive: bool) -> None:
    """List what an object contains."""
    _tree(ctx, ref, "contents", recursive)


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

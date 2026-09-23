import os
from pathlib import Path

import click
from .exporter import to_moo_files
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

"""Verb changes between two dumps, as plain text to read from top to bottom.

``moodb diff OLD --plain`` is a thin layer over these functions. Nothing here
is escaped or encoded: verb source is written as the MOO code it is, once.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from pathlib import Path

import attrs

from .database import MooDatabase, MooObject, Verb
from .inspection import label, moo_string, object_continuity, verb_args, verb_perms, verb_signature
from .lazy import LazyDatabase

_TRAILER = re.compile(r'"Last modified .*";')
_RULE = "=" * 72
_RWXD_BITS = 0xF
_ARG_BITS = 0xF0

# How an object of one dump relates to the object with its number in the other.
STATES = {
    "same": "",
    "unproven": "by number only",
    "reused": "reused number",
    "absent": "new object",
    "gone": "removed object",
}


def verb_holders(db: MooDatabase) -> dict[int, MooObject]:
    """Every permanent object that defines a verb, by number."""
    if not isinstance(db, LazyDatabase):
        return {o.id: o for o in db.objects.values() if o.verbs and not o.anon}
    found = {o.id: o for o in db.objects_with_programs()}
    # A verb can be defined and never programmed, and the index lists only
    # programs, so the head of every other record is read for its definitions.
    for oid, (_, _, anon) in db.index.records.items():
        if oid in found or anon:
            continue
        found[oid] = db.objects.loaded.get(oid) or db.read_head(oid)  # type: ignore[attr-defined]
    return {oid: o for oid, o in found.items() if o.verbs and not o.anon}


@attrs.frozen
class VerbChange:
    kind: str  # "added", "changed" or "removed"
    old: Verb | None
    new: Verb | None
    old_index: int | None  # 0-based position on the object, as `code REF:N` takes it
    new_index: int | None

    @property
    def metadata(self) -> list[str]:
        """What changed besides the source, one line each; empty for an added or removed verb."""
        if self.old is None or self.new is None:
            return []
        old, new = self.old, self.new
        lines = []
        if old.name != new.name:
            lines.append(f"names: {moo_string(old.name)} -> {moo_string(new.name)}")
        if verb_args(old) != verb_args(new):
            lines.append(f"args: {verb_args(old)} -> {verb_args(new)}")
        if verb_perms(old) != verb_perms(new):
            lines.append(f"perms: {verb_perms(old) or '-'} -> {verb_perms(new) or '-'}")
        if (old.perms ^ new.perms) & ~(_RWXD_BITS | _ARG_BITS):
            lines.append(f"other permission bits: {old.perms} -> {new.perms}")
        if int(old.owner) != int(new.owner):
            lines.append(f"owner: #{int(old.owner)} -> #{int(new.owner)}")
        if (old.code is None) != (new.code is None):
            lines.append(f"program: {_program_size(old)} -> {_program_size(new)}")
        return lines

    @property
    def source_changed(self) -> bool:
        return self.kind == "changed" and (self.old.code or []) != (self.new.code or [])

    @property
    def only_trailer(self) -> bool:
        """Is the "Last modified" line the only thing that differs?"""
        if not self.source_changed or self.metadata:
            return False
        was, now = self.old.code or [], self.new.code or []
        return bool(was and now) and was[:-1] == now[:-1] and all(_TRAILER.fullmatch(code[-1]) for code in (was, now))


def _program_size(verb: Verb) -> str:
    return "no program" if verb.code is None else f"{len(verb.code)} lines"


def _keyed(obj: MooObject | None) -> dict[tuple, tuple[int, Verb]]:
    """Each verb under its signature and occurrence, the pairing `diff --view code` uses."""
    seen: Counter[tuple] = Counter()
    keyed = {}
    for index, verb in enumerate(obj.verbs if obj is not None else ()):
        base = verb_signature(verb)
        keyed[(*base, seen[base])] = (index, verb)
        seen[base] += 1
    return keyed


def verb_changes(before: MooObject | None, after: MooObject | None) -> list[VerbChange]:
    """The verbs of one object that were added, changed or removed, in NEW's verb order, removed ones last.

    Verbs pair by exact names, argument spec and occurrence among identical
    definitions. Of the verbs that leaves unpaired, an old and a new one with
    the same source are one renamed or respecified verb, and so are two with
    the same names; anything else is a removal and an addition.
    """
    old, new = _keyed(before), _keyed(after)
    pairs = [(old[key], new[key]) for key in old if key in new]
    lost = [entry for key, entry in old.items() if key not in new]
    gained = [entry for key, entry in new.items() if key not in old]
    for same in (lambda a, b: bool(a.code) and a.code == b.code, lambda a, b: a.name == b.name):
        for entry in list(lost):
            match = next((candidate for candidate in gained if same(entry[1], candidate[1])), None)
            if match is not None:
                pairs.append((entry, match))
                lost.remove(entry)
                gained.remove(match)
    changes = [VerbChange("added", None, verb, None, index) for index, verb in gained]
    for (old_index, was), (new_index, now) in pairs:
        if (was.name, int(was.owner), was.perms, was.preps, was.code) != (now.name, int(now.owner), now.perms, now.preps, now.code):
            changes.append(VerbChange("changed", was, now, old_index, new_index))
    changes.sort(key=lambda change: change.new_index)
    return changes + [VerbChange("removed", verb, None, index, None) for index, verb in lost]


@attrs.frozen
class ObjectChanges:
    num: int
    label: str  # from NEW, or from OLD when NEW has no such object
    state: str  # a key of STATES
    changes: list[VerbChange]

    def count(self, kind: str) -> int:
        return sum(1 for change in self.changes if change.kind == kind)

    @property
    def only_trailer(self) -> int:
        return sum(1 for change in self.changes if change.only_trailer)

    @property
    def path(self) -> str:
        return f"objects/permanent-{self.num}.txt"


def changed_objects(old: MooDatabase, new: MooDatabase, object_ids: tuple[int, ...] | None = None,
                    identity: str = "creation_time", old_names: dict[int, str] | None = None,
                    new_names: dict[int, str] | None = None) -> list[ObjectChanges]:
    """Every permanent object whose verbs differ between old and new, in object-number order.

    With object_ids, only those numbers are compared. Anonymous objects are
    not compared. An object only one dump has contributes all its verbs as
    added or removed.
    """
    if object_ids is None:
        before, after = verb_holders(old), verb_holders(new)
    else:
        before, after = ({num: db.objects[num] for num in object_ids if num in db.objects and not db.objects[num].anon}
                         for db in (old, new))
    found = []
    for num in sorted(before.keys() | after.keys()):
        changes = verb_changes(before.get(num), after.get(num))
        if not changes:
            continue
        # A holder lacks the object when it has no verbs there; the dump may still have the object.
        here = [db.objects.get(num) for db in (old, new)]
        was, now = (None if o is None or o.anon else o for o in here)
        if now is None:
            state, text = "gone", label(old, num, old_names)
        elif was is None:
            state, text = "absent", label(new, num, new_names)
        else:
            state, text = object_continuity(old, new, num, identity), label(new, num, new_names)
        found.append(ObjectChanges(num, text, state, changes))
    return found


def _numbered(code: list[str] | None, mark: str = " ") -> list[str]:
    if code is None:
        return ["(no program)"]
    return [f"{mark}{n:5}  {line}" for n, line in enumerate(code, 1)] or ["(empty program)"]


def _range(start: int, stop: int) -> str:
    """A unified-diff range for lines [start, stop), 0-based."""
    length = stop - start
    return f"{start + 1 if length else start},{length}"


def _source_diff(was: list[str], now: list[str], context: int, full: bool) -> list[str]:
    matcher = difflib.SequenceMatcher(None, was, now, autojunk=False)
    lines = []
    if full:
        # The whole new verb, numbered as NEW has it; removed lines sit where they were.
        for tag, i, j, k, l in matcher.get_opcodes():
            if tag == "equal":
                lines += [f" {n + 1:5}  {now[n]}" for n in range(k, l)]
                continue
            lines += [f"-{'':5}  {line}" for line in was[i:j]]
            lines += [f"+{n + 1:5}  {now[n]}" for n in range(k, l)]
        return lines
    for group in matcher.get_grouped_opcodes(context):
        lines.append(f"@@ -{_range(group[0][1], group[-1][2])} +{_range(group[0][3], group[-1][4])} @@")
        for tag, i, j, k, l in group:
            if tag == "equal":
                lines += [" " + line for line in was[i:j]]
                continue
            lines += ["-" + line for line in was[i:j]]
            lines += ["+" + line for line in now[k:l]]
    return lines


def _line_counts(was: list[str], now: list[str]) -> tuple[int, int]:
    codes = [code for code in difflib.SequenceMatcher(None, was, now, autojunk=False).get_opcodes() if code[0] != "equal"]
    return sum(l - k for _, _, _, k, l in codes), sum(j - i for _, i, j, _, _ in codes)


def _owner(db: MooDatabase, verb: Verb) -> str:
    name = db.name_of(int(verb.owner))
    return f"#{int(verb.owner)}" + (f" {moo_string(name)}" if name is not None else "")


def render_verb(num: int, change: VerbChange, old: MooDatabase, new: MooDatabase, context: int = 3, full: bool = False) -> list[str]:
    """One verb: a header naming it, then its source or what changed in it."""
    verb, db = (change.old, old) if change.new is None else (change.new, new)
    index = change.old_index if change.new is None else change.new_index
    lines = [_RULE, f"{change.kind.upper()}  #{num}:{(verb.name.split() or [''])[0]}  [{index}]",
             f"names {moo_string(verb.name)}   args {verb_args(verb)}   perms {verb_perms(verb) or '-'}   owner {_owner(db, verb)}"]
    if change.kind != "changed":
        return lines + [_RULE] + _numbered(verb.code) + [""]
    was, now = change.old.code or [], change.new.code or []
    lines += change.metadata
    if not change.source_changed:
        lines.append(f"source unchanged ({_program_size(change.new)})")
        if now and _TRAILER.fullmatch(now[-1]):
            lines.append(now[-1])
        return lines + [_RULE, ""]
    added, removed = _line_counts(was, now)
    lines.append(f"source: +{added} -{removed} lines" + ("; only the Last modified line changed" if change.only_trailer else ""))
    if not full and was and now and was[-1] == now[-1] and _TRAILER.fullmatch(now[-1]):
        # An unchanged trailer can fall outside every hunk; it still says who last saved the verb.
        lines.append(f"Last modified line unchanged: {now[-1]}")
    return lines + [_RULE] + _source_diff(was, now, context, full) + [""]


def _counts(entries: list[ObjectChanges]) -> str:
    total = {kind: sum(entry.count(kind) for entry in entries) for kind in ("added", "changed", "removed")}
    trailers = sum(entry.only_trailer for entry in entries)
    note = f" ({trailers} only in the Last modified line)" if trailers else ""
    return f"{total['added']} added, {total['changed']} changed{note}, {total['removed']} removed verbs"


_STATE_TEXT = {
    "same": "the same object in both dumps (.{identity} is equal)",
    "unproven": "in both dumps by number only (no .{identity} to compare)",
    "reused": "a reused number: .{identity} differs, so OLD's object of this number was another one",
    "absent": "new: OLD has no object of this number",
    "gone": "removed: NEW has no object of this number",
}


def render_object(entry: ObjectChanges, old: MooDatabase, new: MooDatabase, old_label: str, new_label: str,
                  identity: str = "creation_time", context: int = 3, full: bool = False) -> str:
    lines = [entry.label, f"Verb changes from OLD {old_label} to NEW {new_label}", _counts([entry]),
             "Object: " + _STATE_TEXT[entry.state].format(identity=identity), ""]
    for change in entry.changes:
        lines += render_verb(entry.num, change, old, new, context, full)
    return "\n".join(lines)


def _rows(entries: list[ObjectChanges]) -> list[str]:
    rows = []
    for entry in entries:
        state = f"  [{STATES[entry.state]}]" if STATES[entry.state] else ""
        rows.append(f"{entry.count('added'):6} {entry.count('changed'):8} {entry.count('removed'):8}  {entry.label}{state}")
    return rows


def render_index(kept: list[ObjectChanges], left_out: dict[str, list[ObjectChanges]], old_label: str, new_label: str,
                 scope: list[str]) -> str:
    """The index: what was compared and how, then one line per object, then the objects each filter left out."""
    heading = f"{'added':>6} {'changed':>8} {'removed':>8}  object"
    lines = [f"Verb changes from OLD {old_label} to NEW {new_label}", *scope,
             f"{len(kept)} objects: {_counts(kept)}",
             "Each object is in objects/permanent-N.txt. [N] after a verb is its 0-based position on the object.",
             "", heading, *_rows(kept)]
    for reason, entries in left_out.items():
        if entries:
            lines += ["", f"Left out by {reason}: {len(entries)} objects, {_counts(entries)}", heading, *_rows(entries)]
    return "\n".join(lines) + "\n"


def write_plain(files: dict[str, str], destination: str | Path) -> None:
    """Write each text under destination, which must not exist yet."""
    root = Path(destination)
    root.mkdir(parents=True)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

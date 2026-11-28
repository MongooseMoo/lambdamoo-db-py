"""Declarative static code audit over LambdaMOO/ToastStunt textdumps.

Inspects either verb changes between two dumps (``old_db`` -> ``new_db``) or
verbs directly in a single dump. Universal MOO checks (``raw-refs``,
``catches``, ``docstrings``, ``security``, ``duplicates``) require no
world-specific knowledge; project-specific checks are supplied declaratively
via a JSON rules file (``line_rules``, ``verb_rules``, ``boundary_rules``,
``object_rules``, and classification/author/mutator settings).

Findings are static lexical heuristics that point to code to inspect live,
not proofs of runtime execution, reachability, or authorization.
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from .database import MooDatabase, MooObject
from .enums import ObjectFlags
from .inspection import (
    LookupFailed,
    VERB_EXEC,
    descendants,
    dollar_names,
    label,
    mask_moo_strings,
    object_heads,
    own_properties,
    resolve_object,
    verb_args,
    verb_perms,
)
from .verb_changes import ObjectChanges, VerbChange, changed_objects, verb_holders

BUILTIN_CHECKS: tuple[str, ...] = (
    "raw-refs",
    "catches",
    "docstrings",
    "security",
    "duplicates",
)

_DOC_LINE_RE = re.compile(r'^\s*"(?:\\.|[^"\\])*";\s*$')
_LAST_MOD_RE = re.compile(
    r'^"Last modified\s+[A-Z][a-z]{2}\s+([A-Z][a-z]{2})\s+(\d+)\s+\d+:\d+:\d+\s+(\d{4})\s+\S+\s+by\s+(.+?)\s+\(#(\d+)@[^)]+\)\.";$'
)
_ANY_TRAILER_RE = re.compile(r'^"Last modified .*";$')
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

_OBJ_REF_RE = re.compile(r"(?<![A-Za-z0-9_])#(\d+)\b")
_DOLLAR_REF_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_EXCEPT_RE = re.compile(r"\bexcept\b")
_EXCEPT_ANY_RE = re.compile(r"\bexcept\b[^;]*\bANY\b")
_BACKTICK_ANY_RE = re.compile(r"`[^'`]*!\s*ANY\b")

# Universal LambdaMOO/ToastStunt state-mutating builtins and permission primitives.
_DEFAULT_MUTATOR_NAMES: tuple[str, ...] = (
    "move", "recycle", "create", "set_verb_code", "add_verb", "delete_verb",
    "set_verb_info", "set_verb_args", "add_property", "delete_property",
    "set_property_info", "chparent", "chparents", "set_player_flag",
)
_DEFAULT_GUARD_PATTERNS: tuple[str, ...] = (
    r"\bcaller_perms\s*\(",
    r"\bset_task_perms\s*\(",
    r"\.wizard\b",
    r"\bcaller\s*(?:==|!=|in\b)",
)
_DEFAULT_TEST_PREFIXES: tuple[str, ...] = ("test_",)


class AuditRuleError(ValueError):
    """Raised when a declarative rules specification is malformed."""


def load_rules(path: str | Path) -> dict[str, Any]:
    """Load and validate a declarative audit rules JSON file."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return validate_rules(raw)


def validate_rules(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a declarative audit rules mapping (schema version 1)."""
    if not isinstance(raw, Mapping):
        raise AuditRuleError("rules document must be a JSON object")
    version = raw.get("version", 1)
    if version != 1:
        raise AuditRuleError(f"unsupported rules version {version!r}; expected 1")

    allowed_top = {
        "version", "name", "description", "min_doc_lines", "min_duplicate_lines",
        "test_verb_prefixes", "test_object_substrings", "author_groups",
        "wizard_mutators", "wizard_guards",
        "line_rules", "verb_rules", "boundary_rules", "object_rules",
    }
    extra = set(raw.keys()) - allowed_top
    if extra:
        raise AuditRuleError(f"unknown top-level rules key(s): {', '.join(sorted(extra))}")

    rules: dict[str, Any] = dict(raw)
    seen_ids: set[str] = set()

    for section in ("line_rules", "verb_rules", "boundary_rules", "object_rules"):
        items = rules.get(section, [])
        if not isinstance(items, list):
            raise AuditRuleError(f"{section} must be a list")
        for idx, item in enumerate(items):
            if not isinstance(item, Mapping):
                raise AuditRuleError(f"{section}[{idx}] must be an object")
            rid = item.get("id")
            if not isinstance(rid, str) or not rid.strip():
                raise AuditRuleError(f"{section}[{idx}] requires a non-empty string 'id'")
            if rid in seen_ids or rid in BUILTIN_CHECKS:
                raise AuditRuleError(f"duplicate or reserved rule id: {rid!r}")
            seen_ids.add(rid)
            _validate_section_rule(section, item)

    for idx, ag in enumerate(rules.get("author_groups", [])):
        if not isinstance(ag, Mapping) or not isinstance(ag.get("pattern"), str) or not isinstance(ag.get("label"), str):
            raise AuditRuleError(f"author_groups[{idx}] must have string 'pattern' and 'label'")
        _compile_rx(ag["pattern"], f"author_groups[{idx}].pattern", re.IGNORECASE)

    for key in ("wizard_mutators", "wizard_guards", "test_verb_prefixes", "test_object_substrings"):
        val = rules.get(key, [])
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            raise AuditRuleError(f"{key} must be a list of strings")

    return rules


def _compile_rx(pattern: str, context: str, flags: int = 0) -> re.Pattern[str]:
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise AuditRuleError(f"invalid regex in {context}: {exc}") from exc


def _validate_section_rule(section: str, item: Mapping[str, Any]) -> None:
    rid = item["id"]
    if section == "line_rules":
        if not isinstance(item.get("match"), str):
            raise AuditRuleError(f"line_rule {rid!r} requires string 'match'")
        flags = re.IGNORECASE if item.get("ignore_case") else 0
        _compile_rx(item["match"], f"line_rule {rid!r}.match", flags)
        if "exclude_line" in item:
            _compile_rx(item["exclude_line"], f"line_rule {rid!r}.exclude_line", flags)
        if item.get("scope", "prod") not in ("prod", "test", "all"):
            raise AuditRuleError(f"line_rule {rid!r}: scope must be prod, test, or all")
        if item.get("mode", "finding") not in ("finding", "metric"):
            raise AuditRuleError(f"line_rule {rid!r}: mode must be finding or metric")
    elif section == "verb_rules":
        if item.get("scope", "prod") not in ("prod", "test", "all"):
            raise AuditRuleError(f"verb_rule {rid!r}: scope must be prod, test, or all")
        if item.get("change", "added") not in ("added", "changed", "all"):
            raise AuditRuleError(f"verb_rule {rid!r}: change must be added, changed, or all")
        for key in ("name_match", "require", "without"):
            if key in item:
                flags = re.IGNORECASE if item.get("ignore_case") else 0
                _compile_rx(item[key], f"verb_rule {rid!r}.{key}", flags)
    elif section == "boundary_rules":
        if not isinstance(item.get("objects"), list) or not item["objects"]:
            raise AuditRuleError(f"boundary_rule {rid!r} requires non-empty list 'objects'")
        if not isinstance(item.get("allowed_refs", []), list):
            raise AuditRuleError(f"boundary_rule {rid!r}: allowed_refs must be a list")
    elif section == "object_rules":
        if item.get("states") is not None and not isinstance(item["states"], list):
            raise AuditRuleError(f"object_rule {rid!r}: states must be a list")


def parse_last_modified(code: Sequence[str]) -> tuple[str | None, int | None, dt.date | None]:
    """Return ``(author_name, author_id, utc_date)`` from a verb's trailing editor stamp, or Nones."""
    for line in reversed(code):
        s = line.strip()
        if not s:
            continue
        m = _LAST_MOD_RE.match(s)
        if m:
            month = _MONTHS.get(m.group(1))
            parsed_date = dt.date(int(m.group(3)), month, int(m.group(2))) if month else None
            return m.group(4).strip(), int(m.group(5)), parsed_date
        break
    return None, None, None


def _group_author(
    name: str,
    num: int,
    author_groups: Sequence[tuple[re.Pattern[str], str]],
    *,
    from_owner: bool = False,
) -> str:
    n = name.strip()
    base = f"{n} (#{num})"
    for rx, grp_label in author_groups:
        if rx.search(n):
            base = grp_label
            break
    return f"{base} [owner]" if from_owner else base


def _is_test_verb(
    verb_names: str,
    obj_name: str,
    prefixes: Sequence[str],
    obj_substrings: Sequence[str],
) -> bool:
    first = (verb_names.split() or [""])[0].lower()
    if any(first.startswith(p) for p in prefixes):
        return True
    lowered = obj_name.lower()
    return any(sub in lowered for sub in obj_substrings)


def count_leading_doc_lines(code: Sequence[str]) -> int:
    count = 0
    for line in code:
        stripped = line.strip()
        if not stripped:
            continue
        if _ANY_TRAILER_RE.match(stripped):
            break
        if _DOC_LINE_RE.match(line):
            count += 1
        else:
            break
    return count


def _added_line_numbers(old_code: Sequence[str] | None, new_code: Sequence[str]) -> list[int]:
    """1-based line numbers in ``new_code`` inserted or replaced relative to ``old_code``."""
    if old_code is None:
        return list(range(1, len(new_code) + 1))
    was = [line.strip() for line in old_code]
    now = [line.strip() for line in new_code]
    matcher = difflib.SequenceMatcher(None, was, now, autojunk=False)
    nums: list[int] = []
    for tag, _, _, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            nums.extend(range(j1 + 1, j2 + 1))
    return nums


def _build_single_entries(
    db: MooDatabase,
    object_ids: tuple[int, ...] | None,
    names: dict[int, str],
) -> list[ObjectChanges]:
    if object_ids is None:
        holders = verb_holders(db)
    else:
        holders = {num: db.objects[num] for num in object_ids if num in db.objects and not db.objects[num].anon}
    entries: list[ObjectChanges] = []
    for num in sorted(holders):
        obj = holders[num]
        changes = [VerbChange("added", None, v, None, idx) for idx, v in enumerate(obj.verbs)]
        if changes:
            entries.append(ObjectChanges(num, label(db, num, names), "same", changes))
    return entries


def _expand_ids(
    db: MooDatabase,
    object_ids: tuple[int, ...] | None,
    include_descendants: bool,
) -> tuple[int, ...] | None:
    if object_ids is None:
        return None
    if not include_descendants:
        return tuple(sorted(set(object_ids)))
    expanded: set[int] = set(object_ids)
    for oid in object_ids:
        if oid in db.objects:
            for d in descendants(db, db.objects[oid]):
                expanded.add(d.id)
    return tuple(sorted(expanded))


def _resolve_boundary_objects(db: MooDatabase, refs: Sequence[str]) -> set[int]:
    resolved: set[int] = set()
    for ref in refs:
        try:
            resolved.add(resolve_object(db, ref).id)
        except LookupFailed:
            continue
    return resolved


def audit_database(
    new_db: MooDatabase,
    old_db: MooDatabase | None = None,
    *,
    rules: Mapping[str, Any] | None = None,
    object_ids: tuple[int, ...] | None = None,
    include_descendants: bool = False,
    existing_only: bool = False,
    identity: str = "creation_time",
    author: str | None = None,
    since: dt.date | None = None,
    checks: Sequence[str] | None = None,
    doc_lines: int | None = None,
) -> dict[str, Any]:
    """Run universal MOO checks plus any declarative rules over a dump or diff."""
    cfg = validate_rules(rules) if rules is not None else {"version": 1}

    min_doc_lines = doc_lines if doc_lines is not None else int(cfg.get("min_doc_lines", 3))
    min_dup_lines = int(cfg.get("min_duplicate_lines", 5))
    test_prefixes = tuple(p.lower() for p in cfg.get("test_verb_prefixes", _DEFAULT_TEST_PREFIXES))
    test_substrings = tuple(s.lower() for s in cfg.get("test_object_substrings", ()))
    author_groups = [
        (re.compile(ag["pattern"], re.IGNORECASE), ag["label"])
        for ag in cfg.get("author_groups", [])
    ]

    mutator_names = list(_DEFAULT_MUTATOR_NAMES) + list(cfg.get("wizard_mutators", ()))
    mutator_re = re.compile(r"\b(" + "|".join(re.escape(m) for m in mutator_names) + r")\s*\(")
    guard_patterns = list(_DEFAULT_GUARD_PATTERNS) + list(cfg.get("wizard_guards", ()))
    guard_re = re.compile("|".join(f"(?:{p})" for p in guard_patterns))

    # Build available check/category filter set
    all_decl_rules = (
        list(cfg.get("line_rules", []))
        + list(cfg.get("verb_rules", []))
        + list(cfg.get("boundary_rules", []))
        + list(cfg.get("object_rules", []))
    )
    decl_ids = {r["id"] for r in all_decl_rules}
    decl_categories = {r.get("category", "custom") for r in all_decl_rules}
    valid_selectors = set(BUILTIN_CHECKS) | decl_ids | decl_categories

    if checks:
        requested = set(checks)
        unknown = requested - valid_selectors
        if unknown:
            raise AuditRuleError(f"unknown check/rule/category: {', '.join(sorted(unknown))}")
        selected_builtins = requested & set(BUILTIN_CHECKS)
    else:
        requested = None
        selected_builtins = set(BUILTIN_CHECKS)

    def _rule_enabled(rule: Mapping[str, Any]) -> bool:
        if requested is None:
            return True
        return rule["id"] in requested or rule.get("category", "custom") in requested

    # Pre-compile declarative rules
    line_rules = []
    for r in cfg.get("line_rules", []):
        if not _rule_enabled(r):
            continue
        flags = re.IGNORECASE if r.get("ignore_case") else 0
        line_rules.append({
            "spec": r,
            "match_re": re.compile(r["match"], flags),
            "exclude_line_re": re.compile(r["exclude_line"], flags) if r.get("exclude_line") else None,
            "exclude_verbs": {v.lower() for v in r.get("exclude_verbs", ())},
            "scope": r.get("scope", "prod"),
            "code_only": bool(r.get("code_only", True)),
            "skip_docstrings": bool(r.get("skip_docstrings", True)),
            "mode": r.get("mode", "finding"),
            "count_by": r.get("count_by", "line"),
            "items": [],
            "distinct_verbs": set(),
            "line_count": 0,
        })

    verb_rules = []
    for r in cfg.get("verb_rules", []):
        if not _rule_enabled(r):
            continue
        flags = re.IGNORECASE if r.get("ignore_case") else 0
        verb_rules.append({
            "spec": r,
            "scope": r.get("scope", "prod"),
            "change": r.get("change", "added"),
            "args_is": r.get("args_is"),
            "args_not": r.get("args_not"),
            "name_re": re.compile(r["name_match"], flags) if r.get("name_match") else None,
            "require_re": re.compile(r["require"], flags) if r.get("require") else None,
            "without_re": re.compile(r["without"], flags) if r.get("without") else None,
            "code_only": bool(r.get("code_only", False)),
            "items": [],
        })

    boundary_rules = []
    for r in cfg.get("boundary_rules", []):
        if not _rule_enabled(r):
            continue
        boundary_rules.append({
            "spec": r,
            "object_ids": _resolve_boundary_objects(new_db, r["objects"]),
            "allowed_refs": {s.lstrip("$").lower() for s in r.get("allowed_refs", ())},
            "scope": r.get("scope", "prod"),
            "items": [],
        })

    object_rules = []
    for r in cfg.get("object_rules", []):
        if not _rule_enabled(r):
            continue
        object_rules.append({
            "spec": r,
            "items": [],
        })

    new_names = dollar_names(new_db)
    old_names = dollar_names(old_db) if old_db is not None else None
    expanded_ids = _expand_ids(new_db, object_ids, include_descendants)
    mode = "diff" if old_db is not None else "snapshot"

    if old_db is not None:
        entries = changed_objects(
            old_db, new_db, object_ids=expanded_ids, identity=identity,
            old_names=old_names, new_names=new_names,
        )
    else:
        entries = _build_single_entries(new_db, expanded_ids, new_names)

    wizard_ids = {o.id for o in object_heads(new_db) if o.flags & ObjectFlags.WIZARD}
    author_filter = author.lower() if author else None

    overview: Counter[str] = Counter()
    obj_states: Counter[str] = Counter()
    author_totals: Counter[str] = Counter()
    author_prod: Counter[str] = Counter()
    author_test: Counter[str] = Counter()
    object_verb_counts: Counter[tuple[int, str, str]] = Counter()

    # Built-in accumulators
    raw_refs: list[dict[str, Any]] = []
    catches_any_prod: list[dict[str, Any]] = []
    catches_spec_prod: list[dict[str, Any]] = []
    catches_in_test: list[dict[str, Any]] = []
    catches_any_by_obj: Counter[str] = Counter()
    catches_any_by_author: Counter[str] = Counter()

    doc_counts: dict[str, Counter[str]] = {
        "added_prod": Counter(),
        "changed_prod": Counter(),
        "added_test": Counter(),
        "changed_test": Counter(),
    }
    doc_prod_by_author: dict[str, Counter[str]] = defaultdict(Counter)
    doc_failures: list[dict[str, Any]] = []

    security_candidates: list[dict[str, Any]] = []
    dup_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for entry in entries:
        if existing_only and entry.state in ("absent", "reused"):
            overview["objects_left_out_existing"] += 1
            continue
        if entry.state == "gone":
            overview["objects_removed"] += 1
            overview["verbs_removed"] += sum(1 for c in entry.changes if c.kind == "removed")
            continue

        obj = new_db.objects.get(entry.num)
        if obj is None or obj.anon:
            continue

        obj_ref = new_names.get(entry.num, f"#{entry.num}")
        obj_name = obj.name or ""
        obj_matched_any = False
        added_on_obj = 0

        for change in entry.changes:
            if change.kind == "removed":
                overview["verbs_removed"] += 1
                continue
            if change.only_trailer:
                overview["verbs_changed_trailer_only"] += 1
                continue

            verb = change.new
            assert verb is not None
            code = verb.code or []
            lm_name, lm_id, lm_date = parse_last_modified(code)
            if since is not None and (lm_date is None or lm_date < since):
                continue

            owner_id = int(verb.owner)
            owner_name = new_db.name_of(owner_id) or f"#{owner_id}"
            if lm_name is not None and lm_id is not None:
                author_group = _group_author(lm_name, lm_id, author_groups)
                author_raw = f"{lm_name} (#{lm_id})"
            elif change.kind == "added" and owner_id >= 0:
                author_group = _group_author(owner_name, owner_id, author_groups, from_owner=True)
                author_raw = f"{owner_name} (#{owner_id})"
            else:
                author_group = "unknown"
                author_raw = "unknown"

            if author_filter is not None:
                hay = f"{author_group} {author_raw} {owner_name} #{owner_id}".lower()
                if author_filter not in hay:
                    continue

            obj_matched_any = True
            if change.kind == "added":
                overview["verbs_added"] += 1
                added_on_obj += 1
            else:
                overview["verbs_changed"] += 1

            vlabel = (verb.name.split() or [""])[0]
            vindex = change.new_index
            vargs = verb_args(verb)
            vperms = verb_perms(verb)
            test_v = _is_test_verb(verb.name, obj_name, test_prefixes, test_substrings)
            verb_scope = "test" if test_v else "prod"

            author_totals[author_group] += 1
            if test_v:
                author_test[author_group] += 1
                overview[f"verbs_{change.kind}_test"] += 1
            else:
                author_prod[author_group] += 1
                overview[f"verbs_{change.kind}_prod"] += 1
            object_verb_counts[(entry.num, obj_ref, obj_name)] += 1

            masked_full = mask_moo_strings(code)
            added_nums = _added_line_numbers(change.old.code if change.old is not None else None, code)
            dl = count_leading_doc_lines(code)

            # Built-in: docstrings (full new verb body on both added and revised verbs)
            if "docstrings" in selected_builtins and code:
                bucket = f">={min_doc_lines}" if dl >= min_doc_lines else (f"1-{min_doc_lines - 1}" if dl >= 1 else "0")
                doc_counts[f"{change.kind}_{verb_scope}"][bucket] += 1
                if not test_v:
                    doc_prod_by_author[author_group][bucket] += 1
                    if dl < min_doc_lines:
                        doc_failures.append({
                            "object": entry.num, "ref": obj_ref, "name": obj_name,
                            "verb": vlabel, "index": vindex, "change": change.kind,
                            "doc_lines": dl, "author": author_raw,
                        })

            # Built-in: duplicates (identical added prod bodies)
            if "duplicates" in selected_builtins and not test_v and change.kind == "added":
                sig_lines = [
                    ln.strip() for ln in code
                    if ln.strip() and not _DOC_LINE_RE.match(ln) and not _ANY_TRAILER_RE.match(ln.strip())
                ]
                if len(sig_lines) >= min_dup_lines:
                    dup_groups["\n".join(sig_lines)].append({
                        "object": entry.num, "ref": obj_ref, "name": obj_name,
                        "verb": vlabel, "index": vindex, "author": author_raw,
                    })

            # Built-in: security (wizard-owned executable 'this none this' mutators without obvious guard)
            if (
                "security" in selected_builtins
                and not test_v
                and owner_id in wizard_ids
                and bool(verb.perms & VERB_EXEC)
                and vargs == "this none this"
            ):
                added_masked = "\n".join(masked_full[n - 1] for n in added_nums)
                muts = sorted(set(mutator_re.findall(added_masked)))
                if muts and not guard_re.search("\n".join(masked_full)):
                    security_candidates.append({
                        "object": entry.num, "ref": obj_ref, "name": obj_name,
                        "verb": vlabel, "index": vindex, "change": change.kind,
                        "owner": f"{owner_name} (#{owner_id})", "perms": vperms,
                        "author": author_raw, "mutators": muts,
                    })

            # Declarative whole-verb rules
            if verb_rules:
                joined_raw = "\n".join(ln for ln in code if not _ANY_TRAILER_RE.match(ln.strip()))
                joined_masked = "\n".join(masked_full)
                clean_vname = " ".join(t.replace("*", "") for t in verb.name.split())
                for vr in verb_rules:
                    if vr["scope"] != "all" and vr["scope"] != verb_scope:
                        continue
                    if vr["change"] != "all" and vr["change"] != change.kind:
                        continue
                    if vr["args_is"] is not None and vargs != vr["args_is"]:
                        continue
                    if vr["args_not"] is not None and vargs == vr["args_not"]:
                        continue
                    if vr["name_re"] is not None and not vr["name_re"].search(clean_vname):
                        continue
                    haystack = joined_masked if vr["code_only"] else joined_raw
                    if vr["require_re"] is not None and not vr["require_re"].search(haystack):
                        continue
                    if vr["without_re"] is not None and vr["without_re"].search(haystack):
                        continue
                    vr["items"].append({
                        "object": entry.num, "ref": obj_ref, "name": obj_name,
                        "verb": vlabel, "index": vindex, "args": vargs, "perms": vperms,
                        "change": change.kind, "doc_lines": dl, "author": author_raw,
                    })

            # Line-level built-ins & declarative line/boundary rules over newly added/replaced lines
            for lineno in added_nums:
                raw_ln = code[lineno - 1]
                stripped_raw = raw_ln.strip()
                if not stripped_raw or _ANY_TRAILER_RE.match(stripped_raw):
                    continue
                is_doc = bool(_DOC_LINE_RE.match(raw_ln))
                masked_ln = masked_full[lineno - 1]
                loc = {
                    "object": entry.num, "ref": obj_ref, "name": obj_name,
                    "verb": vlabel, "index": vindex, "lineno": lineno,
                    "author": author_raw, "line": stripped_raw,
                }

                # Declarative line rules
                vlabel_lower = vlabel.lower()
                for lr in line_rules:
                    if lr["scope"] != "all" and lr["scope"] != verb_scope:
                        continue
                    if lr["skip_docstrings"] and is_doc:
                        continue
                    if vlabel_lower in lr["exclude_verbs"]:
                        continue
                    target_text = masked_ln if lr["code_only"] else raw_ln
                    if not target_text.strip():
                        continue
                    if lr["exclude_line_re"] is not None and lr["exclude_line_re"].search(target_text):
                        continue
                    if lr["match_re"].search(target_text):
                        lr["line_count"] += 1
                        lr["distinct_verbs"].add((entry.num, vlabel))
                        if lr["mode"] == "finding":
                            lr["items"].append(loc)

                if is_doc or not masked_ln.strip():
                    continue

                # Declarative boundary rules
                for br in boundary_rules:
                    if entry.num not in br["object_ids"]:
                        continue
                    if br["scope"] != "all" and br["scope"] != verb_scope:
                        continue
                    for dref in _DOLLAR_REF_RE.findall(masked_ln):
                        if dref.lower() not in br["allowed_refs"]:
                            br["items"].append({**loc, "dollar_ref": f"${dref}"})

                # Built-in: raw-refs & catches
                if not test_v:
                    if "raw-refs" in selected_builtins:
                        nums = [int(x) for x in _OBJ_REF_RE.findall(masked_ln) if int(x) != 0]
                        if nums:
                            raw_refs.append({**loc, "targets": nums})
                    if "catches" in selected_builtins:
                        if _EXCEPT_RE.search(masked_ln):
                            if _EXCEPT_ANY_RE.search(masked_ln):
                                catches_any_prod.append({**loc, "kind": "except ANY"})
                                catches_any_by_obj[f"{obj_ref} (#{entry.num})"] += 1
                                catches_any_by_author[author_group] += 1
                            else:
                                catches_spec_prod.append({**loc, "kind": "except specific"})
                        if "`" in masked_ln and "!" in masked_ln:
                            if _BACKTICK_ANY_RE.search(masked_ln):
                                catches_any_prod.append({**loc, "kind": "backtick ANY"})
                                catches_any_by_obj[f"{obj_ref} (#{entry.num})"] += 1
                                catches_any_by_author[author_group] += 1
                            else:
                                catches_spec_prod.append({**loc, "kind": "backtick specific"})
                else:
                    if "catches" in selected_builtins:
                        if _EXCEPT_RE.search(masked_ln) or ("`" in masked_ln and "!" in masked_ln):
                            catches_in_test.append(loc)

        if obj_matched_any:
            overview["objects_matched"] += 1
            obj_states[entry.state] += 1

            # Declarative object rules
            if object_rules:
                _evaluate_object_rules(
                    object_rules, obj, entry.state, mode, expanded_ids is not None,
                    added_on_obj, new_db, new_names,
                )

    duplicates = [
        {"count": len(locs), "first_line": sig.splitlines()[0], "locations": locs}
        for sig, locs in dup_groups.items()
        if len({loc["object"] for loc in locs}) >= 2
    ]
    duplicates.sort(key=lambda r: (-r["count"], r["first_line"]))

    builtins_out: dict[str, Any] = {}
    if "raw-refs" in selected_builtins:
        builtins_out["raw-refs"] = {
            "count": len(raw_refs),
            "distinct_verbs": len({(r["object"], r["verb"]) for r in raw_refs}),
            "items": raw_refs,
        }
    if "catches" in selected_builtins:
        builtins_out["catches"] = {
            "prod_any_count": len(catches_any_prod),
            "prod_specific_count": len(catches_spec_prod),
            "test_catch_count": len(catches_in_test),
            "prod_any_by_object": catches_any_by_obj.most_common(25),
            "prod_any_by_author": catches_any_by_author.most_common(15),
            "prod_any": catches_any_prod,
            "prod_specific": catches_spec_prod,
            "test_catches": catches_in_test,
        }
    if "docstrings" in selected_builtins:
        ok_key = f">={min_doc_lines}"
        short_key = f"1-{min_doc_lines - 1}"
        by_author = []
        for a, ctr in sorted(doc_prod_by_author.items(), key=lambda kv: -sum(kv[1].values())):
            tot = sum(ctr.values())
            ok = ctr[ok_key]
            by_author.append({
                "author": a, "passing": ok, "total": tot,
                "pct": round(100.0 * ok / tot, 1) if tot else 0.0,
                "short": ctr[short_key], "zero": ctr["0"],
            })
        builtins_out["docstrings"] = {
            "min_lines": min_doc_lines,
            "counts": {k: dict(v) for k, v in doc_counts.items()},
            "prod_by_author": by_author,
            "prod_failures_count": len(doc_failures),
            "prod_failures": doc_failures,
        }
    if "security" in selected_builtins:
        builtins_out["security"] = {
            "count": len(security_candidates),
            "items": security_candidates,
        }
    if "duplicates" in selected_builtins:
        builtins_out["duplicates"] = {
            "min_lines": min_dup_lines,
            "group_count": len(duplicates),
            "groups": duplicates,
        }

    custom_out: list[dict[str, Any]] = []
    for lr in line_rules:
        spec = lr["spec"]
        custom_out.append({
            "id": spec["id"],
            "kind": "line_rule",
            "category": spec.get("category", "custom"),
            "mode": lr["mode"],
            "description": spec.get("description", ""),
            "count": len(lr["distinct_verbs"]) if lr["count_by"] == "verb" else lr["line_count"],
            "line_count": lr["line_count"],
            "distinct_verbs": len(lr["distinct_verbs"]),
            "items": lr["items"],
        })
    for vr in verb_rules:
        spec = vr["spec"]
        custom_out.append({
            "id": spec["id"],
            "kind": "verb_rule",
            "category": spec.get("category", "custom"),
            "mode": spec.get("mode", "finding"),
            "description": spec.get("description", ""),
            "count": len(vr["items"]),
            "items": vr["items"],
        })
    for br in boundary_rules:
        spec = br["spec"]
        custom_out.append({
            "id": spec["id"],
            "kind": "boundary_rule",
            "category": spec.get("category", "custom"),
            "mode": "finding",
            "description": spec.get("description", ""),
            "checked_objects": len(br["object_ids"]),
            "count": len(br["items"]),
            "items": br["items"],
        })
    for orule in object_rules:
        spec = orule["spec"]
        custom_out.append({
            "id": spec["id"],
            "kind": "object_rule",
            "category": spec.get("category", "custom"),
            "mode": "finding",
            "description": spec.get("description", ""),
            "count": len(orule["items"]),
            "items": orule["items"],
        })

    return {
        "mode": mode,
        "rules_name": cfg.get("name"),
        "overview": {
            "objects_matched": overview["objects_matched"],
            "objects_removed": overview["objects_removed"],
            "objects_left_out_existing": overview["objects_left_out_existing"],
            "object_states": dict(obj_states),
            "verbs_added": overview["verbs_added"],
            "verbs_changed": overview["verbs_changed"],
            "verbs_changed_trailer_only": overview["verbs_changed_trailer_only"],
            "verbs_removed": overview["verbs_removed"],
            "verbs_added_prod": overview["verbs_added_prod"],
            "verbs_changed_prod": overview["verbs_changed_prod"],
            "verbs_added_test": overview["verbs_added_test"],
            "verbs_changed_test": overview["verbs_changed_test"],
            "authors": [
                {"author": a, "total": c, "prod": author_prod[a], "test": author_test[a]}
                for a, c in author_totals.most_common()
            ],
            "top_objects": [
                {"object": num, "ref": ref, "name": name, "verbs": c}
                for (num, ref, name), c in object_verb_counts.most_common(30)
            ],
        },
        "builtins": builtins_out,
        "rules": custom_out,
    }


def _evaluate_object_rules(
    object_rules: Sequence[dict[str, Any]],
    obj: MooObject,
    state: str,
    mode: str,
    explicit_objects: bool,
    added_verbs: int,
    db: MooDatabase,
    names: dict[int, str],
) -> None:
    lower_name = (obj.name or "").lower()
    is_fertile = bool(obj.flags & ObjectFlags.FERTILE)
    own_prop_names: set[str] | None = None

    for orule in object_rules:
        spec = orule["spec"]
        if mode == "diff":
            allowed_states = spec.get("states", ["absent", "reused"])
            if state not in allowed_states:
                continue
            if spec.get("require_added_verbs", True) and added_verbs <= 0:
                continue
        elif not explicit_objects and not spec.get("run_on_full_snapshot", False):
            continue

        if any(sub.lower() in lower_name for sub in spec.get("exclude_name_contains", ())):
            continue
        if any(lower_name.startswith(pre.lower()) for pre in spec.get("exclude_name_prefixes", ())):
            continue

        match_any = False
        has_selector = False
        if spec.get("when_name_contains"):
            has_selector = True
            if any(sub.lower() in lower_name for sub in spec["when_name_contains"]):
                match_any = True
        if spec.get("or_fertile"):
            has_selector = True
            if is_fertile:
                match_any = True
        if spec.get("or_has_children"):
            has_selector = True
            if obj.children:
                match_any = True
        if has_selector and not match_any:
            continue

        issues: list[str] = []
        if spec.get("require_fertile") and not is_fertile:
            issues.append("not_fertile")
        if spec.get("require_dollar_name") and obj.id not in names:
            issues.append("no_dollar_name")
        forbidden_props = spec.get("forbid_own_properties", ())
        if forbidden_props:
            if own_prop_names is None:
                own_prop_names = {p.propertyName.lower() for p in own_properties(obj) if p.propertyName}
            for prop in forbidden_props:
                if prop.lower() in own_prop_names:
                    issues.append(f"has_{prop.lower()}_prop")

        if issues:
            orule["items"].append({
                "object": obj.id,
                "ref": names.get(obj.id, f"#{obj.id}"),
                "name": obj.name or "",
                "state": state,
                "fertile": is_fertile,
                "owner": db.name_of(int(obj.owner)) or f"#{int(obj.owner)}",
                "verbs_added": added_verbs,
                "children": len(obj.children),
                "issues": issues,
            })


def format_audit_text(report: dict[str, Any], *, limit: int = 20) -> str:
    """Render an audit report as concise plain text."""
    ov = report["overview"]
    b = report["builtins"]
    lines: list[str] = []
    rules_note = f"  rules={report['rules_name']}" if report.get("rules_name") else "  (built-in MOO checks only)"
    lines.append(f"Audit mode: {report['mode']}{rules_note}  [static heuristics; confirm findings live]")
    lines.append(
        f"Objects: {ov['objects_matched']} matched ({ov['object_states']}), "
        f"{ov['objects_removed']} removed, {ov['objects_left_out_existing']} skipped by --existing"
    )
    lines.append(
        f"Verbs: {ov['verbs_added']} added ({ov['verbs_added_prod']} prod, {ov['verbs_added_test']} test), "
        f"{ov['verbs_changed']} changed ({ov['verbs_changed_prod']} prod, {ov['verbs_changed_test']} test; "
        f"{ov['verbs_changed_trailer_only']} trailer-only skipped), {ov['verbs_removed']} removed"
    )

    if ov["authors"]:
        lines.append("\nAuthors:")
        for row in ov["authors"][:12]:
            lines.append(f"  {row['author']:30s}  {row['total']:4d} (prod={row['prod']}, test={row['test']})")

    if ov["top_objects"] and limit:
        lines.append("\nTop objects:")
        for row in ov["top_objects"][:min(limit, 20)]:
            lines.append(f"  #{row['object']:<7d} {row['ref']:22s} {row['verbs']:4d}  {row['name']}")

    def _loc(r: dict[str, Any]) -> str:
        ln = f":{r['lineno']}" if "lineno" in r else ""
        return f"  {r['ref']} (#{r['object']}):{r['verb']}{ln} [{r['author']}]: {r['line'][:120]}"

    if "raw-refs" in b:
        s = b["raw-refs"]
        lines.append(f"\n[raw-refs] {s['count']} prod lines across {s['distinct_verbs']} verbs with hardcoded #N")
        for r in s["items"][:limit]:
            lines.append(_loc(r))

    if "catches" in b:
        s = b["catches"]
        lines.append(
            f"\n[catches] prod ANY={s['prod_any_count']}, prod specific={s['prod_specific_count']}, "
            f"test catches={s['test_catch_count']}"
        )
        if s["prod_any_by_author"]:
            lines.append("  prod ANY by author: " + ", ".join(f"{a}={c}" for a, c in s["prod_any_by_author"][:8]))
        if s["prod_any_by_object"]:
            lines.append("  prod ANY top objects: " + ", ".join(f"{o}={c}" for o, c in s["prod_any_by_object"][:10]))
        for r in s["prod_any"][:limit]:
            lines.append(_loc(r))

    if "docstrings" in b:
        s = b["docstrings"]
        lines.append(f"\n[docstrings] minimum {s['min_lines']} leading doc lines")
        for bucket, ctr in s["counts"].items():
            lines.append(f"  {bucket:14s}: {ctr}")
        for row in s["prod_by_author"][:12]:
            lines.append(
                f"  {row['author']:30s}  {row['passing']:4d}/{row['total']:4d} ({row['pct']:5.1f}%) "
                f"| short={row['short']} zero={row['zero']}"
            )
        for r in s["prod_failures"][:limit]:
            lines.append(
                f"  {r['ref']} (#{r['object']}):{r['verb']} [{r['change']}, {r['doc_lines']} lines, {r['author']}]"
            )

    if "security" in b:
        s = b["security"]
        lines.append(f"\n[security] {s['count']} wizard-owned executable mutator review candidates")
        for r in s["items"][:limit]:
            lines.append(
                f"  {r['ref']} (#{r['object']} {r['name']}):{r['verb']} [{r['change']}] "
                f"owner={r['owner']} perms={r['perms']} muts={r['mutators']} ({r['author']})"
            )

    if "duplicates" in b:
        s = b["duplicates"]
        lines.append(f"\n[duplicates] {s['group_count']} identical added prod verb body groups (>={s['min_lines']} lines)")
        for g in s["groups"][:limit]:
            objs = ", ".join(f"{loc['ref']}:{loc['verb']}" for loc in g["locations"][:6])
            lines.append(f"  {g['count']}x ({objs}): {g['first_line'][:80]}")

    for rule in report["rules"]:
        cat = rule["category"]
        rid = rule["id"]
        desc = f" — {rule['description']}" if rule["description"] else ""
        if rule["kind"] == "line_rule":
            tag = "metric" if rule["mode"] == "metric" else "finding"
            lines.append(
                f"\n[{cat}/{rid}] ({tag}) count={rule['count']} "
                f"(lines={rule['line_count']}, verbs={rule['distinct_verbs']}){desc}"
            )
            for r in rule["items"][:limit]:
                lines.append(_loc(r))
        elif rule["kind"] == "verb_rule":
            lines.append(f"\n[{cat}/{rid}] count={rule['count']}{desc}")
            for r in rule["items"][:limit]:
                lines.append(
                    f"  {r['ref']} (#{r['object']} {r['name']}):{r['verb']} "
                    f"({r['args']}, {r['perms']}, docs={r['doc_lines']}) [{r['author']}]"
                )
        elif rule["kind"] == "boundary_rule":
            lines.append(
                f"\n[{cat}/{rid}] count={rule['count']} across {rule['checked_objects']} boundary objects{desc}"
            )
            for r in rule["items"][:limit]:
                lines.append(
                    f"  {r['ref']} (#{r['object']}):{r['verb']}:{r['lineno']} -> {r['dollar_ref']} "
                    f"[{r['author']}]: {r['line'][:100]}"
                )
        elif rule["kind"] == "object_rule":
            lines.append(f"\n[{cat}/{rid}] count={rule['count']}{desc}")
            for r in rule["items"][:limit]:
                lines.append(
                    f"  #{r['object']} {r['ref']} \"{r['name']}\" owner={r['owner']} "
                    f"f={int(r['fertile'])} children={r['children']} issues={r['issues']}"
                )

    return "\n".join(lines)

"""Which objects of another database already exist in this one, judged by their verb code."""

import hashlib
from collections import defaultdict

from .database import MooDatabase
from .inspection import all_objects
from .lazy import LazyDatabase


def normalized(code):
    """Verb source with indentation, blank lines and "Last modified" footers removed."""
    return [line.strip() for line in code
            if line.strip() and not line.strip().startswith('"Last modified ')]


def _digest(lines):
    return hashlib.sha256("\n".join(lines).encode("latin-1", "replace")).hexdigest()


def _similarity(a, b):
    """Share of lines the two programs have in common (multiset Jaccard)."""
    ca, cb = defaultdict(int), defaultdict(int)
    for line in a:
        ca[line] += 1
    for line in b:
        cb[line] += 1
    common = sum(min(n, cb[line]) for line, n in ca.items())
    return common / (len(a) + len(b) - common) if a or b else 1.0


class VerbIndex:
    """This database's programs, by exact normalized digest and by each verb name alias."""

    def __init__(self, db: MooDatabase):
        objs = db.objects_with_programs() if isinstance(db, LazyDatabase) else list(all_objects(db))
        self.exact = defaultdict(list)
        self.by_name = defaultdict(list)
        for o in objs:
            for index, verb in enumerate(o.verbs):
                if verb.code is None:
                    continue
                lines = normalized(verb.code)
                place = (o.id, index, verb.name)
                self.exact[_digest(lines)].append(place)
                for alias in verb.name.split():
                    self.by_name[alias.lower()].append((place, lines))

    def find(self, verb, threshold):
        """('exact', places) or ('similar', [(score, place)]) or ('new', [])."""
        lines = normalized(verb.code)
        places = self.exact.get(_digest(lines))
        if places:
            return "exact", places
        candidates = {}
        for alias in verb.name.split():
            for place, other in self.by_name.get(alias.lower(), []):
                if place not in candidates:
                    candidates[place] = _similarity(lines, other)
        close = sorted(((s, p) for p, s in candidates.items() if s >= threshold), reverse=True)
        return ("similar", close) if close else ("new", [])


def match_objects(index: VerbIndex, objs, threshold=0.5):
    """For each of OBJS (from another database), its verbs with code sorted into exact, similar and new."""
    results = []
    for o in objs:
        verbs = []
        for verb in o.verbs:
            if verb.code is None or not normalized(verb.code):
                continue
            kind, where = index.find(verb, threshold)
            verbs.append({"verb": verb.name, "match": kind, "where": where[:3]})
        counts = {k: sum(v["match"] == k for v in verbs) for k in ("exact", "similar", "new")}
        results.append({"object": o.id, "name": o.name, "verbs": verbs, **counts})
    return results


def verdict(result):
    total = result["exact"] + result["similar"] + result["new"]
    if not total:
        return "no code"
    if result["exact"] >= 0.8 * total:
        return "have it"
    if result["exact"] + result["similar"] >= 0.5 * total:
        return "have a different version"
    return "new"

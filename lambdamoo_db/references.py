"""Read-only discovery of object references in property values."""

from collections.abc import Iterator
from typing import Any

from .compare import DiffPath
from .database import MooDatabase, ObjNum


def find_property_references(db: MooDatabase, oid: int | ObjNum) -> Iterator[DiffPath]:
    """Yield every property-value path containing the requested object reference.

    List indexes and map entry indexes are zero-based. Map paths use
    ``entries[index].key`` or ``entries[index].value`` in insertion order.
    Only ObjNum values match; integers, strings, and anonymous references do
    not. Object metadata and referenced waif bodies are outside this scope.
    Paths describe the current database; do not mutate it while iterating.
    Cyclic Python containers are rejected (MOO dump values are acyclic).
    """
    if type(oid) not in (int, ObjNum):
        raise TypeError("oid must be an int or ObjNum")
    target = int(oid)
    for obj_id, obj in db.objects.items():
        for index, prop in enumerate(obj.properties):
            root = DiffPath.object(int(obj_id)).child("properties").child(index).child("value")
            pending: list[tuple[Any, DiffPath, bool]] = [(prop.value, root, False)]
            ancestors: set[int] = set()
            while pending:
                value, path, leaving = pending.pop()
                if leaving:
                    ancestors.remove(id(value))
                elif isinstance(value, ObjNum):
                    if int(value) == target:
                        yield path
                elif isinstance(value, (list, dict)):
                    if id(value) in ancestors:
                        raise ValueError(f"Cyclic property value at {path}")
                    ancestors.add(id(value))
                    pending.append((value, path, True))
                    if isinstance(value, list):
                        for i in range(len(value) - 1, -1, -1):
                            pending.append((value[i], path.child(i), False))
                    else:
                        for i, (key, item) in reversed(list(enumerate(value.items()))):
                            entry = path.child("entries").child(i)
                            pending.append((item, entry.child("value"), False))
                            pending.append((key, entry.child("key"), False))

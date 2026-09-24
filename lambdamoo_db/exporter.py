from io import TextIOWrapper
import json
import os
import re
import shutil
from typing import Any, Optional
import cattrs
import attrs
from cattrs.gen import make_dict_unstructure_fn
from lambdamoo_db.database import Anon, Clear, MooCatch, MooError, MooFinally, ObjNum, WaifReference, MooDatabase


_json_converter = cattrs.Converter(unstruct_collection_overrides={set: list})
_json_converter.register_unstructure_hook_factory(
    attrs.has,
    lambda cls: make_dict_unstructure_fn(cls, _json_converter, _cattrs_include_init_false=True),
)
for _scalar_type in (ObjNum, Anon, MooError, MooCatch, MooFinally):
    _json_converter.register_unstructure_hook(_scalar_type, int)


ILLEGAL_NAMES = [
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM0",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT0",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
]


def converter(x: Any) -> Any:
    if isinstance(x, (ObjNum, Anon, MooError, MooCatch, MooFinally)):
        return int(x)
    if isinstance(x, WaifReference):
        return f"WAIF({x.index})"
    if isinstance(x, Clear):
        return None
    raise TypeError(f"Object of type {type(x).__name__} is not JSON serializable")


def sanitize(filename: str) -> str:
    name = re.sub(r"[\*\?\|:;\/\\<>]", "", filename)
    if name.upper() in ILLEGAL_NAMES:
        return ""
    return name


def to_json_data(value: Any) -> Any:
    """Unstructure a database or projection using the exporter's scalar hooks.

    Typed MOO scalars become integers, including in nested map keys. This is
    the legacy, lossy JSON representation, not a database round-trip format.
    """
    return _json_converter.unstructure(value)


def to_json(db: Any) -> str:
    """Serialize a database or a selected projection of reader values."""
    return json.dumps(to_json_data(db), indent=2, default=converter)


def to_json_file(db: Any, f: TextIOWrapper, indent: Optional[int] = None) -> None:
    json.dump(to_json_data(db), f, indent=indent, default=converter)


def to_moo_files(db: MooDatabase, path: str, corrify: bool) -> None:
    if os.path.exists(path):
        shutil.rmtree(path)

    os.mkdir(path)
    names = {}
    if corrify:
        for p in db.objects[0].properties:
            if p.propertyName and isinstance(p.value, ObjNum) and int(p.value) not in names:
                names[int(p.value)] = "$" + p.propertyName

    def name(i: int | ObjNum) -> str:
        id = str(i)
        if corrify and int(i) in names:
            id = names[int(i)]
        return id

    for i, o in db.objects.items():
        id = name(i)
        os.mkdir(os.path.join(path, id))
        with open(os.path.join(path, id, "info.json"), "w") as f:

            info = {
                "name": o.name,
                "parent": None,
                "parents": [name(p) for p in o.parents],
                "owner": o.owner,
                "location": o.location,
                "verbs": [v.name for v in o.verbs],
            }
            if len(o.parents) < 2:
                info["parent"] = o.parent

            json.dump(to_json_data(info), f, indent=2, default=converter)
        with open(os.path.join(path, id, "props.json"), "w") as f:
            json.dump(to_json_data(o.properties), f, indent=2, default=converter)

        for i, v in enumerate(o.verbs):
            filename = (sanitize(v.name) or str(i)).split(" ", 1)[0] + ".moo"
            with open(os.path.join(path, id, filename), "w") as f:
                f.write("\n".join(v.code))

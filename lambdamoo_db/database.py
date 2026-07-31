from typing import Any, Generator
import attrs
from .enums import MooTypes, ObjectFlags, PropertyFlags


class ObjNum(int):
    def __str__(self):
        return f"#{int(self)}"

    def __repr__(self):
        return f"ObjNum({int(self)})"

    def __eq__(self, other):
        if isinstance(other, int):
            return int(self) == int(other)
        return NotImplemented

    def __hash__(self):
        return hash(int(self))


class Anon(int):
    def __repr__(self):
        return f"Anon({int(self)})"

    def __eq__(self, other):
        if isinstance(other, int):
            return int(self) == int(other)
        return NotImplemented

    def __hash__(self):
        return hash(int(self))


class MooError(int):
    """Wrapper for MOO error values (TYPE_ERR)."""
    def __eq__(self, other):
        if isinstance(other, int):
            return int(self) == int(other)
        return NotImplemented

    def __hash__(self):
        return hash(int(self))


class MooCatch(int):
    """Wrapper for MOO _CATCH values to preserve type during roundtrip."""
    def __eq__(self, other):
        if isinstance(other, int):
            return int(self) == int(other)
        return NotImplemented

    def __hash__(self):
        return hash(int(self))


class MooFinally(int):
    """Wrapper for MOO _FINALLY values to preserve type during roundtrip."""
    def __eq__(self, other):
        if isinstance(other, int):
            return int(self) == int(other)
        return NotImplemented

    def __hash__(self):
        return hash(int(self))


class Clear:
    """Sentinel for TYPE_CLEAR values (distinct from None/TYPE_NONE)."""
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "CLEAR"


CLEAR = Clear()  # Singleton instance


@attrs.define()
class Verb:
    name: str
    owner: int
    perms: int
    preps: int
    object: int
    # None = no program, [] = empty program, [...] = has code
    code: list[str] | None = attrs.field(init=False, default=None)


@attrs.define()
class Property:
    propertyName: str
    value: Any
    owner: int
    perms: PropertyFlags = attrs.field(converter=PropertyFlags)


@attrs.define()
class MooObject:
    id: int
    name: str
    flags: ObjectFlags = attrs.field(converter=ObjectFlags)
    owner: int
    location: int
    parents: list[int] = attrs.field(factory=list)
    children: list[int] = attrs.field(init=False, factory=list)
    last_move: int = attrs.field(init=False, default=-1)
    contents: list[int] = attrs.field(init=False, factory=list)
    verbs: list[Verb] = attrs.field(init=False, factory=list)
    properties: list[Property] = attrs.field(init=False, factory=list)
    propdefs_count: int = attrs.field(init=False, default=0)  # Properties defined on this object (not inherited)
    anon: bool = attrs.field(default=False)

    @property
    def parent(self) -> int:
        if len(self.parents) > 1:
            raise Exception("Object has multiple parents")
        if not self.parents:
            return -1  # No parent
        return self.parents[0]


@attrs.define()
class Waif:
    waif_class: int
    owner: int
    props: list[Any]  # List of (slot_index, value) tuples for roundtrip
    propdefs_length: int = attrs.field(default=0)  # Original propdefs_length for roundtrip


@attrs.define()
class WaifReference:
    index: int

    def __hash__(self):
        return hash(self.index)


@attrs.define()
class Activation:
    this: int | None = attrs.field(init=False, default=None)
    threaded: int | None = attrs.field(init=False, default=None)
    player: int | None = attrs.field(init=False, default=None)
    programmer: int | None = attrs.field(init=False, default=None)
    vloc: int | None = attrs.field(init=False, default=None)
    debug: bool = attrs.field(init=False)
    verb: str = attrs.field(init=False)
    verbname: str = attrs.field(init=False)
    code: list[str] = attrs.field(init=False, factory=list)
    stack: list[Any] = attrs.field(init=False, factory=list)
    unused1 = attrs.field(init=False, default=0)
    unused2 = attrs.field(init=False, default=0)
    unused3 = attrs.field(init=False, default=0)
    unused4 = attrs.field(init=False, default=0)
    # Pre-header values (preserved for round-trip with type info)
    temp_value: Any = attrs.field(init=False, default=None)  # First value (often discarded)
    temp_this: Any = attrs.field(init=False, default=None)   # Pre-header this (with type)
    temp_vloc: Any = attrs.field(init=False, default=None)   # Pre-header vloc (with type)
    # For full activations (suspended tasks) - additional roundtrip fields
    rtEnv: dict[str, Any] = attrs.field(init=False, factory=dict)  # Runtime environment
    temp_end: Any = attrs.field(init=False, default=None)  # Temp value after PI header
    pc: int = attrs.field(init=False, default=0)  # Program counter
    bi_func: int = attrs.field(init=False, default=0)  # Built-in function flag
    error: int = attrs.field(init=False, default=0)  # Error value
    bi_func_name: str | None = attrs.field(init=False, default=None)  # Built-in function name


@attrs.define()
class VM:
    locals: dict
    stack: list[Activation | None]
    # VM header fields for roundtrip fidelity
    top: int = attrs.field(default=0)
    vector: int = attrs.field(default=0)
    funcId: int = attrs.field(default=0)
    maxStackframes: int = attrs.field(default=50)


@attrs.define()
class QueuedTask:
    firstLineno: int
    id: int
    st: int
    unused: int = attrs.field(init=False, default=0)
    value: Any = attrs.field(init=False, default=None)
    activation: Activation | None = attrs.field(init=False)
    rtEnv: dict[str, Any] = attrs.field(init=False)
    code: list[str] = attrs.field(init=False, factory=list)


@attrs.define()
class SuspendedTask:
    firstLineno: int
    id: int
    startTime: int
    value: Any = attrs.field(init=False, default=None)
    vm: VM = attrs.field(init=False, default=None)


@attrs.define()
class InterruptedTask:
    id: int
    status: str
    vm: VM = attrs.field(init=False, default=None)


TYPE_MAPPING = {
    int: MooTypes.INT,
    str: MooTypes.STR,
    ObjNum: MooTypes.OBJ,
    float: MooTypes.FLOAT,
    list: MooTypes.LIST,
    dict: MooTypes.MAP,
    bool: MooTypes.BOOL,
    type(None): MooTypes.NONE,
}


@attrs.define
class MooDatabase:
    versionstring: str = attrs.field(init=False)
    version: int = attrs.field(init=False)
    total_objects: int = attrs.field(init=False, default=0)
    total_verbs: int = attrs.field(init=False, default=0)
    total_players: int = attrs.field(init=False, default=0)
    clocks: list = attrs.field(factory=list)
    objects: dict[int, MooObject] = attrs.field(factory=dict)
    queuedTasks: list[QueuedTask] = attrs.field(factory=list)
    suspendedTasks: list[SuspendedTask] = attrs.field(factory=list)
    interruptedTasks: list[InterruptedTask] = attrs.field(factory=list)
    waifs: dict[int, Waif] = attrs.field(factory=dict)
    players: list[int] = attrs.field(factory=list)
    recycled_objects: set[int] = attrs.field(factory=set)
    pending_anon_ids: list[int] = attrs.field(factory=list)  # For pre-creating anons in pending section
    connections: list[str] = attrs.field(factory=list)  # Connection lines for roundtrip
    connections_with_listeners: str = attrs.field(default=" with listeners")  # Listener tag suffix
    line_ending: str = attrs.field(default="\n")  # Line ending style for roundtrip (\n or \r\n)

    def all_verbs(self) -> Generator[Verb, None, None]:
        for obj in self.objects.values():
            for verb in obj.verbs:
                yield verb

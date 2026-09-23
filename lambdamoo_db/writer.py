from io import TextIOWrapper
from logging import getLogger
from typing import Any, SupportsInt

import attrs
from attrs import asdict, define

from lambdamoo_db.enums import DBVersions, MooTypes

from . import templates
from .database import (
    CLEAR,
    TYPE_MAPPING,
    VM,
    Activation,
    Anon,
    Clear,
    InterruptedTask,
    MooCatch,
    MooDatabase,
    MooError,
    MooFinally,
    MooObject,
    ObjNum,
    Property,
    QueuedTask,
    SuspendedTask,
    Verb,
    Waif,
    WaifReference,
)

logger = getLogger(__name__)


@define
class Writer:
    db: MooDatabase
    output_file: TextIOWrapper
    _written_waifs: dict = attrs.field(factory=dict)  # original_index -> write_index

    @property
    def _nl(self) -> str:
        """Get the line ending to use for output."""
        return self.db.line_ending

    def write(self, text: str) -> None:
        self.output_file.write(text)

    def writeInt(self, i: SupportsInt) -> None:
        # Guard against bool (which is int subclass but semantically wrong)
        if isinstance(i, bool):
            raise TypeError("writeInt() does not accept bool values")
        # Convert to int and format (works for ints, IntFlag, IntEnum, etc.)
        # Note: no trailing newline - callers add newlines as needed
        self.write(f"{int(i)}")

    def writeString(self, s: str) -> None:
        self.write(f"{s}{self._nl}")

    def writeObj(self, obj: ObjNum) -> None:
        self.write(f"{int(obj)}")

    def writeFloat(self, f: float) -> None:
        # Match ToastStunt: DBL_DIG + 4 = 19 significant digits with %g format
        self.write(f"{f:.19g}")

    def writeBool(self, b: bool) -> None:
        self.writeInt(1 if b else 0)

    def writeRawValue(self, v: Any) -> None:
        """Write a value without type tag (just the raw data)."""
        if isinstance(v, bool):
            self.writeBool(v)
            self.write(self._nl)
        elif isinstance(v, MooError):
            self.writeInt(v)
            self.write(self._nl)
        elif isinstance(v, ObjNum):
            self.writeObj(v)
            self.write(self._nl)
        elif isinstance(v, int):
            self.writeInt(v)
            self.write(self._nl)
        elif isinstance(v, str):
            self.writeString(v)
        elif isinstance(v, float):
            self.writeFloat(v)
            self.write(self._nl)
        elif isinstance(v, list):
            self.writeListContents(v)
        elif isinstance(v, dict):
            self.writeMapContents(v)
        elif isinstance(v, Clear):
            pass  # CLEAR has no value
        elif v is None:
            pass  # NONE has no value
        elif isinstance(v, SupportsInt):
            self.writeInt(v)
            self.write(self._nl)
        else:
            raise TypeError(f"Unsupported value type: {type(v).__name__}")

    def writeListContents(self, lst: list[Any]) -> None:
        """Write list contents without type tag (count + items)."""
        self.writeInt(len(lst))
        self.write(self._nl)
        for item in lst:
            self.writeValue(item)

    def writeMapContents(self, m: dict[Any, Any]) -> None:
        """Write map contents without type tag (count + key/value pairs)."""
        self.writeInt(len(m))
        self.write(self._nl)
        for key, value in m.items():
            self.writeValue(key)
            self.writeValue(value)

    def writeWaif(self, waif_ref: WaifReference) -> None:
        """Write a waif - definition on first write, reference on subsequent.

        ToastStunt expects waif indices in the file to be sequential (0, 1, 2...)
        matching the order they're first written. We remap from our internal
        indices to sequential write indices.
        """
        original_index = waif_ref.index

        if original_index in self._written_waifs:
            # Reference format: "r {write_index}\n.\n"
            write_index = self._written_waifs[original_index]
            self.writeString(f"r {write_index}")
            self.writeString(".")
        else:
            # Definition format - assign next sequential write index
            write_index = len(self._written_waifs)
            self._written_waifs[original_index] = write_index
            waif = self.db.waifs[original_index]
            props = [
                prop
                if (
                    isinstance(prop, tuple)
                    and len(prop) == 2
                    and isinstance(prop[0], int)
                )
                else (slot_idx, prop)
                for slot_idx, prop in enumerate(waif.props)
            ]

            # Header: "c {write_index}" (c = creation/definition)
            self.writeString(f"c {write_index}")
            # Class objnum
            self.writeInt(waif.waif_class)
            self.write(self._nl)
            # Owner objnum
            self.writeInt(waif.owner)
            self.write(self._nl)
            # propdefs_length (original from the file)
            self.writeInt(waif.propdefs_length or len(props))
            self.write(self._nl)
            for slot_idx, prop_value in props:
                self.writeInt(slot_idx)
                self.write(self._nl)
                self.writeValue(prop_value)
            # Terminator: -1
            self.writeInt(-1)
            self.write(self._nl)
            # End marker
            self.writeString(".")

    def writeList(self, lst: list[Any]) -> None:
        """Write a type-tagged list."""
        self.writeInt(MooTypes.LIST)
        self.write(self._nl)
        self.writeListContents(lst)

    def writeMap(self, m: dict[Any, Any]) -> None:
        """Write a type-tagged map."""
        self.writeInt(MooTypes.MAP)
        self.write(self._nl)
        self.writeMapContents(m)

    def writeValue(self, v: Any) -> None:
        """Write a type-tagged value (type tag on its own line, then value)."""
        # Determine type tag
        if isinstance(v, bool):
            logger.debug(f"  Writing [TYPE_BOOL] = {v}")
            self.writeInt(MooTypes.BOOL)
            self.write(self._nl)
            self.writeBool(v)
            self.write(self._nl)
        elif isinstance(v, MooError):
            logger.debug(f"  Writing [TYPE_ERR] = {v}")
            self.writeInt(MooTypes.ERR)
            self.write(self._nl)
            self.writeInt(v)
            self.write(self._nl)
        elif isinstance(v, MooCatch):
            logger.debug(f"  Writing [TYPE_CATCH] = {v}")
            self.writeInt(MooTypes._CATCH)
            self.write(self._nl)
            self.writeInt(int(v))
            self.write(self._nl)
        elif isinstance(v, MooFinally):
            logger.debug(f"  Writing [TYPE_FINALLY] = {v}")
            self.writeInt(MooTypes._FINALLY)
            self.write(self._nl)
            self.writeInt(int(v))
            self.write(self._nl)
        elif isinstance(v, ObjNum):
            logger.debug(f"  Writing [TYPE_OBJ] = #{v}")
            self.writeInt(MooTypes.OBJ)
            self.write(self._nl)
            self.writeObj(v)
            self.write(self._nl)
        elif isinstance(v, Anon):
            logger.debug(f"  Writing [TYPE_ANON] = {v}")
            self.writeInt(MooTypes.ANON)
            self.write(self._nl)
            self.writeInt(int(v))
            self.write(self._nl)
        elif isinstance(v, int):
            logger.debug(f"  Writing [TYPE_INT] = {v}")
            self.writeInt(MooTypes.INT)
            self.write(self._nl)
            self.writeInt(v)
            self.write(self._nl)
        elif isinstance(v, str):
            logger.debug(f"  Writing [TYPE_STR] = {v!r}")
            self.writeInt(MooTypes.STR)
            self.write(self._nl)
            self.writeString(v)
        elif isinstance(v, float):
            logger.debug(f"  Writing [TYPE_FLOAT] = {v}")
            self.writeInt(MooTypes.FLOAT)
            self.write(self._nl)
            self.writeFloat(v)
            self.write(self._nl)
        elif isinstance(v, list):
            logger.debug(f"  Writing [TYPE_LIST] count={len(v)}")
            self.writeInt(MooTypes.LIST)
            self.write(self._nl)
            self.writeListContents(v)
        elif isinstance(v, dict):
            logger.debug(f"  Writing [TYPE_MAP] count={len(v)}")
            self.writeInt(MooTypes.MAP)
            self.write(self._nl)
            self.writeMapContents(v)
        elif isinstance(v, Clear):
            logger.debug(f"  Writing [TYPE_CLEAR]")
            self.writeInt(MooTypes.CLEAR)
            self.write(self._nl)
        elif v is None:
            logger.debug(f"  Writing [TYPE_NONE]")
            self.writeInt(MooTypes.NONE)
            self.write(self._nl)
        elif isinstance(v, WaifReference):
            logger.debug(f"  Writing [TYPE_WAIF] = {v}")
            self.writeInt(MooTypes.WAIF)
            self.write(self._nl)
            self.writeWaif(v)
        elif isinstance(v, SupportsInt):
            logger.debug(f"  Writing [TYPE_INT] = {v}")
            self.writeInt(MooTypes.INT)
            self.write(self._nl)
            self.writeInt(v)
            self.write(self._nl)
        else:
            raise TypeError(f"Unsupported value type: {type(v).__name__}")

    def writeDatabase(self) -> None:
        if DBVersions.DBV_Exceptions <= self.db.version < DBVersions.DBV_NextGen:
            self.writeDatabaseV4()
            return

        self.writeString(self.db.versionstring)
        self.writePlayers()
        self.writePending()
        self.writeClocks()
        self.writeTaskQueue()
        self.writeSuspendedTasks()
        self.writeInterruptedTasks()
        self.writeConnections()
        self.writeObjects()
        self.writeVerbs()

    def writeDatabaseV4(self) -> None:
        self.writeString(self.db.versionstring)
        self.writeInt(self.db.total_objects)
        self.write(self._nl)
        self.writeInt(self.db.total_verbs)
        self.write(self._nl)
        self.writeString(self.db.v4_dummy)
        self.writePlayers()
        self.writeObjectsV4()
        self.writeVerbs(include_count=False)
        self.writeClocks()
        self.writeTaskQueue()
        self.writeSuspendedTasks()
        if self.db.has_connections_section:
            self.writeConnections()

    def writePlayers(self) -> None:
        self.writeInt(len(self.db.players))
        self.write(self._nl)
        for player in self.db.players:
            self.writeInt(player)
            self.write(self._nl)

    def writePending(self) -> None:
        self.writeString(
            templates.pending_values_count.format(count=len(self.db.pending_values))
        )
        for value in self.db.pending_values:
            self.writeValue(value)

    def writeObjects(self) -> None:
        # Write total count (includes recycled slots)
        self.writeInt(self.db.total_objects)
        self.write(self._nl)
        # Write regular objects in order, including recycled placeholders
        for obj_id in range(self.db.total_objects):
            if obj_id in self.db.recycled_objects:
                self.writeString(f"# {obj_id} recycled")
            elif obj_id in self.db.objects:
                self.writeObject(self.db.objects[obj_id])
            else:
                raise ValueError(f"Object {obj_id} missing and not marked recycled")

        # Write anonymous objects (those with anon=True or id >= total_objects)
        anon_objects = [
            obj for obj in self.db.objects.values()
            if getattr(obj, 'anon', False) or obj.id >= self.db.total_objects
        ]

        if anon_objects:
            # Write count of anon objects
            self.writeInt(len(anon_objects))
            self.write(self._nl)
            # Write each anon object
            for obj in sorted(anon_objects, key=lambda o: o.id):
                self.writeObject(obj)

        # Write 0 to signal end of anonymous objects
        self.writeInt(0)
        self.write(self._nl)

    def writeObjectsV4(self) -> None:
        for obj_id in range(self.db.total_objects):
            if obj_id in self.db.recycled_objects:
                self.writeString(f"# {obj_id} recycled")
            elif obj_id in self.db.objects:
                self.writeObjectV4(self.db.objects[obj_id])
            else:
                raise ValueError(f"Object {obj_id} missing and not marked recycled")

    def writeObjectV4(self, obj: MooObject) -> None:
        self.writeString(f"#{obj.id}")
        self.writeString(obj.name)
        self.writeString(obj.v4_blank_line)
        for value in (
            obj.flags,
            obj.owner,
            obj.location,
            obj.v4_first_content,
            obj.v4_neighbor,
            obj.parent,
            obj.v4_first_child,
            obj.v4_sibling,
        ):
            self.writeInt(value)
            self.write(self._nl)
        self.writeCollection(obj.verbs, writer=self.writeVerbMetadata)
        self.write_properties(obj)

    def writeObject(self, obj: MooObject) -> None:
        obj_num = obj.id
        logger.debug(f"Writing object #{obj_num} {obj.name!r}")
        self.writeString(f"#{obj_num}")
        self.writeString(obj.name)
        logger.debug(f"  name = {obj.name!r}")
        self.writeInt(obj.flags)
        self.write(self._nl)
        logger.debug(f"  flags = {obj.flags}")
        self.writeInt(obj.owner)
        self.write(self._nl)
        logger.debug(f"  owner = #{obj.owner}")
        self.writeValue(obj.location)
        logger.debug(f"  location = {obj.location}")
        self.writeValue(obj.last_move)
        logger.debug(f"  last_move = {obj.last_move}")
        # Contents list - convert to ObjNums
        contents_as_objnums = [ObjNum(c) for c in obj.contents]
        self.writeValue(contents_as_objnums)
        logger.debug(f"  contents = {obj.contents}")
        # Single parent → ObjNum, multiple parents → list of ObjNums, no parents → -1
        if len(obj.parents) == 0:
            self.writeValue(ObjNum(-1))
            logger.debug(f"  parent = #-1 (none)")
        elif len(obj.parents) == 1:
            self.writeValue(ObjNum(obj.parents[0]))
            logger.debug(f"  parent = #{obj.parents[0]}")
        else:
            # Convert to ObjNums for proper TYPE_OBJ encoding
            parents_as_objnums = [ObjNum(p) for p in obj.parents]
            self.writeValue(parents_as_objnums)
            logger.debug(f"  parents = {obj.parents}")
        # Children list - convert to ObjNums
        children_as_objnums = [ObjNum(c) for c in obj.children]
        self.writeValue(children_as_objnums)
        logger.debug(f"  children = {obj.children}")
        logger.debug(f"  verbs count = {len(obj.verbs)}")
        self.writeCollection(obj.verbs, writer=self.writeVerbMetadata)
        self.write_properties(obj)
        logger.debug(f"Completed writing object #{obj_num} {obj.name!r}")

    def writeVerbMetadata(self, verb: Verb) -> None:
        self.writeString(verb.name)
        self.writeInt(verb.owner)
        self.write(self._nl)
        self.writeInt(verb.perms)
        self.write(self._nl)
        self.writeInt(verb.preps)
        self.write(self._nl)

    def write_properties(self, obj: MooObject) -> None:
        # Write propdefs (properties DEFINED on this object, with names)
        logger.debug(f"  Writing properties for #{obj.id} {obj.name!r}")
        self.writeInt(obj.propdefs_count)
        self.write(self._nl)
        logger.debug(f"    propdefs_count = {obj.propdefs_count}")
        for i, prop in enumerate(obj.properties[:obj.propdefs_count]):
            self.writeString(prop.propertyName)
            logger.debug(f"    propdef[{i}] name = {prop.propertyName!r}")
        # Write all property values (defined + inherited)
        self.writeInt(len(obj.properties))
        self.write(self._nl)
        logger.debug(f"    total properties (nval) = {len(obj.properties)}")
        for idx, prop in enumerate(obj.properties):
            logger.debug(f"    property[{idx}] name = {prop.propertyName!r}")
            self.writeProperty(prop)
            logger.debug(f"      owner=#{prop.owner}, perms={prop.perms}")

    def writeProperty(self, prop: Property):
        self.writeValue(prop.value)
        self.writeInt(prop.owner)
        self.write(self._nl)
        self.writeInt(prop.perms)
        self.write(self._nl)

    def writeVerbs(self, include_count: bool = True) -> None:
        # Count verbs with programs (None = no program, [] = empty program)
        verbs_with_programs = [v for v in self.db.all_verbs() if v.code is not None]
        if include_count:
            self.writeInt(len(verbs_with_programs))
            self.write(self._nl)
        # Write each verb program
        for verb in verbs_with_programs:
            self.writeVerb(verb)

    def writeVerb(self, verb: Verb) -> None:
        objnum = verb.object
        object = self.db.objects[objnum]
        index = object.verbs.index(verb)
        vloc = f"#{objnum}:{index}"
        self.writeString(vloc)
        self.writeCode(verb.code)

    def writeCode(self, code: list[str]) -> None:
        for line in code:
            self.writeString(line)
        self.writeString(".")

    def writeCollection(self, collection, template=None, writer=None):
        if writer is None:
            writer = self.writeString
        if template is None:
            self.writeInt(len(collection))
            self.write(self._nl)
        else:
            self.writeString(template.format(count=len(collection)))
        for item in collection:
            writer(item)

    def writeClocks(self):
        self.writeCollection(self.db.clocks, templates.clock_count)

    def writeTaskQueue(self):
        self.writeCollection(self.db.queuedTasks, templates.task_count, self.writeQueuedTask)

    def writeQueuedTask(self, task: QueuedTask) -> None:
        taskHeader = templates.task_header.format(**asdict(task))
        self.writeString(taskHeader)
        self.writeActivationAsPI(task.activation)
        self.writeRtEnv(task.rtEnv)
        self.writeCode(task.code)

    def writeActivationAsPI(self, activation: Activation):
        # Write pre-header values (matched to read_activation_as_pi in reader)
        # These values preserve type info from the original file
        # 1. temp_value (first value, often discarded during interpretation)
        self.writeValue(activation.temp_value)
        # 2. temp_this (pre-header this, with type info)
        if self.db.version >= DBVersions.DBV_This:
            self.writeValue(activation.temp_this)
        # 3. temp_vloc (pre-header vloc, with type info)
        if self.db.version >= DBVersions.DBV_Anon:
            self.writeValue(activation.temp_vloc)
        # 4. threaded (just an int, no type tag for v17)
        if self.db.version >= DBVersions.DBV_Threaded:
            self.writeInt(activation.threaded if activation.threaded is not None else 0)
            self.write(self._nl)
        # Activation header line
        activation_header = templates.activation_header.format(**asdict(activation))
        self.writeString(activation_header)
        # Argstr placeholders
        self.writeString(activation.argstr)
        self.writeString(activation.dobjstr)
        self.writeString(activation.prepstr)
        self.writeString(activation.iobjstr)
        self.writeString(activation.verb)
        self.writeString(activation.verbname)

    def writeActivation(self, activation):
        # Write language version
        if self.db.version >= DBVersions.DBV_Float:
            language_version = activation.language_version or self.db.version
            self.writeString(templates.langver.format(version=language_version))
        # Write code
        self.writeCode(activation.code)
        # Write runtime environment
        self.writeRtEnv(activation.rtEnv)
        # Write stack header and values
        self.writeString(templates.stack_header.format(slots=len(activation.stack)))
        for val in activation.stack:
            self.writeValue(val)
        # Write activation as PI
        self.writeActivationAsPI(activation)
        # Write trailing temp value
        self.writeValue(activation.temp_end)
        # Write PC header
        self.writeString(templates.pc.format(pc=activation.pc, bi_func=activation.bi_func, error=activation.error))
        # Write built-in function name if present
        if activation.bi_func and activation.bi_func_name:
            self.writeString(activation.bi_func_name)

    def writeSuspendedTasks(self):
        self.writeCollection(self.db.suspendedTasks, templates.suspended_task_count, self.writeSuspendedTask)

    def writeInterruptedTasks(self):
        self.writeCollection(self.db.interruptedTasks, templates.interrupted_task_count, self.writeInterruptedTask)

    def writeInterruptedTask(self, task: InterruptedTask):
        # Header format: "id status"
        self.writeString(f"{task.id} {task.status}")
        self.writeVM(task.vm)

    def writeSuspendedTask(self, task: SuspendedTask):
        # Header format: "startTime id type_code" where type_code is the value type
        # If no value, we don't write the type code part
        if task.value is not None:
            # Get the type code from TYPE_MAPPING
            value_type = TYPE_MAPPING.get(type(task.value), MooTypes.INT)
            self.writeString(f"{task.startTime} {task.id} {int(value_type)}")
            # Write value data WITHOUT type tag (type is in header)
            self.writeRawValue(task.value)
        else:
            self.writeString(f"{task.startTime} {task.id}")
        self.writeVM(task.vm)

    def writeVM(self, vm: VM):
        if self.db.version >= DBVersions.DBV_TaskLocal:
            self.writeValue(vm.locals)
        # Write VM header
        header = templates.vm_header.format(
            top=vm.top, vector=vm.vector, funcId=vm.funcId, maxStackframes=vm.maxStackframes
        )
        self.writeString(header)
        # Write stack activations
        for activation in vm.stack:
            self.writeActivation(activation)

    def writeRtEnv(self, env: dict[str, Any]):
        header = templates.var_count.format(count=len(env))
        self.writeString(header)
        for name, value in env.items():
            self.writeString(name)
            moo_type = TYPE_MAPPING.get(type(value), MooTypes.INT)
            self.writeInt(moo_type)
            self.write(self._nl)
            if moo_type != MooTypes.NONE:
                self.writeRawValue(value)

    def writeConnections(self):
        # Write connection count and data
        # connections_with_listeners is the exact string suffix (e.g., " with listeners" or "")
        self.writeString(f"{len(self.db.connections)} active connections{self.db.connections_with_listeners}")
        for conn in self.db.connections:
            self.writeString(conn)


def dump(db: MooDatabase, f: TextIOWrapper) -> None:
    writer = Writer(db=db, output_file=f)
    writer.writeDatabase()

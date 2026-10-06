import re
from io import TextIOWrapper
from logging import DEBUG, getLogger
from typing import Any, NoReturn, Pattern, Union

import parse

from . import templates
from .database import (CLEAR, VM, Activation, Anon, InterruptedTask, MooCatch, MooDatabase, MooError,
                       MooFinally, MooObject, ObjNum, Property, QueuedTask, SuspendedTask,
                       Verb, Waif, WaifReference)
from .enums import DBVersions, MooTypes, PropertyFlags

logger = getLogger(__name__)


def load(filename: str) -> MooDatabase:
    """Load a database from a file"""
    with open(filename, "rb") as f:
        line_ending = detect_line_ending(f.read(1000))

    # Open in text mode for parsing
    with open(filename, "r", encoding="latin-1") as f:
        r = Reader(f, filename)
        db = r.parse()
        db.line_ending = line_ending
        return db


def detect_line_ending(chunk: bytes) -> str:
    """The line ending used by a dump, judged from its first bytes."""
    return '\r\n' if b'\r\n' in chunk else '\n'


def compile_re(template: str) -> Pattern[str]:
    compiled = parse.compile(template)
    if compiled._match_re is None:
        raise Exception(f"Failed to compile template: {template}")
    return compiled._match_re


versionRe = compile_re(templates.version)
varCountRe = compile_re(templates.var_count)
clockCountRe = compile_re(templates.clock_count)
taskCountRe = compile_re(templates.task_count)
taskHeaderRe = compile_re(templates.task_header)
activationHeaderRe = compile_re(templates.activation_header)
pendingValueRe = compile_re(templates.pending_values_count)
suspendedTaskCountRe = compile_re(templates.suspended_task_count)
# Use raw regex for suspended task header - the parse template was broken
# Format: "startTime id [value_type]" where value_type is optional
suspendedTaskHeaderRe = re.compile(r"(?P<startTime>-?\d+) (?P<id>\d+)(?: (?P<value>\d+))?")
interruptedTaskCountRe = compile_re(templates.interrupted_task_count)
interruptedTaskHeaderRe = re.compile(r"(?P<id>\d+) (?P<status>[\w\W]+)")
vmHeaderRe = compile_re(templates.vm_header)
connectionCountRe = re.compile(r"(?P<count>\d+) active connections(?P<listener_tag> with listeners|)")
langverRe = compile_re(templates.langver)
stackheaderRe = compile_re(templates.stack_header)
pcRe = compile_re(templates.pc)
waifHeaderRe = compile_re(templates.waif_header)


class Reader:

    def __init__(self, fio: TextIOWrapper, filename: str = "") -> None:
        self.filename = filename
        self.file = fio
        self.line = 0
        # A 100 MB dump has millions of values; formatting a debug message for
        # each one costs more than parsing it, so it is done only when wanted.
        self.debug = logger.isEnabledFor(DEBUG)

    def parse_error(self, message: str) -> NoReturn:
        raise Exception(f"Parse Error: {self.filename}:{self.line} : {message}")

    def parse(self) -> "MooDatabase":
        db = MooDatabase()
        self.read_version(db)
        if DBVersions.DBV_Exceptions <= db.version < DBVersions.DBV_NextGen:
            self.parse_v4(db)
        elif db.version == DBVersions.DBV_Bool:
            self.parse_v17(db)
        else:
            self.parse_error(f"Unknown db version {db.version}")
        return db

    def read_version(self, db: MooDatabase) -> None:
        db.versionstring = self.readString()
        version = versionRe.match(db.versionstring)
        if not version:
            self.parse_error("Invalid version string")
        db.version = int(version.group("version"))

    def parse_v4(self, db: MooDatabase) -> None:
        logger.debug("Parsing pre-next-generation database")
        db.total_objects = self.readInt()
        db.total_verbs = self.readInt()
        db.v4_dummy = self.readString()
        self.readPlayers(db)
        self.readObjects(db)
        self.readVerbs(db)
        self.readClocks(db)
        self.readTaskQueue(db)
        self.readSuspendedTasks(db)
        self.tryReadConnections(db)

    def parse_v17(self, db: MooDatabase) -> None:
        logger.debug("Parsing v17 database")
        self.read_v17_header(db)
        self.readObjects(db)
        if db.version >= DBVersions.DBV_Anon:
            self.readAnonObjects(db)
        db.total_verbs = self.readInt()
        self.readVerbs(db)

    def read_v17_header(self, db: MooDatabase) -> None:
        """Everything between the version line and the first object record."""
        self.readPlayers(db)
        self.readPending(db)
        self.readClocks(db)
        self.readTaskQueue(db)
        self.readSuspendedTasks(db)
        self.readInterruptedTasks(db)
        self.readConnections(db)
        db.total_objects = self.readInt()

    def readValue(self, db: MooDatabase, *, known_type: int | None = None) -> Any:
        line_at_start = self.line
        if known_type is not None:
            val_type = known_type
        else:
            val_type = self.readInt()

        match val_type:
            case MooTypes.STR:
                result = self.readString()
            case MooTypes.OBJ:
                result = self.readObjnum()
            case MooTypes.ANON:
                result = self.readAnon(db)
            case MooTypes.INT:
                result = self.readInt()
            case MooTypes.FLOAT:
                result = self.readFloat()
            case MooTypes.ERR:
                result = self.readErr()
            case MooTypes.LIST:
                result = self.readList(db)
            case MooTypes.CLEAR:
                result = CLEAR
            case MooTypes.NONE:
                result = None
            case MooTypes.MAP:
                result = self.readMap(db)
            case MooTypes.BOOL:
                result = self.readBool()
            case MooTypes._CATCH:
                result = MooCatch(self.readInt())
            case MooTypes._FINALLY:
                result = MooFinally(self.readInt())
            case MooTypes.WAIF:
                result = self.readWaif(db)
            case _:
                self.parse_error(f"unknown type {val_type}")
        if self.debug:
            self.log_value(val_type, line_at_start, result)
        return result

    def log_value(self, val_type: int, line_at_start: int, result: Any) -> None:
        name = MooTypes(val_type).name.lstrip("_")
        if val_type in (MooTypes.CLEAR, MooTypes.NONE):
            logger.debug(f"  [TYPE_{name} @ line {line_at_start}]")
        elif val_type in (MooTypes.LIST, MooTypes.MAP):
            logger.debug(f"  [TYPE_{name} @ line {line_at_start}] count={len(result)}")
        elif val_type == MooTypes.STR:
            logger.debug(f"  [TYPE_STR @ line {line_at_start}] = {result!r}")
        else:
            logger.debug(f"  [TYPE_{name} @ line {line_at_start}] = {result}")

    def readString(self) -> str:
        """Read a string from the database file"""
        self.line += 1
        return self.file.readline().rstrip("\r\n")

    def readInt(self) -> int:
        """Read an integer from the database file"""
        return int(self.readString())

    def readErr(self) -> MooError:
        return MooError(self.readInt())

    def readFloat(self) -> float:
        return float(self.readString())

    def readObjnum(self) -> ObjNum:
        return ObjNum(self.readString())

    def readBool(self) -> bool:
        return bool(self.readInt())

    def readList(self, db: MooDatabase) -> list[Any]:
        length = self.readInt()
        result = []
        for _ in range(length):
            result.append(self.readValue(db))
        return result

    def readMap(self, db: MooDatabase) -> dict:
        # self.parse_error(f'MAP @ Line {self.line}')
        items = self.readInt()
        map = {}
        for _ in range(items):
            key = self.readValue(db)
            val = self.readValue(db)
            map[key] = val
        return map

    def readWaif(self, db: MooDatabase):
        #  waif.cc:950 read_waif()
        header = waifHeaderRe.match(self.readString())
        if not header:
            self.parse_error(f"Invalid waif header")
        index = int(header.group("index"))
        if header.group("flag") == "r":
            # Reference
            _terminator = self.readString()
            return WaifReference(index)

        _class = self.readObjnum()
        owner = self.readObjnum()
        props = []  # Will store (slot_index, value) tuples
        propdefs_length = self.readInt()
        new = Waif(_class, owner, props, propdefs_length)

        db.waifs[index] = new
        while (cur := self.readInt()) < 3 * 32 and cur > -1:
            value = self.readValue(db)
            props.append((cur, value))  # Store as (slot_index, value) tuple
        _terminator = self.readString()
        return WaifReference(index)

    def readObject_v4(self, db: MooDatabase) -> Union[MooObject, None]:
        line_at_start = self.line
        objNumber = self.readString()
        if not objNumber.startswith("#"):
            self.parse_error("object number does not have #")

        if "recycled" in objNumber:
            # Format: "# 112 recycled" - extract the ID and track it
            parts = objNumber.replace("#", "").split()
            recycled_id = int(parts[0])
            db.recycled_objects.add(recycled_id)
            logger.debug(f"Tracking recycled object {recycled_id}")
            return None

        oid = int(objNumber[1:])
        logger.debug(f"Reading object #{oid} at line {line_at_start}")
        name = self.readString()
        logger.debug(f"  name = {name!r}")
        blank_line = self.readString()
        flags = self.readInt()
        logger.debug(f"  flags = {flags}")
        owner = self.readObjnum()
        logger.debug(f"  owner = #{owner}")
        location = self.readObjnum()
        logger.debug(f"  location = #{location}")
        firstContent = self.readInt()
        neighbor = self.readInt()
        parent = self.readObjnum()
        logger.debug(f"  parent = #{parent}")
        firstChild = self.readInt()
        sibling = self.readInt()
        obj = MooObject(
            id=oid,
            name=name,
            flags=flags,
            owner=owner,
            location=location,
            parents=[parent],
        )
        obj.v4_blank_line = blank_line
        obj.v4_first_content = firstContent
        obj.v4_neighbor = neighbor
        obj.v4_first_child = firstChild
        obj.v4_sibling = sibling
        numVerbs = self.readInt()
        logger.debug(f"  verbs count = {numVerbs}")
        for _ in range(numVerbs):
            self.readVerbMetadata(obj)

        self.readProperties(db, obj)
        logger.debug(f"Completed reading object #{oid} {obj.name!r}")
        return obj

    def readObject_ng(self, db: MooDatabase) -> Union[MooObject, None]:
        obj = self.read_object_head_ng(db)
        if obj is not None:
            self.readProperties(db, obj)
            logger.debug(f"Completed reading object #{obj.id} {obj.name!r}")
        return obj

    def read_object_head_ng(self, db: MooDatabase) -> Union[MooObject, None]:
        """An object record up to its properties: the header fields and the verb definitions."""
        line_at_start = self.line
        objNumber = self.readString()
        if not objNumber.startswith("#"):
            self.parse_error("object number does not have #")

        if "recycled" in objNumber:
            # Format: "# 112 recycled" - extract the ID and track it
            parts = objNumber.replace("#", "").split()
            recycled_id = int(parts[0])
            db.recycled_objects.add(recycled_id)
            logger.debug(f"Tracking recycled object {recycled_id}")
            return None

        oid = int(objNumber[1:])
        logger.debug(f"Reading object #{oid} at line {line_at_start}")
        name = self.readString()
        logger.debug(f"  name = {name!r}")
        flags = self.readInt()
        logger.debug(f"  flags = {flags}")
        owner = self.readObjnum()
        logger.debug(f"  owner = #{owner}")
        location = self.readValue(db)
        logger.debug(f"  location = {location}")
        last_move = -1
        if db.version >= DBVersions.DBV_Last_Move:
            last_move = self.readValue(db)
            logger.debug(f"  last_move = {last_move}")

        contents = self.readValue(db)
        logger.debug(f"  contents = {contents}")
        parents = self.readValue(db)
        if not isinstance(parents, list):
            parents = [parents]
        logger.debug(f"  parents = {parents}")
        children = self.readValue(db)
        logger.debug(f"  children = {children}")
        obj = MooObject(oid, name, flags, owner, location, parents)
        obj.last_move = last_move
        obj.contents = contents
        obj.children = children
        numVerbs = self.readInt()
        logger.debug(f"  verbs count = {numVerbs}")
        for _ in range(numVerbs):
            self.readVerbMetadata(obj)
        return obj

    def readAnon(self, db: MooDatabase) -> Anon:
        oid = self.readInt()
        return Anon(oid)  # Anon(-1) represents null anon reference

    def readConnections(self, db: MooDatabase) -> None:
        headerMatch = self._read_and_match(connectionCountRe, "Bad active connections header line")
        count = int(headerMatch.group("count"))
        # Store the listener_tag string directly (e.g., " with listeners" or "")
        db.connections_with_listeners = headerMatch.group("listener_tag")
        # Store connection lines for roundtrip
        for _ in range(count):
            db.connections.append(self.readString())

    def tryReadConnections(self, db: MooDatabase) -> bool:
        """Try to read connections section if present. Returns True if found."""
        line = self.file.readline()
        if not line:  # EOF
            logger.debug("No connections section (EOF)")
            return False
        self.line += 1
        line = line.rstrip("\r\n")
        match = connectionCountRe.match(line)
        if not match:
            logger.debug(f"No connections section (line was: {line!r})")
            return False
        count = int(match.group("count"))
        logger.debug(f"Found connections section with {count} connections")
        db.has_connections_section = True
        db.connections_with_listeners = match.group("listener_tag")
        for _ in range(count):
            db.connections.append(self.readString())
        return True

    def readVerbs(self, db: MooDatabase) -> None:
        logger.debug(f"Reading {db.total_verbs} verbs")
        self._read_and_process_items(db, db.total_verbs, self.readVerb)
        logger.debug(f"Finished reading {db.total_verbs} verbs")

    def readVerb(self, db: MooDatabase) -> None:
        objNumber, verbNumber, code = self.read_program()
        obj = db.objects.get(objNumber)
        if not obj:
            self.parse_error(f"object {objNumber} not found")
        self.attach_program(obj, verbNumber, code)

    def read_program(self) -> tuple[int, int, list[str]]:
        """One verb program: the object it is on, the verb's index there, and its code."""
        verbLocation = self.readString()
        if ":" not in verbLocation:
            self.parse_error("verb does not have seperator")

        sep = verbLocation.index(":")
        return int(verbLocation[1:sep]), int(verbLocation[sep + 1:]), self.readCode()

    def attach_program(self, obj: MooObject, verbNumber: int, code: list[str]) -> None:
        verb = obj.verbs[verbNumber]
        if not verb:
            self.parse_error(f"verb ${verbNumber} not found on object ${obj.id}")

        verb.object = obj.id
        verb.code = code

    def readCode(self) -> list[str]:
        code = []
        lastLine = self.readString()
        while lastLine != ".":
            code.append(lastLine)
            lastLine = self.readString()
        return code

    def readPlayers(self, db: MooDatabase) -> None:
        db.total_players = self.readInt()
        logger.debug(f"Reading {db.total_players} players")
        self._read_and_process_items(db, db.total_players, lambda _: db.players.append(self.readObjnum()))
        assert db.total_players == len(db.players)
        logger.debug(f"Finished reading {db.total_players} players")

    def readAnonObjects(self, db: MooDatabase) -> None:
        while True:
            num_anon = self.readInt()
            if num_anon == 0:
                break
            if num_anon > 0:
                for i in range(num_anon):
                    obj = self.readObject_ng(db)
                    obj.anon = True
                    db.objects[obj.id] = obj
        for o in db.objects.values():
            if o.anon:
                self.process_propnames(db, o)

    def readObjects(self, db: MooDatabase) -> None:
        db.objects = {}
        reader = (
            self.readObject_v4
            if db.version < DBVersions.DBV_NextGen
            else self.readObject_ng
        )
        for _ in range(db.total_objects):
            obj = reader(db)
            if not obj:
                continue
            db.objects[obj.id] = obj
        for o in db.objects.values():
            self.process_propnames(db, o)

    def process_propnames(self, db: MooDatabase, obj: MooObject) -> None:
        # Slots are the object's own propdefs, then each ancestor's in db_ancestors() order
        # (db_properties.cc db_find_property), which covers multiple inheritance too.
        names = [p.propertyName for a in db.ancestors(obj) for p in a.properties[: a.propdefs_count]]
        for i, p in enumerate(obj.properties, 1):
            n = names[i - 1] if i <= len(names) else i
            if not p.propertyName:
                p.propertyName = n
            elif n != p.propertyName:
                self.parse_error(f"property name mismatch: {n} != {p.propertyName}")

    def readVerbMetadata(self, obj: MooObject) -> None:
        name = self.readString()
        owner = self.readObjnum()
        perms = self.readInt()
        preps = self.readInt()
        verb = Verb(name, owner, perms, preps, obj.id)
        obj.verbs.append(verb)

    def readProperties(self, db: MooDatabase, obj: MooObject):
        logger.debug(f"Reading properties for #{obj.id} {obj.name!r}")
        # propdefs_count = properties DEFINED on this object (with names)
        propdefs_count = self.readInt()
        obj.propdefs_count = propdefs_count
        logger.debug(f"  propdefs_count = {propdefs_count}")
        debug = self.debug
        propertyNames = []
        for i in range(propdefs_count):
            name = self.readString()
            propertyNames.append(name)
            if debug:
                logger.debug(f"  propdef[{i}] name = {name!r}")
        # nval = total property VALUES (defined + inherited)
        nval = self.readInt()
        logger.debug(f"  total properties (nval) = {nval}")
        for idx in range(nval):
            propertyName = propertyNames[idx] if idx < propdefs_count else None
            if debug:
                logger.debug(f"  property[{idx}] name = {propertyName!r}")
            value = self.readValue(db)
            owner = self.readObjnum()
            perms = PropertyFlags(self.readInt())
            if debug:
                logger.debug(f"    owner=#{owner}, perms={perms}")
            property = Property(propertyName, value, owner, perms)
            obj.properties.append(property)

    def readPending(self, db: MooDatabase) -> None:
        valueMatch = self._read_and_match(pendingValueRe, "Bad pending finalizations")
        finalizationCount = int(valueMatch.group("count"))
        for _ in range(finalizationCount):
            db.pending_values.append(self.readValue(db))

    def readClocks(self, db: MooDatabase) -> None:
        clockMatch = self._read_and_match(clockCountRe, "Could not find clock definitions")
        numClocks = int(clockMatch.group("count"))
        self._read_and_process_items(db, numClocks, self.readClock)

    def readClock(self, db: MooDatabase) -> None:
        """Obsolete"""
        db.clocks.append(self.readString())


    def readTaskQueue(self, db: MooDatabase) -> None:
        queuedTasksMatch = self._read_and_match(taskCountRe, "Could not find task queue")
        numTasks = int(queuedTasksMatch.group("count"))
        logger.debug(f"Reading {numTasks} queued tasks")
        self._read_and_process_items(db, numTasks, self.readQueuedTask)
        assert numTasks == len(db.queuedTasks)
        logger.debug(f"Finished reading {numTasks} queued tasks")

    def readQueuedTask(self, db: MooDatabase) -> None:
        headerMatch = self._read_and_match(taskHeaderRe, "Could not find task header")
        unused = int(headerMatch[1])
        firstLineno = int(headerMatch[2])
        st = int(headerMatch[3])
        id = int(headerMatch[4])
        task = QueuedTask(firstLineno, id, st)
        task.activation = self.read_activation_as_pi(db)
        task.rtEnv = self.readRTEnv(db)
        task.code = self.readCode()
        task.unused = unused
        db.queuedTasks.append(task)

    def read_activation_as_pi(self, db: MooDatabase) -> Activation:
        # Read pre-header values (preserve with type info for round-trip)
        temp_value = self.readValue(db)
        temp_this = None
        temp_vloc = None
        if db.version >= DBVersions.DBV_This:
            temp_this = self.readValue(db)
        if db.version >= DBVersions.DBV_Anon:
            temp_vloc = self.readValue(db)
        if db.version >= DBVersions.DBV_Threaded:
            threaded = self.readInt()
        else:
            threaded = 0

        headerLine = self.readString()
        headerMatch = activationHeaderRe.match(headerLine)
        if not headerMatch:  # or headerMatch.length !== 6) {
            self.parse_error("Could not find activation header")

        activation = Activation()
        # Store pre-header values for round-trip
        activation.temp_value = temp_value
        activation.temp_this = temp_this
        activation.temp_vloc = temp_vloc
        # Parse header values
        activation.this = int(headerMatch[1])
        activation.unused1 = int(headerMatch[2])
        activation.threaded = threaded
        activation.unused2 = int(headerMatch[3])
        activation.player = int(headerMatch[4])
        activation.unused3 = int(headerMatch[5])
        activation.programmer = int(headerMatch[6])
        activation.vloc = int(headerMatch[7])
        activation.unused4 = int(headerMatch[8])
        activation.debug = int(headerMatch[9])
        activation.argstr = self.readString()
        activation.dobjstr = self.readString()
        activation.prepstr = self.readString()
        activation.iobjstr = self.readString()
        activation.verb = self.readString()
        activation.verbname = self.readString()
        return activation

    def read_activation(self, db: MooDatabase) -> Activation:
        language_version = db.version
        if db.version < DBVersions.DBV_Float:
            pass
        else:
            langver = self.readString()
            if not (langverMatch := langverRe.match(langver)):
                self.parse_error(f"Bad language version header {langver}")
            language_version = int(langverMatch.group("version"))

        code = self.readCode()
        rt = self.readRTEnv(db)
        stackheader = self.readString()
        stackheaderMatch = stackheaderRe.match(stackheader)
        if not stackheaderMatch:
            self.parse_error("READ_ACTIV: bad stack header")
        stack = []
        for _ in range(int(stackheaderMatch.group("slots"))):
            _s = self.readValue(db)
            stack.append(_s)
        activation = self.read_activation_as_pi(db)
        activation.language_version = language_version
        activation.stack = stack
        activation.code = code
        activation.rtEnv = rt  # Store runtime environment
        temp_end = self.readValue(db)
        activation.temp_end = temp_end  # Store trailing temp value
        pchead = self.readString()
        if not (pcMatch := pcRe.match(pchead)):
            self.parse_error("READ_ACTIV: bad pc")
        activation.pc = int(pcMatch.group("pc"))
        activation.bi_func = int(pcMatch.group("bi_func"))
        activation.error = int(pcMatch.group("error"))
        if activation.bi_func:
            activation.bi_func_name = self.readString()
        return activation

    def readRTEnv(self, db: MooDatabase) -> dict[str, Any]:
        varCountMatch = self._read_and_match(varCountRe, "Could not find variable count for RT Env")
        varCount = int(varCountMatch.group("count"))
        logger.debug(f"Reading RTEnv with {varCount} variables")
        rtEnv = {}
        for _ in range(varCount):
            name = self.readString()
            value = self.readValue(db)
            rtEnv[name] = value
        return rtEnv

    def readSuspendedTasks(self, db: MooDatabase) -> None:
        suspendedMatch = self._read_and_match(suspendedTaskCountRe, "Bad suspended tasks header")
        count = int(suspendedMatch.group("count"))
        self._read_and_process_items(db, count, self.readSuspendedTask)

    def readSuspendedTask(self, db: MooDatabase) -> None:
        taskMatch = self._read_and_match(suspendedTaskHeaderRe, "Bad suspended task header")
        id = int(taskMatch.group("id"))
        startTime = int(taskMatch.group("startTime"))
        task = SuspendedTask(0, id, startTime)  # Set line number to 0 for a suspended task since we don't know it (only opcodes, not text)
        if val := taskMatch.group("value"):
            task.value = self.readValue(db, known_type=int(val))
        task.vm = self.readVM(db)
        db.suspendedTasks.append(task)

    def readInterruptedTasks(self, db: MooDatabase):
        interruptedMatch = self._read_and_match(interruptedTaskCountRe, "Bad suspended tasks header")
        count = int(interruptedMatch.group("count"))
        self._read_and_process_items(db, count, self.readInterruptedTask)

    def readInterruptedTask(self, db: MooDatabase) -> None:
        headerMatch = self._read_and_match(interruptedTaskHeaderRe, "Bad interrupted tasks header")
        task_id = int(headerMatch.group("id"))
        status = headerMatch.group("status")
        task = InterruptedTask(task_id, status)
        task.vm = self.readVM(db)
        db.interruptedTasks.append(task)

    def readVM(self, db: MooDatabase) -> VM:
        if db.version >= DBVersions.DBV_TaskLocal:
            local = self.readValue(db)
        else:
            local = {}
        header = self.readString()
        headerMatch = vmHeaderRe.match(header)
        if not headerMatch:
            self.parse_error(f"Bad VM Header {header}")
        top = int(headerMatch.group("top"))
        vector = int(headerMatch.group("vector"))
        funcId = int(headerMatch.group("funcId"))
        maxStackframes = int(headerMatch.group("maxStackframes"))
        stack = []
        for _ in range(top + 1):
            stack.append(self.read_activation(db))
        return VM(local, stack, top, vector, funcId, maxStackframes)

    def _read_and_match(self, pattern, error_message):
        line = self.readString()
        match = pattern.match(line)
        if not match:
            self.parse_error(error_message)
        return match

    def _read_and_process_items(self, db: MooDatabase, count: int, process_function):
        for _ in range(count):
            process_function(db)

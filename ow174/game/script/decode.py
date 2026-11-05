"""Reads body statescript chunks back the way the client does (the readers named in
ow174/game/statescript.py), for tests and logs: the frame header, the instance list, the variables with
their bindings, the states with their payloads and the owner event lists. Every payload is read by the
client's codec table (ow174/game/script/codecs.py, data/statescript_codecs_174.json), not by the encoder's
writers, so an encoder that writes a class differently from the client shows here.

`read_chunk(data, bits, known)` takes a chunk (statescript.chunk) and the client's instance table
(instance id -> graph index); a full frame's descriptors add to it. Like the client it reads every list by
the graph of the instance it has under that id, and it raises ClientCrash where the client would crash:
- a variable is found by (instance id, variable id) (0x7FF78AAB3150): there is none for id 0 (an
  m_syncVars entry without an identifier) or for an instance the table does not have, and the client uses
  it anyway: an own-instance binding reads its bag (0x7FF78991D6DB), 0x7FF78AAB78A0 stores the value;
- a listed instance it does not have (0x7FF78991CA20), a binding or an event list naming one
  (0x7FF78991D6E5, 0x7FF78991BA30).
`ClientBody` keeps that table from chunk to chunk as the client does.
"""

import struct
from dataclasses import dataclass, field

from ow174.game.bits import BitReader
from ow174.game.script import codecs, expr
from ow174.game.script.codecs import f32, w_u16, w_var
from ow174.game.script.graph import bodies
from ow174.game.script.graph import graph as graph_of

STEC_CLASSES = codecs.stec_classes()


class DecodeError(ValueError):
    pass


class ClientCrash(DecodeError):
    """The client would crash reading this chunk. `pos` is the bit in the frame payload."""

    def __init__(self, pos: int, where: str, what: str) -> None:
        super().__init__(f"the client crashes at payload bit {pos} ({where}): {what}")
        self.pos = pos
        self.where = where
        self.what = what


class FrameRefused(DecodeError):
    """The client would refuse this chunk (it rolls it back and tries it again every tick)."""


def _pos(reader: BitReader) -> int:
    return reader.pos - getattr(reader, "base", 0)


def w_u32(reader: BitReader) -> int:
    """0+16, 10+24, 11+32 (0x7FF789C63000)."""
    if not reader.bit():
        return reader.bits(16)
    if not reader.bit():
        return reader.bits(24)
    return reader.bits(32)


def signed_u16(reader: BitReader) -> int:
    negative = reader.bit()
    value = w_u16(reader)
    return -value if negative else value


def count(reader: BitReader) -> int:
    if not reader.bit():
        return 0
    if not reader.bit():
        return 1
    if not reader.bit():
        return 2
    return w_u16(reader)


def mask(reader: BitReader, size: int) -> set[int]:
    if size <= 8:
        return {index for index in range(size) if reader.bit()}
    groups = ((size - 1) >> 3) + 1
    present = [reader.bit() for _ in range(groups)]
    chosen = set()
    for g in range(groups):
        if present[g]:
            for index in range(8 * g, min(size, 8 * g + 8)):
                if reader.bit():
                    chosen.add(index)
    return chosen


def read_value(reader: BitReader):
    tag = reader.bits(4)
    if tag == 0:
        return bool(reader.bit())
    if tag == 1:
        return reader.bits(4)
    if tag in (2, 3, 4):
        return reader.signed({2: 8, 3: 16, 4: 32}[tag])
    if tag == 5:
        return float(reader.bits(4))
    if tag == 6:
        return float(reader.signed(8))
    if tag == 7:
        return struct.unpack("<e", reader.bits(16).to_bytes(2, "little"))[0]
    if tag == 8:
        return f32(reader)
    if tag == 9:
        if not reader.bit():
            raise DecodeError("asset mask form")
        high = reader.bits(16)
        return expr.Asset(high << 48 | w_var(reader) if high else 0)
    if tag == 12:
        present = [reader.bit() for _ in range(3)]
        return expr.Vec3(*(f32(reader) if flag else 0.0 for flag in present))
    if tag == 13:
        return expr.Entity(reader.bits(32) if reader.bit() else 0)
    if tag == 14:
        kind = reader.bits(3)
        return expr.Handle(kind, w_var(reader), w_u16(reader), w_u16(reader))
    if tag == 15:
        sub = reader.bits(3)
        if sub == 3:
            return None
        if sub == 2:
            data = bytearray()
            while (byte := reader.bits(8)) != 0:
                data.append(byte)
            return data.decode("utf-8")
        if sub == 6:
            size = w_u16(reader)
            if reader.bit():
                raise DecodeError("keyed array")
            if size and reader.bit():
                raise DecodeError("untagged array")
            return tuple(read_value(reader) for _ in range(size))
    raise DecodeError(f"value tag {tag}")


@dataclass(frozen=True)
class DecodedBinding:
    instance: int
    state: int
    slot: int
    priority: float
    above: bool


def read_bindings(reader: BitReader, own: int, graphs: dict[int, int], unresolved: str | None = None):
    """A binding list (0x7FF78991D4F0) of a variable of instance `own` (0: the entity); `unresolved` names
    the variable when the client has none (0x7FF78AAB3150 gave it nothing)."""
    found = []
    for _ in range(count(reader)):
        for _ in range(count(reader)):
            if reader.bit():
                reader.bits(16)
            else:
                w_u16(reader)
        pos = _pos(reader)
        if reader.bit():
            instance = signed_u16(reader)
            if instance not in graphs:
                raise ClientCrash(pos, "0x7FF78991D6E5", f"a binding names instance {instance}, it has none")
        elif unresolved:  # the own-instance form reads the variable's bag: [var + 0x90]
            raise ClientCrash(pos, "0x7FF78991D6DB", f"an own-instance binding of {unresolved}")
        elif own == 0:  # the entity's bag has no instance: [0 + 0x1F8]
            raise ClientCrash(pos, "0x7FF78991D6EA", "an own-instance binding of an entity variable")
        else:
            instance = own
        state = reader.bits(graph_of(graphs[instance]).states_bits)
        slot = w_var(reader) if reader.bit() else 0
        priority, above = 0.0, True
        if reader.bit():
            if reader.bit():
                negative = reader.bit()
                priority = -float(w_var(reader)) if negative else float(w_var(reader))
            else:
                priority = f32(reader)
            above = bool(reader.bit())
        if reader.bit():
            reader.bit()
        found.append(DecodedBinding(instance, state, slot, priority, above))
    return tuple(found)


def read_var(reader: BitReader, own: int, graphs: dict[int, int], var: int | None = None):
    """One variable (0x7FF78991D2C0) of instance `own` (0: the entity), the client's id `var` (None: not
    checked). The client resolves it by (instance id, low 16 bits of the id), 0x7FF78AAB3150."""
    start = _pos(reader)
    unresolved = None
    if var is not None and (var & 0xFFFF == 0 or (own and own not in graphs)):
        unresolved = f"variable #{var}" if var & 0xFFFF else "an id-less variable"
        unresolved += f" of instance {own}"
    value = read_value(reader)
    if unresolved:
        unresolved += f" (from bit {start}, value {value!r}), which does not resolve"
    bindings = read_bindings(reader, own, graphs, unresolved)
    if unresolved:
        raise ClientCrash(start, "0x7FF78AAB78A0", unresolved)
    return value, bindings


def _link(reader: BitReader, owner: bool, graphs: dict | None):
    """The linked-state field (0x7FF78AA837D0): a bit; with it the linked instance's id and its state index,
    in that graph's width (m_statesBitCount in an owner frame, else a remote-sync index). The client looks the
    instance up and reads its asset at once: an id it does not have crashes it (0x7FF78AA8385A)."""
    if not reader.bit():
        return None
    pos = _pos(reader)
    instance = w_u16(reader)
    if not graphs or instance not in graphs:
        raise ClientCrash(pos, "0x7FF78AA8385A", f"a linked state names instance {instance}, it has none")
    linked = graph_of(graphs[instance])
    if owner:
        return instance, reader.bits(linked.states_bits)
    node = linked.remote_node(reader.bits(linked.remote_bits))
    return instance, node.state if node is not None else None


def read_payload(
    reader: BitReader,
    cls: str,
    frame_time: int,
    frame_ms: int,
    owner: bool = True,
    full: bool = False,
    graphs=None,
) -> dict:
    """A state's payload as the client reads it: by the codec table (its property block, then its class
    reader), shown as the values the encoder takes (statescript.write_payload). `owner` = H, `full` = a full
    frame."""
    item = codecs.codec(cls)
    if item is None:
        raise DecodeError(f"no codec for the state class {cls}")
    if not item.verified:
        raise DecodeError(f"the payload of {cls} is not read to the end in the client code ({item.status})")
    fields = codecs.read(
        reader, item.layout, owner, full, link=lambda r: _link(r, owner, graphs), value=read_value
    )
    return _meaning(item.codec, fields, frame_time, frame_ms)


def _meaning(name: str, fields: dict, frame_time: int, frame_ms: int) -> dict:
    """The fields of a payload as the encoder's values: times relative to 16 * CmFD made absolute, optional
    parts filled with what the client uses for them."""
    get = fields.get
    out: dict = {}
    if get("link"):
        out["link"] = fields["link"]
    if name == "switch":
        out["current"] = bool(fields["current"])
    elif name == "stack":
        if "top" in fields:
            out.update(top=bool(fields["top"]), under=bool(fields["under"]))
    elif name in ("button", "counter2", "frames", "subscript"):
        out.update({key: fields[key] for key in ("counter", "target", "child") if key in fields})
    elif name == "volley":
        out.update(offset=get("offset", 0), volleys=get("volleys", 1), counter=fields["counter"])
        if "t1" in fields:
            out["start"] = frame_time - fields["t1"]
        if "subindex" in fields:
            out["subindex"] = fields["subindex"]
    elif name == "anim":
        out["counter"] = fields["counter"]
        if get("has_time"):
            out["time"] = get("time", 0)
    elif name == "chase":
        cur = expr.Vec3(fields["x"], fields["y"], fields["z"]) if fields["vector"] else fields["x"]
        last = frame_time + frame_ms - fields["t"] if get("has_last") else None
        out.update(cur=cur, reached=bool(fields["reached"]), remaining=get("remaining"), last=last)
    elif name == "ability":
        out.update(flags=fields["flags"], cur=get("cur", 0.0), rate=get("rate", 1.0))
        if get("has_cur"):
            out["last"] = frame_time + frame_ms - fields["t"]
    elif name == "targets":
        if get("has_list"):
            out["targets"] = tuple((item["entity"], item["age"]) for item in fields["targets"])
    elif name == "message":
        sender = None
        if get("has_sender"):
            sender = (0xA0000000 if fields["sender_kind"] else 0x80000000) | fields["sender"]
        out.update(sender=sender, stacked=bool(fields["stacked"]))
    elif name == "send":
        if get("has_reply"):
            out["reply"] = fields["reply"]
    elif name == "hits":
        hits = tuple(
            (item["value"], item["entity"], item.get("extra", 0), item["w"], bool(item["a"]), bool(item["b"]))
            for item in fields["hits"]
        )
        out.update(hits=hits, flag=bool(fields["flag"]))
    elif name == "flags13":
        out["value"] = fields["low"] | get("high", 0) << 6
    elif name == "pulser":
        out.update(fresh=bool(fields["fresh"]), count=fields["count"])
    elif name not in ("none", "link"):
        out.update({key: value for key, value in fields.items() if key != "link"})
    return out


@dataclass
class DecodedInstance:
    id: int
    graph: int = 0
    parent: tuple[int, int] | None = None
    gone: bool = False
    descriptor: bool = False
    vars: dict = field(default_factory=dict)
    states: dict = field(default_factory=dict)  # m_states index -> (active, payload)
    events: list | None = None  # [(time, state, finish, param)]


@dataclass
class Frame:
    first: int
    last: int
    owner: bool
    correction: bool = False
    cmfd: int = 0
    instances: dict[int, DecodedInstance] = field(default_factory=dict)
    entity_vars: dict = field(default_factory=dict)

    @property
    def full(self) -> bool:
        return self.first == 0


def _descriptor(reader: BitReader, item: DecodedInstance) -> None:
    item.graph = reader.bits(16)
    if reader.bit():
        reader.entity_id()
    reader.bit()
    if reader.bit():
        item.parent = (w_var(reader), w_var(reader))
    if reader.bit():
        raise DecodeError("descriptor references")


def _entity_vars(reader: BitReader, frame: Frame, graphs: dict) -> None:
    if not reader.bit():
        return
    while var := w_var(reader):
        frame.entity_vars[var] = read_var(reader, 0, graphs, var)


def _instance_vars(reader: BitReader, item: DecodedInstance, graphs: dict, full: bool) -> None:
    """The per-instance variables by the graph the client has under this id: in a full frame a presence
    bit per entry with an id (0x7FF78991C270), in a delta a mask over every m_syncVars entry
    (0x7FF78991CA20: an entry without an id gives variable id 0)."""
    graph = graph_of(item.graph)
    if full:
        for entry in graph.presence_vars():
            if reader.bit():
                item.vars[entry.var] = read_var(reader, item.id, graphs, entry.var)
    else:
        for index in sorted(mask(reader, len(graph.sync_vars))):
            entry = graph.sync_vars[index]
            item.vars[entry.var] = read_var(reader, item.id, graphs, entry.var or 0)
    if reader.bit():
        while var := w_var(reader):
            item.vars[var] = read_var(reader, item.id, graphs, var)


def _owner_states(
    reader: BitReader, item: DecodedInstance, frame: Frame, frame_ms: int, graphs: dict
) -> None:
    graph = graph_of(item.graph)
    frame_time = frame.cmfd * frame_ms

    def payload(cls: str) -> dict:
        return read_payload(reader, cls, frame_time, frame_ms, owner=True, full=frame.full, graphs=graphs)

    if frame.full:
        for node in graph.owner_states():
            if node.cls in STEC_CLASSES:
                if reader.bit():
                    active = bool(reader.bit())
                    item.states[node.state] = (active, payload(node.cls))
            elif reader.bit():
                item.states[node.state] = (True, payload(node.cls))
            else:
                item.states[node.state] = (False, {})
        return
    for index in sorted(mask(reader, len(graph.states))):
        node = graph.states[index]
        if node.cls in STEC_CLASSES:
            if not reader.bit():
                continue
            active = bool(reader.bit())
            item.states[index] = (active, payload(node.cls))
        elif reader.bit():
            item.states[index] = (True, payload(node.cls))
        else:
            item.states[index] = (False, {})


def _remote_states(
    reader: BitReader, item: DecodedInstance, frame: Frame, frame_ms: int, graphs: dict
) -> None:
    """The states of an H = 0 frame (0x7FF78991BE30 / 0x7FF78991C620): a full frame walks the networked
    m_remoteSyncNodes with X (an StEC class then Y), a delta has a mask over the raw list; then StAc (StEC)
    or A, and the payload with H = 0. States are keyed by their m_states index."""
    graph = graph_of(item.graph)
    networked = graph.remote_states()

    def payload(cls: str) -> dict:
        return read_payload(reader, cls, 0, frame_ms, owner=False, full=frame.full, graphs=graphs)

    if frame.full:
        for node in networked:
            if not reader.bit():  # X = 0: off
                item.states[node.state] = (False, {})
                continue
            if node.cls in STEC_CLASSES:
                if reader.bit():  # Y
                    active = bool(reader.bit())
                    item.states[node.state] = (active, payload(node.cls))
            elif reader.bit():
                item.states[node.state] = (True, payload(node.cls))
            else:
                item.states[node.state] = (False, {})
        return
    for index in sorted(mask(reader, len(graph.remote_nodes))):
        node = graph.remote_nodes[index]
        if node is None or node not in networked:
            raise DecodeError(f"instance {item.id}: an H=0 delta sets remote index {index}, it has no bits")
        if node.cls in STEC_CLASSES:
            if not reader.bit():  # X
                continue
            active = bool(reader.bit())
            item.states[node.state] = (active, payload(node.cls))
        elif reader.bit():
            item.states[node.state] = (True, payload(node.cls))
        else:
            item.states[node.state] = (False, {})


def _events(reader: BitReader, item: DecodedInstance, frame: Frame, frame_ms: int) -> None:
    states_bits = graph_of(item.graph).states_bits
    time = frame.cmfd * frame_ms
    events = []
    while (code := reader.bits(2)) != 3:
        param = 1
        if code == 1:
            param = 0 if reader.bit() else w_u16(reader)
        state = reader.bits(states_bits)
        if not reader.bit():
            step = w_var(reader) if reader.bit() else -w_var(reader)
            time += step
        events.append((time, state, code == 2, param))
    item.events = events


def read_chunk(
    data: bytes, bits: int, known: dict[int, int] | None = None, frame_ms: int = 16, records=None
) -> Frame:
    """A chunk as statescript.chunk() wrote it (bytes and its length in bits). `known`: the client's
    instance table (id -> graph index); `records` (optional): the ids it has a network record of, the only
    ones a delta may name without a descriptor (else it refuses the frame, 0x7FF78991C510)."""
    try:
        return _read_chunk(data, bits, known, frame_ms, records)
    except EOFError as error:
        raise DecodeError(f"the frame ends inside its body ({error})") from error


def _read_chunk(data: bytes, bits: int, known, frame_ms: int, records) -> Frame:
    reader = BitReader(data)
    first = w_u32(reader)
    span = w_var(reader)
    if reader.bit():
        raise DecodeError("fragments")
    size = w_var(reader)
    if reader.pos + size > bits:
        raise DecodeError("the payload runs past the chunk")
    reader.base = reader.pos
    reader.bit()  # lead
    frame = Frame(first, first + span, bool(reader.bit()))
    lbss = None
    if frame.owner:
        frame.correction = bool(reader.bit())
        frame.cmfd = w_var(reader)
        if not frame.full:
            lbss = w_var(reader)  # the body's length: a stale sub-frame is skipped by it
            lbss += reader.pos
    graphs = dict(known or {})
    order = []
    index = 0
    while step := w_u16(reader):
        index += step
        item = DecodedInstance(index, graphs.get(index, 0))
        if frame.full:
            item.descriptor = True
            _descriptor(reader, item)
        elif reader.bit():
            item.gone = True
        elif reader.bit():
            item.descriptor = True
            _descriptor(reader, item)
        elif records is not None and index not in records:
            raise FrameRefused(f"instance {index} is named without a descriptor and has no record")
        frame.instances[index] = item
        order.append(item)
    # Before the body the dispatcher makes the new instances and destroys the gone ones (0x7FF78991EE10).
    for item in order:
        if item.gone:
            graphs.pop(item.id, None)
        elif item.descriptor:
            graphs[item.id] = item.graph
    _entity_vars(reader, frame, graphs)
    for item in order:
        if item.gone:
            continue
        if item.id not in graphs:
            raise ClientCrash(_pos(reader), "0x7FF78991CA20", f"instance {item.id} is listed, it has none")
        _instance_vars(reader, item, graphs, frame.full)
        if frame.owner:
            _owner_states(reader, item, frame, frame_ms, graphs)
            if frame.full:
                _events(reader, item, frame, frame_ms)
        else:
            _remote_states(reader, item, frame, frame_ms, graphs)
    if frame.owner and not frame.full:
        if reader.bit():
            current = 0
            while step := signed_u16(reader):
                current += step
                if current not in graphs:
                    raise ClientCrash(_pos(reader), "0x7FF78991BA30", f"events of instance {current}: none")
                item = frame.instances.get(current) or DecodedInstance(current, graphs.get(current, 0))
                frame.instances[current] = item
                _events(reader, item, frame, frame_ms)
        if reader.pos != lbss:
            raise DecodeError(f"LBSS says the body ends at {lbss}, it ends at {reader.pos}")
        if reader.bit():
            raise DecodeError("more sub-frames")
    if reader.bits(2):
        raise DecodeError("frame events")
    return frame


def initial_graphs(hero: int) -> list:
    """The graphs the client builds for a hero's body itself (its statescript component's initial graphs,
    in definition order; None for an empty entry)."""
    record = next(item for item in bodies().values() if int(item["hero"], 16) == hero)
    return [item.get("graph") for item in record["graphs"]]


class ClientBody:
    """A body's statescript as the client keeps it from chunk to chunk: its instance table, the network
    records, and the states and variables the frames set.

    The client builds the body's initial graphs itself when it gets the create (0x7FF789AF9DE0; the
    networked create, component vt+0x78, gives each the highest id + 1: 1..N in definition order). A full
    frame records every instance it lists, keeps an instance of the same id, graph and parent (P-id reuse,
    0x7FF78991F200) and makes the others, then destroys the networked instances it did not list; a delta
    makes the instances it gives a descriptor and destroys the gone ones. Every chunk is read by this
    table (read_chunk), so one that would crash the client raises ClientCrash."""

    def __init__(self, initial=()) -> None:
        self.instances: dict[int, int] = {}  # id -> graph index
        for index in initial:
            if index is not None:  # an empty entry makes nothing (and takes no id)
                self.instances[max(self.instances, default=0) + 1] = index
        self.records: dict[int, tuple] = {}  # id -> (graph index, parent)
        self.states: dict[tuple[int, int], bool] = {}
        self.vars: dict[tuple[int, int], object] = {}
        self.applied = 0

    @classmethod
    def for_hero(cls, hero: int) -> "ClientBody":
        return cls(initial_graphs(hero))

    @property
    def graphs(self) -> dict[int, int]:
        return self.instances

    def apply(self, update) -> Frame:
        """Read one body chunk (a world.EntityUpdate) as the client and apply it."""
        frame = read_chunk(update.chunk.getvalue(), update.chunk.count, self.instances, records=self.records)
        if frame.full:  # it nulls what the network set, and only remote-synced states can stay on (H = 0)
            self.records = {number: (item.graph, item.parent) for number, item in frame.instances.items()}
            self.instances = {key: value for key, value in self.instances.items() if key in self.records}
            self.states = {}
            self.vars = {}
        for number, item in frame.instances.items():
            if item.gone:
                self.instances.pop(number, None)
                self.records.pop(number, None)
                self.states = {key: on for key, on in self.states.items() if key[0] != number}
                self.vars = {key: value for key, value in self.vars.items() if key[0] != number}
                continue
            if item.descriptor:
                self.instances[number] = item.graph
                self.records[number] = (item.graph, item.parent)
            for index, (active, _) in item.states.items():
                self.states[(number, index)] = active
            for var, (value, _) in item.vars.items():
                self.vars[(number, var)] = value
        for var, (value, _) in frame.entity_vars.items():
            self.vars[(0, var)] = value
        self.applied = max(self.applied, frame.last)
        return frame

    def on(self) -> set:
        return {key for key, active in self.states.items() if active}

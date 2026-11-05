"""Observed native body-script replay, independent of Match, combat decisions and UDP delivery.

Recording helpers only read stored runtime data: no value evaluation, payload methods, RNG draws or
shot consumption. Replay consumes shots after validating each command, like Combat.command does.
"""

import dataclasses
import hashlib
import json
import math
from functools import cache

from ow174.game.commands import FRAME_HALF, FRAME_MASK, Command, frame_distance
from ow174.game.content import SOLDIER
from ow174.game.script import graph, runtime
from ow174.game.script.driver import HUD_MAX_HEALTH, OWNER_HEALTH, BodyScript
from ow174.paths import DATA_DIR, ROOT

SCHEMA = "ow174.native-body-replay.v2"
BUILD = "1.74.0.0.104319"
COMMAND_FIELDS = (
    "frame", "forward", "right", "yaw", "pitch", "buttons", "action",
    "extra", "subframe", "click_yaw", "click_pitch",
)
HEADER_FIELDS = {
    "schema", "build", "scope", "model_hash", "hero", "entity", "body_definition", "start_frame",
    "quanta_us", "mode", "health", "maximum_health", "ult_seconds", "seed", "command_epoch",
    "body_epoch", "server_tick", "gap_policy", "shot_policy", "initial_hash",
}
MODEL_FILES = (
    "ow174/game/native_replay.py", "ow174/game/commands.py", "ow174/game/pools.py",
    "ow174/game/script/driver.py", "ow174/game/script/runtime.py", "ow174/game/script/expr.py",
    "ow174/game/script/nodes.py", "ow174/game/script/graph.py", "ow174/game/script/rulesets.py",
    "ow174/game/script/codecs.py", "ow174/game/statescript.py",
)
MODEL_DATA = (
    "game_graphs_174.json.gz", "game_stats_174.json", "game_heroes_174.json",
    "statescript_codecs_174.json",
)


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _stored(value):
    """Canonical stored values with stable references, preserving float bits and ordered sequences."""
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Nonfinite runtime value cannot be hashed")
        return {"float": value.hex()}
    if isinstance(value, runtime.State):
        return {"state": [value.instance.id, value.index]}
    if isinstance(value, runtime.Instance):
        return {"instance": value.id}
    if isinstance(value, runtime.Var):
        return {"var": list(value.key)}
    if isinstance(value, graph.Node):
        return {"node": value.pos, "class": value.cls}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, (tuple, list)):
        return [_stored(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return {"set": sorted((_stored(item) for item in value), key=_json)}
    if isinstance(value, dict):
        if all(type(key) is str for key in value):
            return {"map": [[key, _stored(value[key])] for key in sorted(value)]}
        if all(type(key) is int for key in value):
            return {"map": [[key, _stored(value[key])] for key in sorted(value)]}
        return {"map": sorted(([_stored(key), _stored(item)] for key, item in value.items()),
                              key=lambda pair: _json(pair[0]))}
    if dataclasses.is_dataclass(value):
        return {"type": type(value).__name__,
                "fields": {item.name: _stored(getattr(value, item.name))
                           for item in dataclasses.fields(value)}}
    raise ValueError(f"Unsupported stored runtime value: {type(value).__name__}")


def _variables(values: dict, encode=_stored) -> list:
    return [
        [identifier, encode(var.base), encode(var.links), encode(list(var.watchers))]
        for identifier, var in sorted(values.items())
    ]


def _snapshot(script: BodyScript, encode) -> dict:
    """Read key execution state without evaluating a variable, state payload or graph callback."""
    component = script.component
    instances = []
    for instance in component.instances.values():
        states = []
        for state in sorted(instance.states.values(), key=lambda item: item.index):
            if state.active or state.counter:
                fields = {name: (_stored(value) if isinstance(value, dict)
                                 and any(type(key) not in (str, int) for key in value) else value)
                          for name, value in vars(state).items()
                          if name not in ("instance", "node")}
                states.append([state.index, state.node.cls, encode(fields)])
        instances.append({
            "id": instance.id, "graph": instance.graph.index,
            "parent": encode(instance.parent), "parent_state": instance.parent_state,
            "start": instance.start, "initialized": instance.initialized,
            "stopped": instance.stopped, "teardown": instance.teardown,
            # Resync, teardown and volley reporting traverse this lazy-allocation order.
            # Include even never-activated states: first access can change later causality.
            "state_order": list(instance.states),
            "vars": _variables(instance.vars, encode), "states": states,
            "queue": encode(instance.queue), "deferred": encode(instance.deferred),
            "ticking": encode(instance.ticking), "params": encode(instance.params),
        })
    return {
        "hero": script.hero, "entity": script.entity, "frame": script.frame,
        "frame_ms": script.frame_ms, "buttons": script.buttons, "retired": script.retired,
        "world": encode(vars(script.world)), "server_only": sorted(script.server_only),
        "ult_seconds": encode(script.ult_seconds), "instances": instances,
        "component": {
            "frame": component.frame, "now": component.now, "held": list(component.held),
            "button": component.button, "owner_dead": component.owner_dead,
            "next_id": component.next_id,
            "vars": _variables(component.vars, encode), "disabled": encode(component.disabled),
            "destroyed": encode(component.destroyed),
            "fault": str(component.fault) if component.fault else None,
            "rng": _digest(_stored(component.rng.getstate())),
        },
    }


def stored_snapshot(script: BodyScript) -> dict:
    return _snapshot(script, _stored)


def _runtime_json(value):
    # JSON's C encoder handles scalar state fields directly; Python visits only native references.
    # This avoids thousands of small JSON encodes per observed command without evaluating the VM.
    if isinstance(value, (runtime.State, runtime.Instance, runtime.Var, graph.Node, bytes, set, frozenset)):
        return _stored(value)
    if dataclasses.is_dataclass(value):
        return {"type": type(value).__name__,
                "fields": {item.name: getattr(value, item.name) for item in dataclasses.fields(value)}}
    raise ValueError(f"Unsupported stored runtime value: {type(value).__name__}")


def snapshot_hash(script: BodyScript) -> str:
    encoded = json.dumps(_snapshot(script, lambda value: value), default=_runtime_json,
                         sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def shot_summary(script: BodyScript) -> list[dict]:
    return [
        {"instance": state.instance.id, "graph": state.instance.graph.index, "state": state.index,
         "activation": state.counter, "shot": number, "time_ms": stamp}
        for state, number, stamp in script.component.shots
    ]


@cache
def model_hash() -> str:
    manifest = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in MODEL_FILES}
    manifest.update({f"data/{name}": hashlib.sha256((DATA_DIR / name).read_bytes()).hexdigest()
                     for name in MODEL_DATA})
    return _digest(manifest)


def make_header(
    script: BodyScript, *, quanta_us: int, command_epoch: int, body_epoch: int, server_tick: int,
) -> dict:
    """Call immediately after native BodyScript construction, before its first command."""
    if script.hero != SOLDIER:
        raise ValueError("This observed body baseline currently supports Soldier: 76 only")
    health = script.component.vars[OWNER_HEALTH].base
    maximum = script.component.vars[HUD_MAX_HEALTH].base
    body = next(key for key, item in graph.bodies().items() if item is script.record)
    return {
        "schema": SCHEMA, "build": BUILD, "scope": "body-script-only", "model_hash": model_hash(),
        "hero": script.hero, "entity": script.entity, "body_definition": body,
        "start_frame": script.frame, "quanta_us": quanta_us, "mode": script.world.mode,
        "health": health, "maximum_health": maximum, "ult_seconds": script.ult_seconds,
        "seed": 1, "command_epoch": command_epoch, "body_epoch": body_epoch, "server_tick": server_tick,
        "gap_policy": "reject", "shot_policy": "drain_after_command", "initial_hash": snapshot_hash(script),
    }


def make_command_event(
    script: BodyScript, command: Command, *, seq: int, server_tick: int, before_hash: str,
    command_epoch: int, body_epoch: int,
) -> dict:
    return {
        "kind": "command", "seq": seq, "server_tick": server_tick,
        "command_epoch": command_epoch, "body_epoch": body_epoch,
        "command": {name: getattr(command, name) for name in COMMAND_FIELDS},
        "before_hash": before_hash, "after_hash": snapshot_hash(script), "shots": shot_summary(script),
    }


def make_end_event(
    script: BodyScript, *, seq: int, server_tick: int, command_epoch: int, body_epoch: int,
    reason: str = "retired",
) -> dict:
    """Write before retire(), after the last command's combat consumer has drained its shots."""
    return {
        "kind": "end", "seq": seq, "server_tick": server_tick,
        "command_epoch": command_epoch, "body_epoch": body_epoch,
        "final_hash": snapshot_hash(script), "reason": reason,
    }


def _int(value, name: str, low: int = 0, high: int | None = None) -> int:
    if type(value) is not int or value < low or (high is not None and value > high):
        raise ValueError(f"Invalid {name}")
    return value


def _command(data: dict) -> Command:
    if not isinstance(data, dict) or set(data) != set(COMMAND_FIELDS):
        raise ValueError("Native v2 command needs exactly all eleven fields")
    ranges = {"frame": (0, FRAME_MASK), "forward": (-128, 127), "right": (-128, 127),
              "buttons": (0, 0xFFFFFF), "action": (0, 255), "extra": (0, 0xFFFF), "subframe": (0, 255)}
    for name, (low, high) in ranges.items():
        _int(data[name], name, low, high)
    for name in ("yaw", "pitch"):
        if type(data[name]) is not int:
            raise ValueError(f"Invalid {name}")
    for name in ("click_yaw", "click_pitch"):
        if data[name] is not None:
            _int(data[name], name, -32768, 32767)
    if (data["click_yaw"] is None) != (data["click_pitch"] is None):
        raise ValueError("Click angles must both be present or both absent")
    return Command(**data)


def parse_trace(text: str) -> dict:
    """Read a document or header-first JSONL; a damaged stream tail becomes an explicit fault."""
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            raise ValueError("Empty native trace") from None
        first = json.loads(lines[0])
        if not isinstance(first, dict) or set(first) != {"header"}:
            raise ValueError("JSONL trace must begin with a header wrapper") from None
        document = {"header": first["header"], "events": []}
        for number, line in enumerate(lines[1:], 2):
            try:
                document["events"].append(json.loads(line))
            except json.JSONDecodeError:
                document["events"].append({
                    "kind": "fault", "error": f"Invalid/truncated JSONL line {number}",
                })
                break
    if isinstance(document, dict) and set(document) == {"header"}:
        return {"header": document["header"], "events": []}
    if not isinstance(document, dict) or set(document) != {"header", "events"}:
        raise ValueError("Native trace needs header and events")
    return document


def replay_document(document: dict) -> dict:
    """Validate observed boundaries. Never fill absent input or claim a whole native capture."""
    if not isinstance(document, dict) or set(document) != {"header", "events"}:
        raise ValueError("Native trace needs header and events")
    header, events = document["header"], document["events"]
    if not isinstance(header, dict) or header.get("schema") != SCHEMA or header.get("build") != BUILD:
        raise ValueError("Native trace schema/build mismatch")
    if set(header) != HEADER_FIELDS:
        raise ValueError("Native v2 header fields do not match the fixed schema")
    if not isinstance(events, list):
        raise ValueError("Events must be a list")
    if header.get("scope") != "body-script-only" or header.get("gap_policy") != "reject":
        raise ValueError("Unsupported scope or missing-input policy")
    if (
        header.get("shot_policy") != "drain_after_command"
        or type(header.get("seed")) is not int or header["seed"] != 1
    ):
        raise ValueError("Unsupported shot consumer or initialization seed")
    for name in ("entity", "start_frame"):
        _int(header.get(name), name, 0, FRAME_MASK)
    for name in ("body_epoch", "command_epoch", "server_tick"):
        _int(header.get(name), name)
    _int(header.get("quanta_us"), "quanta_us", 1)
    _int(header.get("mode"), "mode", 0, (1 << 64) - 1)
    for name in ("health", "maximum_health", "ult_seconds"):
        value = header.get(name)
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"Invalid {name}")
    if header["health"] != header["maximum_health"]:
        raise ValueError("This constructor baseline requires initial health == maximum_health")
    if header.get("hero") != SOLDIER:
        raise ValueError("Only the observed Soldier body baseline is supported")
    expected_body = next(key for key, item in graph.bodies().items() if int(item["hero"], 16) == SOLDIER)
    if header["body_definition"] != expected_body:
        raise ValueError("Body definition does not match the supported native hero")
    rows, diagnostics = [], []
    ended = False
    script = None

    def fail(kind: str, **details) -> None:
        diagnostics.append({"kind": kind, **details})

    if header.get("model_hash") != model_hash():
        fail("model_mismatch")
    else:
        script = BodyScript(header["hero"], header["entity"], header["start_frame"],
                            quanta=header["quanta_us"], mode=header["mode"],
                            health=header["health"], ult_seconds=header["ult_seconds"])
        if snapshot_hash(script) != header.get("initial_hash"):
            fail("initial_state_mismatch")
        previous_seq, previous_tick = 0, header["server_tick"]
        for offset, event in enumerate(events):
            if diagnostics:
                break
            if not isinstance(event, dict):
                fail("invalid_event", offset=offset)
                break
            kind = event.get("kind")
            if kind == "fault":
                fail("capture_fault", error=str(event.get("error", "unspecified fault")))
                break
            if kind not in ("command", "end"):
                fail("unsupported_event", event_kind=kind)
                break
            fields = {"kind", "seq", "server_tick", "command_epoch", "body_epoch"}
            fields |= {"command", "before_hash", "after_hash", "shots"} if kind == "command" else {
                "final_hash", "reason",
            }
            if set(event) != fields:
                fail("invalid_event_fields", offset=offset)
                break
            seq, tick = event.get("seq"), event.get("server_tick")
            if (
                type(seq) is not int or seq != previous_seq + 1
                or type(tick) is not int or tick < previous_tick
            ):
                fail("event_order", offset=offset)
                break
            previous_seq, previous_tick = seq, tick
            if any(type(event.get(name)) is not int or event[name] != header[name]
                   for name in ("body_epoch", "command_epoch")):
                fail("epoch_boundary", seq=seq)
                break
            if kind == "end":
                if snapshot_hash(script) != event.get("final_hash"):
                    fail("final_state_mismatch", seq=seq)
                elif offset != len(events) - 1:
                    fail("events_after_end", seq=seq)
                else:
                    ended = True
                break
            command = _command(event.get("command"))
            before = snapshot_hash(script)
            if before != event.get("before_hash"):
                fail("before_state_mismatch", seq=seq, expected=event.get("before_hash"), actual=before)
                break
            distance = frame_distance(command.frame, script.frame)
            if 1 < distance < FRAME_HALF:
                fail("source_gap", seq=seq, first=(script.frame + 1) & FRAME_MASK,
                     last=(command.frame - 1) & FRAME_MASK, count=distance - 1)
                break
            if distance == 1 and command.frame < script.frame:
                fail("epoch_boundary", seq=seq, reason="Native body timers/CmFD need a fresh lifecycle")
                break
            try:
                script.command(command.frame, command.buttons, command.action, command.forward, command.right)
                after, shots = snapshot_hash(script), shot_summary(script)
            except (ValueError, RuntimeError) as error:
                fail("runtime_fault", seq=seq, error=str(error))
                break
            if after != event.get("after_hash") or shots != event.get("shots"):
                fail("after_state_or_shots_mismatch", seq=seq, expected=event.get("after_hash"), actual=after)
                break
            rows.append({"seq": seq, "frame": command.frame, "after_hash": after, "shots": shots,
                         "disposition": "applied" if distance == 1 else "ignored_old_or_duplicate"})
            script.component.shots.clear()
    if not ended and not diagnostics:
        fail("missing_end", reason="An open/truncated tail is not a complete observed trace")
    try:
        final_hash = snapshot_hash(script) if script is not None and not script.component.fault else None
    except (ValueError, RuntimeError) as error:
        final_hash = None
        fail("snapshot_fault", error=str(error))
    return {
        "schema": SCHEMA + ".result", "scope": "body-script-only",
        "status": "matched_observed_body_trace" if ended and not diagnostics else "incomplete",
        "complete_native_capture": False, "observed_trace_closed": ended,
        "validated_commands": len(rows), "commands": rows, "diagnostics": diagnostics,
        "state_hash": _digest(rows), "input_hash": _digest(document),
        "final_hash": final_hash,
    }

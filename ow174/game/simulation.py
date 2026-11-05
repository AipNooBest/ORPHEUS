"""Offline command recording and deterministic FlatMover replay; separate from the live queue.

Epochs are explicit recording metadata. Neither a packet's arrival server tick nor a raw client
frame establishes the original client's simulation frequency. quanta_us is an offline model choice.
"""

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from itertools import pairwise

from ow174.game.commands import Command
from ow174.game.flat_model import FlatMover

SCHEMA = "ow174.command-replay.v1"
RESULT_SCHEMA = "ow174.command-replay-result.v1"
BUILD = "1.74.0.0.104319"
FRAME_MODULUS = 1 << 32
FRAME_HALF = FRAME_MODULUS // 2
FRAME_MASK = FRAME_MODULUS - 1
RESTART_GAP = 1024
RESTART_LOW_FRAME = 128
COMMAND_FIELD_ORDER = ("frame", "forward", "right", "yaw", "pitch", "buttons", "action")
COMMAND_FIELDS = set(COMMAND_FIELD_ORDER)
# These defaults adapt v1 to the richer live decoder; they are not part of the v1 recording schema.
COMMAND_EXTENSION_DEFAULTS = {"extra": 0, "subframe": 0xFF, "click_yaw": None, "click_pitch": None}


def _integer(value, name: str, minimum: int = 0, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _keys(value, expected: set[str], name: str) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{name} needs exactly these fields: {', '.join(sorted(expected))}")


def _command(value: dict) -> Command:
    _keys(value, COMMAND_FIELDS, "Command")
    ranges = {
        "frame": (0, FRAME_MASK), "forward": (-128, 127), "right": (-128, 127),
        "buttons": (0, 0xFFFFFF), "action": (0, 255),
    }
    for name, (minimum, maximum) in ranges.items():
        _integer(value[name], name, minimum, maximum)
    for name in ("yaw", "pitch"):
        if not isinstance(value[name], int) or isinstance(value[name], bool):
            raise ValueError(f"{name} must be an integer in the decoded angle representation")
    return Command(**value, **COMMAND_EXTENSION_DEFAULTS)


def _record_command(command: Command) -> dict:
    """Project only losslessly representable commands into the fixed seven-field v1 schema."""
    if not isinstance(command, Command):
        raise ValueError("Recording needs a decoded Command")
    fields = asdict(command)
    for name in fields.keys() - COMMAND_FIELDS:
        if name not in COMMAND_EXTENSION_DEFAULTS:
            raise ValueError(f"v1 does not support command field {name!r}")
        default, value = COMMAND_EXTENSION_DEFAULTS[name], fields[name]
        if type(value) is not type(default) or value != default:
            raise ValueError(f"v1 cannot record nondefault command field {name!r}")
    projected = {name: fields[name] for name in COMMAND_FIELD_ORDER}
    _command(projected)
    return projected


def _digest(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _number(value: float) -> float:
    rounded = round(value, 9)
    return 0.0 if rounded == 0 else rounded


@dataclass(frozen=True)
class TimedCommand:
    player: str
    epoch: int
    server_tick: int
    unwrapped_frame: int
    command: Command


@dataclass
class _Track:
    commands: dict[int, TimedCommand] = field(default_factory=dict)
    highwater: int | None = None
    duplicates: int = 0
    out_of_order: int = 0


class CommandTimeline:
    """Collect first, then replay sorted commands. No missing commands are manufactured.

    Serial unwrapping assumes each observation is within half a u32 cycle of the newest known frame.
    Exactly half a cycle is ambiguous and rejected. A large backwards jump is only a diagnostic;
    the declared epoch remains authoritative, because a delayed packet can have the same pattern.
    """

    def __init__(self) -> None:
        self.tracks: dict[tuple[str, int], _Track] = {}
        self.diagnostics: list[dict] = []
        self._last_server_tick: int | None = None

    def ingest(self, player: str, epoch: int, server_tick: int, commands: Iterable[Command]) -> int:
        if not isinstance(player, str) or not player:
            raise ValueError("Player must be a nonempty string")
        _integer(epoch, "Epoch")
        _integer(server_tick, "Server tick")
        if self._last_server_tick is not None and server_tick < self._last_server_tick:
            raise ValueError("Packet server ticks must follow recording order")
        batch = [_command(_record_command(command)) for command in commands]
        track = self.tracks.setdefault((player, epoch), _Track())
        previous = track.highwater
        fresh = 0
        for command in batch:
            if track.highwater is None:
                unwrapped = command.frame
            else:
                delta = (command.frame - track.highwater) & FRAME_MASK
                if delta == FRAME_HALF:
                    raise ValueError("A half-cycle frame difference is ambiguous")
                if delta > FRAME_HALF:
                    delta -= FRAME_MODULUS
                unwrapped = track.highwater + delta
            old = track.commands.get(unwrapped)
            if old is not None:
                if old.command != command:
                    raise ValueError("Conflicting payloads for the same player/epoch/frame")
                track.duplicates += 1
                continue
            if track.highwater is not None and unwrapped < track.highwater:
                track.out_of_order += 1
            track.commands[unwrapped] = TimedCommand(player, epoch, server_tick, unwrapped, command)
            track.highwater = max(track.highwater, unwrapped) if track.highwater is not None else unwrapped
            fresh += 1
        if fresh and previous is not None and batch:
            prior, newest = previous & FRAME_MASK, batch[-1].frame
            backwards = prior - newest
            if newest <= RESTART_LOW_FRAME and RESTART_GAP <= backwards < FRAME_HALF:
                self.diagnostics.append({
                    "kind": "restart_candidate", "player": player, "epoch": epoch,
                    "server_tick": server_tick, "previous_frame": prior, "received_frame": newest,
                    "resolution": "kept_in_declared_epoch",
                })
        self._last_server_tick = server_tick
        return fresh

    def ordered(self, player: str, epoch: int) -> list[TimedCommand]:
        track = self.tracks[(player, epoch)]
        frames = sorted(track.commands)
        # Anchor the first observed frame to its raw u32 cycle, independent of arrival ordering.
        offset = (frames[0] // FRAME_MODULUS) * FRAME_MODULUS if frames else 0
        return [replace(track.commands[frame], unwrapped_frame=frame - offset) for frame in frames]

    def summary(self, player: str, epoch: int, quanta_us: int) -> dict:
        _integer(quanta_us, "quanta_us", 1)
        track = self.tracks[(player, epoch)]
        frames = [item.unwrapped_frame for item in self.ordered(player, epoch)]
        gaps = [
            {"first_unwrapped_frame": left + 1, "last_unwrapped_frame": right - 1,
             "count": right - left - 1}
            for left, right in pairwise(frames) if right > left + 1
        ]
        span = frames[-1] - frames[0] if frames else 0
        return {
            "player": player, "epoch": epoch, "known_commands": len(frames),
            "duplicates": track.duplicates, "out_of_order": track.out_of_order,
            "first_unwrapped_frame": frames[0] if frames else None,
            "last_unwrapped_frame": frames[-1] if frames else None,
            "source_frame_span": span, "source_span_us": span * quanta_us,
            "applied_time_us": len(frames) * quanta_us, "missing_ranges": gaps,
        }


class CommandRecording:
    """Record decoded Commands, not raw datagrams. Player initial states are caller-supplied."""

    def __init__(self, quanta_us: int, players: dict) -> None:
        _integer(quanta_us, "quanta_us", 1)
        self.document = {"schema": SCHEMA, "build": BUILD, "quanta_us": quanta_us,
                         "players": players, "packets": []}

    def record(self, player: str, epoch: int, server_tick: int, commands: Iterable[Command]) -> None:
        self.document["packets"].append({
            "player": player, "epoch": epoch, "server_tick": server_tick,
            "commands": [_record_command(command) for command in commands],
        })

    def to_document(self) -> dict:
        # Return an independent JSON value so later recordings cannot change an exported capture.
        return json.loads(json.dumps(self.document, ensure_ascii=False, allow_nan=False))


def replay_document(document: dict) -> dict:
    """Validate the v1 recording and replay known commands with the existing flat-floor model."""
    _keys(document, {"schema", "build", "quanta_us", "players", "packets"}, "Recording")
    if document["schema"] != SCHEMA or document["build"] != BUILD:
        raise ValueError(f"Expected schema {SCHEMA} and build {BUILD}")
    quanta = _integer(document["quanta_us"], "quanta_us", 1)
    players = document["players"]
    if not isinstance(players, dict):
        raise ValueError("Players must be an object of initial states")
    for player, state in players.items():
        if not isinstance(player, str) or not player:
            raise ValueError("Player must be a nonempty string")
        _keys(state, {"position", "yaw"}, "Player initial state")
        position = state["position"]
        if not isinstance(position, list) or len(position) != 3:
            raise ValueError("Initial position needs three finite numbers")
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in position
        ):
            raise ValueError("Initial position needs three finite numbers")
        _integer(state["yaw"], "Initial yaw", -32768, 32767)
    if not isinstance(document["packets"], list):
        raise ValueError("Packets must be a list")
    timeline = CommandTimeline()
    for packet in document["packets"]:
        _keys(packet, {"player", "epoch", "server_tick", "commands"}, "Packet")
        if packet["player"] not in players:
            raise ValueError("Packet player has no initial state")
        if not isinstance(packet["commands"], list):
            raise ValueError("Packet commands must be a list")
        timeline.ingest(packet["player"], packet["epoch"], packet["server_tick"],
                        [_command(value) for value in packet["commands"]])
    states, summaries = [], []
    for player, epoch in sorted(timeline.tracks):
        initial = players[player]
        mover = FlatMover(initial["position"], initial["yaw"])
        ordered = timeline.ordered(player, epoch)
        for index, timed in enumerate(ordered):
            try:
                mover.step(timed.command, quanta / 1_000_000)
            except OverflowError as error:
                raise ValueError("Replay parameters exceed the movement model's numeric range") from error
            if not all(math.isfinite(value) for value in (*mover.position, *mover.velocity)):
                raise ValueError("Replay produced a nonfinite movement state")
            states.append({
                "player": player, "epoch": epoch, "frame": timed.command.frame,
                "unwrapped_frame": timed.unwrapped_frame,
                "source_offset_us": (timed.unwrapped_frame - ordered[0].unwrapped_frame) * quanta,
                "applied_time_us": (index + 1) * quanta,
                "position": [_number(value) for value in mover.position],
                "velocity": [_number(value) for value in mover.velocity],
                "yaw": mover.yaw, "pitch": mover.pitch, "crouched": mover.crouched,
            })
        summaries.append(timeline.summary(player, epoch, quanta))
    return {
        "schema": RESULT_SCHEMA, "build": BUILD, "model": "existing-flat-mover-v1",
        "quanta_us": quanta, "states": states, "summaries": summaries,
        "diagnostics": timeline.diagnostics, "recording_hash": _digest(document),
        "state_hash": _digest(states),
    }

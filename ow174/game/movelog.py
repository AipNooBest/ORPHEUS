"""The movement log: research output that shows, frame by frame, what the server ran for each player's body
and which records of the player's own body it sent. On with the environment variable OW174_MOVELOG=1, off
(and free) otherwise.

One JSON line per event in logs/movelog/<time>_<match>_<slot>_<name>.jsonl, each with the wall clock t
(time.time(): the clock a capture of a client on the same PC uses too) and the server's tick:
    begin   the player, his hero, the map and the quanta
    body    a new mover: the first body, a hero switch or a new body (body id, hero, position, yaw)
    in      the commands of one datagram: how many were new, the first and the newest frame, and whether
            the mover walks on the map's collision yet (else a flat floor at the spawn height)
    cmd     one new command as the client sent it (frame, throttles, yaw, pitch, buttons, action)
    snap    the mover's state after the tick of a command frame (every field of mover.Snapshot); a frame
            the client's commands never brought (Mover.step runs it with the last command) has a snap and
            no cmd
    mods    the movement mods the body's statescript has on, when they change
    sent    a record of the owner's own body: its frame, the packet frame, the count between them and the
            command ack of that datagram (correction.py)

Every line is flushed to the file at once, but a folder listing (Explorer, dir, Get-ChildItem) shows the
size NTFS keeps in the folder's entry, which a write does not update: a log the server keeps open showed 0
bytes for a whole match with every line in it (2026-10-03). Looking at the file by its path updates the
entry (measured: stale after every write, right after os.stat, which takes 0.1 ms), so a log does that once
a second, and it is closed when the player's game is dropped (server.GameServer.drop).
"""

import contextlib
import dataclasses
import json
import os
import time
import weakref

from ow174.paths import LOGS_DIR

ENABLED = os.environ.get("OW174_MOVELOG") == "1"
FOLDER = LOGS_DIR / "movelog"
VERSION = 1
SHOW_EVERY = 1.0  # seconds between updates of the size a folder listing shows


class PlayerLog:
    """The log file of one player in one match."""

    def __init__(self, player) -> None:
        match = player.match
        FOLDER.mkdir(parents=True, exist_ok=True)
        name = "".join(c if c.isalnum() else "_" for c in player.name)[:24]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = FOLDER / f"{stamp}_{match.id[0] & 0xFFFF:04X}_{player.slot}_{name}.jsonl"
        self.file = open(self.path, "a", encoding="utf-8")  # noqa: SIM115 - open for the whole match
        self.mover = None  # the mover whose snapshots are logged
        self.frame: int | None = None  # its newest frame logged
        self.mods = None
        self.shown = 0.0  # time.monotonic() of the next update of the listed size
        game_map = match.game_map
        self.write(
            "begin",
            match.tick,
            version=VERSION,
            match=match.label(),
            player=player.name,
            slot=player.slot,
            account=player.account_lo,
            hero=f"{player.hero.guid:016X}",
            quanta=player.quanta,
            map=game_map.name,
            map_guid=f"{game_map.map_guid:016X}",
            mode=f"{game_map.mode_guid:016X}",
        )

    def write(self, kind: str, tick: int, **fields) -> None:
        line = {"k": kind, "t": round(time.time(), 6), "tick": tick, **fields}
        self.file.write(json.dumps(line, separators=(",", ":")) + "\n")
        self.file.flush()
        now = time.monotonic()
        if now >= self.shown:
            self.shown = now + SHOW_EVERY
            with contextlib.suppress(OSError):
                os.stat(self.path)  # the folder listing's size (see the module's notes)


_logs: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def log_of(player) -> PlayerLog | None:
    """The player's log, opened on first use; None while the log is off."""
    if not ENABLED:
        return None
    log = _logs.get(player)
    if log is None:
        log = _logs[player] = PlayerLog(player)
        weakref.finalize(player, log.file.close)  # closed with the player
    return log


def close(player) -> None:
    """The player's game is gone: his log is complete (a game that comes back starts a new one)."""
    log = _logs.pop(player, None)
    if log is not None:
        log.file.close()


def _fields(snapshot) -> dict:
    if dataclasses.is_dataclass(snapshot):
        return {field.name: getattr(snapshot, field.name) for field in dataclasses.fields(snapshot)}
    return dict(vars(snapshot))


def commands(player, fresh) -> None:
    """Before Player.take_commands runs them: a new mover's start, and the new commands."""
    log = log_of(player)
    if log is None:
        return
    tick = player.match.tick
    mover = player.mover
    if mover is not log.mover:
        log.mover, log.frame, log.mods = mover, None, None
        log.write(
            "body",
            tick,
            body=player.body,
            hero=f"{player.hero.guid:016X}",
            has_body=player.has_body,
            position=list(mover.position),
            yaw=mover.yaw,
        )
    if fresh:
        on_map = getattr(mover, "on_map", None)
        log.write("in", tick, n=len(fresh), first=fresh[0].frame, newest=fresh[-1].frame, on_map=on_map)
    for command in fresh:
        log.write(
            "cmd",
            tick,
            f=command.frame,
            fwd=command.forward,
            right=command.right,
            yaw=command.yaw,
            pitch=command.pitch,
            btn=command.buttons,
            act=command.action,
            body=player.has_body,
        )


def snapshots(player) -> None:
    """After Player.take_commands: the snapshots its commands made and the mods now on."""
    log = log_of(player)
    if log is None or player.mover is not log.mover:
        return
    tick = player.match.tick
    mover = player.mover
    for snapshot in mover.history:
        if log.frame is None or snapshot.frame > log.frame:
            log.write("snap", tick, **_fields(snapshot))
            log.frame = snapshot.frame
    mods = [[mod.index, mod.op, mod.value, mod.start, mod.end, mod.priority] for mod in mover.mods.values()]
    if mods != log.mods:
        log.mods = mods
        log.write("mods", tick, f=log.frame, mods=mods)


def sent(player, tick: int, frame: int, ack: int) -> None:
    """A record of the owner's own body goes out in packet frame `tick`."""
    log = log_of(player)
    if log is not None:
        log.write("sent", tick, f=frame, count=tick - frame, ack=ack)

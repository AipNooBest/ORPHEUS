"""Optional local body-script traces, separate from the movement log and full match replay.

OW174_BODY_TRACE=1 records a header at creation, command observations before combat
consumes shots, and an end at retirement. Recording errors disable that trace;
they must never change gameplay. Unrecorded external mutations remain detectable
through before/after hashes rather than being inferred as commands.
"""

import contextlib
import json
import logging
import os
import time
import weakref

from ow174.game import native_replay
from ow174.paths import LOGS_DIR

log = logging.getLogger("ow174.game")
ENABLED = os.environ.get("OW174_BODY_TRACE") == "1"
FOLDER = LOGS_DIR / "bodytrace"
_logs = weakref.WeakKeyDictionary()
_epochs = weakref.WeakKeyDictionary()


class Writer:
    def __init__(self, script, header, body_epoch):
        FOLDER.mkdir(parents=True, exist_ok=True)
        self.path = FOLDER / f"{time.time_ns()}_{script.entity:08X}_{body_epoch}.jsonl"
        self.file = self.path.open("x", encoding="utf-8")
        self.seq = 0
        self.body_epoch = body_epoch
        try:
            self.write({"header": header})
        except BaseException:
            self.file.close()
            raise
        weakref.finalize(script, self.file.close)

    def write(self, value):
        self.file.write(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
        self.file.flush()

    def close(self):
        self.file.close()


def _discard(script, error):
    writer = _logs.pop(script, None)
    if writer is not None:
        with contextlib.suppress(OSError, ValueError):
            writer.write({"kind": "fault", "seq": writer.seq + 1, "error": str(error)[:2000]})
        with contextlib.suppress(OSError):
            writer.close()
    log.warning("[bodytrace] recording stopped for %08X: %s", script.entity, error)


def started(player, script):
    if not ENABLED:
        return
    try:
        epoch = _epochs.get(player, 0) + 1
        header = native_replay.make_header(
            script, quanta_us=player.quanta, command_epoch=player.commands.epoch,
            body_epoch=epoch, server_tick=player.match.tick,
        )
        writer = Writer(script, header, epoch)
        _epochs[player] = epoch
        _logs[script] = writer
    except Exception as error:  # noqa: BLE001 - diagnostics must not affect the game
        _discard(script, error)


def before(script):
    if not ENABLED or script not in _logs:
        return None
    try:
        return native_replay.snapshot_hash(script)
    except Exception as error:  # noqa: BLE001 - faulted/unsupported scripts are still useful evidence
        _discard(script, error)
        return None


def command(player, script, value, before_hash):
    writer = _logs.get(script) if ENABLED else None
    if writer is None:
        return
    try:
        event = native_replay.make_command_event(
            script, value, seq=writer.seq + 1, server_tick=player.match.tick,
            before_hash=before_hash, command_epoch=player.commands.epoch, body_epoch=writer.body_epoch,
        )
        writer.write(event)
        writer.seq += 1
    except Exception as error:  # noqa: BLE001 - recording failure must not discard a valid command
        _discard(script, error)


def failed(script, error):
    if ENABLED and script in _logs:
        _discard(script, error)


def stopped(player, script):
    writer = _logs.get(script) if ENABLED else None
    if writer is None:
        return
    try:
        event = native_replay.make_end_event(
            script, seq=writer.seq + 1, server_tick=player.match.tick,
            command_epoch=player.commands.epoch, body_epoch=writer.body_epoch,
        )
        writer.write(event)
        writer.close()
        _logs.pop(script, None)
    except Exception as error:  # noqa: BLE001 - retiring a body must not depend on its recorder
        _discard(script, error)

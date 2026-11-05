"""Corrections of the owner's own body: the server's state of the body, sent to the player who moves it.

The owner's client moves its own body itself and keeps the state it predicted for each command frame (64
of them, mover +0x1580). A ch3 movement record on the body it possesses goes, past the gate that keeps
other entities' states, to its player's component 59 (0x7FF7896DA7E7 -> 0x7FF7896DA8AC): when
comp59+0x3E0 <= the record's frame, the state is copied to comp59+0x70 and +0x3E0 = frame + 1. On its
next tick (0x7FF78970F660) the client takes its predicted state of that same frame (history vt+0x60 and
vt+0x58, the frame checked at 0x7FF78970FA0D) and compares the two with 0x7FF789B73E00 in mode 2: every
field exactly (flags, frames, throttles, gravity, spring steps and speed, pitch, angles, position,
velocity, +832), all but +384. Equal: nothing happens but the bookkeeping (0x7FF789ACE870 drops mods that
ended before it) and +0x3E0 = 0. Different, or no prediction of that frame: comp59+0x60 = 1 and the
client puts its body in the server's state and runs its later commands again.

So a record must carry the state after the tick of command frame F with frame = F, and F must not be after
the packet frame (the record's frame is the packet frame minus an unsigned count, 0x7FF789B76DFE). The
client numbers its command frames like our packet frames: whenever a packet frame passes its own frame it
jumps to that frame plus a lead of at most 16 (0x7FF7896DAF7A..0x7FF7896DB296). The server runs the
commands as they come, often before their frame is the server's tick, so it sends the newest state whose
frame is not after the packet frame and that it did not send yet, one per frame. Retail sent the local
body a ch3 record in every frame (OW2 traffic).

What the client does with a different state (0x7FF789710390, 0x7FF78970FD80, 0x7FF789B7DF20): when its
body's +12 (a discontinuity counter, 0 in our records and in the body's create) equals the server's, it
puts the body in the server's state and runs the commands after F again, from the window it keeps for
sending them (comp59+0xAF0, at most 32 frames); else it takes the server's state with no replay. It keeps
only the commands from ack - 1 on (0x7FF7896DAF4F -> 0x7FF789B3D6E0), and a frame it no longer has runs
with its oldest command (0x7FF789B3D586). So while it corrects, the ack stays at most two past the newest
record (`command_ack`). The spring offsets themselves (+44, +48) are not sent: the decoded state has them
at 0 (0x7FF789B75EC0) and the body takes them so (0x7FF789B4AC10), so after a correction the client's
spring starts again from 0 while ours goes on (a few more corrections on stairs and slopes, unverified).

Only for a body whose statescript the server runs (match.BODY_SCRIPT_HEROES): the mods it has on change the
mover (Sprint), and a body whose abilities move it in ways the server does not know (Genji's double jump,
Lucio's wall ride, Mercy's flight) would be pulled back by every record. Of those, only the heroes in HEROES:
the ones whose movement the server's mover follows.
"""

from ow174.game import movelog
from ow174.game.content import SOLDIER
from ow174.game.mover import Mover, Snapshot
from ow174.game.world import NO_INPUT, EntityUpdate, Movement

CORRECTIONS = True  # False: the owner never gets its own body's state (each client then keeps its own)
HEROES = {SOLDIER}  # the heroes whose movement abilities the mover follows
ACK_LEAD = 2  # the client keeps the commands from ack - 1 on; a replay needs them from the record's frame + 1


def snapshot_for(mover: Mover, tick: int) -> Snapshot | None:
    """The newest state of a command frame not after packet frame `tick` that was not sent yet."""
    sent = mover.corrected
    for snapshot in reversed(mover.history):
        if snapshot.frame <= tick:
            return snapshot if sent is None or snapshot.frame > sent else None
    return None


def movement(snapshot: Snapshot) -> Movement:
    """The record's state: the mover's snapshot at its own frame."""
    if snapshot.input_frame is None:
        input_frame = NO_INPUT
    elif snapshot.input_frame == snapshot.frame:
        input_frame = None  # the state's own frame: +34 and +35 are then +32 and +33
    else:
        input_frame = snapshot.input_frame
    return Movement(
        snapshot.position,
        snapshot.yaw,
        snapshot.pitch,
        snapshot.velocity,
        snapshot.flags,
        throttles=snapshot.throttles,
        input_frame=input_frame,
        input_throttles=snapshot.input_throttles,
        air_ticks=snapshot.air_ticks,
        gravity=snapshot.gravity,
        frame=snapshot.frame,
        crouch_frame=snapshot.crouch_end,
        spring=snapshot.spring,
        spring_speed=snapshot.spring_speed,
        fall=snapshot.fall,
    )


def correcting(player) -> bool:
    """Whether the player's own body gets its records: the server runs its statescript, follows its
    hero's movement and the client has the body."""
    script = player.body_script
    if not CORRECTIONS or not player.has_body or script is None or not script.create_arrived:
        return False
    return player.hero.guid in HEROES


def own_record(player, tick: int) -> EntityUpdate | None:
    """This frame's record of the player's own body, if it gets one."""
    if not correcting(player):
        return None
    snapshot = snapshot_for(player.mover, tick)
    if snapshot is None:
        return None
    player.mover.corrected = snapshot.frame
    if movelog.ENABLED:
        movelog.sent(player, tick, snapshot.frame, command_ack(player, tick))
    return EntityUpdate(player.body, movement=movement(snapshot))


def command_ack(player, tick: int) -> int:
    """The command ack of a frame: the newest command frame the server has (the client stops sending the
    ones before it), but while the owner gets records at most ACK_LEAD past the newest one sent, so that
    the client still has the commands a correction runs again."""
    newest = player.commands.last_frame or tick
    sent = player.mover.corrected
    if sent is None or not correcting(player):
        return newest
    return min(newest, sent + ACK_LEAD)

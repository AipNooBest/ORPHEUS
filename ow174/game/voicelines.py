"""In-match hero voice lines: a spawn bark.

When a player spawns, the server makes the player's own body statescript enter one of the hero's
voice-stimulus states, and the client plays the bark. No new wire message is needed: a voice stimulus
rides the body's ordinary statescript frames.

How a bark reaches the client:
- A voice stimulus is a statescript state, STU_40E05F6B (the client's STUConfigVarVoiceStimulus,
  0x819B1577). Its m_stimulus holds a voice-stimulus GUID (0x0EE0...); when the client's copy of the body
  runs that state on, it plays the hero's line for that stimulus (the hero data picks the actual sound and
  its variations). The server treats the state as a plain State (no server effect) and only needs it on.
- The server runs the body's statescript (ow174/game/script) and sends the owner an H=1 frame each tick
  with every state that is on and is neither client-only nor server-only (graph.py). So a *networked*
  voice stimulus that the server turns on is carried to the owner's client in the next frame, exactly like
  any other state, and the client plays it. Other clients get it the same way through the body's remote
  frames (observers.py).
- A hero's voice graphs are subscripts of its body (reached from the body's graphs through SubScript
  nodes), so they are already live instances of the body's statescript component at the spawn. We pick a
  networked stimulus among them and begin it.

What is verified (tests/test_game_voicelines.py, with the client model of tests/test_script_driver.py):
- Beginning a networked voice stimulus on a spawned body gets it into the owner frame; the client model
  reads the frame whole (no crash) and ends with the state on, then off once we end it, staying in step
  with the server throughout. Checked for every hero that has such a stimulus.
- The heroes that have one: the server can bark for them. The rest have no networked voice stimulus in
  their body statescript at the spawn (their spawn chatter is driven entirely by the client's own voice
  component, which the server does not reach), so for them this is a no-op. `heroes_with_lines()` lists
  the ones that work.

What is NOT verified: that the chosen stimulus is the line retail plays on spawn, and that the audio
sounds right in the real client. We cannot run the game here. We pick the first networked stimulus the
hero has unless `PREFERRED_STIMULUS` names one; fill that table in (a hero GUID -> a 0x0EE0 stimulus GUID)
once you have heard the lines in the game and want a particular one.
"""

import logging

log = logging.getLogger("ow174.game")

VOICE_CLASS = "STU_40E05F6B"  # STUConfigVarVoiceStimulus holder; the state that plays a bark on the client

# Turn the feature off here to stop all spawn barks.
VOICE_LINES = True
# How long the stimulus state stays on before the server ends it (a one-shot bark; the client starts the
# line when the state turns on). Long enough for any line; leaving it on does no harm (the client stays in
# step either way), ending it just keeps the body's state tidy.
BARK_SECONDS = 4.0
# hero GUID (the 02E...) -> the voice-stimulus GUID (0x0EE0...) to play on spawn, overriding the default
# "first networked stimulus" pick. Empty by default; add entries once you have picked lines in the game.
PREFERRED_STIMULUS: dict[int, int] = {}


def _candidates(body_script) -> list[tuple[object, int, int | None]]:
    """(instance, m_states index, stimulus GUID) of every networked voice stimulus live in the body's
    statescript: a state that is a voice stimulus and is neither client-only nor server-only, so an owner
    frame carries it to the client."""
    found = []
    for instance in body_script.component.instances.values():
        for index, node in enumerate(instance.graph.states):
            if node is None or node.cls != VOICE_CLASS or not node.is_networked:
                continue
            cfg = node.fields.get("m_stimulus")
            stimulus = cfg.get("m_voiceStimulus") if isinstance(cfg, dict) else None
            found.append((instance, index, _guid(stimulus)))
    return found


def _guid(value) -> int | None:
    if isinstance(value, str) and value.startswith("0x"):
        try:
            return int(value, 16)
        except ValueError:
            return None
    return value if isinstance(value, int) else None


def _pick(body_script) -> tuple[object, int] | None:
    """The voice stimulus to play on this body: the one named for its hero in PREFERRED_STIMULUS, else the
    first networked stimulus it has. None when the hero has none."""
    candidates = _candidates(body_script)
    if not candidates:
        return None
    wanted = PREFERRED_STIMULUS.get(body_script.hero)
    if wanted is not None:
        for instance, index, stimulus in candidates:
            if stimulus == wanted:
                return instance, index
    instance, index, _ = candidates[0]
    return instance, index


def heroes_with_lines(bodies=None) -> dict[int, str]:
    """hero GUID -> name for the heroes a spawn body has a networked voice stimulus for (the ones this can
    bark for). Reads the statescript data, so it is a plain lookup, no match needed."""
    from ow174.game.script import graph as graphs
    from ow174.game.script.driver import BodyScript

    out = {}
    for body in (bodies or graphs.bodies()).values():
        hero = int(body["hero"], 16)
        try:
            script = BodyScript(hero, 0xA0000101, 1000)
        except ValueError:  # the driver's "no statescript data for the hero"
            continue
        if _candidates(script):
            out[hero] = body["name"]
    return out


class VoiceLines:
    """A match's spawn barks: begins a voice stimulus on a player's body when he spawns, and ends it a few
    seconds later. Owned by Combat (combat.py), which calls spawned() and expire()."""

    def __init__(self, match) -> None:
        self.match = match
        self.pending: list[tuple[float, object, object, int]] = []  # (end, body_script, instance, index)

    def spawned(self, player, now: float) -> None:
        """Play the player's spawn line. A no-op when the feature is off, the player has no body script, or
        the hero has no networked voice stimulus."""
        if not VOICE_LINES:
            return
        script = player.body_script
        if script is None or script.retired:
            return
        try:
            picked = _pick(script)
            if picked is None:
                return
            instance, index = picked
            state = instance.state(index)
            if state is None or state.active:
                return
            state.begin()
            self.pending.append((now + BARK_SECONDS, script, instance, index))
            log.info("[game] %s: %s voice line on spawn", self.match.label(), player.name)
        except Exception:
            log.exception("[game] %s: %s's spawn voice line failed", self.match.label(), player.name)

    def expire(self, now: float) -> None:
        """End the barks whose time is up. A body that has gone (retired on a respawn or hero switch) is
        dropped without ending anything: its states went with it."""
        for item in list(self.pending):
            end, script, instance, index = item
            if script.retired:
                self.pending.remove(item)
                continue
            if now < end:
                continue
            self.pending.remove(item)
            try:
                state = instance.state(index)
                if state is not None and state.active:
                    state.end(True)
            except Exception:
                log.exception("[game] %s: ending a spawn voice line failed", self.match.label())

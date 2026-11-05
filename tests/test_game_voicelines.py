"""Spawn voice lines (ow174/game/voicelines.py): that beginning a hero's networked voice stimulus on a
spawned body reaches the client and stays in step, read back through the body-frame client model of
test_script_driver.py."""

import logging
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ow174.game import voicelines
from ow174.game.script import decode
from ow174.game.script import graph as graphs
from ow174.game.script.driver import POLLED_SWITCHES, SETTLE_FRAMES, TRUSTED_NATIVES, BodyScript

START = 1000


def server_on(body: BodyScript) -> set:
    """The networked states the server has on, less the uncertain switches it keeps off the client (the
    same measure test_script_driver.py checks the client against)."""
    on = {
        (item.id, state.index)
        for item in body.component.instances.values()
        for state in item.states.values()
        if state.active and state.networked
    }
    uncertain = {
        (item.id, state.index)
        for item in body.component.instances.values()
        for state in item.states.values()
        if state.node.cls in POLLED_SWITCHES and state.natives - TRUSTED_NATIVES
    }
    return on - uncertain


def hero_guid(name: str) -> int:
    return next(int(body["hero"], 16) for body in graphs.bodies().values() if body["name"] == name)


class StubMatch:
    def label(self) -> str:
        return "test match"


class StubPlayer:
    def __init__(self, script: BodyScript) -> None:
        self.body_script = script
        self.name = "Tester"


def spawned_body(hero: int):
    """A body of the hero whose full frame a fresh client model has applied, settled past SETTLE_FRAMES so
    the client is in step with the server (as test_script_driver does for Soldier)."""
    logging.getLogger("ow174.script").setLevel(logging.ERROR)
    logging.getLogger("ow174.game").setLevel(logging.ERROR)
    body = BodyScript(hero, 0xA0000101, START)
    client = decode.ClientBody(decode.initial_graphs(hero))
    [tree] = body.spawn_frames()
    client.apply(tree)
    body.stream.arrived(tree.chunk_last)
    frame = START
    for _ in range(SETTLE_FRAMES + 3):
        frame += 1
        body.command(frame, 0)
        for update in body.frames():
            client.apply(update)
            body.stream.arrived(update.chunk_last)
    return body, client, frame


def run_frames(body, client, frame, count):
    for _ in range(count):
        frame += 1
        body.command(frame, 0)
        for update in body.frames():
            client.apply(update)  # a crash raises decode.ClientCrash
            body.stream.arrived(update.chunk_last)
    return frame


class VoiceLineTests(unittest.TestCase):
    def test_some_heroes_have_spawn_lines(self):
        have = voicelines.heroes_with_lines()
        self.assertTrue(have, "no hero has a networked spawn voice stimulus")
        # Mercy is one of them (graph 113's stimulus), used by the round-trip test below.
        self.assertIn(hero_guid("Mercy"), have)
        self.assertLessEqual(len(have), len(graphs.bodies()))

    def test_spawn_line_reaches_the_client_and_ends_in_step(self):
        hero = hero_guid("Mercy")
        body, client, frame = spawned_body(hero)
        self.assertEqual(client.on(), server_on(body))  # in step before the line

        voice = voicelines.VoiceLines(StubMatch())
        voice.spawned(StubPlayer(body), now=0.0)
        self.assertEqual(len(voice.pending), 1)
        _, _, instance, index = voice.pending[0]
        key = (instance.id, index)
        self.assertEqual(body.component.instances[instance.id].states[index].node.cls, voicelines.VOICE_CLASS)

        frame = run_frames(body, client, frame, 3)
        self.assertIn(key, client.on())  # the client is playing the bark
        self.assertEqual(client.on(), server_on(body))

        voice.expire(now=voicelines.BARK_SECONDS + 1.0)  # the bark's time is up
        self.assertFalse(voice.pending)
        frame = run_frames(body, client, frame, 3)
        self.assertNotIn(key, client.on())  # and the client has stopped it
        self.assertEqual(client.on(), server_on(body))

    def test_every_hero_with_a_line_reaches_the_client(self):
        for hero, name in voicelines.heroes_with_lines().items():
            body, client, frame = spawned_body(hero)
            voice = voicelines.VoiceLines(StubMatch())
            voice.spawned(StubPlayer(body), now=0.0)
            self.assertEqual(len(voice.pending), 1, name)
            _, _, instance, index = voice.pending[0]
            frame = run_frames(body, client, frame, 3)
            self.assertIn((instance.id, index), client.on(), name)
            self.assertEqual(client.on(), server_on(body), name)

    def test_disabled_is_a_no_op(self):
        body, _, _ = spawned_body(hero_guid("Mercy"))
        voice = voicelines.VoiceLines(StubMatch())
        with mock.patch.object(voicelines, "VOICE_LINES", False):
            voice.spawned(StubPlayer(body), now=0.0)
        self.assertFalse(voice.pending)

    def test_a_hero_without_a_networked_stimulus_is_a_no_op(self):
        # Soldier: 76's only live voice graph is server-only, so the server has no bark for him.
        hero = hero_guid("Soldier: 76")
        self.assertNotIn(hero, voicelines.heroes_with_lines())
        body, _, _ = spawned_body(hero)
        voice = voicelines.VoiceLines(StubMatch())
        voice.spawned(StubPlayer(body), now=0.0)  # must not raise
        self.assertFalse(voice.pending)

    def test_a_retired_body_is_skipped(self):
        body, _, _ = spawned_body(hero_guid("Mercy"))
        body.retire()
        voice = voicelines.VoiceLines(StubMatch())
        voice.spawned(StubPlayer(body), now=0.0)
        self.assertFalse(voice.pending)


if __name__ == "__main__":
    unittest.main()

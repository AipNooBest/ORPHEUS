basic.json is a synthetic decoded-command fixture, not a captured original-client session.
It exercises an overlapping command and a late command that fills a gap, using FlatMover.
The build is a target compatibility label. This fixture does not prove original movement fidelity.

From the repository root:
  .venv\Scripts\python.exe tools/replay_game_commands.py data/replays/basic.json --output logs/sample_replay.json
Use --expect-state-hash with the previously recorded result to detect behavior changes.

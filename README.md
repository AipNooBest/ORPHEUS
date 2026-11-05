# ORPHEUS

ORPHEUS is a server restoration project for Overwatch 1 retail build **1.74.0.0.104319**. The goal is to reproduce the server behavior of that build across its heroes, maps, modes, and online features, including the state and events that drive the original client's presentation. Development is ongoing.

## Restoration checklist

Check an item when its stated scope has repeatable evidence and any required original-client acceptance has passed. Checked subitems record completed work within an open milestone.

- [ ] **Research and replay.** Rebuild the required data from documented sources, resolve protocol and graph semantics, and reproduce complete sessions from recorded inputs and events.
  - [x] Pin the target build and document upstream sources and data provenance.
  - [x] Replay controlled encrypted server sessions and compare wire output and registered server state.
- [ ] **Login, profiles and social features.** Complete authentication, account and profile state, progression, cosmetics, parties and other social features supported by the target build.
- [ ] **Lobby and match lifecycle.** Verify queues, team assignment, game setup, hero selection, match handoff, joining, leaving, reconnecting and returning to the lobby.
- [ ] **Network synchronization.** Verify authoritative movement, prediction and correction, input timing, and state consistency for players and observers under delay, loss, reordering and repeated connections.
- [ ] **Heroes and abilities.** Cover every hero's weapons, reloads, aiming, movement abilities, passives, cooldowns, resources, ultimates, transformations and deployables, with shared mechanics tested across the roster.
- [ ] **Combat and health.** Reproduce hit detection, projectiles, explosions, damage, healing, armor, shields, barriers, status effects, eliminations, assists and respawns.
- [ ] **Maps and world interactions.** Verify collision, movement surfaces, spawn rooms, doors, health packs, destructible objects and environmental deaths for the applicable maps and modes.
  - [x] Extract and integrity-check local collision caches for all 95 catalog maps and handle missing caches explicitly.
- [ ] **Modes and match rules.** Verify objectives, scoring, timers, overtime, round transitions, victory conditions and results across the build's supported modes.
- [ ] **Bots and scripted scenarios.** Verify training and tutorial behavior, including target identity, routes, combat, spawning, death, respawn and scenario resets.
- [ ] **Models, skins and animation.** Verify first- and third-person model changes, skins, weapon handling, scopes and aim-down-sights, ability animations, and spawn, death and respawn sequences.
- [ ] **HUD, menus and visual feedback.** Verify health and ammo displays, overhead bars, reticles, hit markers, material-specific impact effects, elimination names, kill feed, on-fire meter, scoreboards and match screens.
- [ ] **Audio and dialogue.** Verify asset selection and playback triggers for weapon, hit, critical-hit and death sounds, multikill and match announcements, ambient audio and music, and contextual, randomized and Easter egg dialogue. Check listeners, timing and supported languages.
- [ ] **Stability and delivery.** Verify tick and packet budgets, long-running matches, recovery, reproducible deployment, and final acceptance with the original client.
  - [x] Verify server-only startup of the audited public source checkout using separate runtime data.

## Current validation

The first end-to-end validation group is Soldier: 76 in the Practice Range. Offline checks cover movement, firing and reloading, bot damage and respawn, hero switches, and leaving and rejoining. Original-client acceptance for this group is pending. Test results and remaining gaps are recorded in the [implementation and verification record](docs/STATE.md) and the [changelog](CHANGELOG.md).

## Architecture

| Layer | Responsibility |
| --- | --- |
| `ow174/bnet`, `ow174/lobby` | Local login, accounts, parties, lobby messages, matchmaking and match handoff |
| `ow174/game` | Encrypted UDP transport, input, movement, collisions, damage and entity replication |
| `ow174/game/script` | Target build statescript graph execution and owner and observer state encoding |
| `ow174/dashboard` | Local administration and diagnostics |
| `tools`, `tests` | Data extraction, collision cache building, replay and regression |
| `relay` | Source and build script for the Windows client relay |

## Run the local server

Use Windows and Python 3.12, the interpreter used for the current regression suite. Set up Python in PowerShell from the repository directory, then provision the separate runtime data below.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`START_SERVER_LOCAL.cmd` runs the loopback services. The local dashboard listens on `127.0.0.1:3725`, the lobby on `127.0.0.1:3724`, and the game server uses UDP `127.0.0.1:3730`. The server uses prepared collision caches. Close the server console to stop it.

For a manual client test, use `START_LOCAL.cmd --game-exe "C:\path\to\_retail_\Overwatch.exe"` with your own 1.74.0.0.104319 client. This entry starts the client as well as the server. The unattended development workflow uses the server-only entry or offline tests.

### Separate deployment data

The server reads protocol tables, graph and map data, the lobby signing input, and prepared collision from the directory set by `ORPHEUS_RUNTIME_DIR`. To run the public checkout, provision that directory from an existing prepared local research checkout for build 104319. A copy of the client alone does not produce every table. Some extraction steps use local protocol research and disassembly.

```powershell
.\.venv\Scripts\python.exe tools\prepare_server_runtime.py --source 'C:\path\to\local-research' --destination 'C:\path\to\orpheus-runtime' --map 0688
$env:ORPHEUS_RUNTIME_DIR = 'C:\path\to\orpheus-runtime'
.\START_SERVER_LOCAL.cmd
```

Omit `--map` to copy all prepared collision caches. The tool copies the expected server data files and writes their hashes. The public repository contains server source. Store the separate runtime directory locally or provision it through an authorized distribution channel. Server-only startup requires no installed client or Relay DLL. The client installation and extraction tools belong to the preparation step. See [data provenance](data/README.md) and [collision extraction](tools/build_collision.py).

For local AI research, `tools/extract_game_graphs.py --ai-spawners` reads a prepared `ai_spawners_174.json` and includes the map placement graphs in a bundle written outside the repository. Pass that bundle to `prepare_server_runtime.py --game-graphs` when preparing a new runtime directory. AI execution and original-client acceptance remain separate checklist items.

## Development

The full regression suite uses the prepared local research data, including additional fixtures listed in the data provenance document.

```powershell
.\.venv\Scripts\python.exe -m pip install ruff
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

Develop and verify shared mechanics across the heroes, maps and modes that use them. For each acceptance group, check gameplay together with its models, animation, HUD and audio.

See the [development workflow](AGENTS.md), [changelog](CHANGELOG.md), and [replay contract](docs/CONTROLLED_SERVER_JOURNAL_V2.txt). Raw diagnostic captures may include authentication or session data and belong in local storage.

## Provenance

ORPHEUS is an independent project that builds on the original [1.74 lobby server](https://github.com/Boi-027/overwatch-1.74-lobby-server) and subsequent preservation research. See the [ORPHEUS contributors](CONTRIBUTORS.md), [upstream references and credits](CREDITS.md), and the original [license notice](LICENSE). Client binaries, game assets, resource keys, and unpublished captures require their own redistribution review.

# ORPHEUS

ORPHEUS reconstructs server behavior for Overwatch 1.74.0.0.104319 using a locally installed client. The current delivery target is a complete Soldier: 76 session in the Practice Range. The project is a work in progress.

The local server can load the Practice Range and simulate movement, some weapon and ability behavior, damage, bot deaths, respawns, and hero changes. Automated tests cover these server and protocol paths. Client acceptance is pending for animation and HUD presentation, hit feedback, bot names and kill feed, audio, out-of-bounds death, and several ability behaviors. The [implementation and verification record](docs/STATE.md) separates these results.

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

Use Windows and Python 3.12, the interpreter used for the current regression suite. In PowerShell from the repository directory, set up the environment.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\START_SERVER_LOCAL.cmd
```

`START_SERVER_LOCAL.cmd` runs the loopback services without starting a game. The local dashboard listens on `127.0.0.1:3725`, the lobby on `127.0.0.1:3724`, and the game server uses UDP `127.0.0.1:3730`. Close the server console to stop it.

For a manual client test, use `START_LOCAL.cmd --game-exe "C:\path\to\_retail_\Overwatch.exe"` with your own 1.74.0.0.104319 client. This entry starts the client as well as the server. The unattended development workflow uses the server-only entry or offline tests.

The existing local checkout uses target build data in `data/` and collision caches generated from the user's client. See [data provenance](data/README.md) and [collision extraction](tools/build_collision.py). A source-only export requires locally supplied or regenerated runtime data. It has not been validated as a standalone installation.

## Development

```powershell
.\.venv\Scripts\python.exe -m pip install ruff
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -B -m unittest discover -s tests
```

The first validation group covers Soldier: 76 and the Practice Range through model, animation, movement, weapon and HUD, abilities, damage, death, respawn, UI, and audio. Common mechanisms are tested against other applicable data before the next hero or map is added to acceptance.

See the [development workflow](AGENTS.md), [changelog](CHANGELOG.md), and [replay contract](docs/CONTROLLED_SERVER_JOURNAL_V2.txt). Raw diagnostic captures may include authentication or session data and belong in local storage.

## Provenance

ORPHEUS builds on the original [1.74 lobby server](https://github.com/Boi-027/overwatch-1.74-lobby-server) and subsequent preservation research. The original [license notice](LICENSE) and [credits](CREDITS.md) are retained. Client binaries, game assets, resource keys, and unpublished captures require their own redistribution review.

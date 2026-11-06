# ORPHEUS

[简体中文](README.zh-CN.md)

ORPHEUS is an open-source server reimplementation for **Overwatch 1 retail 1.74.0.0.104319**. It is an independent, unofficial research and game preservation project. The long-term goal is to reproduce the server behavior and client-facing events for this build across its heroes, maps and modes. The server is still in development, with Soldier: 76 in the Practice Range as the first end-to-end target.

[Contribute](CONTRIBUTING.md) · [Roadmap](docs/ROADMAP.md) · [Rust migration](docs/RUST_MIGRATION.md) · [Verification record](docs/STATE.md) · [Changelog](CHANGELOG.md) · [Open issues](https://github.com/leafall903/ORPHEUS/issues)

## Current status

| Area | Implemented and tested on the server | Evidence from the original client and remaining work |
| --- | --- | --- |
| Local login and lobby | Local authentication emulation, lobby, hero selection and match handoff. Automated checks cover handshake and server startup with prepared data. | Login, Practice Range loading and hero selection or switching have been observed. Broader lobby flows and match rules need verification. |
| Soldier: 76 | Body and weapon state, movement and Sprint, rifle fire and reload, Helix Rockets and Biotic Field. Automated tests cover selected scenarios including repeated switching and rejoining, lost packets, and ammunition state. | The tester has fired on and killed training bots. Stable weapon presentation, ammunition HUD, input response and ability behavior still need a complete client pass. |
| Practice Range combat | Hit and damage resolution, bot health, death and respawn, and replication of the original overhead health display graph for stock bots. Automated tests use controlled fixtures and prepared collision from the target map. | The tester has reported missing overhead health bars, unnamed eliminations, missing kill feed and missing headshot audio. The display graph change has wire evidence. Its rendering still needs a documented client result. |
| Networking and research | Encrypted UDP, player input, entity lifecycles, server snapshots, replay and selected loss/reorder checks. | Client prediction, combat feedback, two-client agreement, longer sessions and broader map behavior remain open. |
| Heroes, modes and audio | Shared systems and partial coverage exist. | Full hero abilities, objectives, match flow, hit and multikill feedback, and contextual voice playback are still in development. |

An automated result covers the fixture or replay named in its evidence. A manual observation covers the build, commit and steps that were actually tried. No complete original-client acceptance pass has been recorded for the first target scenario. See the [verification record](docs/STATE.md) and [changelog](CHANGELOG.md).

## Get involved

- **Developers and AI-assisted contributors** can start with the public source tests. The guide covers Codex, Claude Code and the same review requirements for all submissions. [Contribution guide](CONTRIBUTING.md)
- **Reverse engineers** can help pin a single field, state transition, event or timing rule to build 104319. Start with the relevant evidence and report uncertainty explicitly. [Research evidence](docs/RESEARCH_EVIDENCE.txt)
- **Players** with the target client and authorized runtime can report a short, repeatable gameplay result. Reports of working behavior are welcome alongside bugs. [Gameplay verification](https://github.com/leafall903/ORPHEUS/issues/new?template=gameplay-verification.yml)
- **New contributors** can look for tasks that use the public checkout and synthetic data. [Good first issues](https://github.com/leafall903/ORPHEUS/labels/good%20first%20issue)

## Run locally

The Rust crate includes transport, protocol codecs, account and collection handling, a local Battle.net listener, a UDP match worker and the runtime preparation tool. The UDP worker serves match loading and exercises reliable retransmission in loopback tests. World creation and simulation are still being ported. Run `cargo test --locked --all-targets` with stable Rust to verify the public fixtures. The current game server entry point runs the Python implementation described below.

The documented environment is Windows with Python 3.12. Node.js 24 is used for the dashboard test. The public source checks can run without the game or any prepared runtime.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt "ruff==0.17.3"
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_game_commands.py
```

See the [full list of public checks](CONTRIBUTING.md#public-source-checks) before running broader test discovery.

### Separate deployment data

To run the local server, set `ORPHEUS_RUNTIME_DIR` to a prepared, authorized runtime for build 104319. The runtime includes protocol data, graphs, lobby signing input and collision caches. A copy of the game client alone cannot supply all inputs to the preparation tool.

```powershell
cargo run --locked --bin prepare_server_runtime -- --source 'C:\path\to\local-research' --destination 'C:\path\to\orpheus-runtime' --map 0688
$env:ORPHEUS_RUNTIME_DIR = 'C:\path\to\orpheus-runtime'
.\START_SERVER_LOCAL.cmd
```

The preparation command copies files from an already prepared checkout and writes a manifest. Omit `--map` to copy all available prepared caches. The server-only entry uses loopback and requires neither an installed game nor a relay DLL. Its default dashboard is at `127.0.0.1:3725`. For original-client testing, see [the manual procedure](CONTRIBUTING.md#original-client-verification) and [runtime data provenance](data/README.md).

## Project layout

| Path | Role |
| --- | --- |
| `rust/src` | Rust ports, automated regression tests and runtime preparation |
| `ow174/bnet`, `ow174/lobby` | Local login, accounts, social and lobby services, matchmaking and handoff |
| `ow174/game`, `ow174/game/script` | Encrypted game transport, inputs, simulation, combat and statescript replication |
| `ow174/dashboard`, `relay` | Local administration and the Windows client relay source |
| `tests`, `tools`, `docs` | Verification, extraction and replay tools, and research records |

The [roadmap](docs/ROADMAP.md) tracks the full build scope. [CREDITS.md](CREDITS.md) records the original 1.74 lobby project, research sources and upstream authors. The public source retains the original [MIT license notice](LICENSE). Overwatch and its assets belong to their respective rights holders. Keep game clients, commercial assets, keys, private logs, raw captures and dumps out of public submissions.

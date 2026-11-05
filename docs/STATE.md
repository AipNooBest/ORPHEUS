# Project state

Local evidence updated 2026-11-05, for client **1.74.0.0.104319**. The gameplay
integration uses LizRollin's wip revision
`9f8f3bcb5eccab6fb7738bee3c2371fcf0623f29`, on top of the Boi baseline and the
local startup/research work. [WIP.md](../WIP.md) retains the upstream author's
test claims and known issues. Those claims are separate from the observations below.

The current user instruction is unattended server development through the full
M0-M6 list: do not start or operate the game. All real-client acceptance is
deferred to the user after the server implementation and self-checks are complete.
The local server-only entry is `START_SERVER_LOCAL.cmd` / `--mode retail
--no-launch`; it retains the Battle.net/RPC/lobby/game services without a game
path, relay preparation or client-launch manager.

## Local client observations

- The real `_retail_/Overwatch.exe` logs into the local emulator without running
  the official Battle.net launcher.
- The Practice Range loads and the hero-selection screen appears. A hero switch
  was observed and recorded by the server.
- After the gameplay integration, the user reports killing training robots. The
  corresponding log records multiple damage notifications (25001), eliminations
  (25005), bot deaths and respawns.
- The user reports **no enemy overhead health bar**, no expected hit effect,
  and an elimination label without the robot's name. These remain unresolved.
  A robot's expected effect must be checked separately from flesh blood effects.

The automated UI check was stopped by the user with Escape. It did not establish
stable weapon/ammo HUD, a complete shooting/reload cycle, ten repeated hero
switches/reentries, or multiplayer agreement. Server logs and model tests do not
replace these client acceptance checks.

## Gameplay implementation now present

The canonical gameplay path uses `game/script/driver.py`, `runtime.py`,
`nodes.py` and `expr.py`, with target-build graph and codec data. It runs body,
weapon-manager and weapon instances and encodes owner/observer state frames.
It is no longer the earlier flat-movement/lobby-only implementation.

The imported implementation includes collision-based movement, hitscan and
projectiles, health pools, damage/death/respawn, Practice Range bots, and partial
hero abilities and match presentation. Each feature still needs scenario-specific
client validation. It does not constitute a complete game server or support all
heroes and match rules.

Local repairs add bounded expression execution and malformed-bytecode handling;
freeze reliable body creates to their original hero, skin, pose and projectile
lease; retire queued/in-flight entity records and ACK callbacks after destroy;
and reject late input packets without resetting only one command consumer.
Command decoding handles uint32 wrap. The native body driver explicitly rejects
a forward wrap before changing its old-epoch timers/state; automatic live epoch
recovery is not implemented.

Runtime event/update exhaustion and immediate plug cycles now fail the entire
component with `ScriptBudgetError` (entity, frame and time included). Pending
shots/events are discarded and later command/snapshot/frame generation is
rejected. The live match handler retires that body's script; other players keep
running. This prevents half-completed simulations from looking successful. It
does not add automatic recovery or prove the cause of the reported live issues.

The latest full Python regression ran 874 tests, all passing. The formerly skipped
King's Row physics case now runs with the locally built cache. A separate 62.448-second offline
Soldier sequence applied/decoded 3903 owner chunks, observed ammo spending and
seven refills, and ended with no active volley after release. This is a native
driver/local decoder model check, not real-client HUD or combat acceptance.

The Practice Range collision cache was built from this user's local CASC data:
`0688.col`, format 5, 470397 bytes, SHA-256
`dfdb71fbe9ad079d9d0b56fe12bd7c992a76c6ec942084d86f6388827f36a169`.
This proves cache extraction and loading inputs, not complete map collision fidelity.

## Research and regression boundary

`script/research_runtime.py`, `script/body.py`, `script/replication.py`,
`game/simulation.py`, `game/flat_model.py` and `game/range_model.py` preserve
the earlier strict research, replay and geometry models. The live body driver
does not run through those prototypes. Their reports measure their stated
models; they are not a list of failures of the imported native gameplay runtime.

The v1 replay schema deliberately keeps its original seven input fields. Native
command extensions with non-default values require a newer recording contract;
the v1 adapter rejects them instead of silently losing information. See
[SIMULATION_CONTRACT.txt](SIMULATION_CONTRACT.txt).

Observed native body traces now have a separate v2 recording/replay path, including
all eleven decoded command fields, model/data fingerprints, stored-state and
shot-boundary checks, explicit retirement, and missing-input/fault/truncation
diagnostics. Movement log v2 also preserves windows before old/duplicate filtering.
These diagnostics are opt-in. A 975-command offline-generated body trace matched
its replay; it is not a full Match/combat/network capture. See
[NATIVE_BODY_REPLAY_V2.txt](NATIVE_BODY_REPLAY_V2.txt).

The repeated lifecycle protocol model covers ten Soldier/Reaper/Soldier round
trips and ten leave/rejoins. Real encrypted loopback UDP packets pass through the
production router and ACK handling, with an independent world-record reader
feeding the body decoder model. The cases exercise create/full ACK gates,
ordinary retransmission, retired records, observer references, fresh ammo/weapon
state, and old connection packets after reentry. This is backend protocol-model
acceptance; the user's real-client lifecycle/HUD checks remain separate.

## Remaining acceptance and implementation work

- Validate Soldier: 76 firing/reload and stable HUD for at least 60 seconds, then
  repeated hero switches and leaving/reentering the Practice Range.
- Resolve robot overhead bars, hit presentation and elimination-name lookup using
  target-build evidence. The bot body cannot accept player-only component 123.
- Recheck upstream defects: stuck firing/ammo after kills, Sprint eventually
  failing, crashes after kills, skin-dependent Biotic Field and stale HUD health.
  The new regression repairs are not proof that these live symptoms are cured.
- Establish two-client combat and loss/reordering acceptance, then complete mode
  phases, objectives, doors, health packs, match results and additional heroes.
- Verify real command-epoch transitions and owner-frame recovery. Do not infer a
  restart merely from a low frame in a late datagram.

## Local startup

`START_LOCAL.cmd` uses `.venv`, `--test-players 1` and `--bnet-port 1118` on this
machine. Port 1119 is occupied by another application; the relay RPC port remains
21119. Passing an install folder or `Overwatch Launcher.exe` resolves to the
actual game executable. The original client files and launcher are retained.

## Lobby and data

The existing lobby includes menus/scenes, loot boxes, catalog purchases, profile
and cosmetic management, events/challenges, competitive menu data and the local
dashboard. See the upstream README for its scope.

`data/` contains target-build schemas/catalogs/graphs and some 1.68 reference
traffic decoded with 1.74 schemas. Raw captures are not bundled. Cross-version
references, unavailable specifications and evidence limits are recorded in
[RESEARCH_EVIDENCE.txt](RESEARCH_EVIDENCE.txt).

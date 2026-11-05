# Project state

## Works

- Login, the main menu, the lobby hero and lobby scenes (events).
- Loot boxes: all 11 types can be opened.
- The shop: prices come from the catalog, and each item takes the right currency (credits, league tokens or competitive points).
- The three Anniversary Remix scenes.
- The dashboard: profile, events, loot boxes, shop and skins.
- Hero challenges with a Play menu banner (Tracer, Symmetra, Hanzo's Kanezaka) and their rewards.
- Competitive seasons, ratings and the Top 500 board in the menus.

## Experimental game server

The `wip` branch includes a UDP game server. See [WIP.md](../WIP.md) for the upstream
gameplay research status. Loading a map does not imply a complete playable match.

On 2026-11-05, a local Windows client of build 1.74.0.0.104319 was observed to:

- Log in through the local Battle.net emulator, without the official launcher.
- Load the Practice Range, spawn, and display hero selection.
- Switch heroes; the server log recorded a switch to Doomfist.

Walking, multiplayer visibility and matchmaking are described in the upstream WIP notes;
they were not independently verified during this local UI check. The user stopped UI
automation with Escape after the hero-selection observation.

Weapons, abilities, reliable combat HUD, damage, death/respawn, objectives and complete
match phases are not implemented. `game/script/graph.py` loads graph data; the expression
interpreter and graph runtime still need implementation. Current movement is a flat mover,
without proper map collision or gravity.

## Local startup

- Use `START_LOCAL.cmd` for this machine's single-player research setup. It uses the
  isolated `.venv`, `--test-players 1`, and `--bnet-port 1118`.
- The original port 1119 is occupied on this machine. `--bnet-port` changes only the
  local client/stall port; the relay still connects to RPC port 21119.
- An installation folder or `Overwatch Launcher.exe` passed to `--game-exe` is resolved
  to the real `_retail_/Overwatch.exe`. The official launcher is never executed.
- The original game files and official launcher are retained.

## Next combat milestone

Start with Soldier: 76 in the Practice Range. Establish body statescript instances,
state/variable layouts and owner input acknowledgements, then verify that the weapon
and ammo HUD remain stable through client prediction rollback. Only then add firing,
ammo/reload, target hit detection and replicated damage. Input decoding or server-only
damage calculations alone are not combat acceptance criteria.

## Data

`data/` holds message layouts and catalogs taken from a 1.68 reference capture and the 1.74 client. Player names in it are anonymized. The raw captures are not included. `ow174/bnet/descriptors.pb` is Battle.net protocol data the server needs; it is not a program.

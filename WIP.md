# Work in progress: game server and matchmaking

This document is the imported upstream author's gameplay status at LizRollin
revision `9f8f3bcb5eccab6fb7738bee3c2371fcf0623f29`. Its "checked in the game"
claims are the author's, not independent acceptance of this local integration.
See [docs/STATE.md](docs/STATE.md) for local observations, user-reported defects
and the remaining acceptance work.

This branch adds our own game server and real matchmaking. It works in the game, but a lot is missing.

## What works (checked in the game)

- Matchmaking: a search that fills the teams starts a match. Parties are never split, role queue gives
  every player one of the roles they picked. The maps and modes come from the queue cards.
- For tests, the dashboard's "Players to start" starts a match with fewer players (1 = alone).
- "Game Found!" shows for 4 s before a queue match loads. The Practice Range loads at once.
- The game connects to our UDP game server (port 3730), loads the map and spawns the player.
- Hero select in the Practice Range and in the PvP modes, with skins. A skin chosen on the hero select
  screen during a match changes the body. The H key opens hero select again.
- PvP modes: "Assemble your team" with a 30 s countdown, then hero select closes.
- Movement: the server runs the client's own character physics on each map's collision, so your own
  movement and other players' bodies stay in sync (checked on the Practice Range and on Kanezaka).
- Soldier: 76: weapon, abilities and HUD; hits where the shooter saw the target, damage, death, respawn
  and the kill notices. Other players see his weapon and his shots.
- The Practice Range's training bots walk their routes, die and come back.
- Each hero's health bar. Chat lines when a player joins, leaves or switches heroes.
- Enemy players have a name, a health bar and an outline. F1 shows your hero. The competitive loading
  screen shows both teams.
- Leaving a match, alone or with your group. Leaving alone also leaves the group.
- Dashboard: players to start, the default account for the game.

Written but not checked in the game yet: the kill feed, Helix Rockets and Biotic Field on other players'
screens, no General chat during a match (it comes back in the menu), a queue match found while you are in
the Practice Range, a hero voice line on spawn (ow174/game/voicelines.py: the server begins one of the
hero's networked voice stimuli on the body and the client plays it; verified to reach the client in step,
not yet heard in the game. Works for the 18 heroes that have such a stimulus in their body statescript;
Soldier: 76 and the others whose spawn chatter is purely client-side get none).

## Known problems

- Soldier: 76 in the Practice Range: after a kill the fire button can stay pressed and the ammo stops going
  down; Biotic Field does not show with some skins; Sprint stops working after a while; a crash after
  kills. Pick a default skin for now.
- The HUD's health number does not follow damage.
- Tab does not open. The competitive loading screen shows no ratings.
- Soldier's ultimate, Helix self-damage and rocket jumps. Other players' shots can point the wrong way.
- The training bots have no names and no outline.
- Kanezaka's free-for-all always respawns you at the same point.
- Not tested over the internet yet (real ping).

## Not done yet

- Other heroes: their abilities need the server to run their statescript too.
- Match rules: the phases after "Assemble your team", objectives (points, payloads), spawn room doors,
  health packs, breakable objects, the end of the match.
- Custom games and "play while you wait" games, and the custom game browser.
- Event modes whose loading waits for a mode script (for example the Halloween ones).

## Data

`data/game_*.json` come from the scripts in `tools/` (`extract_game_*.py`, `dump_schemas.py`), which read a
DataTool dump of the 1.74 client. Map collision is read from the player's own game install and cached in
`cache/collision` (`tools/build_collision.py`). The game server needs `cryptography` (in `requirements.txt`).

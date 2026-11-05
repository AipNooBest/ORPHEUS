# Overwatch 1.74 Lobby Server

An offline lobby server for Overwatch 1.74 (build 104319) on Windows.

## How to start

1. Install [Python](https://www.python.org/downloads/windows/) 3.10 or newer.
2. Double-click `START.bat`.
3. Choose a mode. Press Enter for the normal one (retail).
4. The first time, pick your `Overwatch.exe`.

The server and the game start. Keep the black window open while you play.

For local server work without launching a game, use `START_SERVER_LOCAL.cmd`.
It binds the local services to loopback and uses Battle.net port 1118. From the
project environment, the equivalent is `python -m ow174 --mode retail --no-launch
--host 127.0.0.1 --bnet-port 1118 --test-players 1` (one line). `--no-launch` does
not find or close a game, prepare the relay, or create a client-launch manager.
The dashboard's client-start action is unavailable in this mode. Join modes and
`--game-exe` cannot be combined with it.

Local diagnostics are opt-in: `OW174_MOVELOG=1` retains full received input
windows and movement observations; `OW174_BODY_TRACE=1` records body-script
boundaries for `tools/replay_native_body.py`. Body hashing has measurable cost,
so it stays off by default. See [the v2 contract](docs/NATIVE_BODY_REPLAY_V2.txt)
for scope and incomplete-trace diagnostics, and [local state](docs/STATE.md) for
the distinction between implementation, model tests and real-client acceptance.

To switch modes, close the game and the black window, then start `START.bat` again and choose another mode.

- **Play in retail mode**: the default Overwatch menu, with a hero in the lobby.
- **Play in tournament mode**: the mode used on LANs by pros, with a simpler menu.
- **Host a server for others and play on it**: your server, open to players on other PCs, and your game on it (see below).
- **Host a server for others without playing here**: the same, without starting the game.
- **Join a server in retail mode**: play on someone else's server with the default Overwatch menu. Type its address, like `1.2.3.4:3724`, and your name. The next time, Enter reuses them.
- **Join a server in tournament mode**: the same with the mode used on LANs by pros.

## Host a server for others

1. In your router and firewall, open TCP port 3724 (the lobby) and UDP port 3730 (matches).
2. Start `START.bat` and choose 3 (to play too) or 4.
3. Type your public address when it asks (whatismyip.com shows it). The next time, Enter reuses it. Just Enter, the first time, if only players on your network play.
4. Give players your address with the port, for example `1.2.3.4:3724`. They choose one of the **Join a server** modes.

The server reads each map's collision from your own copy of the game, and builds a map's collision the first time a match is played there (or all maps at once with `py tools/build_collision.py`).

A match starts when its teams are full. To start it with fewer players, set "Players to start" in the dashboard. The dashboard stays reachable only on your own computer.

From a console: `START.bat --mode host --game-host 1.2.3.4`, another lobby port with `--port 12357`.

Anyone can log in with any name, so a player can take another player's name.

To manage your profile, events and loot boxes, open http://127.0.0.1:3725 in your browser.

The game is not included. You need your own copy of build 1.74.0.0.104319.

## Without START.bat

Open a terminal in this folder and run:

```
py -m ow174
```

It asks for the mode too. To skip the question, give it: `py -m ow174 --mode tournament`. `py -m ow174 --help` lists all options.

## If something goes wrong

- Start the game only with `START.bat` or `py -m ow174`, not from a shortcut.
- If your antivirus blocks it, add this folder and the game folder to its exceptions.
- If the game asks for an email and password, type anything. The server does not check them.
- Logs are in the `logs` folder. Send `logs/ow174.log` when you ask for help.

## For developers

```
py -m pip install -r requirements.txt ruff
py -m ruff check .
py -B -m unittest discover -s tests
```

The code is in `ow174/`. The relay DLL source is in `relay/`.

## Credits

Based on [Boi-027's research](https://github.com/Boi-027/Overwatch-1-v1.74-Lobby-Research). Thanks to everyone listed in [CREDITS.md](CREDITS.md). MIT license.

"""The one entry point, `py -m ow174` (START.bat): start the servers, then the game.

Modes:
  retail           the full main menu with the lobby hero: the lobby, a local Battle.net and the
                   game with the relay DLL, which strips TLS from its Battle.net connection (default)
  tournament       a simpler menu without the hero; the game dials the lobby directly, no relay
  host             retail, with the lobby open to players on other PCs: you host and play
  server           only the lobby server, open to players on other PCs
  join             the game with the full menu on someone else's server (--server host:port and
                   --name): a Battle.net emulator and the relay of its own send it there
  join-tournament  only the game, in tournament mode, on someone else's server (--server host:port)

--no-launch runs the selected host services without locating, closing or starting a game.
In retail/host it keeps the local Battle.net emulator, with no dashboard client-launch manager.
"""

import argparse
import logging
import socket
import sys
import threading
from pathlib import Path

from ow174.accounts.profile import load_or_create_profile
from ow174.accounts.registry import account_id_for
from ow174.bnet.session_key import MAX_NAME_BYTES
from ow174.content.presence import PRO_ACCOUNT_BITS
from ow174.dashboard.server import start_dashboard
from ow174.jam.handshake import lobby_state_blob_key
from ow174.launcher import LaunchError
from ow174.launcher.game import close_running_copy, find_game, start_game
from ow174.launcher.relay import ensure_relay_dll
from ow174.launcher.requirements import ensure_requirements
from ow174.launcher.retail import DEFAULT_BNET_PORT, RetailGames
from ow174.lobby.battle_tag_query import ask_battle_tag
from ow174.lobby.research import watch_inject_file
from ow174.lobby.server import LobbyServer
from ow174.lobby.settings import Settings
from ow174.log import setup_logging
from ow174.paths import (
    PLAYER_NAME_FILE,
    PUBLIC_ADDRESS_FILE,
    RUNTIME_DIR,
    SERVER_ADDRESS_FILE,
    Paths,
    missing_runtime_files,
)

log = logging.getLogger("ow174")

MODES = ("retail", "tournament", "host", "server", "join", "join-tournament")
HOSTING = ("host", "server")
MODE_MENU = """
Which mode?
  1  Play in retail mode (default Overwatch mode)
  2  Play in tournament mode (used on LANs by pros)
  3  Host a server for others and play on it
  4  Host a server for others without playing here
  5  Join a server in retail mode (default Overwatch mode)
  6  Join a server in tournament mode (used on LANs by pros)
"""
MAX_NAME_LENGTH = 24
EVERY_ADDRESS = "0.0.0.0"
CHECK_SECONDS = 5.0  # how long a join waits for the server to answer


def bnet_port_number(text: str) -> int:
    try:
        port = int(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Battle.net port must be between 1 and 65535") from error
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("Battle.net port must be between 1 and 65535")
    return port


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    defaults = Settings()
    parser = argparse.ArgumentParser(
        prog="START.bat", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=MODES, help="what to start; without it you are asked")
    parser.add_argument(
        "--no-launch", action="store_true",
        help="run host services only; no game lookup, relay or client launch (not for join modes)",
    )
    parser.add_argument("--server", help="address of the server to join, such as 1.2.3.4:3724")
    parser.add_argument("--name", help="your name on the server you join with the full menu")
    parser.add_argument(
        "--game-exe", type=Path,
        help="Overwatch.exe to start (default: the one picked before; incompatible with --no-launch)",
    )
    parser.add_argument(
        "--locale",
        default="auto",
        help="game language such as enUS; 'auto' picks one the build has, 'none' none",
    )
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="seconds to wait for the game (default: 30)"
    )
    parser.add_argument(
        "--host",
        help="address the lobby listens on (default: every address in server mode, else 127.0.0.1)",
    )
    parser.add_argument("--port", type=int, default=defaults.port, help="lobby port (default: 3724)")
    parser.add_argument(
        "--bnet-port",
        type=bnet_port_number,
        default=DEFAULT_BNET_PORT,
        help="local Battle.net client port (default: 1119; change it if that port is taken)",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=defaults.dashboard_port,
        help="dashboard port (default: 3725, 0: off)",
    )
    parser.add_argument(
        "--game-port",
        type=int,
        default=defaults.game_port,
        help="the game server's UDP port (default: 3730, 0: off)",
    )
    parser.add_argument(
        "--game-host",
        default=defaults.game_host,
        help="address games are sent to for a match, if not the one they reach the lobby at "
        "(behind a router's port forward: the public address)",
    )
    parser.add_argument(
        "--test-players",
        type=int,
        default=defaults.test_players,
        help="start a match once this many players search (default: 0 = full teams only)",
    )
    parser.add_argument(
        "--save", type=Path, default=defaults.paths.template, help="template profile for new accounts"
    )
    parser.add_argument(
        "--data-dir", type=Path, help="keep profiles and logs in this folder instead (the tests use it)"
    )
    return parser.parse_args(argv)


def ask_mode() -> str:
    """Ask for the mode in the console. Enter picks retail."""
    print(MODE_MENU)
    while True:
        answer = input(f"Press 1 to {len(MODES)}, then Enter (just Enter for 1): ").strip()
        if answer == "" or (answer.isdigit() and 1 <= int(answer) <= len(MODES)):
            return MODES[int(answer or "1") - 1]


def ask_public_address(saved: Path = PUBLIC_ADDRESS_FILE) -> str:
    """Ask the host for the public address players over the internet join with; their games are sent
    there for a match too. Enter reuses the one from last time, - is none (only players on this network
    or a VPN)."""
    last = saved.read_text(encoding="utf-8").strip() if saved.is_file() else ""
    print("Players over the internet join with your public address (whatismyip.com shows it).")
    hint = f"just Enter for {last}, - for none" if last else "just Enter if only your network plays"
    while True:
        answer = input(f"Your public address ({hint}): ").strip()
        if answer == "-" or not (answer or last):
            saved.unlink(missing_ok=True)
            return ""
        answer = answer or last
        if is_host_name(answer):
            saved.write_text(answer, encoding="utf-8")
            return answer
        print("Type only the address, for example 1.2.3.4, without a port.")


def is_host_name(text: str) -> bool:
    return bool(text) and all(char.isalnum() or char in ".-" for char in text)


def ask_server(saved: Path = SERVER_ADDRESS_FILE) -> str:
    """Ask for the server address. Enter reuses the one from last time."""
    last = saved.read_text(encoding="utf-8").strip() if saved.is_file() else ""
    hint = f" (just Enter for {last})" if last else ""
    while True:
        answer = input(f"Server address, like 1.2.3.4:3724{hint}: ").strip() or last
        if is_server_address(answer):
            saved.write_text(answer, encoding="utf-8")
            return answer
        print("Type the address as host:port, for example 1.2.3.4:3724.")


def is_server_address(text: str) -> bool:
    host, _, port = text.rpartition(":")
    return bool(host) and " " not in host and port.isdigit() and 0 < int(port) < 65536


def ask_name(saved: Path = PLAYER_NAME_FILE) -> str:
    """Ask for the player's name on the server. Enter reuses the one from last time."""
    last = saved.read_text(encoding="utf-8").strip() if saved.is_file() else ""
    hint = f" (just Enter for {last})" if last else ""
    while True:
        answer = input(f"Your name on the server{hint}: ").strip() or last
        if is_player_name(answer):
            saved.write_text(answer, encoding="utf-8")
            return answer
        print(f"Use 1 to {MAX_NAME_LENGTH} letters, digits, - or _.")


def is_player_name(text: str) -> bool:
    return (
        0 < len(text) <= MAX_NAME_LENGTH
        and len(text.encode("utf-8")) <= MAX_NAME_BYTES
        and all(char.isalnum() or char in "-_" for char in text)
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.mode is None:
        args.mode = ask_mode() if sys.stdin.isatty() and not args.no_launch else "retail"
        if args.mode in HOSTING and not args.game_host:
            args.game_host = ask_public_address()
    setup_logging(_paths(args).log_file)
    try:
        run(args)
    except LaunchError as error:
        log.error("%s", error)
        return 1
    except EOFError:  # a question without a console to answer it
        log.error("No answer to the question. Give it as an option instead, see START.bat --help.")
        return 1
    except KeyboardInterrupt:
        log.info("Stopped.")
    return 0


def _paths(args: argparse.Namespace) -> Paths:
    if args.data_dir is None:
        return Paths(template=args.save)
    folder = args.data_dir
    return Paths(
        profiles=folder / "profiles",
        template=args.save,
        client_log=folder / "client_msgs.log",
        inject_file=folder / "inject.jsonl",
        log_file=folder / "ow174.log",
    )


def run(args: argparse.Namespace) -> None:
    if args.no_launch and args.mode in ("join", "join-tournament"):
        raise LaunchError("--no-launch runs host services and cannot be used with a join mode.")
    if args.no_launch and args.game_exe is not None:
        raise LaunchError("--game-exe selects a client to launch and cannot be used with --no-launch.")
    if args.mode == "join":
        join(args)
        return
    if args.mode == "join-tournament":
        join_tournament(args)
        return
    retail = args.mode in ("retail", "host")
    if RUNTIME_DIR is not None:
        missing = missing_runtime_files(matches=args.game_port > 0, bnet=retail)
        if missing:
            raise LaunchError(f"Missing files in ORPHEUS_RUNTIME_DIR: {', '.join(missing)}")
    try:
        lobby_state_blob_key()
    except (OSError, ValueError) as error:
        raise LaunchError(f"Lobby state blob key is unavailable: {error}") from error
    launches_game = args.mode != "server" and not args.no_launch
    settings = Settings(
        host=listen_host(args),
        port=args.port,
        dashboard_port=args.dashboard_port,
        game_port=args.game_port,
        game_host=args.game_host,
        test_players=max(0, args.test_players),
        paths=_paths(args),
        build_missing_collision=launches_game,
    )
    game = relay = None
    if launches_game:
        game = find_game(args.game_exe)
        close_running_copy(game)
    if retail or settings.game_port > 0:
        ensure_requirements()
    if retail and not args.no_launch:
        relay = ensure_relay_dll()

    load_or_create_profile(settings.paths.template)
    server = LobbyServer(settings)
    listener = _bind(server)
    if settings.game_port > 0:
        _start_game_server(server)
    if settings.dashboard_port > 0:
        start_dashboard(server, port=settings.dashboard_port)
    threading.Thread(
        target=watch_inject_file, args=(server, settings.paths.inject_file), daemon=True, name="inject"
    ).start()
    if retail:
        if not args.no_launch:
            server.games = RetailGames(game, relay, args.locale, args.timeout, bnet_port=args.bnet_port)
        _start_bnet(server, args.bnet_port)
    _log_banner(server)

    if args.no_launch:
        log.info("[+] Server services ready; client launching is disabled.")
    elif retail:
        server.games.start()
    elif args.mode == "tournament":
        start_game(game, ["--tank_TournamentMode", f"--lobbyServer=127.0.0.1:{settings.port}"], args.locale)
    else:
        log.info("[+] To play here too, choose 3 next time instead of 4.")
    log.info("Keep this window open while you play; closing it stops the server.")
    server.serve_forever(listener)


def join(args: argparse.Namespace) -> None:
    """Start the game with the full menu on someone else's server. A Battle.net emulator of our own
    logs it in with the player's name and sends it on to that server's lobby."""
    address = _server_address(args)
    name = args.name or ask_name()
    if not is_player_name(name):
        raise LaunchError(f"{name} can't be a name. Use 1 to {MAX_NAME_LENGTH} letters, digits, - or _.")
    lobby = _ipv4_address(address)
    tag = _battle_tag_on(lobby, address, name)
    game = find_game(args.game_exe)
    close_running_copy(game)
    ensure_requirements()
    relay = ensure_relay_dll()
    # These need the packages ensure_requirements installs.
    from ow174.bnet.rpc_server import Player
    from ow174.bnet.service import RPC_PORT, start_bnet

    account = account_id_for(name)
    player = Player(account, account ^ PRO_ACCOUNT_BITS, tag, name)
    try:
        start_bnet(lambda peer: player, lobby, ports=(args.bnet_port, RPC_PORT))
    except OSError as error:
        raise LaunchError(
            f"The Battle.net ports are taken ({error}). Is a server or another game running here?"
        ) from error
    game_process = RetailGames(game, relay, args.locale, args.timeout, bnet_port=args.bnet_port).start()
    log.info("[+] The game is starting on %s as %s (%s).", address, name, tag)
    log.info("Keep this window open while you play; closing it disconnects the game.")
    game_process.wait()


def join_tournament(args: argparse.Namespace) -> None:
    """Start only the game, in tournament mode, pointed at someone else's server."""
    address = _server_address(args)
    host, _, port = address.rpartition(":")
    try:
        socket.create_connection((host, int(port)), timeout=CHECK_SECONDS).close()
    except OSError as error:
        raise LaunchError(_unreachable(address, error)) from error
    game = find_game(args.game_exe)
    close_running_copy(game)
    start_game(game, ["--tank_TournamentMode", f"--lobbyServer={address}"], args.locale)
    log.info("[+] The game is starting on %s. You can close this window.", address)


def listen_host(args: argparse.Namespace) -> str:
    """Where the lobby listens: --host, else every address when hosting (it is there for other PCs)
    and only this PC in the modes that play alone."""
    return args.host or (EVERY_ADDRESS if args.mode in HOSTING else Settings().host)


def _battle_tag_on(lobby: str, address: str, name: str) -> str:
    """The BattleTag the server shows for the name (its nickname can differ). Asking also checks
    that the server is up and new enough for a join with the full menu."""
    host, _, port = lobby.rpartition(":")
    try:
        tag = ask_battle_tag(host, int(port), name)
    except OSError as error:
        raise LaunchError(_unreachable(address, error)) from error
    if not tag:
        raise LaunchError(
            f"The server {address} runs an older version. Ask the host to update it, or choose 6."
        )
    return tag


def _unreachable(address: str, error: OSError) -> str:
    return f"Can't reach the server {address} ({error}). Check the address, and that the server runs."


def _server_address(args: argparse.Namespace) -> str:
    address = args.server or ask_server()
    if not is_server_address(address):
        raise LaunchError(f"{address} is not a server address. Use host:port, like 1.2.3.4:3724.")
    return address


def _ipv4_address(address: str) -> str:
    """host:port with the host as an IPv4 address: the Battle.net referral takes no names."""
    host, _, port = address.rpartition(":")
    try:
        return f"{socket.gethostbyname(host)}:{port}"
    except OSError as error:
        raise LaunchError(f"Can't find the server {host} ({error}).") from error


def _bind(server: LobbyServer):
    try:
        return server.listen()
    except OSError as error:
        raise LaunchError(
            f"Port {server.settings.port} is taken ({error}). "
            "Is the server already running in another window?"
        ) from error


def _start_game_server(server: LobbyServer) -> None:
    try:
        server.start_game_server()
    except OSError as error:
        raise LaunchError(
            f"UDP port {server.settings.game_port} is taken ({error}). "
            "Is the server already running in another window?"
        ) from error


def _start_bnet(server: LobbyServer, bnet_port: int = DEFAULT_BNET_PORT) -> None:
    # These need the packages ensure_requirements installs.
    from ow174.bnet.rpc_server import Player
    from ow174.bnet.service import RPC_PORT, start_bnet

    def player(peer: tuple) -> Player:
        # Battle.net logs in as the account the lobby will use, with the ids its presence shows.
        account = server.game_account(peer[1], RPC_PORT)
        game_account = account.account_lo ^ PRO_ACCOUNT_BITS
        return Player(account.account_lo, game_account, account.battle_tag, account.name)

    try:
        start_bnet(player, f"127.0.0.1:{server.settings.port}", ports=(bnet_port, RPC_PORT))
    except OSError as error:
        raise LaunchError(
            f"The Battle.net ports are taken ({error}). Is the server already running in another window?"
        ) from error


def _log_banner(server: LobbyServer) -> None:
    settings = server.settings
    groups = server.schemas.groups
    log.info("OVERWATCH 1.74 LOBBY SERVER")
    log.info(" [*] Bind:           %s:%d", settings.host, settings.port)
    if settings.host == EVERY_ADDRESS:
        addresses = ", ".join(f"{address}:{settings.port}" for address in network_addresses())
        log.info(" [*] Your network:   %s", addresses or "no address found")
        if settings.game_host:
            log.info(
                " [*] Internet:       %s:%d (open TCP %d and UDP %d in your router and firewall)",
                settings.game_host,
                settings.port,
                settings.port,
                settings.game_port,
            )
        else:
            log.info(" [*] Internet:       off (no public address, --game-host)")
    log.info(" [*] Accounts:       %s (profiles/)", ", ".join(server.accounts.all_saved()) or "none yet")
    log.info(" [*] New accounts:   copy of %s", settings.paths.template.name)
    log.info(" [*] Schemas:        %d messages in %d protocols", sum(map(len, groups.values())), len(groups))
    log.info(" [*] Log file:       %s", settings.paths.log_file)


def network_addresses() -> list[str]:
    """This PC's IPv4 addresses that players on other PCs can dial (LAN, a VPN such as Radmin)."""
    try:
        found = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return []
    addresses = []
    for *_, (address, _port) in found:
        if not address.startswith(("127.", "169.254.")) and address not in addresses:
            addresses.append(address)
    return addresses

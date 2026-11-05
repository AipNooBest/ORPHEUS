"""Picking the mode when START.bat is double-clicked, and joining someone else's server."""

import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ow174 import cli
from ow174.accounts.registry import account_id_for
from ow174.bnet.service import RPC_PORT
from ow174.launcher import LaunchError


class AskModeTests(unittest.TestCase):
    def ask(self, *answers):
        with patch("builtins.input", side_effect=answers), patch("builtins.print"):
            return cli.ask_mode()

    def test_enter_picks_retail(self):
        self.assertEqual(self.ask(""), "retail")

    def test_numbers_pick_the_modes(self):
        self.assertEqual(self.ask("1"), "retail")
        self.assertEqual(self.ask(" 2 "), "tournament")
        self.assertEqual(self.ask("3"), "host")
        self.assertEqual(self.ask("4"), "server")
        self.assertEqual(self.ask("5"), "join")
        self.assertEqual(self.ask("6"), "join-tournament")

    def test_anything_else_asks_again(self):
        self.assertEqual(self.ask("tournament", "9", "0", "2"), "tournament")

    def test_a_mode_on_the_command_line_is_not_asked(self):
        self.assertEqual(cli.parse_args(["--mode", "tournament"]).mode, "tournament")
        self.assertIsNone(cli.parse_args([]).mode)


class BnetPortTests(unittest.TestCase):
    def test_the_local_bnet_port_defaults_to_1119(self):
        self.assertEqual(cli.parse_args([]).bnet_port, 1119)

    def test_a_custom_port_and_the_valid_boundaries_are_accepted(self):
        for port in (1, 1118, 65535):
            with self.subTest(port=port):
                self.assertEqual(cli.parse_args(["--bnet-port", str(port)]).bnet_port, port)

    def test_invalid_ports_are_rejected_before_startup(self):
        for port in ("0", "-1", "65536", "not-a-port"):
            with self.subTest(port=port), patch("sys.stderr"), self.assertRaises(SystemExit):
                cli.parse_args(["--bnet-port", port])

    def test_the_retail_bnet_service_uses_the_configured_stall_port_and_fixed_rpc_port(self):
        server = SimpleNamespace(settings=SimpleNamespace(port=3724))
        for port in (1119, 1118):
            with self.subTest(port=port), patch("ow174.bnet.service.start_bnet") as start_bnet:
                cli._start_bnet(server, port)
                self.assertEqual(start_bnet.call_args.args[1], "127.0.0.1:3724")
                self.assertEqual(start_bnet.call_args.kwargs, {"ports": (port, RPC_PORT)})


class NoLaunchTests(unittest.TestCase):
    @contextmanager
    def host_services(self, mode, *, no_launch=True, options=()):
        flags = ["--mode", mode, *options]
        if no_launch:
            flags.append("--no-launch")
        args = cli.parse_args(flags)
        names = (
            "find_game", "close_running_copy", "start_game", "ensure_requirements", "ensure_relay_dll",
            "load_or_create_profile", "LobbyServer", "_bind", "_start_game_server", "start_dashboard",
            "RetailGames", "_start_bnet", "_log_banner",
        )
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch.object(cli, name)) for name in names}
            mocks["thread"] = stack.enter_context(patch.object(cli.threading, "Thread"))
            server = mocks["LobbyServer"].return_value
            server.games = None  # the real LobbyServer starts without a client manager
            mocks["find_game"].return_value = Path("Overwatch.exe")
            mocks["ensure_relay_dll"].return_value = Path("relay.dll")
            yield args, mocks, server

    def test_no_launch_is_explicit_and_defaults_off(self):
        self.assertFalse(cli.parse_args([]).no_launch)
        self.assertTrue(cli.parse_args(["--no-launch"]).no_launch)

    def test_all_host_modes_keep_services_and_never_touch_a_game(self):
        for mode in ("retail", "host", "tournament", "server"):
            with self.subTest(mode=mode), self.host_services(mode) as (args, mocks, server):
                cli.run(args)
                for name in (
                    "find_game", "close_running_copy", "start_game", "ensure_relay_dll", "RetailGames",
                ):
                    mocks[name].assert_not_called()
                self.assertIsNone(server.games)  # also disables dashboard client launching
                self.assertFalse(mocks["LobbyServer"].call_args.args[0].build_missing_collision)
                mocks["_bind"].assert_called_once_with(server)
                mocks["_start_game_server"].assert_called_once_with(server)
                mocks["start_dashboard"].assert_called_once_with(server, port=args.dashboard_port)
                mocks["ensure_requirements"].assert_called_once()
                mocks["thread"].return_value.start.assert_called_once()
                server.serve_forever.assert_called_once_with(mocks["_bind"].return_value)
                if mode in ("retail", "host"):
                    mocks["_start_bnet"].assert_called_once_with(server, args.bnet_port)
                else:
                    mocks["_start_bnet"].assert_not_called()

    def test_no_launch_preserves_the_service_disable_options(self):
        with self.host_services("server", options=("--game-port", "0", "--dashboard-port", "0")) as values:
            args, mocks, _server = values
            cli.run(args)
            mocks["_start_game_server"].assert_not_called()
            mocks["start_dashboard"].assert_not_called()
            mocks["ensure_requirements"].assert_not_called()

    def test_join_modes_are_refused_before_network_or_client_work(self):
        for mode in ("join", "join-tournament"):
            with (
                self.subTest(mode=mode),
                patch.object(cli, "join") as join,
                patch.object(cli, "join_tournament") as tournament,
                self.assertRaisesRegex(LaunchError, "join mode"),
            ):
                cli.run(cli.parse_args(["--mode", mode, "--no-launch"]))
            join.assert_not_called()
            tournament.assert_not_called()

    def test_a_client_path_is_refused_instead_of_silently_ignored(self):
        with self.host_services("retail", options=("--game-exe", "Overwatch.exe")) as (args, mocks, _server):
            with self.assertRaisesRegex(LaunchError, "--game-exe"):
                cli.run(args)
            mocks["LobbyServer"].assert_not_called()
            mocks["find_game"].assert_not_called()

    def test_no_launch_without_a_mode_does_not_ask_an_interactive_question(self):
        with (
            patch.object(cli.sys.stdin, "isatty", return_value=True),
            patch.object(cli, "ask_mode") as ask,
            patch.object(cli, "setup_logging"),
            patch.object(cli, "run") as run,
        ):
            self.assertEqual(cli.main(["--no-launch"]), 0)
        ask.assert_not_called()
        self.assertEqual(run.call_args.args[0].mode, "retail")
        self.assertTrue(run.call_args.args[0].no_launch)

    def test_normal_retail_still_uses_its_client_manager(self):
        with self.host_services("retail", no_launch=False) as (args, mocks, server):
            cli.run(args)
            mocks["find_game"].assert_called_once_with(None)
            mocks["close_running_copy"].assert_called_once_with(Path("Overwatch.exe"))
            mocks["ensure_relay_dll"].assert_called_once()
            self.assertTrue(mocks["LobbyServer"].call_args.args[0].build_missing_collision)
            mocks["RetailGames"].assert_called_once_with(
                Path("Overwatch.exe"), Path("relay.dll"), "auto", 30.0, bnet_port=args.bnet_port,
            )
            self.assertIs(server.games, mocks["RetailGames"].return_value)
            server.games.start.assert_called_once()

    def test_normal_tournament_still_starts_the_requested_client(self):
        with self.host_services("tournament", no_launch=False) as (args, mocks, _server):
            cli.run(args)
            mocks["start_game"].assert_called_once_with(
                Path("Overwatch.exe"), ["--tank_TournamentMode", "--lobbyServer=127.0.0.1:3724"], "auto",
            )


class JoinTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.saved = Path(temp.name) / "server_address.txt"

    def ask(self, *answers):
        with patch("builtins.input", side_effect=answers), patch("builtins.print"):
            return cli.ask_server(self.saved)

    def test_server_addresses(self):
        self.assertTrue(cli.is_server_address("1.2.3.4:12357"))
        self.assertTrue(cli.is_server_address("lobby.example.org:3724"))
        for text in ("", "1.2.3.4", ":12357", "1.2.3.4:port", "1.2.3.4:0", "1.2.3.4:70000", "a b:1"):
            self.assertFalse(cli.is_server_address(text), text)

    def test_the_address_is_remembered_and_enter_reuses_it(self):
        self.assertEqual(self.ask("nope", "1.2.3.4:12357"), "1.2.3.4:12357")
        self.assertEqual(self.ask(""), "1.2.3.4:12357")

    def test_a_tournament_join_starts_only_the_game_on_that_server(self):
        args = SimpleNamespace(server="1.2.3.4:12357", game_exe=None, locale="auto")
        with (
            patch.object(cli.socket, "create_connection"),
            patch.object(cli, "find_game", return_value=Path("Overwatch.exe")),
            patch.object(cli, "close_running_copy"),
            patch.object(cli, "start_game") as start_game,
            patch.object(cli, "LobbyServer") as lobby,
        ):
            cli.join_tournament(args)
        start_game.assert_called_once_with(
            Path("Overwatch.exe"), ["--tank_TournamentMode", "--lobbyServer=1.2.3.4:12357"], "auto"
        )
        lobby.assert_not_called()

    def test_a_bad_address_on_the_command_line_is_refused(self):
        args = SimpleNamespace(server="1.2.3.4", name="Jinxzi", game_exe=None, locale="auto")
        for join in (cli.join, cli.join_tournament):
            with self.assertRaises(LaunchError):
                join(args)

    def test_a_join_to_a_server_that_does_not_answer_starts_no_game(self):
        # Checked before anything else: a running game is not closed for nothing.
        args = SimpleNamespace(server="1.2.3.4:12357", name="Jinxzi", game_exe=None, locale="auto")
        refused = ConnectionRefusedError("refused")
        with (
            patch.object(cli.socket, "gethostbyname", return_value="1.2.3.4"),
            patch.object(cli.socket, "create_connection", side_effect=refused),
            patch.object(cli, "close_running_copy") as close,
        ):
            with self.assertRaisesRegex(LaunchError, "Can't reach"):
                cli.join_tournament(args)
            with patch.object(cli, "ask_battle_tag", side_effect=refused), self.assertRaises(LaunchError):
                cli.join(args)
            older_server = patch.object(cli, "ask_battle_tag", return_value="")
            with older_server, self.assertRaisesRegex(LaunchError, "older version"):
                cli.join(args)
        close.assert_not_called()

    def test_hosting_listens_for_other_pcs(self):
        self.assertEqual(cli.listen_host(cli.parse_args(["--mode", "server"])), "0.0.0.0")
        self.assertEqual(cli.listen_host(cli.parse_args(["--mode", "host"])), "0.0.0.0")
        self.assertEqual(cli.listen_host(cli.parse_args(["--mode", "retail"])), "127.0.0.1")
        self.assertEqual(
            cli.listen_host(cli.parse_args(["--mode", "server", "--host", "10.0.0.2"])), "10.0.0.2"
        )

    def test_the_public_address_is_remembered_and_dash_drops_it(self):
        saved = self.saved.with_name("public_address.txt")

        def ask(*answers):
            with patch("builtins.input", side_effect=answers), patch("builtins.print"):
                return cli.ask_public_address(saved)

        self.assertEqual(ask(""), "")  # nothing saved yet: only the host's network plays
        self.assertEqual(ask("1.2.3.4:3724", "1.2.3.4"), "1.2.3.4")
        self.assertEqual(ask(""), "1.2.3.4")
        self.assertEqual(ask("-"), "")
        self.assertFalse(saved.exists())

    def test_names(self):
        for name in ("Jinxzi", "Игрок_2", "a-b"):
            self.assertTrue(cli.is_player_name(name), name)
        for name in ("", "two words", "Tag#1234", "a" * 25):
            self.assertFalse(cli.is_player_name(name), name)

    def test_the_name_is_remembered_and_enter_reuses_it(self):
        saved = self.saved.with_name("player_name.txt")
        with patch("builtins.input", side_effect=["bad name", "Jinxzi", ""]), patch("builtins.print"):
            self.assertEqual(cli.ask_name(saved), "Jinxzi")
            self.assertEqual(cli.ask_name(saved), "Jinxzi")

    def test_a_join_logs_in_with_the_name_and_goes_to_that_server(self):
        # The game gets the full menu from a Battle.net emulator of its own, which puts the name in
        # the session key and sends the game on to the server's lobby.
        self.check_join(1119)

    def test_a_join_uses_the_custom_local_bnet_port_for_the_service_and_game(self):
        self.check_join(1118)

    def check_join(self, port):
        args = cli.parse_args([
            "--mode", "join", "--server", "lobby.example.org:12357", "--name", "Jinxzi",
            "--bnet-port", str(port),
        ])
        with (
            patch.object(cli, "find_game", return_value=Path("Overwatch.exe")),
            patch.object(cli, "close_running_copy"),
            patch.object(cli, "ensure_requirements"),
            patch.object(cli, "ensure_relay_dll", return_value=Path("relay.dll")),
            patch.object(cli.socket, "gethostbyname", return_value="1.2.3.4"),
            patch.object(cli, "ask_battle_tag", return_value="XQC#6691") as ask,
            patch("ow174.bnet.service.start_bnet") as start_bnet,
            patch.object(cli, "RetailGames") as games,
        ):
            cli.join(args)
        player, lobby = start_bnet.call_args.args
        self.assertEqual(start_bnet.call_args.kwargs, {"ports": (port, RPC_PORT)})
        self.assertEqual(lobby, "1.2.3.4:12357")
        account = player(("127.0.0.1", 50000))
        self.assertEqual((account.account, account.name), (account_id_for("Jinxzi"), "Jinxzi"))
        ask.assert_called_once_with("1.2.3.4", 12357, "Jinxzi")
        self.assertEqual(account.battle_tag, "XQC#6691")  # the nickname the host shows
        games.assert_called_once_with(Path("Overwatch.exe"), Path("relay.dll"), "auto", 30.0, bnet_port=port)
        games.return_value.start.return_value.wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()

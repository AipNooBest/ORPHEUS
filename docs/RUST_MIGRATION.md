# Rust migration

The migration target is a Rust implementation of ORPHEUS for retail build 1.74.0.0.104319. The Python server is the reference while its behavior is ported. Its current gameplay limitations remain tracked in the [restoration roadmap](ROADMAP.md).

| Stage | Scope | Exit criteria |
| --- | --- | --- |
| Transport and serialization | JAM lobby handshake and framing, lobby and game message codecs, game UDP encryption and ACKs, bit streams and command records | Rust reproduces the reference bytes and fragmented streams, rejects malformed records, and handles loss, duplicates and rollover. |
| Login and lobby | Battle.net login, accounts, profiles, lobby, chat, parties, catalog, queues and match handoff | Match the reference service requests and replies. Load existing profile and external runtime formats. Verify a lobby client can acquire a valid match seat. |
| Match simulation | Tick scheduling, collision, movement, statescript, combat, bots, heroes, maps and modes | Match the registered state and packet order from encrypted Practice Range and two-peer replays. Check sustained-fire tick time and snapshot gaps with the same inputs. |
| Server tools and dashboard | Data preparation and extraction tools, replay and diagnostics, administration API and browser UI | Reproduce output formats and acceptance checks with Rust tooling and the Rust dashboard backend. Migrate browser behavior to Rust compiled to WebAssembly. |
| Windows deployment | Launcher, retail relay and server-only distribution | Build the Windows tools and relay, and boot the audited checkout from the separate runtime. Verify supported connection paths with the original client. |
| Cutover | Change the normal entry points and remove superseded implementations | Pass the prior behavioral suite, confirm original-client acceptance, and complete a server-only smoke test of the audited public checkout. |

The Rust crate has a local Battle.net listener and a verified subset of the lobby login, including presence, progression, the hero collection and roster messages with known party and ranked inputs. A headless test joins the solo Practice Range through the lobby dispatcher and native UDP worker using the base graph bundle. It verifies ownership, baseline retries, controller and mode delivery, bot movement, and hero selection and replacement under packet loss.

The Rust movement components cover command recovery, timed modifiers, steering, ground probes, ordered static shape sweeps, and capsule contact generation and clustering. They have Python comparisons using prepared 104319 data. Per-body contact tracking, pose integration and owner corrections still need to reach the match worker. The Rust statescript components cover expression evaluation, supported same-time initialization, checked state payload layouts, and full snapshots for owners and observers. Unsupported startup actions report diagnostics. The enriched bundle still requires the bot VM before native world entry. Timed and event-driven script execution, body and bot state replication in matches, and combat remain open.

The Python server serves the regular lobby and match entry points during the migration.

Public Rust tests use synthetic protocol vectors and local loopback connections. They need neither a client nor prepared deployment data. The Rust `prepare_server_runtime` command assembles an external runtime pack from authorized inputs. The Practice Range pack was checked against the Python tool by comparing the manifest and every copied file.

```powershell
cargo test --locked --all-targets
cargo fmt --all -- --check
cargo clippy --locked --all-targets -- -D warnings
```

Keep data formats and existing verification scenarios stable during replacement. Reuse shared codecs and keep each subsystem as small as the observed behavior allows. Use the same clock, input, collision data and packet-loss schedule for both implementations. Check bytes where the format is fixed, and compare decoded event order and registered state when runs contain nondeterministic values. Measure tick latency and snapshot gaps before claiming a performance improvement. The target client remains the final check for presentation and feel.

Record the source and target build of external research in [CREDITS.md](../CREDITS.md) and the relevant change. Treat Beta hit-confirm and voice behavior as leads to verify against the retail graph and message schemas. Keep client executables, prepared data, private archives, logs, and compiled binaries outside published source.

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

The first Rust crate ports the transport and protocol layer. The Python server remains the runnable entry point while the lobby and simulation are ported. Public Rust tests use synthetic protocol vectors and local loopback connections. They need neither a client nor prepared deployment data.

```powershell
cargo test --locked --all-targets
cargo fmt --all -- --check
cargo clippy --locked --all-targets -- -D warnings
```

Keep data formats and existing verification scenarios stable during replacement. Use the same clock, input, collision data and packet-loss schedule for both implementations. Check bytes where the format is fixed, and compare decoded event order and registered state when runs contain nondeterministic values. The target client remains the final check for presentation and feel.

Record the source and target build of external research in [CREDITS.md](../CREDITS.md) and the relevant change. Treat Beta hit-confirm and voice behavior as leads to verify against the retail graph and message schemas. Keep client executables, prepared data, private archives, logs, and compiled binaries outside published source.

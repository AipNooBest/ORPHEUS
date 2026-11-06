# Credits and acknowledgments

This repository builds on existing Overwatch preservation work. Its lobby improvements, dashboard, packaging and experiments would not have been possible without the code, tools and findings shared by other researchers.

## Foundation

- **[Boi-027](https://github.com/Boi-027/Overwatch-1-v1.74-Lobby-Research)** — the original Overwatch 1.74 Lobby Research repository on which this project is based, including its protocol research, implementation and reference material. Please check out the original project.
- **Arlecchino** — the original copyright holder named in the upstream MIT license. The original notice is retained in [LICENSE](LICENSE).

## Protocol research and shared findings

- **Xref** — big thanks for sharing a knowledge base of captured Overwatch 2 game traffic and research on the 1.74 game server. It gave us the facts behind the game server, matchmaking and hero select, and saved a lot of time.
- **The AyakaPS Discord community** — fellow Overwatch researchers who share findings, information and research with one another. Thanks to the members whose shared work helped this project.
- **Logo2K, a researcher in the AyakaPS community** — shared research notes and a lobby-map reference:
  - [Overwatch 1.74 lobby research](https://rentry.co/3bswrgmw#overwatch-174-lobby-protocol-channel-setup-login-and-loot-boxes)
  - [Lobby map list](https://rentry.co/wcwyv3r8) — a list of every lobby map, updated as additional information is found.
- **Zagrion (AyakaPS)** — credited in the upstream research for the server-to-client key work and TCP reassembly method used to recover the reference traffic.
- **Sidiusz** — credited by the upstream project for helping investigate and resolve research blockers.
- **Blizless** — acknowledged alongside AyakaPS in the inherited handshake implementation for state-blob findings.

## Tools and related projects

- **[LizRollin's 1.74 runtime branch](https://github.com/LizRollin/overwatch-1.74-lobby-server/tree/9f8f3bcb5eccab6fb7738bee3c2371fcf0623f29)** —
  the expression interpreter, native statescript codecs, driver, character physics, collision,
  combat and associated tests evaluated and integrated at this pinned revision. Its MIT notice
  is retained. Upstream live-test claims and known defects are recorded separately from our local validation.
- **[AipNooBest's research](https://github.com/AipNooBest/overwatch-1.74-lobby-server/tree/f8426859c3195691d379f2fb291a475dfa40139e)** —
  additional body initialization and state-payload research used for comparison. Later HUD corrections
  were checked before using earlier conclusions.

- **[overtools / OWLib](https://github.com/overtools/OWLib)** — DataTool, TankLib, asset definitions and extraction tools used to inspect client data and prepare catalogs.
  The local target-build extraction also uses the `overwatch/1.74` source revision
  `7afe5f52706e7002ed8c2717299cd60846f3fa3a`, with its pinned TACTLib and revorbstd dependencies.
- **[overtools / TACTLib](https://github.com/overtools/TACTLib)** — CASC/TACT reading and related tooling used by the extraction toolchain.
- **[Plasmawatch](https://github.com/plasmawatch/login-server)** — Battle.net login-server emulation and protocol reference work.
- **[Prometheus](https://github.com/saturn-xvi/prometheus/tree/ad1fea7e6c04c9959c7115cb8ecd9c79c93c1ccb)** —
  early/beta Overwatch research, also compared here for controller/model associations,
  statescript structure and replication methods. Its beta component IDs and memory writes
  are not transplanted into the 1.74 server; it remains a separate project with its own license.
- **The authors of the community TLS relay** — the foundation of the relay used for the local retail connection.

## Rust migration and behavior references

- **Boi-027, Arlecchino and the upstream 1.74 server contributors** supplied the existing lobby handshake,
  JAM cipher and framing that serve as the Rust port's protocol reference. The original MIT notice is retained.
- **LizRollin and the original game runtime contributors** supplied the inherited game transport,
  float32 collision and character mover, statescript runtime and payload layouts, and the hitscan,
  predicted projectile and combat notice behavior used as Rust references. The pinned runtime
  source is listed above.
- **[RustCrypto](https://github.com/RustCrypto)** maintains the AES-GCM, HMAC and SHA-256 crates used by the Rust
  transport. **[The Rust Random Project](https://github.com/rust-random/getrandom)** supplies access to operating
  system randomness for the lobby challenge.
- **[Prost](https://github.com/tokio-rs/prost)** and **[prost-reflect](https://github.com/andrewhickman/prost-reflect)**
  supply protobuf framing and reflection over the external Battle.net descriptors.
- **[Tungstenite](https://github.com/snapview/tungstenite-rs)** supplies WebSocket framing for the local
  Battle.net listener.
- **[lzma-rust2](https://github.com/hasenbanck/lzma-rust2)** supplies XZ decoding for the movement
  and expanded-ray collision caches.
- **[Mio](https://github.com/tokio-rs/mio)** supplies portable UDP readiness polling and cross-thread
  wakeups for the Rust game worker.
- **[flate2](https://docs.rs/flate2/)** supplies gzip decoding for the external state graph bundle.
- The statescript VM uses a deterministic SplitMix64 stream for local random expressions. The fixed-increment generator follows the public-domain implementation. [Sebastiano Vigna's SplitMix64](https://prng.di.unimi.it/splitmix.c)
- **TripleFault and Gamayun contributors** supplied a same-build comparison for encrypted combat scenarios and
  tick measurement design. The locally reviewed 104319 VM code in `internal/vm/natives.go` also corroborates
  the separate possessed-body and current-possessor queries and the health pool type selectors.
  Its `registry.go` and `states.go` provide behavior references for the synced game-code state,
  the tips state and the input bridge. Its `switch.go` and `instance.go` corroborate delayed
  branch timing and event ordering. Gamayun identifies its Go implementation as AGPL-3.0-or-later
  and retains the MIT notice for inherited code.
- **The Claudewatch contributors** supplied the Beta 1.0.0.3 research lead for attacker-side hit-confirm effects
  and separate entity and announcer voice messages. ORPHEUS checks these leads against 104319 client data before
  implementing them. The reference is a locally reviewed binary distribution.

Thank you as well to everyone who shared captures, tools, notes, testing, corrections and encouragement, including contributors whose names were not recorded in the materials available to us. If a credit is missing or needs correcting, please open an issue so it can be fixed.

Original copyright and attribution notices are retained. References to related projects acknowledge their work; their code and materials retain their respective licenses.

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

Thank you as well to everyone who shared captures, tools, notes, testing, corrections and encouragement, including contributors whose names were not recorded in the materials available to us. If a credit is missing or needs correcting, please open an issue so it can be fixed.

Original copyright and attribution notices are retained. References to related projects acknowledge their work; their code and materials retain their respective licenses.

# Changelog

This log records tested changes and their acceptance status. Entries are grouped by date until a version is explicitly released.

## Unreleased

### 2026-11-05

#### Project setup

- Named the project ORPHEUS. Added architecture, local startup, and development instructions, plus persistent rules for Conventional Commits, review, and GitHub synchronization.
- Created `leafall903/ORPHEUS` with `main` as its default branch. The original local Git history retains the research material for development.

#### Baseline

- Integrated local login, lobby, match handoff, collision movement, statescript and encrypted UDP replication for build 104319.
- Added controlled input and packet replay, registered server state comparisons, and collision data readiness. Earlier offline real Practice Range evidence covers movement, bot damage, elimination, and respawn.
- Fixed bot creation ordering and latest health delivery after ACK, terminal script recovery on deliberate same-hero reselect, and the repeated self-reference of a Stack modifier.

#### Verification and acceptance

- The baseline at `5ce3414` passed 1086 Python tests. Ruff and Git diff checks passed.
- The manual test of the earlier `aeb2d57` copy still had gaps in animation and HUD, target feedback, names, kill feed, audio, void death, and hero skills. Complete Soldier: 76 and Practice Range client acceptance remains pending.

#### Fixed

- Retired a player's old health, hit history and respawn timer when the body is replaced or the player leaves. A second body now waits for its own death timer, even if an earlier body was replaced before its timer expired.
- Passed 55 targeted Python tests covering the two-death regression, body recovery, repeated switches and reentry, encrypted server replay, and bot notices. Ruff and Git diff checks passed. Client acceptance for the change is pending.

#### Repository checks

- Kept the CI relay compilation check and removed its artifact upload. Verified the workflow diff and checked that the build and regression steps remain in the workflow.

#### Combat fixes

- Swept the final fraction of projectile flight before expiry. Soldier: 76's Helix Rockets now detect impacts in the final part of their configured range and report the time they actually flew.
- Used the volley timestamp for hitscan notice attribution. Shots caught up after missing input report their own frames. Their aim, position, and target rewind still use the available command sample.
- Passed 36 targeted projectile, combat, command epoch, and timing tests. Client acceptance for the changes is pending.

#### Standalone server

- Added an external runtime directory for protocol tables, graph and map data, the lobby signing input, Battle.net descriptors, and prepared collision. The preparation tool copies the required files from a local research checkout and writes a SHA-256 manifest.
- Made server-only modes load collision caches without searching for a client installation. Startup reports missing runtime files before binding, and the map loader reports missing collision.
- Kept upstream notices and references, added the project contributor list, and documented the separate publication history and GitHub synchronization workflow.
- Passed 69 targeted Python tests, including lobby wire vectors and collision readiness, plus Ruff and Git diff checks. An isolated Windows deployment completed a signed lobby handshake and served the Battle.net WebSocket, dashboard, and UDP match. The Practice Range loaded its prepared collision and spawned 15 bots. Manual client acceptance is pending.
- Passed all 1091 Python tests for the standalone server change in the prepared development checkout.

#### GitHub synchronization

- Published the audited standalone server source on GitHub `main`. Verified the public branch head, the complete reachable object audit, and the clean checkout startup with external runtime data.
- Rehearsed the local synchronization command through history export, strict audit, fast-forward verification, and isolated startup. Documented the command for future tested commits.
- Configured the repository to credit new commits to `leafall903` using the GitHub noreply identity. Codex assistance is recorded in the `Assisted-by` trailer.

#### Biotic Field

- Replicated Soldier: 76's field skin theme to the caster and observers, including the Grillmaster theme. Shared the normal entity creation and movement encoder for the parented and world-space field records. The default field records retain their original wire bytes.
- Passed 72 targeted tests covering the theme, the caster's canister binding, observer placement, healing, expiry, and server replay. Ruff and Git diff checks passed. Client rendering and target-build canister placement are pending acceptance.

#### Statescript vectors

- Preserved the XYZ coordinates of vector constants in the build 104319 graph export. Biotic Field's launch and target-centre offsets now evaluate to their configured values in the shared interpreter. Lowercase coordinates remain supported.
- Passed 83 tests for expressions, abilities, body replication and graph extraction. Live behavior and the Biotic Field launch and collision path remain pending.

#### Match handoff

- Rejected UDP handshakes for expired or cancelled match seats immediately, even when the scheduler receives a packet before the tick that releases the seat. A valid handshake can still claim its seat until the deadline.
- Passed 93 tests covering the production scheduling loop, encrypted handoff, cancellation replay, matchmaking, and leaving. Broader scheduler replay and manual client acceptance remain pending.

#### Target list replication

- Encoded ordered entity and age entries supplied to the shared TrackTargets owner payload. Empty lists keep their existing wire representation.
- Passed 66 tests covering the client-derived codec, a Tactical Visor owner delta, body replication and server replay. Target selection, age calculation, authoritative aim and client acceptance remain pending.

#### Scheduler replay

- Ran finite UDP datagrams, timeouts and receive errors through the production game scheduler in memory. Replay checks clock reads, skipped ticks, socket calls, encrypted sends and ordered join and leave callbacks.
- Exercised controlled callback reentry using the existing seat cancellation and player removal methods. Invalid scripts and scheduler or callback failures reject the journal. Stored-state mode compares stopped boundaries around the loop.
- Passed 53 primary tests and the isolated snapshot regressions. A real 0688 Practice Range replay matched 74 receives and 26 encrypted sends, including Soldier: 76 spawn and a callback-driven seat cancellation. Live thread interleavings and manual client acceptance remain pending.

#### Collision fingerprint validation

- Compared derived collision cache entries directly before encoding the geometry fingerprint. The comparison retains exact types, finite numbers and signed zero checks.
- Passed 29 final collision fingerprint and tape tests. Ten A/B samples on a warmed 0688 world produced identical descriptors, with approximately 21% less median CPU time for the diagnostic fingerprint. Client acceptance remains pending.

#### Practice Range HUD initialization

- Enabled the initialized shared-variable state in the Practice Range HUD graph `20E4`, matching its entry in the 104319 graph. The owner frame size and PvP reference packet are unchanged.
- Passed 91 tests covering owner-frame decoding, selection, body replication, abilities and notices. Client HUD color, sprint animation and the full sprint presentation remain pending.

#### Predicted projectile timing

- Assigned predicted projectile leases and shot records to the statescript volley frame. A Biotic Field cast that finishes during a missed command window now confirms the scheduled canister ID when input resumes.
- Passed 38 projectile, combat, hitscan and ability regressions. The catch-up path still uses the available aiming and position sample. Canister placement and client prediction remain pending acceptance.

#### Match mode HUD initialization

- Started `0CB6 st21` in the match mode's initial and countdown frames, matching the shared-variable entry in the 104319 graph. Existing countdown values still decode from those frames.
- Passed 90 primary regressions and decoded the active mode HUD state from an encrypted 0688 Practice Range frame. A separate real-cache test decoded 47 controller full frames across 23 Soldier: 76 body lifecycles, including ten hero round trips and ten rejoins. HUD rendering and notices remain pending client acceptance.

#### Practice Range bot notice delivery

- Kept the old bot entity available to a connection until its combat notices, any earlier reliable messages, and its last health update are acknowledged. This covers a lost kill datagram, a gap before acknowledged notices, and terminal health sent in a separate datagram. The replacement bot can spawn independently.

- Bounded the old entity's lifetime by the existing transport silence budget of 625 server ticks. Passed 90 tests covering encrypted bot delivery, packet budgets, lifecycle changes, journal replay, and state snapshots. Ruff and Git diff checks passed. Client hit markers, overhead health bars, and elimination names remain pending acceptance.

#### Project-wide restoration checklist

- Reorganized the README around build-wide goals for heroes, maps, modes, online features, presentation, audio, and deployment. Listed completed server foundations separately within the open milestones and described the current Practice Range validation group.

- Added a development rule to mark milestones complete once their evidence and required client acceptance are recorded. Checked README links, checklist structure, and the Git diff.

#### Early player death events

- Marked recent client-only pulses after the first instance frame as fresh. The player death graph now sends the first pulse to the owner and observers when death occurs within the first 500 ms. The first-frame Tracer pulse and the expiry window retain their tested behavior.

- Passed 72 targeted tests covering pulse encoding, runtime, codecs, snapshots, and body replication. Ruff and Git diff checks passed. The original server's freshness policy and client kill feed rendering remain unverified.

#### Kill feed attribution

- Applied instance-variable exclusions from each script's own graph in owner and observer frames. This preserves the killer and critical-hit fields for the player death graph `01CF` when unrelated scripts exclude variables with the same IDs. The existing entity-level filtering stays in place.

- Passed 84 focused tests and decoded startup frames for all 32 hero definitions. Decoded the player death event with killer, critical flag, loadout, and an empty assist list. Also checked that a recreated Reaper child script keeps its own exclusions. The real-cache Practice Range replay passed three lifecycle and ammo checks. Assist attribution, the bot death path, and original-client rendering remain pending.

#### Additional entity graph extraction

- Added optional entity roots to the existing statescript extraction tool. Additional definitions and their referenced graphs go into a separate local bundle using the same parser as the hero graphs.

- Passed 15 extraction and graph analysis tests. A build 104319 extraction included eight additional entity definitions and 13 new graph records, with identical hero definitions and shared graphs. An isolated VM probe encoded and decoded the death pulse and attribution for six bot body definitions. Bot runtime activation and the client name lookup remain pending.

#### Training bot spawn and death statescript

- Loaded the optional bot definitions from the prepared graph bundle. The authoritative death stimulus now reaches the bot's own graph, including `0C18`, and shares the existing body frame encoder. A death in the same world tick as creation runs after the script's initial frame so its pulse can be delivered.

- Sent each connection the initial script after its entity create is acknowledged, then a resendable terminal frame after the initial script is acknowledged. Shooter and observers receive their own delivery sequence. Replacement bots can spawn while the old entity waits for acknowledgement, with the existing 625-tick retirement limit.

- Prepared initial bot scripts one per ready loading tick. Added a runtime preparation option for the separate local graph bundle.

- Passed 110 final targeted regressions, Ruff, and Git diff checks. With the original 0688 collision cache and extended 104319 graph data, two encrypted peers decoded identical death attribution, and a separate battle replay matched 101 controlled steps and 18 sends through a kill and respawn. The stored-state probe verified pending death delivery. Original-client name lookup, kill feed, and audio rendering remain pending.

#### Command clock handoff

- Applied each admissible command in order through the last frame whose two projectile lease windows fit in the wire format. At a fresh command beyond `FF7FFFFF`, or a forward serial wrap, the old link now uses the existing reliable leave flow. ACKs and FIN remain available during the leave grace period.

- Used the initialized body frame to filter stale or ambiguous first packets when the input queue has no frame yet. This keeps movement and the body script on the same input history.

- Passed the 99-test command and handoff group and the 117-test simulation group. All three encrypted handoff cases also passed with the original 0688 collision cache and extended 104319 graph bundle. A controlled replay of an unsafe first input before hero selection matched all nine sends. Ruff and Git diff checks passed. Original-client clock re-anchoring and the independent server-wide tick boundary remain unverified.

#### Shared game message recipients

- Routed each valid entity in a resolved target list through the existing local or world message path in list order. Single-target messages and the implicit self target retain their tested behavior.

- Passed 163 focused tests, including the `00BA` and `00BB` graph actions, script runtime, snapshots and replay. With the original 0688 collision cache, hero switches, rejoins and ammo checks passed, and a separate battle replay matched 101 steps through a bot kill and respawn. Production recipient selection and cross-entity delivery remain pending.

#### Global server clock rollover

- Checked the candidate server frame, including skipped ticks, in the common scheduler and replay entry point. Before the global clock reaches the projectile lease boundary, the server freezes old matches and drains their links within a fixed frame budget.

- Kept the original reliable message order, including leave retransmissions, while dropping stale world records from old links. Reserved handoffs can still authenticate and leave through the same protocol. Once those links, reservations and lobby callbacks finish, the next server clock starts from its initial frame with fresh handoffs.

- Kept waiting queue tickets and live Practice Range requests during the admission pause. Reopening retries the waiting rosters and Practice Range requests. A delayed Game Found callback checks that its reservation is still valid.

- Passed 158 targeted tests. The original 0688 collision and extended 104319 graph pack passed the encrypted mixed active and pending rollover, ten hero round trips, ten rejoins, and a 101-step combat replay. Ruff and Git diff checks passed. Original-client clock re-anchoring and the menu experience remain pending acceptance.

#### Timed bot death statescripts

- Advanced due events in the dead bot's original statescript up to its respawn deadline. The immediate `0C18` attribution and the `0C17` pulse from the graph's 50 ms wait each retain their own frame, including when the server skips ticks.
- Sent each delta after the receiver acknowledged its preceding frame. Lost frames can be resent in order, the old body remains available while delivery is pending, and late observers receive only the baseline for that death.
- Passed 82 focused tests, Ruff, and Git diff checks. The original 0688 collision cache and extended 104319 graphs passed the two-peer encrypted death sequence, a stored-state probe with two pending death frames, three Practice Range lifecycle and ammo tests, and the 101-step combat replay.
- The production audience query, victim name lookup, and original-client kill feed and audio remain pending. The audio listener list was verified on the wire with a controlled input.

#### Player death with idle input

- Dispatched the authoritative player death event as soon as combat registers it. Observers now receive the kill feed pulse and its attribution even when the victim stops sending commands. The victim's command frame stays at the last processed input.
- Passed 89 focused tests, Ruff, and Git diff checks. In an encrypted test using the original 0688 collision cache, the observer decoded the death pulse, killer, critical flag, and loadout at the next send while the victim sent ACKs only. The three Practice Range lifecycle and ammo tests and the timed bot death wire probe also passed.
- Original-client presentation, bot name lookup, and native multikill event production and eligibility remain pending.

#### Directed messages between player bodies

- Routed explicitly addressed statescript messages between current player bodies in the same match. Dispatch uses a bounded FIFO before observer snapshots, preserves the recipient's command clock, and drops entries whose body lifetime ended. Messages emitted during dispatch enter the next batch.
- Verified that killing a Soldier: 76 who is using Tactical Visor delivers `064A.025` to the killer's `0C7D` instance, including the numeric and text-reference fields. A retired body or a body in a different match cannot receive the queued message.
- Passed 119 gameplay and transport tests, 55 storage and replay tests, Ruff, and Git diff checks. An encrypted rifle kill replay matched 55 roots and eight sends. A separate 0688 probe with the extended build 104319 graph bundle verified that pending deliveries affect the registered match state.
- Native scoring, Play of the Game, and original-client presentation remain pending. NPC, controller and mode entity recipients still require their own lifecycle and query evidence.

#### Score event data extraction

- Added a local export for the target build's `003F.054` event labels and score scalers. It reuses the DragonML reader, preserves ordered aliases and resource references, and records the source hash. The generated catalog is kept outside the repository.
- Verified that the existing `064A.025` receiver's `2041.01C` event resolves to the localized Ultimate Shutdown label. The original client graph has a separate award presentation entry. Its transport binding, natural On Fire thresholds, decay and credit rules remain pending.
- Passed 97 focused tests, Ruff, and Git diff checks. Two exports of the original build 104319 file produced identical bytes with 23 labels and 20 scalers. The existing scoreboard export also passed a separate CLI fixture.

#### Initial mode entity variables

- Added an optional local graph bundle section for the typed initial variables in build 104319 mode assets. Statescripts can read the explicit values from the mode entity by its GUID, independently of schema defaults. Bundles without the section retain their previous lookup behavior.
- Verified 25 mode records and 21 explicit values. With original Hanamura and Practice Range collision data, the isolated `0BA4` objective branch counted two events in modes `0014` and `0067`. The same branch saw an unset member in mode `0018`. Normal objective script activation, mode entity replication, and client acceptance remain pending.
- Passed the same 157 focused tests with each bundle format, plus Ruff and Git diff checks. The new bundle passed 800 hero and mode VM boot and input checks, three original Practice Range lifecycle and ammo checks, and the encrypted bot death sequence. Synthetic bot fixtures are isolated from optional real NPC data.

#### Scripted bot health and Biotic Field

- Synchronized nonlethal damage and healing to the shared body health input for players and scripted bots. Kept the NPC death script's ordering for the terminal health change and death event.
- Fixed a failure that dropped Biotic Field from server tracking on its first heal of a scripted friendly bot. In the original 0688 map, an encrypted cast healed two injured bots from 205 to 225 health each. The encrypted stream contained both final health values and both destruction records from normal field expiry.
- Passed 101 focused tests with the extended 104319 runtime and repeated the two-peer bot death sequence. Ruff and Git diff checks passed. Original-client field effects and overhead health bars remain pending acceptance.

#### Bot movement after creation

- Wait for each connection to acknowledge a bot's current create before sending its movement. Initial spawns and respawns use the same acknowledgement as bot health and notices. The existing movement cadence applies once the create is acknowledged.
- Passed 149 tests with the default data and 130 with the extended 104319 runtime. On the original 0688 map, a simulated observer lost four respawn creates and received movement after acknowledging the retry. A second observer received movement while the first waited. The three Practice Range lifecycle and ammo checks, the timed bot death wire probe, Ruff and Git diff checks also passed.
- Original-client movement, name lookup, and audio remain pending acceptance.

#### Terminal bot health before respawn

- Queue dirty health before processing respawns so a tick that skips past the respawn deadline still sends the old bot's zero health. When a connection has acknowledged the old bot's create, the server waits for pending health before its destroy, within the existing retirement grace. This also covers bystanders without a kill notice or an NPC statescript. The replacement create is queued on schedule.
- Passed 148 tests with the default data and 129 with the extended 104319 runtime, plus Ruff and Git diff checks. On original 0688 collision, two encrypted peers received zero health after a 194-tick skip. The passive peer lost the first terminal health packet, received the retry and timed death frames, then received the old body's destroy. The Practice Range ammo, lifecycle, Biotic Field and bot movement replay checks also passed.
- Original-client death presentation, overhead health bars and hit audio remain pending acceptance.

#### Map AI placement graph extraction

- Added `--ai-spawners` to include `.01B` graph roots from a local map spawner table in an external graph bundle. The exporter follows each root's references through the existing graph traversal and reports missing assets. Without the option it keeps the existing root set.
- Extracted 19 additional graphs from the original Practice Range and Tutorial placement data, including `0B8E` patrol and `0B92` firing. All 386 earlier graph records stayed equal, both exports produced identical bytes, and a new runtime directory loaded all 19 roots. The existing missing `0F9C` reference remains reported.
- Passed 139 tests with each data pack, Ruff and Git diff checks. Original 0688 ammo, lifecycle and terminal-health replay checks passed, as did the 800 hero and mode VM controls. Production AI graph execution, bot projectile replication and original-client behavior remain pending.

#### Statescript random float ranges

- Evaluate `STU_BE0BD267` from its float32 bounds using the component's existing seeded random stream. Wait states now schedule the sampled duration.
- Verified 126 ranges from 68 build 104319 graphs. With controlled selection of the original `0B92` behavior, six phases alternated within 1.75 to 2.5 seconds. The `0013` reset branch resumed eligibility after 2.75 seconds, and the fire state then followed the selected firing phases. The previous evaluator exhausted the event budget at one timestamp.
- Passed all 1,170 Python tests with the default data and 176 focused tests with the AI-roots runtime. Ruff, Git diff checks, the 800 hero and mode VM controls, and the original 0688 ammo and lifecycle tests also passed. Retail random sequences, production AI selection and projectile replication, and original-client acceptance remain pending.

#### Observer projectile delivery

- Send the observer's projectile creation before its ch2 launch event. Wait for positive acknowledgements of both the creation and the matching volley state in the shooter's current body stream. Retries use the original launch frame and projectile ID.
- Hold an ordinary explosion release until the launch is acknowledged, with up to 64 server ticks of grace after flight ends. A short flight can finish before the first send and still arrive in order. On timeout or shooter body replacement, clean up the projectile and cancel stale launch retries.
- Verified the original 0688 collision cache with two encrypted peers. After independent loss of the first create and the first launch, the observer received the create, launch, and release in order. A replay matched 305 controlled roots and 68 encrypted sends, and a stored state export included the pending delivery. Original-client projectile effects and the NPC firing path remain pending.
- Passed 125 default-data tests including stored snapshots, 123 tests with the AI-roots runtime, and the 15-test projectile group with both packs after adding the body replacement case. The original Practice Range ammo, lifecycle and Biotic Field probes, Ruff and Git diff checks also passed.

#### NPC weapon bodies and projectiles

- Advance the weapon body on the server clock for AI placements with the firing graph. Consume its weapon volleys through the shared collision and damage path. Use the bot's current lifetime entity for damage and death attribution.
- Replicate live NPC weapon state on the existing body stream before the projectile launch. Wait for the matching state and projectile creation to be acknowledged, retain the newest waiting live snapshot, and deliver terminal frames in order. NPC projectiles share the observer encoder and use server entity IDs.
- In the original 0688 map, a controlled logical weapon input fired the configured `1476` projectile through the production combat tick. Two encrypted peers verified 19 direct damage, lethal attribution to the firing bot, and ordered creation, launch and destruction after deliberate loss. Passed 168 tests with default data and 155 with the AI-roots runtime. The original Practice Range ammo, hero switching, rejoin, Helix, timed and skipped bot death, and Biotic Field probes also passed, along with Ruff and Git diff checks.
- The test supplies the weapon input. Native AI selection and input production, animated muzzle position, projectile impact effects, NPC name lookup and original-client acceptance remain pending. Projectile origin currently uses the server body pose.

#### NPC projectile delivery during body setup

- Keep an NPC projectile for a registered observer while the source body's initial state is still in flight. Queue the projectile create after the source baseline is acknowledged, then wait for the projectile create and exact volley acknowledgements before sending its launch.
- Discard a waiting projectile when the source lifetime retires or its delivery deadline expires. A replacement that uses the same entity ID cannot receive the previous body's launch.
- Passed 18 projectile tests with default data and with the build 104319 runtime. Encrypted regressions cover a lost source create, a lost volley delta, same-ID replacement, and expiry. The original 0688 damage and attribution replay also passed. Original-client projectile presentation remains pending.

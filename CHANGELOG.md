# Changelog

This log records tested changes and their acceptance status. Entries are grouped by date until a version is explicitly released.

## Unreleased

### 2026-11-06

#### Ultimate charge from combat

- Credited the ultimate's charge for the damage a body dealt, the healing it did, and its eliminations
  (combat.py through the new ult_charge.py). The rates are a data table, `game_ult_charge_174.json`: a
  hero fills the meter with its `damage_to_full` / `heal_to_full` of HP and gets `kill_bonus_percent` of
  a full meter per elimination, converted against the cost #338e its own Entries carry, so the meter is
  right in whatever scale a hero's graphs use.
- OW1 computed these rates in its server code rather than in data: the hero's entity definition has no
  charge component (its components are health, weapon, mover, model, first-person, statescript, target
  tags and voice) and the generic settings carry no charge keys. The shipped table is therefore marked
  `placeholder` and holds community-measured values (380 damage for Soldier: 76's full meter, a 20 %
  elimination bonus); replace them from the original server before trusting the balance.
- Added the generating tool `tools/extract_game_ult_charge.py`; the table joins `MATCH_RUNTIME_DATA`, so
  the runtime pack has to carry it (the loader keeps the fallback and says so if the file is absent).
- Added 12 tests: the rule's arithmetic, its defaults and fallback, the shipped table's shape, and the
  credit at the damage, elimination and healing sites. The full suite (1347) passed, Ruff and Git diff
  checks passed. Client acceptance for the charge climbing under fire is pending.
- Logs report when a body's passive charge rule blocks, and when the meter stops moving although the
  rule runs, with what the client acknowledged (frames, ack, pending). On the target client this told a
  blocked rule from a stalled owner stream: the meter froze on the client while every owner frame was
  acknowledged and the server meter reached its cost; suppressing the combat notices (25001, 25005)
  behind the new `OW174_NO_COMBAT_NOTICES=1` lever makes the same scenario run correctly, which pins
  the stall to the notice records sent while the victim's body is retired.

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

#### Authored AI weapon input

- Extract logical-button bindings from the associated AI weapon control definition into the external graph bundle. Resolve the slot and button from the authored values and reject ambiguous duplicate slots. The six original bot body records resolve slot 0 to logical button 20. Two independent exports were byte-identical and retained the previous 405 graphs and eight entities.
- Route the self-targeted `0B92` fire action through the existing logical-button queue. Its state starts the hold at the VM event time and releases it on completion or abort. Bundles without the binding keep their previous behavior.
- On original 0688 collision, all three firing placements reached `0BA3` and launched `1476` after a controlled selection and reset. The first firing phase lasted 1,941 ms. Cancelling the next phase released the input, which remained released for 200 frames. Verified stored state export, the original Soldier ammo, switch and rejoin probes, and the encrypted NPC and Helix projectile replays.
- Passed 223 tests with the extended build 104319 runtime, including graph extraction, VM storage, and encrypted server replay. Ruff and Git diff checks passed.
- The control supplies the selection and `0013` event. Normal map activation, the native reset producer, and original-client acceptance remain pending.

#### Automatic Practice Range robot fire

- Start the original `0B92` graph once per lifetime at the authored Practice Range firing placements when the external graph and weapon binding are available. The server selects the single placement behavior on entry and sends one `0013` reset to that instance at creation. Other body graphs keep their existing initialization.
- Advance the original wait and weapon states on the shared clock, seeded per entity. Replicate the weapon state and projectiles while keeping the private placement graph in server storage. Retire the behavior, held inputs, and pending shots before running death callbacks. A respawn initializes a fresh graph under the new entity ID.
- In a headless original 0688 match, all three firing robots used the encrypted projectile path without injected input. A robot killed during a firing phase stopped launching and its replacement resumed after initialization. In a 64-second idle-match run, the three robots each launched at least 103 projectiles. The Soldier: 76 ammo, switch and rejoin probes, bot death and Biotic Field probes, and the Helix replay also passed.
- Passed 184 default-data tests and 216 with the build 104319 runtime. The 11-test bot statescript group, including the added same-frame death regression, passed with both packs. Ruff and Git diff checks passed.
- The spawn reset and single-candidate selection use a server reconstruction policy. The exact original scheduling, reset producer, animated muzzle and aim, displaced return movement, and original-client presentation remain pending.

#### Projectile travel on skipped ticks

- Sweep a projectile through the elapsed server ticks up to its remaining flight bound. A repeated world tick no longer moves it twice. A projectile received from player input starts with one simulation step when the scheduler first advances it.
- In the original 0688 map, an automatically fired robot projectile hit the same stationary target after about 0.129 seconds of flight and dealt 19 damage with either 13 regular ticks or one update after 12 skipped ticks. The same sweep checks static collision and the current target capsules. Historic moving targets and skipped AI input transitions remain pending.
- Passed 91 focused tests with each data pack, plus the original 0688 automatic firing lifecycle and encrypted Helix replay. Ruff and Git diff checks passed. Original-client projectile effects remain pending acceptance.

#### NPC weapon simulation after skipped ticks

- Replay up to the latest 64 combat frames when a live NPC weapon body is active. Sweep admitted projectiles before advancing the NPC weapon graphs in each frame, and consume volley reports before their state advances again. A player projectile accepted just before the scheduler resumes still takes its first step at the current frame.
- Use the simulated collision frame for NPC death. On original 0688, regular and skipped runs produced identical launch frames, volley counters, configured muzzle sequence, and 57 damage to a stationary target. A second run matched an NPC shot at frame 1455, its death at 1465, and cessation of later shots.
- Passed 107 focused tests with each data pack, Ruff and Git diff checks. The replay uses the latest poses and caps long gaps. Delivery of intermediate launches to a backlogged observer and original-client presentation remain pending.

#### NPC launch delivery through an observer backlog

- Retain the weapon activation required by each NPC launch while the observer catches up. Wait for the projectile create and matching activation acknowledgements, then wait for the launch acknowledgement before installing a conflicting activation. Other live state keeps its newest values. Expiry and source retirement release the dependency.
- In an encrypted original 0688 replay, one scheduler jump queued four shots at frames 1436, 1455, 1474, and 1493. The previous observer path received the first and fourth. All four now arrive in order with their matching activations. Verified a server state export while the launches were pending and an encrypted regression with deliberate loss of the first launch.
- Passed 207 focused tests with each data pack, Ruff and Git diff checks, and the original 0688 damage, NPC death, and player Helix replay controls.
- Original-client projectile presentation remains pending acceptance.

#### Soldier Sprint across missing commands

- Advance the body statescript after each reconstructed movement frame in the mover's existing bounded command window. Both use the same continuous input and subsequent neutral frames. Synthetic frames omit the previous command's one-shot action and refresh movement modifiers for the next frame.
- On the original 0688 map, a 40-command gap matched the explicit fallback sequence in position, velocity, and correction frame. The encrypted owner stream cleared Sprint variable 699 in both runs. Retrying the received frame left movement unchanged.
- Passed 199 default-data tests and 198 tests with the build 104319 pack, which skipped one King's Row test because that cache was absent. Ruff and Git diff checks passed. Long-gap policy and original-client animation and HUD appearance remain pending acceptance.

#### Command acknowledgements before the first movement correction

- Keep the owner's replay input while a Soldier body create or its first movement correction is awaiting delivery. When no correction has been sent, cap command ACK at two frames after the newest eligible movement snapshot, or after the packet tick if there is no eligible history. Selected and previously sent corrections retain their existing limit.
- In an encrypted original 0688 match, the first datagram's budget excluded the correction. Its ACK stayed at 1089. The next datagram carried the correction for frame 1090 and ACK 1092, retaining the inputs the client needs to replay from 1091.
- Passed 211 focused tests with each data pack, Ruff and Git diff checks. The original 0688 Sprint, ammo, ten hero switches and ten rejoin controls also passed.
- Original-client correction and replay remain pending acceptance.

#### Combat across reconstructed input frames

- Resolve weapon and melee reports after each reconstructed movement frame, using its pose and the mover's existing fallback input. Repeated controls retain the last sampled ordinary aim and view delay. One-shot actions and click-only aim stay on the received command. Newly admitted player projectiles still advance with the world tick.
- In an encrypted original 0688 replay, losing 22 held-fire frames and turning away at the endpoint now produces the same four 20-damage hits as contiguous input, at frames 1053, 1059, 1066, and 1073. Both paths leave 26 rounds in the owner stream. Regression coverage includes a genuine click aim, retransmissions, and a Biotic Field cast timer expiring in the gap.
- Passed 242 tests with each data pack, Ruff and Git diff checks. The original 0688 ammo, switch, rejoin, Sprint, Helix and Biotic Field controls also passed.
- The comparison covers a stationary target and the mover's bounded reconstruction window. Longer gaps and original-client feedback remain pending acceptance.

#### Scheduled reports for every weapon round

- Use the weapon's rounded shot time to decide when to report a round, matching the timer that ends the volley. The ammunition output retains its existing calculation. A round due at the finish time is reported once.
- Verified Soldier: 76's full 30-round magazine through the original 0688 collision and encrypted owner stream. The controlled target received thirty 20-damage hits at the authored frames, including the last round at frame 1254. Server and owner ammo both reached zero.
- Passed 243 tests with each data pack, Ruff and Git diff checks. The original 0688 command-gap, Sprint, lifecycle, Helix, Biotic Field and automatic NPC firing controls also passed.
- Original-client hit markers, impact effects and audio remain pending acceptance.

#### Combat notices after body creation

- Keep hit and elimination notices behind the recipient's acknowledgement of the target body. A death notice with an attacker waits for that attacker's body. Players and NPCs share the same delivery path.
- Queue the current visible player's body if combat references it before the next match update. Keep the old body available while its already queued notices wait for reliable delivery, up to the existing retirement deadline. A late acknowledgement for an old creation cannot release a new lifetime's queued notices.
- Passed 173 tests with each data pack, including encrypted replay and stored state checks. In the original 0688 collision scene, two encrypted peers lost their first remote creates and received each notice after the corresponding acknowledgement. Regressions also cover a hit immediately after a hero switch and a lost notice that delays destruction of an old body. Ruff and Git diff checks passed.
- The original localization data resolves the Training Bot and Friendly Bot labels in 13 languages. Those bodies share a voice set. The client-side entity name lookup, combat presentation and audio remain pending.
- A recycled body ID can still conflict with a notice that already entered the reliable stream. The generation check covers notices waiting on creation.

#### Owner ammunition when fire restarts

- Send the new volley’s starting ammunition when its activation differs from the last acknowledged volley. The client reads that starting value when it starts firing. Later frames for the same activation carry the remaining ammunition.
- In an encrypted replay on the original Practice Range collision, the owner acknowledged the first burst, lost the stop packet, and received the second burst with 29 starting rounds. The previous delta gave it 28. The regression also checks that the next frame reports 28 without starting another volley.
- Passed 83 tests with each data pack, including body frame decoding, abilities, combat, projectiles and repeated lifecycles. Original-client prediction, animation and the reported trigger behavior remain pending acceptance.

#### Training bot nameplate graph

- Resolve the original self-filter used by the four stock Practice Range robot definitions so their existing body graph mounts the original nameplate child. Keep the authored child arguments. The filter exception applies to this Practice Range call site. The full native filter semantics remain unknown.
- Two encrypted peers on the original 0688 collision received one nameplate child after creation for each of the 15 initial bots, and one for the replacement after respawn. Tests cover the alternate nameplate path, another mode and other filter arguments. Passed 44 tests with each data pack and the repository lint and diff checks.
- Original-client overhead health and name rendering, elimination name lookup, kill feed and audio remain pending acceptance.

#### Entity lifecycle and missing-input test controls

- Give the transport fixture an empty match roster and verify that the rifle stops at the first neutral fallback frame. The test also verifies the restart delay and that late input does not duplicate the intervening hits. Passed 11 tests with each data pack.

#### Predicted projectile confirmation before release

- Wait for the owner to acknowledge each projectile create before sending its explosion release. The server retries lost creates and keeps a 64-tick delivery window after the release becomes due. If simulation finishes after the input-frame deadline, the window starts at the completion update.
- At expiry, remove unconfirmed entities without an explosion. Replacing the shooter's body also retires its pending creates. The existing projectile lease selection remains in place.
- Passed 48 projectile and combat tests. On original 0688 collision, the first owner Helix create was lost at frame 1104, its retry arrived at 1110, and the release followed at 1113. The pending confirmation was stored in a server snapshot, and replay matched 231 input events and 39 sends. Original-client right-click presentation remains pending.

#### Public collaboration

- Document the supported build, known limits and evidence behind the current server behavior. Publish the M0–M6 roadmap and explain how developers, researchers and players can contribute.
- Add GitHub forms for bugs, feature requests and gameplay verification, plus a pull request checklist for testing, redaction and AI-assisted changes. Preserve contributor authorship through the audited publication workflow.
- Add a public source workflow for Windows, Python 3.12 and Node.js 24. The current source-only selection runs Ruff, 51 Python tests and two JavaScript tests without runtime data. Prepared-data regression and original-client acceptance remain separate.
- Resolve the graph export test's temporary repository path to match the real CLI, including on Windows runners that supply an 8.3 short path.

#### Player body ID retirement

- Reuse a player body ID only after each connected peer that saw the old body acknowledges its destruction, the 30-second retirement interval elapses, and old reliable combat notices clear the delivery queue. Measure the interval on the monotonic clock so missed simulation ticks cannot shorten it.
- If all 255 body IDs are occupied or reserved, keep the current body. Automatic respawn retries until an ID becomes available.
- Passed 73 tests with each data pack, including four encrypted regressions for delayed notices, lost destroy acknowledgements, simulation catch-up and ID exhaustion. The original 0688 collision replays also passed 23 Soldier body lifecycles and two-peer combat notice delivery with packet loss. Ruff and Git diff checks passed. Original-client acceptance remains pending.

#### Collision readiness regression fixture

- Forward the movement step's keyword arguments so the test can verify that a single input packet keeps its pinned collision world while the provider changes.
- Passed all 10 collision readiness tests, both dashboard JavaScript tests, Ruff and Git diff checks. This test fixture change does not require client acceptance.

#### Rifle recovery after an input gap

- Release held input before advancing the body through skipped frames for which the mover has no pose. Advance graph timers and movement modifiers, and consume the skipped combat reports before applying the next real command. Short gaps still replay movement and combat together.
- In an encrypted replay on original 0688 collision, a 65-frame command interval previously produced nine old rifle hits aimed with the next neutral command. After the fix, a neutral command left the bot at 200 health and the rifle at 29 rounds. A real press produced one 20-damage hit at the new frame, and the server and decoded owner ammunition then agreed at 28.
- Passed 147 tests with each data pack, 14 encrypted journal tests, the original 0688 short-gap rifle and Sprint controls, Ruff and Git diff checks. The preceding baseline passed all 1,214 Python tests. The observed in-client firing and prediction issues still require investigation and acceptance.

#### Mode UI delivery before elimination and death notices

- Wait for each peer to acknowledge the mode entity before sending its first `0CB6` UI frame. Keep only the latest countdown while the create is pending.
- Elimination and death notices wait for the receiving peer's UI frame acknowledgement and any referenced body's create acknowledgement. Ordinary hit notices still depend only on the target body. Preserve the existing retirement deadline for bodies referenced by deferred notices.
- On original 0688 collision, the first UI packet was lost. The hit still arrived at frame 1053, the UI retry at 1056, and the elimination notice at 1059 after the retry acknowledgement. Four consecutive mode-create losses also recovered with the UI sent after creation.
- Passed 197 tests with each data pack, including eight new encrypted cases for mode startup, packet loss, countdowns, independent peers and expired references. Stored-state replay, original 0688 body-create loss controls, Ruff and Git diff checks passed.
- The kill feed's separate body pulse can still precede the mode UI under loss. Training bot name lookup and original-client presentation remain pending.

#### Kill feed pulses after mode UI acknowledgement

- Gate the authored NPC `0C18` and player `01CF` kill feed pulses for each recipient. Health, death attribution and other body states still reach the client while the mode UI is pending.
- Use the existing 500 ms freshness policy on the death's world clock for the first send. Preserve the same active count and freshness in queued or cumulative frames until acknowledged.
- Encode NPC death snapshots against each viewer's acknowledged body frame so a deferred pulse can arrive after the rest of the death state. Preserve the layout of historical frames when the live graph has since removed an instance.
- On original 0688 collision, losing the initial UI packet left the lethal hit, zero health and attacker attribution at frame 1053. The independent timed death pulse arrived at 1059 and the kill feed pulse at 1062. Losing the UI beyond the freshness window suppressed the unsent feed pulse.
- Passed 212 tests with each data pack, plus stored-state replay and an exact-build NPC snapshot check. Ruff and Git diff checks passed.
- Original-client kill feed rendering, NPC names and actual sound playback remain pending. Owner corrections still use real command frames.

#### Player possession after world creation

- Send an observer's team script after the other player's controller create is acknowledged. Send that player's component 44/75 association after both controller and body creates are acknowledged.
- Cancel outstanding possession updates when a new one supersedes them or the observed body retires. A lost current association still retransmits. The owning client keeps its existing body-create and possession send order.
- In original 0688 encrypted replay, both controller-create and body-create loss recovered before the dependent association. Separate owner and observer controls verified that a lost old association cannot be resent after a body switch.
- Passed 178 tests with each data pack, stored replay and the original 0688 Soldier fire, reload, hero-switch and rejoin controls. Ruff and Git diff checks passed.
- Human combat notices still need to wait for the association itself. NPC identity and original-client presentation remain pending.

#### Remote player identity before combat presentation

- Keep elimination and killed-by notices out of the reliable queue until the receiving peer acknowledges the remote player's body association and mode UI. The hit notice remains independent.
- Apply the same identity check to remote attackers and victims named by the kill feed. Terminal health and other body state still go out while an association is pending.
- Discard unnumbered notices for an unconfirmed retired body or a superseded association. A confirmed old body may still resolve an earlier notice while its association remains current on that peer. Pending feed pulses also stop referring to a retired local body or starting during leave.
- On original 0688 collision with independently lost association packets, ordinary hits arrived at frame 1062. The elimination arrived at 1080 after the attacker's ACK, and the killed-by notice at 1086 after the victim's ACK. Each arrived once.
- Passed 203 tests with each data pack, stored replay, the original 0688 timed NPC death controls, Ruff and Git diff checks.
- Native NPC name lookup and original-client presentation remain pending.

#### Fixed world-target ability movement

- Run a resolved, fixed world target and explicit speed from a player body graph through the shared mover and map collision. Each posed frame, including bounded recovered frames, advances by the configured speed up to the remaining distance. Arrival finishes the state, blocking aborts it, and ordinary movement resumes without carrying the forced velocity.
- In offline controls with the authored Genji `01DB` and Mercy `006E` nodes, both bodies reached a supplied three-metre target on original Practice Range collision and stopped before a full-height wall. Synthetic controls cover vertical targets, descent against walkable ground, explicit aborts, bounded gaps and invalid targets. A controlled encrypted session reproduces active motion, final pose and registered server state.
- Passed 214 targeted tests with each runtime data pack, eight offline movement controls, Ruff and Git diff checks.
- Native target and speed resolution, entity and bone targets, duration-based movement, simultaneous request priority, and sub-grid travel remain open. Original-client ability prediction, animation and presentation still require acceptance.

#### Shared static collision rays

- Use one static ray traversal for combat occlusion and geometry queries that need a hit position, a ray-facing normal, and the triangle's category and surface flags. Retain the combat caller's distance-only interface and existing mover category.
- Add public source checks for category separation, first-hit ordering, two-sided normals, exact segment endpoints, and vertical rays. The query works with the triangles in its supplied world. Authored ability filters, dynamic targets and the original-client ray behavior still require validation.
- On the original Practice Range and Kanezaka collision caches, 5,790 offline queries returned the same distance or miss as the previous traversal. Passed 169 tests with each runtime data pack, an encrypted stored-state replay, Ruff and Git diff checks.

#### Unit body scale during graph initialization

- Resolve the owned player's standard body scale from the same unit value encoded in entity creates. The graph can read it during initialization, including before the first input frame. Foreign, missing and non-unit size inputs remain unresolved.
- Genji's first Shift reaches the authored cast action with a 15 m range and evaluates the movement speed to 50 m/s. In an offline replay of 57 original commands on Kanezaka collision, the position and velocity still match the recorded session while the cast endpoint is unresolved.
- Passed 172 tests with each runtime data pack, including encrypted stored-state replay, and Ruff and Git diff checks. Offline bootstrap and input checks exercised all 32 playable bodies over 320 command poses. Original aim origin, cast filtering, non-unit body scale and client acceptance remain open.

#### Expanded static ray geometry

- Add optional per-map ray caches with all nonzero geometry masks from the original core chunk and supported indexed static props. Retain original prop mesh surface flags and mode-specific placements for ray queries.
- Load these caches explicitly with the map and source chunk identity checked. Missing or mismatched geometry fails the load. The existing character and weapon paths continue to use the original movement caches.
- Include a selected map's optional ray cache in the prepared runtime and its SHA-256 manifest. Public fixture tests cover separate loading, category and mode selection, surface flags, and runtime preparation.
- On original Practice Range and Kanezaka collision, the existing cache hashes stayed identical and 3,000 movement-category rays returned the same distances. Expanded queries recovered previously omitted core and prop hits on both maps. The original 57-command Kanezaka movement replay and two encrypted Soldier rifle controls still match their baselines.
- Passed 173 targeted tests with each reference data pack, Ruff and Git diff checks.
- Broader prop indexing, analytic native shape behavior, dynamic targets, authored filter resolution and original-client acceptance remain open.

#### Authenticated static root props

- List unindexed client-made map root definitions and export their authenticated local definition and model records with pinned OWLib. Check direct records, bundle containers and slices before emitting the local catalog.
- Resolve extra root model references from the original STUv2 definitions. Add supported static shapes to the separate ray cache in placeable order, retaining mode restrictions and the existing rules for client-only breakables.
- Record placement and catalog provenance in the expanded cache. A missing record fails the build before replacement. The runtime loads the prepared cache from its data pack.
- On original 104319 data, resolved 35 omitted definitions and 27 model records. The Practice Range gains 31 static placements and Kanezaka gains one. Found a previously missing hit for each of the four added definitions. Both original movement caches kept their hashes, and 3,000 movement-category queries kept their prior distances.
- Passed 195 targeted tests with each runtime data pack, Ruff and Git diff checks. The final rebuild reproduced the validated cache hashes.
- Attached child geometry, dynamic and server-generated objects, original ability filters and client acceptance remain open.

#### Analytic static rays against round props

- In expanded static ray scenes, intersect uniformly scaled sphere and capsule props against their authored round surfaces. Retain each prop's original mask and mode selection, and choose the closest hit across round props and triangles.
- Resolve the first positive surface in range, including an exit for an origin inside the shape. Keep existing movement and combat queries on the legacy geometry.
- Original Practice Range controls reproduce a sphere hit at 0.1549701543 m and remove a prism-only hit. On Practice Range and Kanezaka, 3,000 movement-category ray distances still match. All 95 original movement cache hashes matched after the local expanded builds.
- Passed 199 targeted tests with each runtime data pack, Ruff and Git diff checks.
- Existing ray caches retain the authored round primitives. Gameplay filter selection and original-client acceptance remain open.

#### Local raycast filter and receiver metadata

- Export build 104319 raycast filters and receivers from pinned OWLib XML. Preserve complete asset IDs, rule order, repeated references, unset results, explicit zero flags and source hashes.
- Validate typed rules and linked filters before replacing the external JSON catalog. Public checks use synthetic XML fixtures.
- The local build 104319 export contains 66 filters and 19 receivers, including 44 override rows and 146 reference slots. Repeated exports match byte for byte.
- Passed 27 extractor tests, Ruff and Git diff checks.
- Native filter execution, the meaning of receiver result flags and original-client acceptance remain open.

#### Owner graph removal before create acknowledgement

- Send graphless removal records when a transient body subgraph has already been sent and disappears before its create is acknowledged. Keep the removal in cumulative frames until it is acknowledged.
- The Soldier body regression covers receivers that applied or lost the create. Both continue applying owner frames and enter the rifle volley after the removal. The previous server threw an exception while building the removal frame.
- Passed 89 targeted tests with each runtime data pack, Ruff and Git diff checks. The original-client effect and the cause of the earlier live weapon symptoms still need separate verification.

#### Match body positions in statescript expressions

- Resolve entity position reads from the match's current body pose, including the owner's initial create and visible player and bot bodies. Retirement detaches the old script from the match.
- With controlled ray inputs, the authored Genji graph keeps the hit point on a hit and uses the current body position to build its miss endpoint. The existing 57-command Kanezaka replay still matches the recorded poses.
- Passed 158 targeted tests with each runtime data pack and the original Practice Range wire-model lifecycle controls, including ten hero round trips and ten reentries. Native aim anchors, filters and original-client acceptance remain open.

#### Resolved raycast results in authored actions

- Allow the shared raycast action to consume a completed query for a world-space type-0 cast without a radius. Forward the authored filter and ordered ignore list to the query provider. Write validated hit and miss outputs before following the authored link.
- An unavailable query preserves the previous values and the fallback path. Live matches still need a verified aim anchor and filter interpreter before providing results.
- On original Kanezaka geometry, controlled queries feed a wall hit and an unobstructed miss to the original Genji action and move the body about 0.8 m on the next frame. The supplied origin and mask are test inputs. Original-client movement remains pending.
- Passed 163 targeted tests with each runtime data pack, 132 script tests, Ruff and Git diff checks. Public source checks use synthetic raycast fixtures.

#### Biotic Field healing across missed world ticks

- Settle an active field's elapsed healing once within the existing 64-tick combat catch-up window. Clip healing to ticks before expiry and use the most recent target poses for retained ticks.
- On original Practice Range collision, the encrypted replay gives each of two stationary friendly robots 2.8 health over five consecutive ticks or one update spanning all five. Both fields expire and send their destroy records.
- Passed 85 targeted tests with each runtime data pack, Ruff and Git diff checks. Regressions also cover duplicate ticks and skipping past expiry. Original placement, moving targets during a gap, and client effects remain open.

#### Unkeyed membership in statescript expressions

- Evaluate unkeyed `45EDF12D` with the same value equality as the existing condition node. Keyed queries retain the existing native fallback.
- A paired original `017B` graph uses both forms with identical operands. Controlled checks with the original `0254` graph distinguish rifle and Helix damage tags. The shared `01C7` hit feedback graph selects its authored gain for the supplied tags.
- Passed 137 script tests with each runtime data pack, including all five membership cases. Public checks cover the source-only cases. The production hit-event producer and original-client audio remain open.

#### Owner snapshots after confirmed packet loss

- Retire snapshots for owner deltas whose datagrams the transport has retired without acknowledgement. Keep cumulative changes by wire key and preserve the latest projected death pulse. An older acknowledgement cannot discard keys carried only by newer lost chunks.
- On original Practice Range collision, 120 lost rifle deltas left zero pending snapshots. The next delivered release cleared the retained keys and agreed on 22 rounds between the server and the decoded owner stream.
- Passed 116 targeted tests with each runtime data pack, including death-pulse delivery after a lost carrier, lifecycle and stored-state coverage. Ruff and Git diff checks passed. Unresolved transport delivery and original-client acceptance remain open.

#### Entity updates after reliable packet trimming

- Fit reliable messages, unreliable messages, and the ordered entity prefix in priority order when the combined frame exceeds the datagram budget. Consider entity updates after reducing the reliable batch.
- An encrypted Soldier fixture on original Practice Range collision sends the oldest reliable card and the current rifle delta in one datagram. The second card stays queued and the acknowledgement advances the exact owner chunk.
- Passed 71 targeted tests with each runtime data pack, including genuine budget deferral, send failure, correction and combat notice cases. Ruff and Git diff checks passed. Owner updates queued across sustained pressure and original-client acceptance remain open.

#### Projectile reports from transmitted owner chunks

- Record a projectile's owner-state report after the containing datagram is successfully sent. Use the transmitted chunk's command frame and carried keys, including when the body has advanced while the record was queued.
- Budget deferral and send failure leave the report unregistered. Positive acknowledgement of the transmitted chunk still confirms the predicted Helix lease. The original Practice Range replay retries a lost create before releasing the projectile and matches 231 recorded inputs and 39 sends.
- Passed 132 targeted tests with each runtime data pack, Ruff and Git diff checks. Repeated confirmation aliases from already transmitted frames and original-client presentation remain open.

#### Exact owner acknowledgements for projectile confirmations

- Use each report's own datagram delivery outcome for predicted projectile confirmation. Cumulative body acknowledgements still advance the snapshot baseline. A lost report cannot supply a projectile ID even if the baseline later advances past it.
- In the original Practice Range Hanzo input replay, a short loss sequence now gives two IDs, for the original shot and the one acknowledged report frame. The old path gave seven IDs and included six dropped reports. Without loss the replay still gives one ID.
- Passed 30 projectile tests with each runtime data pack. Encrypted cases cover mixed delivery, multiple positive acknowledgements and original-frame fallback when every report is lost. The original Practice Range Helix replay retained its create retry and release order. Original-client prediction and aliases from multiple acknowledged frames remain open.

#### Coalescing unsent owner body frames

- Replace an unsent owner delta in its existing queue position with the newest cumulative projection. Drop its unused snapshot once the replacement is encoded. Keep in-flight deltas, reliable full frames and observer streams on their existing paths.
- While the first owner frame waits for space, keep its non-correcting mode and retain an admitted death pulse. The settling interval uses the command frame of the first transmitted projection.
- Original Practice Range checks cover a blocked initialization, sustained fire and release. Recovery sends the latest frame with the rifle stopped and 27 rounds. The repeated hero switch, rejoin and reload controls also pass.
- Passed 152 targeted tests with each runtime data pack, Ruff and Git diff checks. Original-client prediction and presentation remain open.

#### Death update order under packet pressure

- Keep a replacement owner delta after earlier queued updates for its body. With a deferred neutral delta, component 51 now sends zero health before the replacement sends the death pulse.
- Passed 122 targeted tests with each runtime data pack, Ruff and Git diff checks. The encrypted regression exercises both the deferred case and its normal-order control. Original-client death presentation remains pending.

#### Projectile confirmation by volley activation

- Freeze each shot's volley counter and start time at emission. Use only matching volley data carried by the owner datagram, including inactive single-shot states. Two activations of the same state can now confirm their own projectiles independently.
- In the encrypted Hanzo rapid-fire regression on original Practice Range collision, the two arrows keep distinct confirmation IDs. The captured single-arrow replay still confirms one ID in normal delivery and two after simulated loss. The Helix confirmation retry and release replay still matches all 231 inputs and 39 sends.
- Passed 98 targeted tests with each runtime data pack, Ruff and Git diff checks. Alternate IDs shared by multiple shots of one activation and original-client presentation remain open.

#### Shot arguments during command handoff

- Pass volley activation by keyword so the shot time remains in its existing positional argument. The full local run caught the argument change in the encrypted command-handoff regression.
- Passed 76 targeted tests with each runtime data pack, including the last valid shot before a fresh command-clock handoff. Ruff and Git diff checks passed. Original-client acceptance remains pending.

#### Practice Range nameplate initialization

- Wait for each viewer's acknowledgement of the mode UI before replicating the training bot's `0D24` presenter. The initial body frame still carries the bot's other graphs and weapon state. A later death timer keeps the nameplate projection from the original death.
- Replace a pending presenter-only chunk with a cumulative body update when live or terminal state needs to send. The superseding death chunk follows zero health and carries the required presenter data. A late acknowledgement cannot roll back the terminal baseline, and a presenter alone does not hold body retirement.
- On original Practice Range collision, two-viewer runs with three and eight lost mode datagrams preserve all 15 initial bot delivery ticks and the critical hit, terminal health, and death timing. Separate loss and delayed acknowledgement of a presenter-only datagram preserve the next terminal send and its feed pulse. An expired feed and a dead target's withheld presenter stay retired when the mode arrives late.
- Passed the three encrypted nameplate regressions with both runtime data packs. The original shooting robot retained its projectile creation, launch retry, and destruction sequence for lethal and nonlethal hits. Ruff and Git diff checks passed. Original-client HUD, elimination names, kill feed presentation, and audio remain pending.

#### Ownership delivery before world creation

- Wait for acknowledgement of the reliable prefix through `20302` before creating the local controller and the initial world. An acknowledgement of the ownership datagram alone can leave the message parked behind an earlier reliable response.
- The original Practice Range replay keeps the normal controller create at tick 1038. After losing either the ownership datagram or an earlier response, ownership applies at tick 1044 and the controller follows at tick 1047.
- Updated the loopback and replay fixtures to acknowledge actual startup datagrams and seed the rifle model from the delivered owner frames. Passed 93 targeted tests with each runtime data pack. The encrypted Soldier: 76 check selects the hero, fires, and sees 29 rounds in both the server and decoded owner state. Ruff and Git diff checks passed.
- Original-client presentation remains pending. A separate replay still delivers the controller's hero-select graph before its entity when the controller create datagram is lost.

#### Local controller initialization after creation

- Wait for acknowledgement of the current local controller create before sending its hero selection and HUD graph, scoreboard update, or pending possession. Build the first graph from the latest selected hero and screen state. Preserve queued hero switch notices until the controller is ready.
- The original Practice Range replay keeps the healthy first controller frame at tick 1047. With the create dropped, the retry is acknowledged at tick 1053 and the first controller graph follows at tick 1062. The mode UI keeps its own startup path.
- Healthy preselected Mercy retains the original spawn voice activation and wire timing. In the delayed ownership control, the activation time stays the same and its body frame arrives three ticks later. Original-client HUD and audio remain pending.
- Passed 131 targeted tests with each runtime data pack, including positive acknowledgement of unrelated world updates, an early hero pick, and a late acknowledgement from a retired controller generation. Original collision replays pass the 60-second rifle test, ten hero round trips, and ten rejoins. Ruff and Git diff checks passed.

#### Projectile aliases from volley introductions

- Track which transmitted owner states introduced an activation relative to the baseline used to encode the chunk. A positively acknowledged stop or repeat still confirms the original shot ID. An alternate ID requires a positively acknowledged introduction.
- The stock Mercy replay keeps the four original blaster IDs and the first activation alias. It removes the later stop-frame ID previously shared by all four shots. A deferred frame retains the classification from its original encode baseline.
- Passed 33 projectile tests with each runtime data pack, including the encrypted Mercy and deferred-baseline regressions and the Helix acknowledgement controls. The original Practice Range Hanzo replay still produces one ID without loss and two with loss. Ruff and Git diff checks passed. Native behavior when several shots precede the first received activation remains pending.

#### Own body creation before possession

- Preserve create, possession, and old-body removal order when the records fit in one datagram. If packet budgeting splits them, wait for the new body's acknowledgement before sending possession and for the binding before removing the old body. Apply the same body prerequisite to the initial controller create when it carries a preselected body.
- In original Practice Range loss replays, unrelated messages, bot health and movement still send while the body or possession is pending. The no-loss control keeps all three records together. Separate create and possession losses recover in dependency order.
- Passed the 22 lifecycle tests with each runtime data pack and the 121-test focused run with bundled data. Ruff and Git diff checks passed. Original-client switching and presentation remain pending.

#### NPC state encoding cost

- Retain the latest live NPC snapshot without serializing a full body frame on each change. Encode it when a viewer needs a full baseline and preserve it before a terminal graph can remove live instances.
- In a 384-tick replay on original Practice Range collision, the number of live full-frame encodes fell from 771 to one. All 128 decrypted world payloads matched byte for byte. The encrypted regression also verifies two viewers joining at different frames and an older baseline retry after loss.
- Passed the 95 focused bot, notice, projectile, and pulse tests with each runtime data pack, and the 75-test stored-state and bot run with each pack. Ruff and Git diff checks passed. The reported client stalls and rendering performance still require original-client measurement.

#### World delivery after scheduler stalls

- Send the current world state once when a scheduler step crosses a scheduled send boundary. Prepare combat and body states on the same step. Normal simulation and send rates are preserved.
- In an encrypted replay on original Practice Range collision, the old scheduler skipped four packets after receiving fresh commands. The corrected scheduler sent one current snapshot with its command acknowledgement at each crossed boundary, including a step that crossed two boundaries.
- Passed 135 focused tests with each runtime data pack, covering tick and rollover behavior, bot and projectile delivery, lifecycle loss, and packet budgeting. Ruff and Git diff checks passed. Original-client lag and rendering still need measurement.

#### Local movement correction under packet pressure

- Allow a current owner correction to take space from best-effort remote movement when the byte or entity-record budget would otherwise drop it. Preserve the reliable, unreliable, and queued entity records already selected for the packet. Keep the original selection if the correction still cannot fit.
- Encrypted tests cover a 1230-byte payload limit, the 127-record limit, and unchanged healthy payloads. Under pressure the owner receives the new correction and its matching command acknowledgement.
- Passed 144 focused tests with each runtime data pack, plus Ruff and Git diff checks. Added a performance acceptance item to the roadmap. Original-client frame time and lag remain pending.

#### Beta audio and map research

- Recorded the pinned Prometheus voice endpoint and map loading reference. Added penetration, spawn room presentation and environmental death verification to the roadmap.
- Checked the pinned source links and Git diff. The cited behavior is from the beta. Original-client 104319 audio and map behavior remain open.

#### Player health delivery under packet loss

- Use the shared complete health-pool snapshot for player and NPC bodies. Keep the newest snapshot when an older one is lost, and send updates only after the recipient acknowledges the corresponding body create.
- Encrypted regressions drop the owner's 180-health update before delivering 170, and drop an observer's body create after its snapshot is queued. The owner stays at the latest wire value and the observer receives the current pools after the create retry.
- Passed eight focused encrypted health tests with each runtime data pack, one combat pool test, Ruff and Git diff checks. A separate owner script health update still waits for a fresh command frame. Original-client HUD acceptance remains pending.

#### First owner weapon and HUD state delivery

- Retry the original first owner state chunk after loss. Preserve its command frame, payload and projectile report. Keep unsent initialization coalescing and wait for acknowledgement before sending corrections.
- Cancel an already queued retry when the body script stops. In the encrypted regression, losing the first packet twice leaves the weapon and ammo presenter absent until the retry arrives. The decoded client then displays the 29-round state and accepts the next correction.
- Passed 110 focused tests with each runtime data pack, including health, owner frame loss, packet budget, repeated lifecycles, and spawn voice replication. Ruff and Git diff checks passed. Original-client weapon, HUD, Sprint animation, and audio remain pending.

#### Current health in a newly observed player body

- Freeze the current health pools in a player body create, including for a viewer who joins after the last damage update. A fresh body without a recorded pool still uses the hero's initial values.
- The encrypted regression drops a create at 180 health, then inflicts more damage. The retry retains 180, and the 170 update follows the create acknowledgement.
- Passed 138 focused tests with each runtime data pack, Ruff and Git diff checks. Original-client health display remains pending.

#### Replacement NPC delivery after a lost create

- Retire a superseded bot create when its frozen script is the only reason to wait and the viewer has yet to acknowledge that create. Keep the destroy record and preserve terminal delivery for viewers that acknowledged the old body.
- In the original Practice Range replay, one viewer lost the middle bot's create. Repeating it had also delayed the next live bot three times. The replacement now arrives on the respawn tick and its full graph follows on acknowledgement. A second viewer keeps the original terminal and destroy ordering.
- Passed 135 focused tests with each runtime data pack, Ruff and Git diff checks. Original-client respawn visibility remains pending.

#### Graph census checks with extended runtime data

- Compare both MovementMod census commands against the loaded graph definitions. Preserve the bundled minimum of 171 nodes and allow supplemental graphs to add nodes.
- A 1,315-test run on the previous baseline found this one fixed-count assertion. All 15 Statescript query tests now pass with the bundled census of 171 and the extended census of 174.

#### Observer snapshot cost during combat catch-up

- Cache the static remote-state classification for each graph and read unlinked variables directly. Continue evaluating active state payloads and linked values in graph order.
- On original Practice Range collision, the forced-gap replay retained 77 byte-identical decrypted payloads and the same ordered 57 shots, 45 damage events and three respawns. Four 960-snapshot trials took 99–110 ms after the change, compared with 138–166 ms before. Whole-tick timings overlap.
- Passed 90 focused tests with each runtime data pack, Ruff and Git diff checks. The original-client stalls still need measurement.

#### Melee activation across body ID reuse

- Retire the quick-melee activation counters with the player's body. A reused entity ID now starts its own history, while repeat frames for one activation still cause damage once.
- The regression drives Soldier's authored melee graph twice across two body replacements, using the match allocator with its retained-ID constraint. Both activations deal 30 damage.
- Passed 124 focused tests with each runtime data pack, Ruff and Git diff checks. Original-client acceptance remains pending.

#### Lobby seat cancellation in controlled replay

- Let the real matchmaker cancellation handler record its game-side handoff expiration when it runs against a controlled journal session.
- Replayed both cancellation orders. Cancellation before authentication rejects the late SYN and releases the seat on the next tick. Cancellation after the connection opens leaves it connected. The journal preserves the join and leave callback order.
- Passed 17 focused tests with each runtime data pack, Ruff and Git diff checks. Live lobby and game thread capture remains open.

#### Hero-select close after body binding

- Send the controller's closed menu state after the current local possession. Keep the ordinary same-datagram sequence, and wait for acknowledgement when the create or binding needs a separate packet. Reopening or reselecting supersedes an older close. Hero switch notices keep their existing delivery path.
- In original Practice Range replay, the normal Reaper pick still closes at tick 1089. With the split body create dropped, the previous close appeared at tick 1092. The corrected close follows the binding at tick 1098. The encrypted regression also drops a separate binding and checks that a newer open prevents an old close retry.
- Passed 125 focused tests with each runtime data pack, Ruff and Git diff checks. The frozen preceding baseline passed all 1,317 tests with prepared data. Original-client transition and HUD appearance remain pending.

#### Leased projectile damage across body retirement

- Remove a retiring body's leased projectiles from authoritative collision when their replicated copies are already due to retire. The flight of a projectile from the current body proceeds as before.
- In original Practice Range replay, the old server could still deal 40 direct and 80 blast damage after replacing the shooter's body and retiring the projectile copies. The corrected replay keeps the target at 200 health after replacement, and the unchanged-body control still reduces it to 80.
- Passed 125 focused tests with each runtime data pack, Ruff and Git diff checks. Original-client projectile presentation remains pending.

#### Owner weapon and HUD state after body binding

- Keep the body's plain H0 graph tree independent. Send its H1 owner state after the current possession record, either in the same packet or after the binding is acknowledged. Deferred first owner state still coalesces around the latest accepted command.
- In original Practice Range replay, the body tree arrives during two binding packet losses. The first owner state now waits for the binding acknowledged at tick 1104 and arrives at 1107. The wire regression verifies the ammo presenter and the 29-round state after an early shot.
- Passed 158 focused tests with each runtime data pack, Ruff and Git diff checks. The frozen preceding baseline passed all 1,319 tests with prepared data. Original-client weapon and ammo HUD appearance remain pending.

#### Clock rollover admission in controlled replay

- Record the production scheduler's clock-ready callback and permit an explicit controlled match creation from it.
- The registered-state regression drains the old leave, records clock-ready before the new handoff, then authenticates the new connection. Replay verifies the callback order, handoff and encrypted traffic.
- Passed 21 focused tests with each runtime data pack, Ruff and Git diff checks. Live lobby and game thread capture remains open.

#### Shared collision loading state in replay

- Write collision read tapes as v2 with references for repeated loading-state objects. Preserve the difference between a shared state and separate equal results. Continue checking the World on every ready read and accept standalone v1 tapes with their original per-read behavior.
- On original Practice Range collision, two recorded matches now replay with the shared loading state and pass registered-state verification. The clock rollover regression also covers the shared state while admitting a new encrypted connection.
- Passed 55 focused tests with each runtime data pack, Ruff and Git diff checks. Live thread capture and original-client acceptance remain pending.

### 2026-11-06

#### Statescript setup during hero selection

- Cache parsed graph field paths while continuing to look up the current values. Load the shared state codecs before opening the game server socket so the first match update uses the existing cache.
- On the prepared Practice Range runtime, the median body constructor fell from 12.66 ms to 10.91 ms across 288 paired samples for all 32 heroes. The first shared codec lookup took 132–151 ms on the cold baseline, mostly inside garbage collection. It was already cached after startup with the change. Complete pick timings overlapped.
- Compared 64 picks per build in the original Practice Range scene. All 683 decrypted payloads and the observed body binding and initial state timings matched. Passed 161 focused tests with each runtime data pack, Ruff and Git diff checks. Original-client selection stalls remain open.

#### Bot patrol floor queries

- Reuse the ground height from the slope probe when a patrol takes the same step. Query the final position when a slope shortens the stride or the bot reaches an authored waypoint.
- Across four original Practice Range catch-up scenarios, forced-gap floor queries fell by 38.13%. All 216 decrypted payloads and 25,920 per-bot movement snapshots matched. This measures one part of catch-up work. Original-client stalls still need diagnosis.
- Passed 126 focused tests with each runtime data pack, Ruff and Git diff checks.

#### Delayed game handoff in controlled replay

- Record the game server's reservation check when the production lobby timer decides whether to send a pending handoff. Replay the clock read without changing the reservation state during the check.
- Verified the timer one millisecond before expiry and at expiry. The first case sends the handoff and accepts the encrypted connection. The second releases the seat on the next tick after rejecting the late connection. Both cases preserve wire traffic and registered-state callbacks.
- Passed three focused regressions with each runtime data pack, Ruff and Git diff checks. Live lobby and game thread capture remains open.

#### Lag compensation across a body replacement

- Track replacement bodies from their authoritative world tick. Hitscan and quick melee skip a replacement when the historical view predates it. Pose interpolation for existing bodies and projectile flight keep their current paths.
- The player-body regression preserves damage at the replacement tick and prevents damage and a hit notice one millisecond earlier. In the prepared original Practice Range, encrypted rifle input one millisecond before a bot's replacement previously reduced the new bot to 180 health. It now stays at 200 and sends no hit notice. The at-birth control still deals 20 damage with one notice.
- Passed 126 focused tests with each runtime data pack, a nine-root registered-state replay through two replacements, Ruff and Git diff checks. Original-client combat acceptance remains pending.

#### Bilingual project README

- Keep English as the default README and link to a complete Simplified Chinese translation at the top. The translated page links back to English and keeps the same setup commands and evidence status.
- Verified both language links, relative destinations, anchors and command blocks. The README command discovery check passed three tests.

#### Pending owner health updates

- Refresh the latest queued owner frame when its HUD health changed before the first successful send. Keep the original command frame and apply the existing same-body queue order. A transmitted frame keeps its existing acknowledgement or retry behavior.
- In original Practice Range replay, the packet budget delayed a 155-health owner update at command frame 1076. After lethal damage and no new input, the server now sends the zero health pool before the refreshed HUD value and death pulse at 1076. The fresh-input control still sends the terminal state at 1077.
- Passed 168 focused tests with each runtime data pack, plus the final first-frame, in-flight and retry cases in both packs. Ruff and Git diff checks passed. Original-client health HUD rendering remains pending.

#### Initial controller binding for a preselected hero

- Treat the possession embedded in the first controller create as a prerequisite for owner weapon state. The H0 graph tree remains independent. The first H1 follows the controller in the same datagram when it fits, or waits for its acknowledgement.
- In original Practice Range replay with eight controller-create losses and no commands, the old sender emitted H1 at tick 1080 before its controller. The corrected sender delivers the controller first at tick 1089, followed by the same H1 in that datagram. The decoded ammo presenter is active with 30 rounds.
- Passed 170 focused tests with each runtime data pack, Ruff and Git diff checks. The earlier frozen development commit `1868be5` passed 1,330 tests with prepared data. Original-client appearance and acceptance remain pending.

#### Handoff expiry at the controlled receive boundary

- Let a scripted receive expire a handoff while the production scheduler is outside the game lock. Record the action and result at that receive, with the same scheduler deadline and legacy v2 receive rows.
- Reproduced both sides of the authenticated SYN and matched the production lobby timer's output, clock reads and final registered state. Invalid actions fail before the scheduler starts.
- Passed 18 journal tests with each runtime data pack, Ruff and Git diff checks. Live lobby and game thread capture remains open.

#### Biotic Field timing and delivery

- Start and expire Biotic Field on the server's world clock, retaining the existing approximate deployment delay. Recover eligible healing ticks across a scheduler gap. Owner visuals wait for their canister parent to arrive. An expired field stays retired when a late acknowledgement arrives.
- Route only the Biotic Field entity through healing. Torbjörn and Zarya still get projectile confirmation for their deployables without creating a healing field.
- On original Practice Range collision, immediate, delayed and withheld acknowledgements all open the field at tick 1142 and produce identical health at each sampled tick. A second replay verifies that two injured friendly bots receive equal healing and a terminal update across a five-tick gap.
- Passed 193 focused tests with each runtime data pack, a 268-root replay with matching registered state, Ruff and Git diff checks. Original canister flight, deployment timing and client presentation remain pending.

#### Rust transport foundation

- Ported the game bit stream, encrypted UDP datagrams and ACK window, plus the lobby JAM cipher, encrypted TCP framing and address-bound handshake. The handshake takes its signing input from the caller and the tests use synthetic keys.
- Added Windows Cargo checks and documented the migration and its behavior references in both README entry points and the credits.
- Passed 16 Rust tests, including exact Python wire vectors and an encrypted loopback lobby handshake. Cargo formatting and Clippy passed. Passed 19 focused Python regressions with the prepared Practice Range runtime, plus Ruff and Git diff checks.
- The Python entry point continues to serve the lobby and matches while their behavior is ported. Original-client acceptance of the Rust implementation is pending.

#### Rust message and input processing

- Ported the JAM schema codec, game message channel, client command parser and queue, plus lobby announcement, keepalive and BattleTag query handling.
- Ported Battle.net protobuf framing and local account-name session keys. The descriptor set is loaded from the separately prepared runtime.
- Decoded an encrypted datagram generated by Python with both message lanes and a command record. Verified the message boundary, command values and duplicate rejection in Rust.
- Passed 37 public Rust tests and three optional runtime checks for the 104319 schemas and Battle.net descriptors. Cargo formatting and Clippy passed. The frozen Python reference at `795d56d` passed 1,335 tests. The Rust lobby service and match simulation still need implementation and original-client acceptance.

#### Rust accounts, runtime preparation and world frames

- Ported account storage and identity selection, regional catalog lookup, and the local Battle.net TCP and WebSocket listener. Checked Connect, Logon and Referral responses against the Python implementation using the 104319 descriptors. Exercised shutdown with stalled connections on Windows.
- Ported the deployment data preparation command. For the authorized Practice Range runtime, all 23 copied files match the Python tool by SHA-256 and both manifests describe the same files and hashes.
- Ported world-frame encoding and fixed-step scheduling primitives. A mixed 452-byte world payload matches the Python output. The lobby and game entry points still use Python while the Rust modules are integrated.
- Passed 64 public Rust tests, five optional runtime checks, Cargo formatting, Clippy and Git diff checks. Original-client acceptance remains pending.

#### Rust match admission and map cache loading

- Ported the lobby match ticket, queue-pop and delayed seat delivery messages, and the UDP connection lifecycle. A tampered, stale or expired datagram cannot rebind an existing peer. An expired ticket releases its seat on the timeout sweep.
- Used the prepared 104319 schema to encode and decode a 20600 ticket, then used its directional keys to complete an authenticated SYN exchange over loopback UDP.
- Ported retail lobby template loading, personalization and the account's initial settings and endorsement messages. Compared encoded message digests against the Python reference with the prepared schemas.
- Added a bounded XZ and geometry reader for version 5 movement and expanded-ray collision caches. A local release build decoded all 190 prepared caches across 95 maps. Every decoded field of the two Practice Range caches matched the Python uncompressed byte stream.
- Passed 86 public Rust tests, eight optional runtime checks, 39 Python matchmaker tests, Cargo formatting, Clippy and Git diff checks. The normal lobby and match entry points still use Python. Collision queries, full lobby and match integration, and original-client acceptance remain pending.

#### Rust login session, game ingress and combat primitives

- Connected the lobby transport to BattleTag lookup and login preparation. Checked a 43-message initial login subset against the Python wire order. Dynamic collections, queue content and other post-login handlers still need integration.
- Ported reliable message dispatch before input parsing, Pong and the four map-entry milestones. A mixed frame and its Pong match the Python bytes with the 104319 game schema.
- Added static ray primitives and an indexed core triangle scene. Compared 2,720 ray, ground and ordered nearby queries on both prepared Practice Range caches. Hit results, floating-point fields and ordering matched the Python reference in this comparison. Prop placement and character contacts are separate.
- Ported the body health pools and their dirty updates, including shield and armour order and lethal attribution. Full and partial pool updates use the same world component encoder. Original-client validation of health presentation and combat behavior remains pending.
- Passed 107 public Rust tests, ten optional runtime checks, 27 Python combat tests, Cargo formatting, Clippy and Git diff checks. The normal server entry point still runs the Python implementation.

#### Rust match loading worker and lobby collections

- Added a native UDP worker that reserves match seats, authenticates tickets and schedules frames on the fixed game clock. It serves instance and roster messages, ownership and Pong. The worker keeps one owner for connection state and uses readiness polling with bounded receive batches so datagrams cannot delay the tick indefinitely.
- Ported the reliable outgoing queue and ACK handling. A failed send leaves the queue intact, and a reported loss requeues the original reliable ordinal. In an encrypted loopback with the 104319 schemas, deliberately dropping the first instance frame triggers a retry before the ownership and roster messages. Also checked canceled handoffs, FIN and socket shutdown.
- Added collection, loadout and portrait handling to Rust. The initial login subset now includes progression and hero ownership. Its 45 encoded messages match Python for both the default profile and the level-3000 full-unlock fixture.
- Added collision scene assembly for placed static triangles and round shapes. Compared 2,079 queries across three prepared Practice Range views, including authenticated root placements. The selected hits, ground and nearby outputs matched Python bit for bit.
- Passed 124 public Rust tests and 13 optional runtime checks, plus 67 focused Python reference tests covering ownership, shop behavior, frame delivery and UDP match loading. Cargo formatting, Clippy and Git diff checks passed. World creation, character contact and original-client acceptance are pending. The usual launch commands still use Python.

#### Rust map catalog, graph metadata and round contacts

- Added the shared map and mode catalog, ordered placeables and spawn selection. The UDP worker now uses the same map type as catalog lookup. Checked 95 map listings and all 698 candidate map and mode combinations against Python, including the 551 supported results and their placement and spawn order.
- Loaded the authorized state graph bundle and built the owner, remote and variable-slot indexes from its records. All graph layouts in the 373-graph and enriched 405-graph bundles matched the Python reference. The enriched bundle also loaded eight non-player definitions and 25 mode records.
- Ported float32 sphere and capsule contacts with ordered features and cache placement. A local differential run matched Python bit for bit across 12,288 cases and 7,611 contact points. These primitives await the character solver and do not change the active Python server.
- Added own presence to the Rust login subset. The 46-message subset matches Python for both the default and level-3000 full-unlock profiles, and seven additional presence packets match the reference.
- Passed 140 public Rust tests and 16 optional checks with the prepared Practice Range pack, plus the enriched graph bundle check and 21 Python presence reference tests. Cargo formatting, Clippy and Git diff checks passed. Original-client acceptance remains pending.

#### Rust world bootstrap and lobby roster messages

- Ported the initial world records for map placeables, the player controller, the mode entity and training bots. The 21-record Practice Range baseline matches the Python wire payload. The packet budget splits it into ordered datagrams.
- Added shared statescript value, chunk and full-frame writers. Verified Practice Range and PvP controller frames, team entries and the mode UI frame against Python. The controller and mode frames are ready for the worker's entity delivery path.
- Ported training bot route state and shared mover settings. Compared the 15-bot Practice Range roster, 19,560 movement records, and all 34 fields for each of 38 prepared mover bodies. Ported scoreboard and on-fire fields and matched 462 scoreboard wire cases. Combat events still need to drive the on-fire values.
- Ported the float32 GJK distance solver and one-sided capsule contacts against mesh triangles. The Python comparisons matched on 12,000 synthetic distance queries, 2,048 prepared mesh distance queries and 6,144 prepared capsule contact queries. Mesh candidate traversal, sweeps and character integration are pending.
- Added the lobby clock, party payload and ranked rating projection. Verified the 50- and 51-message login subsets when the required ranked and solo party inputs were available.
- Passed 169 public Rust tests, 24 optional runtime checks on each of two prepared packs, Cargo formatting, Clippy and Git diff checks. The native UDP worker still serves the loading phase. World delivery, hero selection and original-client acceptance are pending. The normal launch scripts still use Python.

#### Rust headless Practice Range world delivery

- Connected the decoded lobby Practice Range request to an account-backed world card and the native UDP worker. The headless test covers the encoded handoff, authenticated UDP, withheld ownership ACK, dropped entity creates, and the controller and mode scripts after their own create ACKs. The supported bootstrap uses the base graph bundle. The enriched bundle's authored bot graphs are detected before opening a seat and await the bot VM.
- Loaded the scene outside the game tick and ran the bots against its prepared collision ground. Both runtime packs matched Python for 7,500 serialized movement records per pack. The world sender borrows queued entities and retains their shared records for retry.
- Shared the collision grid between the float64 ray view and the float32 mesh candidate index. Matched 5,000 triangle-box checks and 4,400 ordered mesh queries against Python. All 4,799 earlier ray and scene query comparisons still matched.
- Ported the hero catalog, graph-derived health pools and owner and observer body creates. The 64 body create vectors cover all 32 heroes. Decoded the original hero pick and open, close and toggle messages. Live hero selection, movement sweeps and original-client acceptance remain pending.
- Passed 183 public Rust tests, 31 optional runtime checks on each prepared pack, Cargo formatting, Clippy and Git diff checks. The usual launch scripts still use Python.

#### Rust hero selection and body delivery

- Connected original hero-pick and screen messages to body creation, controller possession, old-body retirement and the owner hero-select frame in the solo Practice Range worker. Creation uses the shared hero and health data and the account's equipped skin and golden weapon at handoff.
- Kept create, possession and retirement in dependency order. A frame can carry adjacent steps when they fit. A split waits for the required ACK. Lost records retry with the same generation and an old body's ID stays reserved for 30 seconds after destroy acknowledgement. Tested the loss of a complete hero selection batch, reopening the screen, and switching heroes on the encrypted worker.
- Ported controller and body statescript stream sequencing and retry state, plus the scoped expression evaluator. Exact checks cover owner full and cumulative delta frames and 11 expression results from target graphs. Live body and bot script execution still need the VM.
- Ported float32 shape casts and mesh sweeps. The local differential comparisons match Python on 9,108 shape casts and 3,530 mesh sweeps, including scaled props and tightened hit limits. Static-world dispatch and character movement are pending.
- Passed 201 public Rust tests, 32 optional runtime checks on each of two prepared packs, seven Python repeated-lifecycle tests, Cargo formatting, Clippy and Git diff checks. Original-client acceptance remains pending. The usual launch scripts still use Python.

#### Rust character movement and statescript foundations

- Ported command gap recovery, frame-scoped movement modifiers, steering, air control and gravity. Compared 7,296 velocity cases across all 38 prepared mover settings at a precision finer than the movement wire grid.
- Added ordered static-world queries, capsule-to-hull contacts, mesh contact clustering and the movement ground probe. All 95 local movement caches built successfully. Two prepared maps matched the Python reference on 2,800 ordered queries, 2,800 sweeps and 11,396 capsule-to-hull queries. The Practice Range probe matched in 1,536 cases, including 37 ground hits recovered by a sweep.
- Ported supported same-time graph initialization with a work budget and diagnostics for unported actions. Checked actual controller child order and bot startup states. Added the 598-class state layout table, 1,824 reference payload comparisons, and owner and observer full frames for supported initialized graphs.
- Passed 223 public Rust tests, 37 optional runtime checks on each of two prepared packs, and 124 Python movement, contact and script reference tests. Cargo formatting, Clippy and Git diff checks passed. The regular launch scripts still use Python. Per-body movement integration, timed and input-driven bot behavior, and original-client acceptance remain pending.

#### Rust player movement and timed statescript

- Connected ordinary player commands to the native solo Practice Range worker with the base graph bundle. The worker prepares static collision before taking a seat, applies body-specific settings, acknowledges fresh commands and preserves the command clock through hero switches. The encrypted loopback checks acknowledgements, the 96-frame input lead bound and the existing ordered body retries.
- Added per-body contact caches, capsule fit checks, bounded contact projection, stance, jump, landing and spring state. The Python reference matched 8,968 serialized movement records across all 38 prepared mover profiles on Practice Range, plus the wall and ceiling fixture and 2,304 per-proxy movement steps on two maps.
- Advanced supported graphs on a bounded timer queue and added scoped game-message entries and owner and observer cumulative frames. The enriched pack verified the two timer boundaries for a training bot's firing eligibility. Actual bot firing, weapon and ability input, and body script delivery in matches still require their runtime behavior.
- Passed 238 public Rust tests, 39 applicable runtime checks on each of two prepared packs, and 154 Python reference tests. Cargo formatting, Clippy, documentation links and Git diff checks passed. Owner corrections, sustained worker load measurements and original-client acceptance are pending. The regular launch scripts still use Python.

#### Rust input scheduling and login content

- Split command catch-up across worker turns with a shared resumable pose iterator, round-robin peers and a bounded queue. The worker allows 16 work units per peer and 64 per turn. Commands advance the acknowledgement after simulation. Queued hero switches and lease boundaries retain order with earlier poses. Tests cover a 64-pose gap, five busy peers and encrypted recovery under duplicate datagrams.
- Added a 64-pose owner correction history with a creation acknowledgement gate and body generation checks. The correction frame advances after its selected datagram is sent. Exact wire tests cover three input history encodings and the replay acknowledgement window. Owner correction delivery still awaits hero movement scripts.
- Added shared command-to-logical-button mapping, press buffering and timed releases in statescript. The prepared Soldier: 76 Sprint graph matched the reference button-entry behavior. Ability start and stop conditions, variable stacks and weapon input remain open.
- Added dated Arcade cards, priority pass pools and career mode rules to the verified Rust login subset. Matched 720 Arcade and pass messages across 180 profile and date combinations, and three mode-rules messages against the Python wire data. Competitive event cards come from the same Arcade state as the menu.
- Passed 252 public Rust tests, 43 applicable runtime checks on each of two prepared packs, and 155 Python reference tests. Cargo formatting, Clippy, documentation links and Git diff checks passed. Sustained worker load and original-client acceptance remain pending. The regular launch scripts still use Python.

#### Experimental Rust lobby and Practice Range server

- Added a loopback server entry point for local Battle.net, encrypted lobby and UDP Practice Range. It reuses existing account storage, tracks the active login and match seat, and stops partial handshakes and active peers on shutdown. An encrypted socket test reads a saved full-unlock profile and joins the map using the lobby's UDP handoff.
- Added main-menu hero resolution, celebration content, and General and Match channel transfer to the native lobby. Celebration payloads match the Python reference across three prepared profiles. Celebration rewards and broader social and matchmaking behavior remain pending.
- Ported variable stack polling and the supported ability activation and cancellation paths. The isolated Soldier: 76 Sprint graph matches the Python reference for the HUD variable and raw movement modifier across a 12-frame input trace.
- Connected the body script stream and movement corrections to the worker's send path for eligible body components. Tests cover exact retries, delivery gates, body replacement and state after a failed send. The base world has no eligible live hero component yet. The enriched world still requires more bot graph support.
- Passed 260 public Rust tests, 48 prepared base runtime checks, 47 enriched runtime checks and 90 focused Python tests. Cargo formatting, Clippy, an isolated CLI startup and shutdown, documentation links and Git diff checks passed. The regular launch scripts still use Python. Original-client acceptance remains pending.

#### Rust player body preparation and hitscan

- Added the prepared player body factory with authored body, weapon manager and equipped weapon roots. The Soldier: 76 startup matches all 19 reference instance IDs, graphs and parent states. The factory checks readiness once per Practice Range asset set and creates fresh runtime state per body.
- Ported supported animation, effect and scoped message states and owner stack bindings. The initial owner tree carries the effective values. The next frame introduces the bindings. Three independent Python wire vectors and an encrypted fixture-body replay verify the baseline, exact retransmission and cumulative recovery.
- Reused the static ray and capsule kernels for direct hits, with click aim, bounded target rewind, critical hits and falloff. A prepared Practice Range test matches the Python geometry and damage through the existing health pools. The four combat notice vectors match the reference bytes.
- Passed 267 public Rust tests, 53 prepared base checks, 50 enriched checks and 156 focused Python tests. Cargo formatting, Clippy, documentation links and Git diff checks passed. The stock body still requires native graph behavior, sparse input catch-up and weapon volley scheduling. Original-client gameplay and presentation remain pending.

#### Rust weapon timing and bot combat delivery

- Added shared volley scheduling, timed variable outputs and variable watches. The authored Soldier: 76 rifle matches eight reference shot times, held-fire ammo, release, the refill and animation-completion frames, and firing after reload.
- Seeded a newly introduced owner volley with its pre-shot ammo. After acknowledgement, the stream sends live ammo. Wire regressions cover a lost stop and lost activation, and preserve the reference observer values.
- Resumed long unposed command gaps within the worker input budget. A 400-frame gap runs the same 258 retained script frames as Python, with input release, bounded timer work and no synthetic hits during the gap.
- Routed posed direct shots through the prepared Practice Range worker. The encrypted fixture verifies a dropped hit's retry, terminal pool delivery, elimination notice and a fresh bot after respawn. A newer full pool update supersedes the old packet, and the old bot retires before the new entity becomes visible.
- Passed 272 public Rust tests, 56 base runtime checks, 52 enriched runtime checks and 172 focused Python tests. Cargo formatting, Clippy, documentation links and Git diff checks passed. The full stock Soldier body remains gated on unsupported graph behavior. Projectiles, bot weapon scripts and original-client acceptance remain pending.

#### Rust Helix cooldown and projectile prediction

- Added the supported ability cooldown, priority push, state query and branch paths. The authored Helix replay matches the reference shot times, six-second cooldown and damage configuration. Guarded human body queries and typed ChaseVar, Watch and Ability payloads match their reference cases.
- Derived Soldier's three predicted projectile definitions from the authored graph. Added scoped lease allocation, confirmation after an individually delivered owner report, and release after confirmed creation. The observer launch and owner confirmation and explosion records match Python bytes.
- Added shared swept projectile flight with wall and capsule collision, direct damage and capsule-distance splash. The projectile modules still need live-worker integration.
- Set Doomfist as the next hero after Soldier: 76 and the Practice Range training bots pass their acceptance checks.
- Passed 284 public Rust tests, 58 base runtime checks, 54 enriched runtime checks and 172 focused Python tests. Cargo formatting, Clippy, documentation links and Git diff checks passed. Stock owner startup still reports unsupported graph queries. Original-client gameplay acceptance remains pending.

#### Rust gallery equip and match appearance

- Processed retail gallery equip requests after login. The native lobby saves the loadout before confirming it and refreshes the local player card when an icon changes.
- Applied new skin and weapon themes to the reserved match. Equipping a different theme for the active hero uses the existing ordered body replacement. Other heroes retain the change for their next selection.
- Passed 84 focused Python reference tests and two prepared-base encrypted Rust loopback regressions covering gallery confirmation, persistence, a subsequent map load and the new skin theme on the replacement entity. The preceding original-client Rust session reached Practice Range, where the user reported missing weapons, unusable abilities and failed skin selection. The gallery repair awaits a client retest. The stock Soldier owner script still needs additional graph behavior.

#### Rust Soldier owner script startup

- Added current and maximum health queries backed by the existing owner variables, with a regression for the authored Soldier full-health branch. Added bounded random expressions and verified base presentation-state lifecycles. The deterministic random sequence remains a local implementation.
- Activated seven authored Soldier client-subgraph controllers on the server and left their client-only branches to the client. These include rifle and weapon manager states. Logged the first preflight failure once per prepared asset set.
- Compared the same 19-instance stock startup state with the Rust and Python encoders. The owner and observer frames matched byte for byte in this offline comparison.
- Validated authored mode tables in the external base bundle. Together with the VM changes they reduced the stock preflight from 35 diagnostics to 22. The first remaining query is the entity relation at graph `0033`, node `51`. The stock Soldier body still stops before attaching its weapon. No new original-client session was run.
- Passed 287 public Rust tests, 58 base-runtime checks and 54 enriched-runtime checks. Cargo formatting, Clippy, documentation links and Git diff checks passed.

#### Practice Range patrol collision

- Reproduced two elevated patrols crossing solid geometry in build 104319. Python and Rust now move patrolling bots through the same standing capsule and static map contact solver used by player movement, with the same map barrier masks.
- Kept the authored spawns, hint order and destinations. Blocked legs can use a clear authored hint or a short lateral detour that passes capsule clearance and floor support checks. This is a local reconstruction of patrol navigation. The original navigation implementation remains unverified.
- Prepared the detours outside the live tick loop and retained the bot's contact state in Python stored-state replay. Recorded allied pass-through and opposing-body blocking in the roadmap. Actor-to-actor movement collision remains open.
- Verified both elevated patrols over the real 104319 map, including the enemy bot's return leg. In an isolated optimized Rust run of 1,900 patrol updates, the patrol step took 47 µs at p95 and 143 µs at maximum. This measurement covers patrol updates only. Client acceptance is pending.
- Passed 288 public Rust tests, 58 base-runtime checks, 54 enriched-runtime checks and 148 focused Python tests. The Python run skipped one King's Row check because the base pack lacks that map's cache. Cargo formatting, Clippy, Ruff, relative documentation links and Git diff checks passed. The optimized Rust server started and stopped on isolated ports.

#### Rust development policy

- Moved new implementation and fixes to Rust. Froze the Python server as the transition entry point and behavioral reference. The cutover criteria now require broader verified Rust coverage, original-client checks for the replacement workflows, and a server-only deployment smoke test.

#### Rust owner relationships and startup states

- Bound the stock human owner VM to the authenticated controller and the fresh body allocated at each switch. Added distinct queries for the owning controller, current possessor and possessed body. Regressions verify typed operands, body replacement, and the authored Soldier startup and HUD lookups.
- Ported the input bridge's explicit activation and abort behavior. It forwards repeated inputs while active and consumes inactive inputs without starting the bridge.
- Enabled the normal graph lifecycle and variable synchronization for the verified networked ForCode state. The Soldier regression covers all six active states, their empty state payloads, and the linked body and weapon-manager states. Native game-code consumers and the server-only form remain open.
- Verified that two config classes share the retail native evaluator that returns a typed false, and added their exact operand-free forms.
- Reduced the stock Soldier preflight from 22 diagnostics to 13 in the same prepared base runtime. The first remaining blocker is the delayed conditional state at graph `0033`, node `66`. Stock weapon attachment and original-client acceptance remain pending.
- Passed 292 public Rust tests, 58 base-runtime checks and 54 enriched-runtime checks. Cargo formatting, Clippy, documentation links and Git diff checks passed.

#### Rust delayed branches and typed health queries

- Added the delayed conditional state's initial override, interruptible per-branch timers, and subgraph teardown to the shared VM. Its branch flags match the build 104319 payload vectors, and only the owner receives pending timers.
- Resolved authored health, armor and shield queries from the body pool catalog and the existing owner health totals. Current values are returned when the pool distribution is determined by those totals. Named and dynamic pools, and damaged mixed pools without per-type values, still require native state.
- Enabled the graph lifecycle and verified empty payload for the authored tips state. The native tips registry and original-client presentation remain open.
- Reduced stock Soldier startup diagnostics from 13 to 11 in the same base runtime. The first blocker is now the dynamic health pool state at graph `0033`, node `1`. Stock weapon attachment and original-client acceptance remain pending.
- Passed 296 public Rust tests, 58 base-runtime checks and 54 enriched-runtime checks. The codec comparison matched 1,833 owner and observer samples. Cargo formatting, Clippy, documentation links, Git diff checks and isolated release server startup and shutdown passed.

#### Rust body health pool lifecycle

- Added the authored base health pool shape to the shared VM. It registers capacity and initial current health separately, publishes remaining health before the end plug, and retires the pool when its state or instance ends. The initial and live output timing follows an inference from the authored graphs. Named pools, other flag combinations and capacity changes after activation still need validation.
- Made the same `Vitals` data drive health queries, damage, healing and component 51. Player and bot updates share the reliable delivery path, including creation acknowledgement, stale revision rejection and slot clears retained in replacement updates.
- The stock Soldier graph creates 200/200 health. An injected offline hit updates its health output and owner variables to 125/200. The stock startup now has 10 diagnostics, down from 11. The first is the entity filter at graph `004B`, node `21`. Weapon attachment, player damage in a live match and original-client acceptance remain pending.
- Passed 302 public Rust tests, 59 base-runtime checks and 55 enriched-runtime checks. The shared graph reproduces the reference armour and shield components for Reinhardt and Zarya, and the 32-hero initial component corpus remains byte-identical to the reference. Cargo formatting, Clippy, documentation links, Git diff checks and isolated release server startup and shutdown passed.

#### Rust authored target tags and entity filters

- Added optional import of target tags from local build 104319 entity definitions in the Rust runtime preparation command. The manifest hashes the resulting graph bundle. Older bundles without tag records still load and report unknown tag data.
- Shared the owner's tag facts between direct tag queries and filters. The supported filter form applies any, all and exclusion tag predicates, and can use an exclusion mask when the world provides authoritative flags.
- Verified the authored tag branches at graph `004B`, node `21` and graph `0043`, node `45` with the tagged Practice Range runtime. Stock Soldier startup reports 8 diagnostics, down from 10. The first remaining condition at `004B`, node `20` needs the entity's runtime exclusion flags. Tag mutations, instance overrides, weapon attachment and original-client acceptance remain open.
- Passed 307 public Rust tests, 59 tagged base-runtime checks and 55 tagged enriched-runtime checks. The earlier untagged bundle also passed the owner startup regression. Cargo formatting, Clippy, documentation links, Git diff checks and isolated release server startup and shutdown passed.

#### Rust owner startup facts and message tables

- Shared the body creation mask with the entity filter query. The same 104319 weapon-manager graph now starts seven additional subgraphs. A frozen reference replay with the corresponding mask matches the order and parents of all 26 instances. Runtime changes to the original server's exclusion flags still need verification.
- Read the initial on-fire flag from the current possessor's component 64 baseline. Resolved the authenticated human body's absent AI relation as an invalid entity ID. The original 104319 graph and reference data establish the operand shapes. Later client disassembly corroborates the typed absence results. Fire score, decay and active AI relations remain open.
- Added ordered VM maps for authored local message tables and their typed key lookup. The 20-entry table in the Soldier startup graph is local to the VM. Map wire encoding still needs verification.
- Used the inherited state lifecycle and empty payload for the verified zero-valued state on the human owner branch. Other values remain unverified.
- Reduced tagged 104319 Soldier startup diagnostics from 8 to 5. The first remaining operation is `STU_22926252` at `08B6`, node `11`. Stock weapon attachment and original-client acceptance remain pending.
- Passed 308 public Rust tests, 59 tagged base-runtime checks and 55 tagged enriched-runtime checks. The untagged bundle also passed the owner startup regression. Cargo formatting, Clippy, documentation links, Git diff checks and isolated release server startup and shutdown passed.

#### Rust target tag state lifetimes

- Applied the single literal target-tag grant and suppression states from build 104319 to the owner body's shared tag lookup. State end and instance teardown release the effect. Overlapping supported effects preserve the authored base tags, and body replacement starts with fresh effects. A grant during an active suppression and changes to a foreign entity remain unverified.
- Verified the stock Soldier graph grants tag `0422` at `01C7`, node `96` with or without a base tag list. Its state uses the inherited empty payload. The tagged 104319 preflight drops from 5 diagnostics to 4. The first remains `STU_22926252` at `08B6`, node `11`. Stock weapon attachment and original-client acceptance remain pending.
- Clarified that the Python server stays frozen and is retired when Rust exceeds its verified coverage and passes cutover acceptance.
- Passed 311 public Rust tests, 59 tagged base-runtime checks and 55 tagged enriched-runtime checks. The untagged bundle also passed the owner startup regression. Cargo formatting, Clippy, documentation links, Git diff checks and isolated release server startup and shutdown passed.

#### Rust script instance exit and owner startup transitions

- Queued the operand-free instance exit after earlier same-time events. The normal action output still runs, and the instance teardown cancels later work and releases its descendants. A child exit completes the still-linked parent state when the authored flag permits it. Dynamic targets, named exits and active descendant ordering remain unverified in build 104319.
- Completed the direct-owner AI state through the same-time finish queue when the world confirms no AI control entity. Added the typed zero asset returned by the fieldless resource lookup. The 104319 graph shapes match the local archive references, and later client inspection corroborates the execution paths.
- The tagged Soldier startup retires instances 9 and 25 while retaining the allocated ID 26. Its preflight goes from 4 diagnostics to 1, the movement-record sampler at `01C7`, node `7`. Stock weapon attachment and original-client acceptance remain pending.
- Passed 314 public Rust tests, 59 tagged base-runtime checks and 55 tagged enriched-runtime checks. The untagged bundle also passed the owner startup regression. Cargo formatting, Clippy, documentation links, Git diff checks and isolated release server startup and shutdown passed.

#### Rust owner movement sampling

- Started the authored movement sampler from the same mover used for the selected body's spawn, then fed it actual posed input and collision results. Unposed script time retains the latest movement record.
- Reused the 104319 owner flags codec and updated only changed authored outputs. The regular mover provides stance, velocity, throttle, ground contact and jump facts. Scripted movement, foreign targets and linked sampler events still need support.
- Used the embedded standing record at fresh owner construction based on the later client's constructor. The exact 104319 startup context still needs original-client evidence. Tagged Soldier startup reaches the crouch effect action at `01C7`, node `74`. Stock weapon attachment and client acceptance remain pending.
- Passed 317 public Rust tests, 59 tagged base-runtime checks and 55 tagged enriched-runtime checks. The untagged bundle also passed the owner startup regression. Cargo formatting, Clippy, documentation links, Git diff checks and isolated release server startup and shutdown passed.

#### Rust owner projectile flight

- Routed authored predicted straight projectiles from the same posed shot stream as hitscan. Body creation advertises the authored lease blocks, and the worker sweeps each flight against the current opposing bot capsules and map geometry. Direct and splash impacts use the same health, notice and respawn logic as hitscan.
- Used the exact owner report carrying the matching volley state to confirm projectile aliases. Confirmations, including the no-report fallback, wait for the acknowledged body and possession. Explosion release waits for the projectile create acknowledgement. Body replacement stops the old flight and reliably retires its aliases without an explosion.
- The encrypted Practice Range fixture covers Helix's 40 direct and 80 splash damage, an unaffected ally in the flight path, duplicate input and lost confirmation recovery. The stock Soldier body still stops at the authored effect action at `01C7`, node `74`. Observer launch, scripted movement, healing fields and original-client presentation remain open.
- Passed 320 public Rust tests, 60 tagged base-runtime checks and 55 tagged enriched-runtime checks. The untagged bundle also passed the owner startup regression. Cargo formatting, Clippy, documentation links, Git diff checks and isolated release server startup and shutdown passed.

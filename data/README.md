# Runtime data

The server and its tests use the following data sets. The public source checkout uses separately prepared runtime data. See [separate deployment data](../README.md#separate-deployment-data).

| File | Purpose |
| --- | --- |
| `schemas_174.json` | Layouts of 707 lobby messages in 87 protocol groups. |
| `retail_templates.json` | Decoded reference-server templates, including catalogs and prices; display-name markers are anonymized. |
| `extracted_items.json`, `extracted_general_unlocks.json` | Cosmetic names, types, rarity, categories and rewards. |
| `extracted_heroes.json` | Hero identifiers and names. |
| `extracted_maps.json` | Map metadata and seasonal variants. |
| `ai_spawners_174.json` | The training bots: each map's AI spawners (hero, team, place, route) and hint points, and the bot heroes' bodies, health and speed (`tools/extract_ai_spawners.py`). |
| `resource_keys_174.json` | Game content keys used by event resources. These are not account credentials. |
| `lobby_state_blob.key` | Local 64-byte signing input for the target build's lobby handshake. |
| `extracted_events_174.json` | Verified scene mappings used by regression tests. |
| `announced_crcs_174.json` | Protocol identifiers used by the schema-extraction tool. |
| `arcade_cards_174.json` | The Arcade's cards (`tools/extract_arcade_cards.py`). |
| `game_schemas_174.json`, `game_schemas_174.txt` | Layouts of the game link's messages (`tools/dump_schemas.py`). |
| `game_heroes_174.json` | Each playable hero's GUID, name and body (`tools/extract_game_heroes.py`). |
| `game_maps_174.json` | Each map's modes, server-owned placeables and spawn points (`tools/extract_game_maps.py`). |
| `game_queues_174.json` | The queue cards: modes, maps and team sizes (`tools/extract_game_queues.py`). |
| `game_graphs_174.json.gz` | The statescript graphs the game server runs (`tools/extract_game_graphs.py`). |
| `game_stats_174.json` | The stats the Tab board lists for each hero and mode (`tools/extract_game_stats.py`). |
| `game_movers_174.json` | Each hero body's character mover values (speeds, jump, gravity, capsule), from `tools/extract_game_movers.py`. |
| `collision_keys_174.json` | Storage keys of each map's collision chunk in build 104319, so the server can read the collision from your own game (`tools/build_collision.py`, cache in `cache/collision`). |
| `collision_props_174.json` | Storage keys of each map's props (fences, crates) and of their models, and which game modes keep which props; added to the same collision cache. |
| `statescript_codecs_174.json` | What the client reads for each statescript state class in a frame (property block and class reader, read in the client code by `tools/extract_statescript_codecs.py` in IDA); the encoder and the test reader use it. |
| `statescript_names_174.json` | Names for `tools/ssq.py`: class aliases (our research catalog and the VM code), the STU fields that hold a variable (OWLib's STU types), logical buttons, the hero select and HUD graphs, each game mode's scripts (the 1.74 0C5 assets) and the variable names the server code uses. |

The raw reference capture was from client 1.68 and was decoded using 1.74 schemas; templates are not a claim that every original live-service feature has been reproduced. The game client, raw captures and process dumps are not distributed here.

### Local score event metadata

Export the build 104319 `003F.054` settings from a DataTool `054` XML dump with the pinned OWLib checkout. Keep the output outside the repository.

```powershell
.\.venv\Scripts\python.exe tools\extract_game_stats.py 'C:\path\to\xml' --owlib 'C:\path\to\owlib' --score-events 'C:\path\to\score-events.json'
```

The export records the source hash, ordered label rows and `StatEventScoreScaler` rows. Event enums use the numeric values from the pinned types. Label rows retain text resource IDs, duplicate event aliases and anonymous fields for comparison with the original asset. The catalog gives event configuration. Thresholds, score decay, kill credit, and the message that carries an award to the HUD still require separate evidence.

### Additional statescript definitions

Use the build 104319 `003` and `01B` XML dumps and the pinned OWLib checkout to include additional entity definitions in a separate local graph bundle. Repeat `--entity` for each hexadecimal `003` asset index.

```powershell
.\.venv\Scripts\python.exe -X utf8 tools\extract_game_graphs.py --xml 'C:\path\to\xml' --owlib 'C:\path\to\owlib' --entity 1403 --entity 143E --output 'C:\path\to\entity-graphs.json.gz'
```

The bundle's `entities` table holds the added definitions by full asset GUID. Their initial graphs retain definition order and use the same graph reference traversal as the hero catalog.

To serve the bot spawn and death graphs from a prepared pack, include the six Practice Range bot body indexes `1403`, `143E`, `146E`, `14A9`, `14D2` and `14ED`, then pass the generated bundle to the runtime preparation tool. Point `ORPHEUS_RUNTIME_DIR` at the new pack when starting the server.

```powershell
.\.venv\Scripts\python.exe tools\prepare_server_runtime.py --source 'C:\path\to\local-research' --destination 'C:\path\to\orpheus-bot-runtime' --map 0688 --game-graphs 'C:\path\to\entity-graphs.json.gz'
```

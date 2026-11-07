# Contributing to ORPHEUS

You can contribute code, research, documentation or a gameplay report. The public source checks use synthetic fixtures and are a good place to start. Read the [roadmap](docs/ROADMAP.md), check [existing issues](https://github.com/leafall903/ORPHEUS/issues), and pick a task with prerequisites you can meet. For larger work, comment on the issue with the behavior you plan to verify so others can coordinate.

## Development setup

Use Rust for new implementation work. Install Git, stable Rust, `rustfmt` and `clippy`.

```powershell
git clone https://github.com/leafall903/ORPHEUS.git
cd ORPHEUS
```

The frozen Python server remains available as a transition entry point and reference. To run its checks, install 64-bit Python 3.12 on Windows and Node.js 24 for the dashboard test.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt "ruff==0.17.3"
```

Use a fork and a short topic branch for a pull request. Test with your own Git author identity. Keep code, research and evidence scoped to the change.

## Public source checks

The Rust protocol crate uses synthetic inputs by default. Run the following from the repository root with stable Rust, `rustfmt` and `clippy`.

```powershell
cargo fmt --all -- --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked --all-targets
```

To run the additional Rust checks with an authorized 104319 runtime, set `ORPHEUS_RUNTIME_DIR` to that runtime and `ORPHEUS_BNET_DESCRIPTORS` to its `bnet/descriptors.pb`, then run `cargo test --locked --lib -- --ignored`.

Run the following Python and JavaScript checks from the repository root with `ORPHEUS_RUNTIME_DIR` unset to match CI. They use public source and synthetic fixtures.

```powershell
Remove-Item Env:ORPHEUS_RUNTIME_DIR -ErrorAction SilentlyContinue
$python = '.\.venv\Scripts\python.exe'
& $python -m ruff check .
foreach ($module in @(
    'test_game_commands.py',
    'test_game_link.py',
    'test_replay_values.py',
    'test_casc_bundle.py',
    'test_script_expr_budget.py',
    'test_extract_game_graphs.py'
)) {
    & $python -B -m unittest discover -s tests -p $module
    if ($LASTEXITCODE) { throw "Failed: $module" }
}
node --test tests/test_dashboard_client.mjs
git diff --check
```

The [public workflow](.github/workflows/public-checks.yml) runs this source-only test selection for pushes and pull requests. Add the closest focused test for the code you changed. Report the exact commands, pass or fail results, and any skips in your pull request.

## Prepared runtime checks

Gameplay regressions can use an in-memory encrypted peer and real map collision without launching Overwatch. These checks need the correct local runtime and any named map or graph fixture. See [runtime data provenance](data/README.md) and [server setup](README.md#separate-deployment-data).

For example, run the projectile suite against a prepared build 104319 pack.

```powershell
$env:ORPHEUS_RUNTIME_DIR = 'C:\path\to\orpheus-runtime'
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_game_projectiles.py
```

Full discovery also depends on research fixtures kept outside the public export. Some tests read fixtures from the research checkout's own `data` directory. A prepared server pack by itself is insufficient for the full suite. Record any missing prerequisites, the data lane used, and the checks that were actually run.

## Original-client verification

Use your own authorized Overwatch 1 retail **1.74.0.0.104319** installation and a prepared local runtime. The manual launcher starts the local server and original client through the Windows relay.

```powershell
$env:ORPHEUS_RUNTIME_DIR = 'C:\path\to\orpheus-runtime'
.\START_LOCAL.cmd --game-exe 'C:\path\to\_retail_\Overwatch.exe'
```

For the first acceptance group, enter the Practice Range, pick Soldier: 76, and record one behavior at a time. Include the ORPHEUS commit, client build and locale, hero, map, exact inputs, expected and observed behavior, and how many runs agreed. Distinguish server or wire results from what you actually saw or heard. Use the [gameplay verification form](https://github.com/leafall903/ORPHEUS/issues/new?template=gameplay-verification.yml). Reports with clear steps are useful even when logs or video are unavailable.

## Issues and pull requests

- Use a [bug report](https://github.com/leafall903/ORPHEUS/issues/new?template=bug-report.yml) for a reproducible failure or a [feature request](https://github.com/leafall903/ORPHEUS/issues/new?template=feature-request.yml) for a proposed change. Put distinct behaviors in separate reports and link related issues.
- For protocol or graph research, identify the target build and cite the exact field, node, event or reproducible comparison. Separate observations, inferences and open questions. Describe how another contributor can repeat the check.
- Keep a pull request focused. Link the issue, describe expected behavior and report what changed and how you tested it. Include a short [changelog](CHANGELOG.md) entry for a user-visible or verification change. Use clear English [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/).
- Codex, Claude Code and other AI coding tools are welcome. If a tool materially contributed, disclose it in the pull request or an `Assisted-by` commit trailer. Read and understand the diff, run the relevant checks, and be ready to explain the change. Maintainer review applies to every pull request.
- The maintainer reviews pull requests on GitHub and applies accepted patches to the development history with contributor authorship intact. The audited source is then published to `main`. That workflow may close a pull request with a link to the published commit instead of using GitHub's merge button.

Share only small, redacted log excerpts or a video you are authorized to share. Remove personal account and machine information, credentials, authentication or session data, and private paths. Keep clients, game data dumps, captures, signing input, resource keys and crash dumps local. A description and timestamps are enough to start a discussion if an artifact cannot be shared.

# ORPHEUS

[English](README.md)

ORPHEUS 是面向 **《守望先锋 1》正式服 1.74.0.0.104319** 的开源服务端复刻项目，独立开展非官方研究与游戏保存工作。长期目标是还原该版本各英雄、地图和模式的服务端行为，以及服务端向客户端发送的事件。服务端仍在开发中，首个端到端验证场景是士兵 76 与训练靶场。

[参与贡献](CONTRIBUTING.md) · [路线图](docs/ROADMAP.md) · [Rust 迁移](docs/RUST_MIGRATION.md) · [验证记录](docs/STATE.md) · [更新日志](CHANGELOG.md) · [待处理问题](https://github.com/leafall903/ORPHEUS/issues)

## 当前进度

| 领域 | 服务端实现与测试 | 原版客户端证据与待办 |
| --- | --- | --- |
| 本地登录与大厅 | 模拟本地认证，支持大厅、英雄选择和对局交接。自动检查覆盖握手及使用已准备的运行数据启动服务端。 | 已观察到登录、训练靶场加载、选择或切换英雄。更多大厅流程及对局规则仍待验证。 |
| 士兵 76 | 已实现角色身体和武器状态、移动与疾跑、步枪射击与装填、螺旋飞弹和生物力场。自动化测试覆盖部分场景，包括反复换人及重进、丢包和弹药状态。 | 测试者曾射击并击杀训练机器人。武器显示的稳定性、弹药界面、输入响应和技能行为仍待完整实机验证。 |
| 训练靶场战斗 | 已实现命中与伤害判定、机器人生命、死亡和重生，并通过网络同步原版靶场机器人的头顶血条显示图。自动化测试使用受控测试数据及目标地图的碰撞数据。 | 测试者反馈头顶血条缺失、消灭提示无目标名、右上击杀栏缺失和爆头音效缺失。血条显示图的改动已取得报文证据，实际渲染仍待记录实机结果。 |
| 网络与研究 | 已实现加密 UDP、玩家输入、实体生命周期、服务端状态快照和回放，并开展了部分丢包和乱序检查。 | 客户端预测、战斗反馈、双端一致性、长时间运行和更多地图的行为仍待验证。 |
| 英雄、模式与音频 | 已具备共用机制，并覆盖部分功能。 | 完整英雄技能、地图目标、对局流程、命中与连杀反馈和情境语音仍在开发中。 |

自动化结果适用于证据中列出的测试场景或回放。手动观察适用于实际使用的版本、提交和操作步骤。首个目标场景的完整原版客户端验收仍待完成。详见[验证记录](docs/STATE.md)和[更新日志](CHANGELOG.md)。

## 如何参与

- **开发者和使用 AI 工具的贡献者**可以从公开源码测试入手。贡献指南涵盖 Codex、Claude Code，并为所有提交规定统一的审查要求。[贡献指南](CONTRIBUTING.md)
- **逆向研究者**可以帮助确认 104319 版本中的单个字段、状态切换、事件或时序规则。请从相关证据出发，并明确说明尚不确定的部分。[研究证据](docs/RESEARCH_EVIDENCE.txt)
- **玩家**可使用目标版本客户端和已获授权的运行数据，提交简短且可复现的游玩结果。欢迎同时报告正常行为和缺陷。[游玩验证表单](https://github.com/leafall903/ORPHEUS/issues/new?template=gameplay-verification.yml)
- **初次参与者**可以查找只需公开源码和合成数据即可完成的任务。[适合入门的问题](https://github.com/leafall903/ORPHEUS/labels/good%20first%20issue)

## 在本地运行

实验性原生入口 `orpheus_server` 会一起启动本地 Battle.net、大厅和 UDP 对局服务。使用基础状态图包的无客户端回环已覆盖读取存档后的加密登录、单人靶场加载、世界创建、机器人移动、选人和更换身体。对局线程会在准备好的碰撞数据上模拟玩家的基础移动，并按轮次限制补帧工作量。大厅内容、逻辑按键、脚本计时和士兵 76 的独立冲刺图已有对照回归。对局中的英雄脚本移动、玩家自身纠正、完整机器人行为和战斗仍在移植。安装稳定版 Rust 后，运行 `cargo test --locked --all-targets` 可执行公开测试。常规启动入口仍使用 Python。

Rust 已对照验证士兵 76 的步枪连发、装弹和螺旋飞弹冷却时序。加密靶场回环使用测试武器验证了命中通知、丢包恢复和机器人重生。投射物飞行和预测封包已有独立测试。Rust 的原版客户端试玩已进入靶场，用户报告武器缺失、技能无法使用。原版士兵 76 仍需补齐角色脚本。展厅换装和局内更换皮肤已通过加密回环测试，等待实机复测。

文档以 Windows 和 Python 3.12 为运行环境。管理面板测试使用 Node.js 24。执行公开源码检查无需安装游戏或准备运行数据。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt "ruff==0.17.3"
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p test_game_commands.py
```

扩大测试发现范围之前，请先查看[完整的公开源码检查清单](CONTRIBUTING.md#public-source-checks)。

### 独立的部署数据

要运行本地服务端，请将 `ORPHEUS_RUNTIME_DIR` 指向已准备好的 104319 版本授权运行数据目录。其中包括协议数据、状态脚本图、大厅签名所需数据和碰撞缓存。准备工具需要游戏客户端之外的输入。

```powershell
cargo run --locked --bin prepare_server_runtime -- --source 'C:\path\to\local-research' --destination 'C:\path\to\orpheus-runtime' --map 0688
$env:ORPHEUS_RUNTIME_DIR = 'C:\path\to\orpheus-runtime'
.\START_SERVER_LOCAL.cmd
```

准备命令会从已准备好的检出目录复制文件并写入清单。省略 `--map` 会复制所有已经准备好的缓存。纯服务端入口使用本地回环地址，可独立于游戏安装和中继 DLL 运行。默认管理面板地址为 `127.0.0.1:3725`。使用原版客户端测试时，请参阅[手动验证步骤](CONTRIBUTING.md#original-client-verification)和[运行数据来源](data/README.md)。

若要试用原生服务端，请把 `ORPHEUS_RUNTIME_DIR` 指向已获授权的基础靶场运行数据包，然后运行 `cargo run --locked --bin orpheus_server -- --data-dir 'C:\path\to\local-state'`。原生入口使用本地回环地址，从状态目录读取 `profiles/`、`profile.json` 和 `default_account.txt`。将状态目录设为原服务端检出目录即可复用现有存档。该入口已进行无客户端测试，仍待原版客户端验收。已覆盖的范围见 [Rust 迁移](docs/RUST_MIGRATION.md)。

## 项目结构

| 路径 | 职责 |
| --- | --- |
| `rust/src` | Rust 移植代码、自动化回归测试及运行数据准备 |
| `ow174/bnet`、`ow174/lobby` | 本地登录、账号、社交和大厅服务、匹配与对局交接 |
| `ow174/game`、`ow174/game/script` | 游戏加密通信、输入、模拟、战斗及状态脚本同步 |
| `ow174/dashboard`、`relay` | 本地管理和 Windows 客户端中继源码 |
| `tests`、`tools`、`docs` | 验证、提取、回放工具及研究记录 |

[路线图](docs/ROADMAP.md)跟踪整个目标版本的还原范围。[致谢](CREDITS.md)记录了原始 1.74 大厅项目、研究来源和上游作者。公开源码保留原有的 [MIT 许可证声明](LICENSE)。《守望先锋》及其资源归各自权利人所有。请将游戏客户端、商业游戏资源、密钥、私有日志、原始抓包和转储留在本地。

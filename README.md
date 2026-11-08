# 前沿雷达 Frontier Radar

[![CI](https://github.com/bayonetta-ccc/frontier-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/bayonetta-ccc/frontier-radar/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

> 围绕一个具体目标，持续监测可说明范围的高可信来源，把新信息转化为**有证据、有边界、可追溯、可回滚的判断更新**；只推送关键变化，并说明它对你意味着什么、下一步该验证什么。

它不是“新闻摘要器”，也不是“全网舆情大屏”。它保存的不是文章，而是**判断状态**：
当前相信什么、置信度多少、依据哪几条原始证据、什么时候因为什么被修改过。

```text
一次运行：采集 → 去重 → 分类 → 映射到判断 → 证据门槛 → 判断更新 → 报告
                                                     ├─ 不足：明确“本期无需更新”
                                                     └─ 高风险：停在“待你确认”
```

---

## 目录

- [它回答的五个问题](#它回答的五个问题)
- [为什么值得做](#为什么值得做)
- [30 秒跑起来](#30-秒跑起来)
- [演示里会发生什么](#演示里会发生什么)
- [命令一览](#命令一览)
- [核心概念](#核心概念)
- [它是怎么工作的](#它是怎么工作的)
- [证据从哪里来 / 覆盖边界](#证据从哪里来--覆盖边界)
- [验证与指标](#验证与指标)
- [扩展新领域](#扩展新领域)
- [项目结构](#项目结构)
- [设计取舍](#设计取舍)
- [路线图](#路线图)
- [许可证与数据声明](#许可证与数据声明)

> 想先看输出长什么样？直接读 [示例报告导读](docs/EXAMPLES.md)（含三期真实生成的报告）。
> 想了解设计细节？看 [产品需求](docs/PRODUCT_SPEC.md)、[架构说明](docs/ARCHITECTURE.md)、
> [验证方案](docs/VALIDATION.md)、[贡献指南](docs/CONTRIBUTING.md)。

---

## 它回答的五个问题

1. 最近发生了什么？
2. 哪些是有实质意义的前沿信号，哪些只是营销噪声？
3. 谁在推动这些变化？
4. 结论的证据在哪里，哪些仍不能确认？
5. 这些变化与我的职业、目标或当前决策有什么关系？

对应到产品输出，就是四块内容：**变化摘要、关键时间线、主张级证据卡、与我相关面板**。
没有实质变化时，它允许并且鼓励输出“本期无需更新”。

## 为什么值得做

默认面对信息时，真正的困难不是“没有信息”，而是：

| 问题 | 常见工具的失败方式 | 本项目的做法 |
|---|---|---|
| 信息过载 | 同一事件 30 条转载 | 规范化 URL + 正文哈希 + 标题相似度去重，区分原始与转载 |
| 证据断裂 | 摘要给出结论，点不回原文 | 每张证据卡保留原文摘录、链接、来源类型与信息层 |
| 阶段混淆 | 实验室样品 = 量产 = 上车 | 显式阶段刻度，越级推断直接拦截 |
| 缺乏连续性 | 每次搜索从零开始 | 判断状态库 + 版本历史 + 变化类型（新增/增强/削弱/证伪/转向） |
| 不敢说不知道 | 用“或将”“有望”凑结论 | 独立来源门槛；证据不足时标记“暂不更新”并说明缺什么 |

核心承诺只有一句：**少而准、有据可查、知道边界、持续更新，并始终与用户的真实目标相关。**

## 30 秒跑起来

要求：Python ≥ 3.10，零第三方依赖（只用标准库 + SQLite）。

**Windows（推荐）**：双击打开项目文件夹，在地址栏输入 `cmd` 回车，然后：

```bat
run.cmd demo
run.cmd serve --open
```

`run.cmd` 会自动寻找可用的 Python（项目 `.venv` → `py` 启动器 → PATH 里的 `python` →
常见安装目录），并设置 UTF-8 控制台，避免中文乱码。没有装 Python 时它会明确提示下载地址。

**macOS / Linux / 已配置好 Python 的环境**：

```bash
# 1) 重建演示数据并跑完三期（基线 → 变化 → 静默）
python -m frontier_radar demo

# 2) 打开本地界面（只监听 127.0.0.1）
python -m frontier_radar serve --open
```

也可以安装成命令（可选，仍然零依赖）：

```bash
pip install -e .
frontier-radar demo
frontier-radar serve --open
```

也可以一步导出静态站点（不需要起服务）：

```bash
python -m frontier_radar demo --quiet   # 报告与站点在 data/ 下
# data/web/index.html          总览
# data/web/claims.html         判断状态库与版本历史
# data/web/<run_id>.html       每一期报告
```

演示数据是**合成的 fixture**（链接为 `example.com` 占位），目的是让任何人 clone 后都能复现同一套
证据链、判断更新与拦截逻辑；它不代表真实世界的最新进展。报告顶部会始终标注这一点。

第二个演示场景（人形机器人供应链，产品经理视角）：

```bash
python -m frontier_radar --profile humanoid_robot_supply_chain --data-dir data_humanoid demo
```

它演示一条更贴近“不要相信发布会”的判断链：客户采购公告先增强判断 → 客户澄清“只是框架意向”
后证伪 → 良率问题削弱零部件判断 → 标准发布触发转向；发布会与券商研报全程被拦在判断之外。

## 演示里会发生什么

演示任务是一个真实场景：**固态电池 × 产业研究员 × 6 个月内形成路线建议**。

| 期次 | 输入 | 系统输出 |
|---|---|---|
| `2026-07-28` 基线 | 18 条信号（含 1 条转载、2 条噪声） | 建立 **4 个关键判断**，全部标记“待确认”；噪声与转载单独列出 |
| `2026-08-08` 更新 | 8 条信号 | **4 个判断需要更新**：1 条证伪、3 条增强；高风险更新**停在待确认** |
| `2026-08-15` 静默 | 2 条信号（1 转载 + 1 论坛帖） | **本期无需更新**，并说明仍需观察什么 |

第二期里最值得看的一条：

> **证伪**：QuantumScape“已进入整车级客户验证”的基线判断，被宝马发言人的澄清（仅样件/台架测试，
> 尚未完成整车级验证）否定。系统给出 0.54 → 0.20 的置信度下调、相反证据原文、单一来源组提醒，
> 并**拒绝自动写入**——它停在“待你确认”。

同时，一条“听说宁德时代明年量产”的论坛帖、一条“固态电池将重新定义电动车”的营销文，
以及一条与主题无关的“碳酸锂涨价”快讯，都被拦截在判断之外。

想看看“确认之后”的样子：

```bash
python -m frontier_radar demo --confirm-after
python -m frontier_radar claims list
```

## 命令一览

```text
python -m frontier_radar <命令> [参数]
```

| 命令 | 作用 |
|---|---|
| `demo [--confirm-after]` | 重建并运行完整演示（baseline → update → quiet） |
| `task new --demo` | 创建演示任务；也可用 `--topic/--role/--decision/--constraints` 自定义 |
| `task list` / `task show` | 查看任务、来源与抓取状态 |
| `run [--mode auto\|baseline\|update] [--source offline\|live] [--now ISO]` | 运行一次追踪 |
| `claims list [--pending] [-v]` | 查看判断状态库 |
| `claims show <id>` | 查看某条判断的证据台账与完整版本历史 |
| `claims confirm <id>` / `reject <id>` | 确认或驳回待确认修改 |
| `claims rollback <id> --to <v>` | 回滚到历史版本（保留中间版本） |
| `claims retire <id>` | 停用判断（历史不删除） |
| `feedback useful\|irrelevant\|fact_error\|weak_evidence\|wrong_change` | 记录反馈（可带 `--claim` / `--note`） |
| `ingest [--write-cache]` | 检查在线来源可达性并生成缓存（`--source live` 前先跑一遍） |
| `metrics [--json]` | 质量指标与质量门（重复率、链接完整率、变更理由覆盖率等） |
| `serve [--port 8765] [--open]` | 本地 Web 界面 |
| `validate` | 校验领域配置（阶段、实体、锚点、来源、词表） |

全局参数：`--data-dir`、`--db`、`--profile`、`--fixtures`、`-q/--quiet`。

常用示例：

```bash
# 自定义一个真实的追踪任务
python -m frontier_radar task new \
  --id task_my_battery \
  --topic 固态电池 \
  --role "电池工程师（负责技术选型）" \
  --purpose "评估下一代电芯路线" \
  --decision "决定 2027 年产品是否采用半固态过渡方案" \
  --constraints "成本敏感；必须有客户验证数据" \
  --horizon "4 个月内" \
  --watchlist "宁德时代,丰田,硫化物"

# 首次基线 / 后续更新
python -m frontier_radar run --task task_my_battery --mode baseline --now 2026-08-01T09:00:00+08:00
python -m frontier_radar run --task task_my_battery --now 2026-08-08T09:00:00+08:00

# 审阅与纠错
python -m frontier_radar claims list --pending
python -m frontier_radar claims show clm_my_battery_toyota_timeline
python -m frontier_radar claims confirm clm_my_battery_toyota_timeline --note "已核对官方新闻稿"
python -m frontier_radar feedback fact_error --claim clm_my_battery_toyota_timeline --note "车型说法有误"
python -m frontier_radar metrics --json
```

## 核心概念

### 1. 任务 = 主题 + 目标 + 决策 + 约束

只输入“固态电池”无法判断什么与用户相关。任务必须包含：
**关注主题、身份与目的、当前要解决的问题、决策时间范围与关键约束**。
这四件事决定了“与我相关”不是泛泛而谈。

### 2. 信号（Signal）

一条被采集到的原始信息。它先经过去重与分类，才可能成为证据：

- 类别：论文 / 专利 / 标准 / 中试线 / 客户验证 / 量产计划 / 实际量产 / 招聘 / 开源 / 融资 / 观点 / 营销……
- 信息层：**已确认事实 / 公司口径 / 分析推断 / 行业观点 / 未证实传闻 / 版本记录**
- 阶段：论文 → 实验室样品 → 中试线 → 客户验证 → 量产计划 → 实际量产
- 新颖性：与历史信号相比是否真的出现了新东西
- 噪声原因：离题、信息量过低、营销、传闻、重复

### 3. 判断（Claim）不是事实，是“当前证据下的状态”

一条判断包含：表述、置信度、来源可信度、影响判断置信度、阶段、支持/相反证据、待验证问题、
观察指标、状态（待确认 / 追踪中 / 已削弱 / 已证伪 / 已停用）与版本历史。

三种置信度严格分开：

- **来源可信度**：信息来自哪里，是否原始来源；
- **事实置信度**：所声称的事情有多大把握真实；
- **影响判断置信度**：即使事实成立，其潜在影响有多大把握成立。

所以“公司宣布建设试点线”可以确认公司确实这样宣布了，但不能据此确认技术成熟或一定按期量产。

### 4. 变化（Change）只认六种

| 类型 | 含义 | 触发示例 |
|---|---|---|
| 新增 `new` | 首次出现值得跟踪的信号 | 新的技术路线或新主体入场 |
| 增强 `strengthen` | 旧判断获得更多独立证据 | 官方口径 + 第三方独立确认 |
| 削弱 `weaken` | 出现延期、失败、复现问题或反向证据 | 良率未达预期、时间表推迟 |
| 证伪 `falsify` | 原判断被足够可靠的材料否定 | 当事方澄清“尚未进入该阶段” |
| 转向 `shift` | 主体改变路线、目标或时间表；阶段跃迁 | 从“计划量产”推进到“客户验证” |
| 无实质变化 `no_change` | 热度很高，底层事实没有更新 | 同一通稿的第 20 次转载 |

### 5. “敢不推送”是一等公民

默认门槛偏保守：

- 置信度单期移动有上限（增强 ≤ +0.12，削弱 ≤ −0.18），避免一期证据把判断推成“确定事实”；
- 增强通常需要 ≥ 2 个独立来源组；单一来源只记录、不升级；
- 证伪必须来自当事方明确澄清，或 ≥ 2 个独立来源组，并始终需要人工确认；
- 高风险判断（量产时间表、监管结论等）的修改一律停在“待确认”；
- 已有待确认修改时，新证据不会再叠加版本（防止绕过人工复核）。

## 它是怎么工作的

```text
config/domains/*.json      领域配置：阶段、实体、判断锚点、来源、词表
        │
        ▼
sources.py                 采集：offline fixture / live RSS+HTML（失败只记覆盖缺口）
        │  Signal[]
        ▼
pipeline.py                时间窗 → 去重（URL/正文哈希/标题相似）→ 分类 → 映射到锚点
        │  SignalRecord + SignalJudgement
        ▼
evidence.py                证据强度 = 信息性质 × 来源权威 × 类别 × 阶段具体性；正负向判定
        │  EvidenceAssessment
        ▼
claims.py                  证据门槛 → 变化类型 → 置信度更新 → 版本与理由 → 人工确认闸门
        │  Change[]
        ▼
render.py / engine.py      变化摘要、时间线、证据卡（默认层/展开层）、与我相关、覆盖缺口、指标
        │
        ├── data/artifacts/<task>/<run>/digest.md   （可归档/分享）
        ├── data/artifacts/<task>/<run>/digest.json （可重放/审计）
        ├── data/radar.db                           （判断状态、版本、证据台账、反馈）
        └── data/web/*.html                         （静态站点）
```

每个阶段都是确定性的、可测试的。LLM **不是**必需品：它可以作为一个“起草器”接在
`claims.py` 之外（例如起草“与我何干”的措辞），但绝不允许绕过证据门槛直接改判断。

## 证据从哪里来 / 覆盖边界

默认 `offline` 模式读取 `config/fixtures/<domain>/signals/*.json`，保证可复现。
`live` 模式按 `config/domains/*.json` 里的来源 URL 拉取 RSS/Atom 或页面：

```bash
# 先检查可达性（并发抓取，单源 12s / 整轮 45s 上限，可调）
python -m frontier_radar ingest --timeout 12 --total-timeout 45

# 再把抓取结果写入离线缓存，作为下一轮的输入
python -m frontier_radar ingest --write-cache
python -m frontier_radar run --source live --now 2026-11-08T09:00:00+08:00
```

它会诚实地告诉你：

- 哪些来源取到了、哪些是空的、哪些不可达（`unreachable`）；
- 哪些结论仍只有单一来源；
- 哪些信息类型没有被覆盖（付费库、日韩语原文、社交平台深度内容等）；
- 本期是否使用了合成演示数据。

它不承诺：全网覆盖、自动绝对区分前沿与噪声、用热度代替重要性、把官方表述当独立验证、
在证据不足时给出确定性行动判断。

## 验证与指标

`python -m frontier_radar metrics` 会给出**过程指标**与**质量门**：

- 信息质量：重复率、噪声率、变化卡链接完整率、原文摘录完整率、独立来源组数、含相反证据的判断比例；
- 更新质量：每个版本是否留下理由、证伪/削弱/待确认数量、回滚与用户纠错次数；
- 用户价值代理：有变化的期数、静默期数、每期变化数、连续追踪周期数；
- 质量门：重复率 ≤ 15%、噪声率 ≤ 35%、链接完整率 = 100%、变更理由覆盖 = 100%、高风险修改必须人工确认。

这些指标只用于验证信息与更新质量，不替代用户判断。真正的验证问题仍然是：

> **用户是否愿意让系统长期保存并更新自己的关注清单和关键判断？**

如果答案是否定的，它仍只是一个临时搜索工具；如果答案是肯定的，它才开始具备“个人情报资产”的属性。

更完整的验证方案（3—4 期连续运行、抽样复核表、访谈问题）见 [docs/VALIDATION.md](docs/VALIDATION.md)。

## 扩展新领域

新增一个领域只需要一个 JSON：

```bash
cp config/domains/solid_state_battery.json config/domains/my_domain.json
# 修改 slug / name / stages / entities / anchors / sources / lexicon
python -m frontier_radar --profile my_domain validate
```

关键字段：

| 字段 | 作用 |
|---|---|
| `stages` | 阶段刻度（0 起连续编号），防止越级推断 |
| `entities` + `aliases` | 主体识别；别名冲突会被 `validate` 警告 |
| `anchors` | 判断锚点：`keywords`、`entity_keys`、`question`、`watch_indicators`、`baseline_claim` |
| `sources` | 来源类型、权威性、抓取 URL、`independent_group`（用于识别“同源多报”） |
| `category_blueprint` | 类别判定：短语 + 权重，决定“这条信息是什么” |
| `stage_patterns` | 阶段判定：每个阶段的短语与权重 |
| `info_layers` / `negation_phrases` | 信息分层与否定/澄清识别 |
| `noise` / `coverage_notes` / `gap_questions` | 噪声规则、覆盖边界与长期未解问题 |

## 项目结构

```text
run.cmd          Windows 启动器（自动找 Python、设置 UTF-8）
frontier_radar/
  profile.py     领域配置加载与校验辅助
  models.py      信号、判断、版本、变化、报告的数据结构
  db.py          SQLite 持久层（任务/来源/运行/信号/证据/判断/版本/反馈/事件/产物）
  text.py        文本归一化、分词、URL 规范化、指纹、时间解析
  evidence.py    分类器：类别、阶段、信息层、实体、锚点、噪声、证据强度
  claims.py      判断状态库：基线、证据门槛、变化类型、版本、确认、驳回、回滚
  pipeline.py    一次完整运行与报告组装
  render.py      Markdown / HTML 渲染
  engine.py      跨期指标、质量门、静态站点发布
  sources.py     离线 fixture 与在线采集（RSS/Atom/HTML）
  web.py         本地 Web 界面（标准库 http.server）
  cli.py         命令行入口
  validate.py    领域配置与 fixture 校验
config/
  domains/       领域配置：固态电池、人形机器人供应链、量化研究
  demo/          演示任务定义
  fixtures/      合成演示信号
tests/           52 个单元/端到端测试（python -m unittest discover -s tests）
docs/            产品需求、架构、验证方案、贡献指南
scripts/         一键冒烟自检
```

## 设计取舍

- **零依赖、本地优先**：SQLite + 标准库，clone 即跑；不需要账号、密钥或云服务。
- **规则优先，LLM 可选**：确定性逻辑保证可复现与可测试；LLM 只适合做措辞起草，不能替代证据门槛。
- **判断比摘要重要**：系统保存“当前相信什么、为什么”，而不是“今天又出现了多少篇文章”。
- **保守更新**：宁可一期不更新，也不让单一来源或营销口径改变判断。
- **可回滚**：每次判断修改都留版本、理由、触发证据；回滚也是新版本，不删除历史。
- **诚实呈现覆盖**：来源失败、单一来源、证据缺口都进入报告，而不是静默消失。

## 路线图

- [x] 单领域 × 单用户 × 单决策的完整闭环（MVP）
- [x] 判断状态库、版本历史、确认/驳回/回滚
- [x] 在线来源采集与覆盖缺口记录
- [x] 质量指标与静态站点导出
- [ ] 多主题并行追踪与跨主题传导（技术 → 岗位 → 供应链）
- [ ] 实体关系图与分歧地图（等实体数据稳定后再做）
- [ ] 用户自定义证据门槛与推送条件
- [ ] 团队共享情报空间（权限、审计、评论）
- [ ] 可选 LLM 起草：只生成措辞建议，仍需人工确认与证据绑定

## 许可证与数据声明

代码以 [MIT License](LICENSE) 发布。

`config/fixtures/` 下的全部信号为**合成的演示数据**，用于展示产品行为；
其中公司名称、时间与表述均为示例，不代表真实新闻或事实。
真实使用请配置自己的来源，并遵守目标站点的 robots 协议、服务条款与版权要求。

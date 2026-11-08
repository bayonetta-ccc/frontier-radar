# 参与贡献

## 开发环境

```bash
python --version          # 需要 ≥ 3.10
python -m unittest discover -s tests -v
python -m frontier_radar demo
python -m frontier_radar serve --open
```

项目没有第三方运行时依赖。请不要为了“方便”引入 Web 框架、ORM 或 LLM SDK：
如果确实需要，先开 issue 说明为什么标准库不够。

## 目录约定

| 目录 | 放什么 |
|---|---|
| `frontier_radar/` | 核心代码；按层组织，依赖只能自上而下 |
| `config/domains/` | 领域 profile；新增领域只加 JSON |
| `config/fixtures/` | 合成 fixture；不得包含真实个人数据或未授权内容 |
| `config/demo/` | 演示任务定义 |
| `tests/` | `unittest`；每个新规则至少一个用例 |
| `docs/` | 产品、架构、验证、贡献文档 |

## 修改原则

1. **证据优先**：任何改变判断结果的逻辑，必须先能解释“哪条证据、什么门槛、为什么”。
2. **保守更新**：让系统更容易更新判断的改动，需要同时给出“不会误更新”的测试。
3. **可回滚**：不要直接覆盖判断；写入新版本，保留理由与触发证据。
4. **事实与判断分离**：不要把公司口径、推断、观点写成“已确认事实”。
5. **覆盖透明**：采集失败必须进入覆盖缺口，不能静默跳过。
6. **中文优先，代码英文化**：面向用户的文案用中文；标识符、日志键、测试名用英文。

## 新增一个领域

1. 复制 `config/domains/solid_state_battery.json` 为 `config/domains/<slug>.json`；
2. 修改 `slug`、`name`、`stages`、`entities`、`anchors`、`sources`；
3. 用 `category_blueprint` 定义“这条信息是什么”，用 `stage_patterns` 定义“推进到哪一步”；
4. 准备 3—5 条 fixture 信号（`config/fixtures/<slug>/signals/*.json`），必须带
   `source_id`、`http(s)` 链接、≥ 24 字摘录与明确时间；
5. 运行：

```bash
python -m frontier_radar --profile <slug> validate
python -m frontier_radar --profile <slug> --data-dir data_<slug> run --mode baseline
```

6. 在 `README.md` 的“扩展新领域”与 `docs/ARCHITECTURE.md` 的可扩展点中保持同步。

## 测试要求

- 新增分类规则：在 `tests/test_classifier.py` 增加正例与反例；
- 新增判断规则：在 `tests/test_pipeline.py` 增加端到端用例（用 fixture 注入，不 mock 数据库）；
- 修复 bug：先写一个会失败的测试，再修复；
- 提交前跑：

```bash
python -m unittest discover -s tests -v
python -m frontier_radar demo --quiet
python -m frontier_radar metrics --json
```

## 提交信息

使用简短的祈使句，说明“改了什么、为什么”：

```text
fix: 阻止“否认延期”被误判为反向证据
feat: 支持标准类判断的阶段跃迁
docs: 补充覆盖缺口字段说明
test: 增加同源多报去重用例
```

## 不接受的贡献

- 把未授权内容（付费墙内容、个人隐私、未公开数据）放进 fixture；
- 为了“看起来更智能”而绕过证据门槛或人工确认；
- 引入需要 API key 才能跑测试的依赖；
- 用热度、情绪或点击量替代重要性判断；
- 在没有相反证据与覆盖缺口说明的情况下输出确定性结论。

## 行为准则

讨论聚焦证据与可复现的实验。批评结论可以，攻击人不可以。
对不确定的问题，明确说“不知道”比编一个答案更有价值。

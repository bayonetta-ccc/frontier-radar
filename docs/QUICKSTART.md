# 5 分钟上手

本页只做一件事：让你在 5 分钟内看到这个产品的核心差异——**它如何拒绝一次看起来合理的更新**。

## 环境

- Python ≥ 3.10
- 无需安装第三方依赖，不需要 API key
- Windows / macOS / Linux 均可

> Windows 上如果 `python` 命令打开的是 Microsoft Store，或提示“不是内部或外部命令”，
> 请改用项目自带的 `run.cmd`（见下），它会自动找到可用解释器并设置 UTF-8 控制台。

## 1. 跑一遍演示

在项目目录打开终端：

```bat
:: Windows
run.cmd demo
```

```bash
# macOS / Linux
python -m frontier_radar demo
```

你会看到三期运行：

```text
· 2026-07-28  baseline  变化 0 待确认 0 — 首次追踪：建立初始判断，全部等待用户确认。
· 2026-08-08  week-2    变化 4 待确认 4 — 十天后：识别增强 / 证伪；高风险修改停在待确认。
· 2026-08-15  quiet     变化 0 待确认 0 — 再一周：只有重复转载，明确输出“本期无需更新”。
```

## 2. 看第二期的第一条变化卡

打开 `data/artifacts/task_solid_state_2026/run_20260808T010000_update/digest.md`，找到：

```text
### 1. [证伪] 原判断已被否定，降级为待重建假设（依据：宝马澄清：QuantumScape 尚未完成整车级验证…）
- 置信度：0.54 → 0.20 ｜ 阶段：客户验证 → 客户验证 ｜ 待确认
- 为什么变化：出现 1 条高权重相反证据（合计 0.58），覆盖 1 个独立来源，
  且包含来自当事方的明确澄清；当前判断被足够可靠的材料否定。
  注意：目前仍只有单一来源组，结论需独立来源复核。
- 相反证据：宝马澄清，样件仍处于台架测试，尚未完成整车级验证。
```

注意三件事：

1. 它没有把“宝马澄清”直接当成结论，而是用它去否定一个**判断**；
2. 它主动标注“只有单一来源组，需要复核”；
3. 它**没有自动写入**，而是停在“待确认”。

## 3. 打开界面，自己确认或驳回

```bash
python -m frontier_radar serve --open
```

- 首页：三期运行记录 + 待确认提醒；
- 点击任一期：变化摘要、时间线、证据卡（可展开原文摘录与原始链接）、“与我相关”；
- 判断库：每条判断的置信度、阶段、待验证问题、版本历史；
- “确认修改 / 驳回修改”会真正改变判断状态，并写入事件日志。

## 4. 看看“确认之后”

```bash
python -m frontier_radar demo --confirm-after
python -m frontier_radar claims list
python -m frontier_radar claims show clm_2026_quantumscape_validation
```

`claims show` 会显示完整的版本历史与证据台账：基线版本、证伪版本、确认时间、理由，
以及被移出“当前依据”的旧支持证据。

## 5. 换成你自己的主题

```bash
python -m frontier_radar task new \
  --id task_my_topic \
  --topic "AI Agent 协议" \
  --role "产品经理" \
  --purpose "判断技术栈与生态变化" \
  --decision "决定是否把某个 Agent 协议纳入产品架构" \
  --constraints "需要可核验的客户案例；不采用发布会口径" \
  --horizon "3 个月内" \
  --watchlist "MCP,AgentKit"

python -m frontier_radar run --task task_my_topic --mode baseline \
  --now 2026-11-08T09:00:00+08:00
```

默认读取 `config/fixtures/solid_state_battery/` 的演示信号。要做真实主题，
按 [CONTRIBUTING](CONTRIBUTING.md) 新增一个领域 profile 与来源，或先用
`ingest --write-cache` 抓一轮真实 RSS。

## 常见问题

**Q：为什么要停在“待确认”，不能自动更新吗？**
A：高风险判断（量产时间表、监管结论）一旦误判代价很高。系统把“改判断”当成需要负责的动作，
而不是一次摘要刷新。你可以在界面或 CLI 里一键确认。

**Q：为什么只有 4 个判断？**
A：MVP 刻意限制在 3—5 个关键判断，避免变成“什么都跟踪、什么都不深”。
基线的判断由证据锚点选出，全部需要你确认。

**Q：没有网络能跑吗？**
A：能。默认就是离线 fixture 模式。`live` 模式是尽力而为，失败会记为覆盖缺口，
不会阻断整次运行，也不会假装抓到了。

**Q：能接入 LLM 吗？**
A：可以，但只建议接在“措辞起草”环节，输出仍然要经过证据门槛与人工确认。
见 [ARCHITECTURE](ARCHITECTURE.md) 的“可扩展点”。

# Open Source Strategy

这份文档回答一个很窄的问题：哪些外部开源项目值得我们借设计、做适配，哪些只适合参考，哪些不该成为核心依赖。

核心结论先放前面：本项目的主脑仍然是 GPT/Codex。我们的职责是把 CTF 工作流、workspace、trace、resume、artifact、verification、memory、UI 和 benchmark 组织起来，而不是自己重造一个通用推理引擎。

## 结论总览

| 项目 | 许可证 | 角色判断 | 适合我们借什么 | 不建议做什么 |
| --- | --- | --- | --- | --- |
| [verialabs/ctf-agent](https://github.com/verialabs/ctf-agent) | MIT | 可直接借设计 | 多模型竞速、协调器调度、trace/replay、平台化 sandbox 思路 | 不要把它的 solver 结构硬塞进我们的主循环 |
| [BoxPwnr](https://github.com/0ca/BoxPwnr) | AGPL-3.0 | 只可参考，适合外置适配 | 广泛平台覆盖、benchmark 组织方式、trace 产物、平台抽象 | 不要内嵌代码到本仓库 |
| [NYU-LLM-CTF/nyuctf_agents](https://github.com/NYU-LLM-CTF/nyuctf_agents) | MIT | 可直接借设计 | planner / executor / autoprompter 拆分、基准化运行方式、multi-agent baseline | 不要把 baseline 逻辑当成我们的主脑 |
| [NUSGreyhats/ctf-agent-workstation](https://github.com/NUSGreyhats/ctf-agent-workstation) | GPL-3.0 | 只可参考 | 协作工作台、云 VM、per-run workspace、web UI、团队协作模型 | 不要复制其实现或把它作为内建代码依赖 |
| [aliasrobotics/cai](https://github.com/aliasrobotics/cai) | 混合：MIT + research-only 专有扩展 | 只可参考 | 多 provider 姿态、guardrails、security AI 产品化视角 | 不要引入其受限部分，也不要把它当作通用 solver 框架 |

## 1. 哪些可以直接借设计

### verialabs/ctf-agent
它的价值在于把 CTF 任务当作并行竞速问题：协调器负责调度，多个 solver 负责局部探索，trace 记录足够完整，sandbox 也比较明确。这个方向和我们现在的 Orchestrator + LLMActionLoop + Verifier + memory 很接近。

适合直接吸收的设计：
- coordinator 负责调度与汇总，而不是自己做推理
- 多 solver 并行 racing
- 统一 trace / replay / summary
- sandbox 和题型工具清单分层

### NYU-LLM-CTF/nyuctf_agents
它更像一个干净的 benchmark baseline：planner、executor、auto-prompter 的角色划分很清楚，适合拿来对照我们自己的 workflow contract。

适合直接吸收的设计：
- planner / executor / autoprompter 分层
- benchmark 驱动的运行入口
- 任务级别的运行数据和复现路径
- 把 baseline 与 multi-agent 变体拆开管理

## 2. 哪些因为 GPL/AGPL 只可参考

### BoxPwnr
它的强项不是单一解题器，而是 benchmark 容器：平台很多，适配面广，trace 和结果也比较完整。它很适合当 benchmark backend 的参考对象，但因为是 AGPL-3.0，不适合把内部实现直接并进来。

### NUSGreyhats/ctf-agent-workstation
它更像一个完整的协作工作台：云 VM、web UI、协作 runs、workspace、通知、提权和平台管理都放在一个系统里。这个方向和我们有交集，但 GPL-3.0 意味着更适合当架构参考，而不是内建依赖。

### aliasrobotics/cai
CAI 的公开仓库是混合授权：一部分来自 MIT 代码，一部分是 research-only 的专有扩展。它可以作为安全 AI 产品化的参考，但不适合把实现层拿来拼接到我们的主系统里。

## 3. 哪些适合做可选 adapter

- BoxPwnr 作为 benchmark backend adapter：值得。它已经把大量平台和评测流程串起来了，适合作为外置评测后端，和我们自己的 eval/local benchmark 并存。
- OpenAI Agents SDK 作为未来 provider backend adapter：值得，但只能做可选后端。它适合接 OpenAI 原生模型、Runner、hand-off、guardrails、session 这类 first-party 能力；但我们的主 orchestrator 仍应保留在仓库里。

不建议把 verialabs、NYU baseline、NUSGreyhats 或 CAI 直接做成内建 adapter。它们更适合做设计参考，或者在边界很清楚时单独包一层外部适配。

## 4. 为什么本项目不替代 GPT，而是包住 GPT

因为我们的价值不是“再造一个大模型”，而是把大模型变成一个可控、可复现、可评测的 CTF 工作流系统。

GPT/Codex 负责：
- 生成假设
- 选择下一步动作
- 在长上下文里综合证据
- 把源码、trace、skill notes、memory、tool registry 组合成决策

本项目负责：
- 工具执行
- workspace 与 artifact 管理
- trace 与 resume
- verification 和 flag 管控
- benchmark 与 memory
- UI 和协作
- 安全边界与脱敏

如果把 GPT 也自己替掉，我们会把资源消耗在模型训练、推理、上下文管理和对齐上，最后丢掉当前最重要的产品能力：稳定的工作流、可审计性、可复现性和模型可替换性。

## 5. 是否值得把 BoxPwnr 作为 benchmark backend

值得。

理由很直接：它已经覆盖了很多 CTF / lab 平台，天然偏 benchmark，且自带 trace、结果、平台抽象和 external solver 入口。对我们来说，最合理的方式不是引入它的 solver 主体，而是把它看作一个外部 benchmark backend，和本地 evals、NYU CTF Bench、Cybench 之类的数据集并列。

推荐方式：
- 外置进程 / CLI 级别集成
- 只接 benchmark 运行和结果采集
- 不把 BoxPwnr 的 solver loop 复制进主仓库

## 6. 是否值得把 OpenAI Agents SDK 作为未来 provider backend

值得，但只作为 provider / runtime backend，不作为主 orchestrator。

它的价值在于：
- MIT 许可
- 原生支持 OpenAI 模型和 Responses API 生态
- Agent + Runner 已经封装了 turns、tools、guardrails、handoffs、sessions
- 适合作为第一方 OpenAI 能力的桥接层

但它不该吞掉我们自己的工作流控制层。我们的系统需要保留：
- strict JSON action loop
- structured observations
- challenge trace / memory / artifact 语义
- CTF 专用安全约束
- benchmark 一致性

所以最好的位置是：可选 provider backend，不是核心依赖。

## 最终建议

1. 主线继续围绕 GPT/Codex + 我们自己的 workflow substrate。
2. MIT 项目只拿设计，不拿实现。
3. AGPL / GPL 项目放到外置适配层或纯参考区。
4. BoxPwnr 作为 benchmark backend 的候选优先级最高。
5. OpenAI Agents SDK 作为未来 provider backend 的候选优先级第二。


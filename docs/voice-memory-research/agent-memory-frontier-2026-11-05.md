# Agent 记忆前沿研究、GitHub 项目与商业许可对比

**检索日期：2026-11-05（北京时间）。** 我核对了论文原文、官方项目主页和 GitHub 仓库。论文标注首发及修订时间；项目标注最近推送时间。以下是代表性筛选，论文中的效果属于作者报告，本次未逐项复现实验。

**许可证核验日期：2026-11-05（北京时间）。** 本文保留研究综述的全部内容，补齐论文、代码、许可证及时间来源链接，并增加商业使用范围说明。商用判断针对明确被相应许可证覆盖的代码；模型权重、数据集、第三方依赖及托管服务条款需分别判断。文中的“许可程度”表示对闭源商业产品的友好程度，不是法律评分，也不代表项目成熟度。

## 研究方向

我的判断是：**Agent 记忆的研究重点，正在从“存储与召回历史”，转向“学习如何管理记忆，以及如何用记忆改善后续行动”。** 最值得关注的方向有四个：

- **学习记忆操作策略**：让 Agent 学会何时写入、检索、更新、压缩和遗忘，代表是 AgeMem、MemSkill。
- **根据实际效果选择经验**：记忆不仅按语义相似度排序，还根据任务反馈学习哪些经验有用，代表是 MemRL。
- **结合事件、关系和时间**：保留经历的上下文，同时建立可查询的事实关系，代表是 Hindsight、EverMemOS 和近期混合记忆研究。
- **用后续任务验证记忆价值**：除了问“记得什么”，还测“是否因此做得更好”，代表是 MemoryArena。上述方向分别可见下表中的原论文。

## 近期论文

先看近期论文。这里把**较新的探索性工作**和**已有官方代码的研究**放在一起比较，便于判断研究价值与复现条件。

| 论文 | 时间：首发 → 最近修订 | 核心贡献与阅读价值 | 官方代码 / 证据边界 |
|---|---|---|---|
| [Long-Term Agent Memory through Complementary Curation and Consolidation](https://arxiv.org/abs/2610.39790) | **2026-10-29** | 将情景记忆与结构化关系记忆结合，再离线合并冗余、处理过时信息。适合研究“事件原文和事实图谱如何互补” | 近期预印本；本次未确认官方代码。实验集中在文本对话 |
| [Agentic Memory Management Is the Next Frontier of Information Retrieval](https://arxiv.org/abs/2610.32464) | **2026-10-23** | 从信息检索角度讨论记忆的组织、检索、使用与效用；提出不同记忆对象具有不同检索语义 | **观点论文**，适合找研究问题，不能作为算法效果证明 |
| [Dual-Layer Agentic Memory](https://arxiv.org/abs/2608.22215) | **2026-08-23 → 08-31** | 快速写入阶段决定“不写、新增或更新”；慢速阶段将高价值外部记忆通过微调内化到模型参数 | 预印本；论文注明代码和数据将在接收后发布 |
| [MemCon：Memory as a Controlled Process](https://arxiv.org/abs/2607.13591) | **2026-07-15 → 10-12** | 用在线反馈学习何时检索、检索多少、何时复用计划，以及何时归纳或遗忘；可以包裹不同记忆后端 | 预印本；本次未核实官方代码 |
| [MemTrace](https://arxiv.org/abs/2605.28732) | **2026-05-27 → 10-08** | 将记忆处理流程表示为可追踪的演化图，定位错误来自提取、合并、检索还是后续使用 | [GitHub](https://github.com/zjunlp/MemTrace)。适合做记忆系统调试和错误归因 |
| [MSA：Memory Sparse Attention](https://arxiv.org/abs/2603.23516) | **2026-03-06 → 04-13** | 在模型内部结合稀疏注意力、压缩 KV 和路由，探索超大规模潜在记忆 | [GitHub](https://github.com/EverMind-AI/MSA)。属于模型架构路线，需要模型和推理系统适配 |
| [MemSkill](https://arxiv.org/abs/2602.02474) | **2026-02-02 → 05-24** | 将记忆提取、整合、裁剪等操作做成可学习、可演化的技能，由 controller、executor、designer 组成闭环 | [GitHub](https://github.com/ViktorAxelsen/MemSkill)。重点是学习“如何处理记忆”的技能 |
| [MemRL](https://arxiv.org/abs/2601.03192) | **2026-01-06 → 02-12** | 冻结 LLM，通过环境反馈更新情景记忆的效用；先筛语义相关性，再选择高价值经验 | [GitHub](https://github.com/MemTensor/MemRL)。适合研究不更新模型权重的持续改进 |
| [AgeMem](https://arxiv.org/abs/2601.01885) | **2026-01-05 → 07-23** | 将长期记忆和短期上下文管理统一为 Agent 的工具动作，用强化学习训练操作策略 | [GitHub](https://github.com/y1y5/AgeMem)。论文标注 ACL 2026 SAC Highlight |
| [EverMemOS](https://arxiv.org/abs/2601.02163) | **2026-01-05 → 01-09** | 将经历形成 MemCells，再归纳为主题场景 MemScenes 和用户画像，检索时重建所需上下文 | [当前仓库 EverOS](https://github.com/EverMind-AI/EverOS)。适合长期对话和用户状态研究；当前主线已继续演进 |

表中省略年份的修订日期均为 **2026 年**。研究价值与代码商用许可是两个独立维度；补充许可核验见后文。

## 基础工作与综述

理解这些论文之前，以下基础工作也值得补读：

| 工作 | 首发时间 | 为什么仍然重要 |
|---|---|---|
| [MemGPT](https://arxiv.org/abs/2310.08560) | **2023-10-12** | 用操作系统分层内存的思路管理有限上下文，是 Agent 自主管理记忆的重要起点 |
| [A-MEM](https://arxiv.org/abs/2502.12110) | **2025-02-17** | 借鉴卡片笔记法，为记忆建立动态链接，并让新记忆促使旧记忆更新；[系统代码](https://github.com/WujiangXu/A-mem-sys) |
| [Mem0](https://arxiv.org/abs/2504.19413) | **2025-04-28** | 研究从对话提取、整合和检索持久信息，是实用记忆系统的重要比较基线 |
| [Hindsight](https://arxiv.org/abs/2512.12818) | **2025-12-14** | 区分事实、经历、实体归纳和信念，通过 retain、recall、reflect 支持可追溯推理 |
| [SimpleMem](https://arxiv.org/abs/2601.02553) | **2026-01-05** | 研究结构化压缩、在线语义整合和意图驱动检索，关注准确率与 token 成本的平衡 |

如果希望先建立整体知识框架，推荐综述 [Memory in the Age of AI Agents](https://arxiv.org/abs/2512.13564)，**2025-12-15 首发、2026-01-13 修订**。它按记忆的载体、功能和生命周期分类，配套的[官方论文清单](https://github.com/Shichun-Liu/Agent-Memory-Paper-List)适合作为阅读入口。

## 开源项目与商用许可

再看可实际使用的开源项目。下表日期来自 GitHub 公共 API 的 `pushed_at`，使用 **UTC 日期**；它表示仓库最近发生推送，不等于正式版本发布日期。

“高（宽松）”表示在履行许可义务后，可用于闭源收费产品；“有条件”表示允许商业经营，但受源码提供及相同许可等条件约束。

| GitHub 项目 | 最近推送 | 许可证 | 商用许可程度 | 主要能力与适用场景 |
|---|---|---|---|---|
| [Mem0](https://github.com/mem0ai/mem0) | [2026-11-04](https://api.github.com/repos/mem0ai/mem0) | [Apache-2.0](https://github.com/mem0ai/mem0/blob/main/LICENSE) | **允许；高（宽松）**。可闭源商用，保留许可及适用声明、标注修改 | 提供用户、会话和 Agent 记忆接口。适合先搭建个性化助手、客服记忆和工程基线 |
| [Graphiti](https://github.com/getzep/graphiti) | [2026-11-03](https://api.github.com/repos/getzep/graphiti) | [Apache-2.0](https://github.com/getzep/graphiti/blob/main/LICENSE) | **允许；高（宽松）**。可闭源商用；Zep 托管服务另适用其条款 | 时序图谱，记录事实有效期、关系变化及来源。适合回答“现在是什么”和“过去是什么” |
| [Hindsight](https://github.com/vectorize-io/hindsight) | [2026-11-04](https://api.github.com/repos/vectorize-io/hindsight) | [MIT](https://github.com/vectorize-io/hindsight/blob/main/LICENSE) | **允许；高（宽松）**。可闭源商用，保留版权和许可声明 | 提供 retain / recall / reflect，结合语义、关键词、图关系和时间检索。适合需要跨事件理解与来源追踪的 Agent |
| [Letta Code](https://github.com/letta-ai/letta-code) | [2026-11-05](https://api.github.com/repos/letta-ai/letta-code) | [Apache-2.0](https://github.com/letta-ai/letta-code/blob/main/LICENSE) | **允许；高（宽松）**。开源代码可闭源商用，云服务另计 | 有状态 Agent 运行框架，支持自编辑记忆块、历史搜索、后台记忆整理和 Git 管理的 MemFS |
| [LangMem](https://github.com/langchain-ai/langmem) | [2026-11-03](https://api.github.com/repos/langchain-ai/langmem) | [MIT](https://github.com/langchain-ai/langmem/blob/main/LICENSE) | **允许；高（宽松）**。可闭源商用，保留版权和许可声明 | 提供记忆管理工具、后台提取与整合，并原生连接 LangGraph Store。适合已有 LangGraph 的系统 |
| [MemOS](https://github.com/MemTensor/MemOS) | [2026-11-04](https://api.github.com/repos/MemTensor/MemOS) | [Apache-2.0](https://github.com/MemTensor/MemOS/blob/main/LICENSE) | **允许；高（宽松）**。开源代码可闭源商用；外部依赖和云服务分别核验 | 统一记忆增删改查，支持多模态、工具轨迹、记忆空间组合及反馈纠错。适合比较完整的记忆基础设施 |
| [EverOS，原 EverMemOS](https://github.com/EverMind-AI/EverOS) | [2026-11-05](https://api.github.com/repos/EverMind-AI/EverOS) | [Apache-2.0](https://github.com/EverMind-AI/EverOS/blob/main/LICENSE) | **允许；高（宽松）**。可闭源商用，保留许可及适用声明、标注修改 | 当前强调本地优先、Markdown 持久化、跨应用记忆，以及离线整合画像和技能 |
| [OpenViking](https://github.com/volcengine/OpenViking) | [2026-11-05](https://api.github.com/repos/volcengine/OpenViking) | [AGPL-3.0](https://github.com/volcengine/OpenViking/blob/main/LICENSE) | **允许；有条件（强 copyleft）**。收费可以；分发及修改后提供网络服务涉及源码义务 | 用虚拟文件系统统一知识、记忆和技能；通过摘要、概览、原文三层逐步读取。适合大型项目上下文 |
| [SimpleMem](https://github.com/aiming-lab/SimpleMem) | [2026-07-24](https://api.github.com/repos/aiming-lab/SimpleMem) | [MIT](https://github.com/aiming-lab/SimpleMem/blob/main/LICENSE) | **允许；高（宽松）**。可闭源商用，保留版权和许可声明 | 结构化记忆、压缩和检索，提供论文复现入口。适合研究及成本比较 |
| [memU](https://github.com/NevaMind-AI/memU) | [2026-11-05](https://api.github.com/repos/NevaMind-AI/memU) | [Apache-2.0，按许可证文本](https://github.com/NevaMind-AI/memU/blob/main/LICENSE.txt) | **允许；高（宽松）**。开源代码可闭源商用，配套在线服务另计 | 当前以共享 Wiki 和从 Agent 历史提取 Markdown 技能为特点，适合跨会话、跨工具的个人记忆 |

有三处变化会直接影响选型：

- **Letta 的当前源码已迁到 `letta-ai/letta-code`**。旧 `letta-ai/letta` 仓库说明，V1 API Server 保留在 archive 分支，当前项目应使用新源码。[官方说明](https://github.com/letta-ai/letta)
- **EverMemOS 旧链接已跳转到 EverOS**。复现早期论文时，应核对对应版本，不能假定当前主线实现与论文完全一致。[当前仓库](https://github.com/EverMind-AI/EverOS)
- **Mem0 当前 README 的部分高分属于托管平台**，明确包含开源 SDK 没有的专有优化。因此，应分别比较开源实现和托管服务。[README 中的说明](https://github.com/mem0ai/mem0#new-memory-algorithm-april-2026)

## 评测与数据授权

评测方面，建议至少区分下面四类能力，避免只凭一张准确率排行榜判断项目优劣。

| 评测 | 论文首发时间 | 主要测什么 | 使用价值 |
|---|---|---|---|
| [LoCoMo](https://arxiv.org/abs/2402.17753) · [代码](https://github.com/snap-research/locomo) | **2024-02-27** | 长期对话中的事实、时间、多跳问答等 | 适合做对话记忆基线 |
| [LongMemEval](https://arxiv.org/abs/2410.10813) · [代码](https://github.com/xiaowu0162/LongMemEval) | **2024-10-14** | 信息提取、跨会话推理、时间推理、知识更新、拒绝无依据回答 | 适合测试持续变化的用户记忆 |
| [MemoryArena](https://arxiv.org/abs/2602.16313) · [代码](https://github.com/ZexueHe/MemoryArena) | **2026-02-18** | Agent 是否利用前面任务的经验完成后续相互依赖的任务 | 更直接检验“记忆是否改善行动” |
| [AMA-Bench](https://arxiv.org/abs/2602.22769) · [代码](https://github.com/AMA-Bench/AMA-Bench) | **2026-02-26** | 对状态、动作、观察和工具输出组成的长轨迹进行记忆问答 | 补充对话之外的轨迹和因果信息；主要仍以问答评测 |

评测能力与商业使用授权分别如下：

| 评测资源 | 本次核实的许可依据 | 商用判断 |
|---|---|---|
| LoCoMo | 仓库 [LICENSE.txt](https://github.com/snap-research/locomo/blob/main/LICENSE.txt) 为 **CC BY-NC 4.0** | **现有许可不覆盖商业用途**。非商业复现需遵守署名等条款；用于商业研发、训练或评测时，应先判断具体用途是否落入 NC 限制，必要时取得额外授权。公司内部运行不自动等于非商业用途 |
| LongMemEval | 代码仓库 [LICENSE](https://github.com/xiaowu0162/LongMemEval/blob/main/LICENSE) 为 **MIT** | **已许可代码可宽松商用**；单独下载的数据、第三方材料及模型仍核对各自条款 |
| MemoryArena | [官方仓库及 README](https://github.com/ZexueHe/MemoryArena)；本次未找到根目录 LICENSE，README 未说明完整许可 | **商用授权未核实**。公开可见不能直接视为可商用或可任意复制分发；环境和子模块也分别核验 |
| AMA-Bench | 代码仓库 [LICENSE](https://github.com/AMA-Bench/AMA-Bench/blob/main/LICENSE) 为 **MIT** | **已许可代码可宽松商用**；单独下载的数据与接入环境不自动随代码取得许可 |

LoCoMo 的 NC 限制来自仓库许可证，不是因为它属于学术论文。CC BY-NC 4.0 将非商业用途限定为主要不面向商业利益或金钱报酬的用途。不能把“不对外卖数据”当成唯一判断标准。依据：[CC 官方说明](https://creativecommons.org/licenses/by-nc/4.0/)、[正式条文第 1、2 节](https://creativecommons.org/licenses/by-nc/4.0/legalcode.en)。

## 按任务选择方案

我的选型建议是按任务缩小范围，而不是选一个通用冠军：

| 目标 | 优先研究或试用 |
|---|---|
| 快速给助手增加用户偏好、历史事实记忆 | **Mem0**，作为工程基线 |
| 对历史事件进行综合理解，并追踪依据 | **Hindsight、EverOS** |
| 处理人物、组织、关系与事实随时间变化 | **Graphiti** |
| 已有 LangGraph，希望自行控制记忆流程 | **LangMem** |
| 希望 Agent 自己修改记忆，并长期运行 | **Letta Code** |
| 统一项目文档、知识、记忆和技能 | **OpenViking、MemOS** |
| 做研究，探索记忆带来的持续学习 | **MemRL → AgeMem → MemSkill** |

真正做比较时，我建议固定同一模型、同一批数据和同一检索预算，同时记录**回答正确率、知识更新正确率、任务成功率、写入成本和响应延迟**。这是基于上述研究的工程判断：一个记忆系统是否合适，取决于它能否在你的任务中持续保留有用信息、纠正旧信息，并改善后续结果。

## 不同许可证能否商业化，许可范围有多大

**MIT 和 Apache-2.0 都属于宽松许可，商业使用空间很大；AGPL-3.0 同样允许收费经营，但保留覆盖代码及修改完全闭源的空间更小；CC BY-NC 4.0 不授予商业用途许可；授权未核实的仓库不能按宽松许可处理。**

以下判断以履行许可证条件为前提，针对许可证覆盖的软件或材料。

| 许可证 / 状态 | 能否商业使用 | 闭源收费产品 / SaaS | 出售副本、交付客户 | 主要义务与许可程度 |
|---|---|---|---|---|
| [MIT](https://opensource.org/license/mit) | **可以**，许可证本身不按收入或用户量收费 | **可以**，无须仅因使用 MIT 代码而公开自己的源代码 | **可以**，包括修改后分发 | **高（宽松）**。在软件副本或实质部分保留版权及许可声明；有免责声明 |
| [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) | **可以**，许可证授予免版税的相应权利 | **可以**，不要求仅因使用该代码而公开自己的源代码 | **可以**，可在条件允许范围内为自己的修改设置不同条款 | **高（宽松）**。分发时附许可、标注修改、保留适用声明，并按要求保留上游 NOTICE；含明确但有限范围的贡献者专利授权，不授予品牌使用许可 |
| [AGPL-3.0](https://opensource.org/license/agpl-3.0) | **可以**，允许收费 | **有条件**。修改后提供远程网络交互时，须向这些用户提供获取对应源码的机会 | **可以**，但须履行适用的源码提供及相同许可义务 | **对收费经营开放，对闭源保留限制较多**。不适合直接假定覆盖代码可以始终保持闭源 |
| [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/) | **该许可不授予商业用途权利**；额外授权或法定例外另论 | 不能仅据此许可将覆盖材料用于商业服务 | 商业售卖覆盖材料不在该许可范围内 | **商业许可范围受限**。非商业使用仍需遵守署名、修改说明等条件 |
| 未核实完整授权 / 未提供明确许可 | **不能确认** | 不应默认可以 | 不应默认可以复制、修改或分发 | **未知**。GitHub 上可以查看、fork，不等于取得商业再利用授权；见 [GitHub 官方许可说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository) |

### MIT 与 Apache-2.0 的实际区别

- **MIT 条款较简洁**，允许使用、修改、分发和销售，主要要求保留版权与许可声明。许可证文本没有像 Apache-2.0 第 3 条那样单列明确的专利授权条款。[MIT 原文](https://opensource.org/license/mit)
- **Apache-2.0 同样适合闭源商用**，但对分发声明、修改标识和 NOTICE 处理规定更细；其专利授权仅覆盖条文定义的贡献者相关专利请求项，并设有相应终止条件，不能理解为涵盖所有第三方专利。[第 3—6 条](https://www.apache.org/licenses/LICENSE-2.0)

对希望保留业务代码、算法修改及产品差异化的商业团队，这两种许可通常都具有较大的使用空间。这里的“宽松”不免除保留声明等义务，也不意味着上游的企业版、付费服务或独立模型权重免费开放。

### AGPL-3.0 对 OpenViking 商业化的具体影响

OpenViking 当前根目录为 [AGPL-3.0](https://github.com/volcengine/OpenViking/blob/main/LICENSE)。关键在于代码的修改、组合和交付方式：

- **收费运营、收部署费和服务费可以**，AGPL 不禁止盈利。
- **修改后通过网络供用户交互**：第 13 条要求向这些用户显著提供免费获取该版本对应源码的机会；不只看是否把软件安装包交给客户。
- **向客户分发覆盖的软件或衍生作品**：按第 4—6 条履行许可与对应源码义务；涉及组合后的覆盖作品时，不能只公开自己选定的一小部分补丁。
- **独立业务代码的范围不能一概而论**：AGPL 不自动覆盖整个公司代码库，也不能仅凭拆成 HTTP 服务或容器就认定完全不受影响；需判断是否形成同一覆盖作品、衍生作品或真正独立的程序。

因此，如果商业计划要求覆盖的引擎修改始终闭源，优先选择满足需求的 MIT / Apache-2.0 方案会更直接。若选择 OpenViking，应先确定源码提供范围，或确认权利人是否愿意另授适合该用途的许可；本次未核实存在可直接购买的替代商业授权。依据：[项目许可](https://github.com/volcengine/OpenViking/blob/main/LICENSE)、[AGPL 正式文本第 4—6、13 条](https://opensource.org/license/agpl-3.0)。

## 研究代码的补充许可核验

除了工程项目，前文的研究实现也需要单独核验。下面只针对明确引用的仓库，不扩展到同名第三方实现。

| 研究实现 | 许可依据 | 商用许可程度 / 当前结论 |
|---|---|---|
| [MemTrace](https://github.com/zjunlp/MemTrace) | [MIT](https://github.com/zjunlp/MemTrace/blob/main/LICENSE) | **允许；高（宽松）**，针对许可覆盖的代码 |
| [MemRL](https://github.com/MemTensor/MemRL) | [MIT](https://github.com/MemTensor/MemRL/blob/main/LICENSE) | **允许；高（宽松）**，训练数据、基础模型与环境另计 |
| [MemSkill](https://github.com/ViktorAxelsen/MemSkill) | [Apache-2.0](https://github.com/ViktorAxelsen/MemSkill/blob/main/LICENSE) | **允许；高（宽松）**，保留许可及适用声明、标注修改 |
| [A-MEM 系统实现](https://github.com/WujiangXu/A-mem-sys) | [MIT](https://github.com/WujiangXu/A-mem-sys/blob/main/LICENSE) | **允许；高（宽松）**，针对该系统仓库的许可代码 |
| [AgeMem](https://github.com/y1y5/AgeMem) | 根目录未找到完整 LICENSE；[pyproject.toml](https://github.com/y1y5/AgeMem/blob/main/pyproject.toml) 带有 Trinity-RFT 包的 Apache 许可分类 | **完整商用授权待核实**。包元数据提示不能直接替代对项目新增代码许可范围的确认，不据此直接标成“已确认 Apache-2.0” |
| [MSA](https://github.com/EverMind-AI/MSA) | [原始 README](https://raw.githubusercontent.com/EverMind-AI/MSA/main/README.md) 标注 MIT 徽标；本次未找到根目录独立 LICENSE | **作者声明为宽松许可，但完整许可证据待补齐**。实际代码发行物、[模型发布页](https://huggingface.co/EverMind-AI/MSA-4B)和权重分别核对；不把 README 徽标当作所有附件的统一授权 |
| 前文尚未确认官方代码的论文 | 论文页面及论文自身许可 | **未评估代码商用许可**。论文可阅读、方法可研究，不等于已经取得未发布实现、论文图文或模型权重的商业复用许可 |

“未核实”不等于确认禁止商用，而是目前没有足够依据给出“可直接用于商业产品”的结论。公开仓库与开源授权的区别，见 [GitHub 官方说明](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository)及 [Choose a License：No License](https://choosealicense.com/no-permission/)。

## 面向商业产品的选择建议

如果目标是开发闭源收费的语音 / 数字人长期记忆产品，可先在 **Hindsight、Graphiti、Mem0、LangMem、MemOS、EverOS、Letta Code、SimpleMem、memU** 中按架构和效果比较；它们本次核实的根目录许可证为 MIT 或 Apache-2.0。这个建议仅针对许可覆盖的开源部分。

**OpenViking 的商用并非不可行**，但需把源码提供义务纳入产品架构和交付方式；**LoCoMo 的数据授权也应与记忆引擎代码授权分开处理**。评测授权尚未明确时，可先建设自有、获得授权的中文语音对话测试集，保留时间更新、纠错、跨会话和任务完成等评测维度。

正式选择版本时，保存对应 tag / commit 的 LICENSE、NOTICE 和依赖清单。本文链接多数指向可变化的主分支，以上结论是 **2026-11-05** 的核验快照，不能替代对实际采用版本及具体集成方式的确认。

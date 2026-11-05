# 语音生活陪伴记忆设计：GitHub 开源项目对比

结合[记忆设计文档](../voice-memory-implementation-plan.md)，优先值得研究的是 **Hindsight、Graphiti 和 Memobase**：分别接近你的存储与召回架构、时间事实模型、用户画像与生活陪伴需求。

截至 **2026 年 11 月 5 日**，筛选了下面 12 个项目。相似度是按你的设计做的架构判断；依据官方仓库、文档及部分关键代码，尚未运行压测。

## 项目对比

| 项目／许可证 | 相似度与定位 | 与你的设计相似的部分 | 关键差异及接入判断 |
| --- | --- | --- | --- |
| **[Hindsight](https://github.com/vectorize-io/hindsight)**／MIT | **高：整体记忆引擎** | PostgreSQL＋pgvector；事实、经历、证据支持的归纳与反思；语义、关键词、图、时间四路召回，RRF 融合与 token 预算。对应 M07–M08、M11–M13。 | **最适合做首个工程对照。** 存储路线与你接近，但神经重排的耗时需要实测。对象存储引擎 [Memlake 属于企业版](https://hindsight.vectorize.io/blog/2026/10/13/memlake)，不能计入开源版现成能力。 |
| **[Graphiti](https://github.com/getzep/graphiti)**／Apache-2.0 | **高：时间事实与关系** | 双时间跟踪、实体关系、事实有效区间、冲突后失效并保留历史、episode 来源追溯、混合检索。最接近 M02–M05、M12。 | **最值得借鉴数据模型。** 默认依赖 Neo4j、FalkorDB 等图存储，与你首期坚持 PostgreSQL 的路线有差异；账号、授权和运维也需自行建设。 |
| **[Memobase](https://github.com/memodb-io/memobase)**／Apache-2.0 | **高：用户画像与生活事件** | 面向用户的结构化画像、事件时间线、可配置画像字段、对话缓冲与后台批处理、上下文组装。接近 M01、M07、M13–M14。 | **适合参考陪伴产品的画像组织。** 使用 FastAPI、Postgres、Redis；默认处理后清理输入 blob，需要另外保留你的证据胶囊，并适配跨会话新鲜度要求。 |
| **[MemOS](https://github.com/MemTensor/MemOS)**／Apache-2.0 | **较高：记忆生命周期平台** | 统一增删改查、记忆反馈纠正、多模态、Memory Cube 隔离与组合、MemScheduler 异步处理。可参考写入、管理及后台任务设计。 | 能力范围广，但服务端自托管方案使用 **Neo4j＋Qdrant**，会增加基础设施。适合研究模块划分，首期整套接入的成本较高。 |
| **[Mem0](https://github.com/mem0ai/mem0)**／Apache-2.0 | **较高：通用记忆组件** | 用户／Agent／会话范围、自动事实抽取、多种向量存储适配、语义＋关键词＋实体信号召回。可参考 M01、M11–M12、M24。 | 当前新算法强调 **ADD-only 追加抽取**，仍需补充你的纠正与失效规则。开源代码对部分时间参数明确拒绝，不能把平台时间推理和官方平台跑分视为 OSS 能力。见[代码](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py)。 |
| **[memharness](https://github.com/las7/memharness)**／Apache-2.0 | **局部很高：可审计事实存储** | 区分现实有效时间与系统获知时间；纠正关联前后版本；逐事实来源；支持 `as_of`、`why`、`diff` 和按来源遗忘。很贴近第 7 节及 M03–M05。 | **小而直接，适合阅读实现细节。** TypeScript＋SQLite，存储层不调用模型；自动抽取由上层承担，不能直接满足你的 PostgreSQL、多用户和长期归档要求。 |
| **[Letta Code](https://github.com/letta-ai/letta-code)**／Apache-2.0 | **中高：持续身份与上下文管理** | Memory Blocks、跨会话身份、后台 sleeptime、消息搜索、Git 跟踪的 MemFS。可参考 M08、M13–M14。 | 模型能主动改写记忆、提示和技能，与你“模型提案、服务事务提交”的边界不同。[Letta 原仓库](https://github.com/letta-ai/letta)已说明活跃代码迁至此处，旧 V1 服务端保存在 [archive 分支](https://github.com/letta-ai/letta/tree/archive)。 |
| **[OpenAlma](https://github.com/mekineer-com/OpenAlma)**／GPL-3.0 | **高：生活陪伴产品方向** | 跨聊天、语音与视觉的陪伴记忆；经历、人物档案、后台整合；允许查看与纠正记忆，支持实体图和时间查询。 | **产品体验值得参考。** 当前为 Beta；自动抽取默认等待夜间睡眠间隔及累计输入阈值，与“数秒结构化、下一轮纠正生效”的要求差距明显。 |
| **[MemoryBank / SiliconFriend](https://github.com/zhongwanjun/MemoryBank-SiliconFriend)**／MIT | **高：陪伴记忆研究思路** | 长期陪伴、历史互动总结、用户特征、按重要性与时间进行遗忘或强化。接近 M07、M09、M14。 | 更适合作为研究基线。其遗忘与强化机制需要按你的规则调整，尤其是**机器自我复述不算用户确认、降权不等于删除**。 |
| **[A-MEM](https://github.com/agiresearch/A-mem)**／MIT | **中：关联组织与记忆演化** | 将记忆组织成带属性、标签和链接的笔记；新记忆可推动旧记忆关联与演化。可参考 M02、M07–M08、M12。 | 使用 ChromaDB，偏算法组件。自动演化过程需要接入你的不可变修订、来源依赖和事实核验，才能用于长期生活事实。 |
| **[EverOS](https://github.com/EverMind-AI/EverOS)**／Apache-2.0 | **中高：可携带与可查看的记忆** | 用户经历与画像分开；后台反思；带来源的知识页；Markdown 作为事实源，SQLite＋LanceDB 为检索索引。可参考 M07–M08、M25。 | 原 **EverMemOS 仓库入口目前重定向至 EverOS**。当前侧重本地、文件化和可携带；与你基于 PostgreSQL 的双时间事务模型有较大结构差异。 |
| **[Open-LLM-VTuber](https://github.com/Open-LLM-VTuber/Open-LLM-VTuber)**／代码 MIT，示例模型另有许可 | **交互高、记忆低：语音数字人** | 实时语音、打断、Live2D 表情、角色配置及聊天记录持久化。适合参考第 15 节的交互体验。 | README 明确写明**长期记忆功能暂时移除**。可作为语音与角色表现参照，目前不能作为记忆引擎候选。 |

## 对当前方案的建议

- **工程验证先选 Hindsight。** 技术栈最接近，适合用同一批中文生活对话，比较召回质量、处理新鲜度和普通路径 P95，而后决定复用多少。
- **事实模型重点读 Graphiti 和 memharness。** 前者覆盖实体、时间、关系与来源，后者更容易看清纠正、历史认知和审计接口如何落地。
- **画像与体验重点读 Memobase、OpenAlma、MemoryBank。** 可以参考如何组织人物、经历、偏好和回顾，但事实提交仍应遵守你自己的授权与证据规则。

本轮**未找到公开证据足以确认完整覆盖你全部 M01–M26 的项目**。尤其是删除后旧任务及备份防复活、证据约束的多年压缩、归档迁移恢复，以及“实际播放完成才形成共同经历”，仍需逐项核验或自行实现。你文档中的十年保障目前也是待验收目标，双方应放到同一套测试下比较。

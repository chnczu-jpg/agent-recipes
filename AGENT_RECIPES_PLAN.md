# Agent Recipes System Plan

本文是 Agent 菜谱系统的落地版方案。它是通用方案，不是 SampleProject 专用方案。SampleProject 只作为第一个真实压力测试项目，不能把 SampleProject 规则写进通用系统默认逻辑。

## 总纲

一句话：

Agent Recipes 把纠偏、课程、资料和跑通过的经验，变成 agent 下次能安全、可验证复用的菜谱。

大白话：菜谱系统不是文档库，也不是一个超大 Skill。它是把以前踩过的坑、学过的课、跑通过的做法，变成下次 agent 能用、敢不用、用前能锁、用后能验的规则。

所有复杂模块都必须服务这句话。模块如果不能帮助 agent 更安全地复用经验、更少乱套、更少重复犯错，就不应该抢主位。

核心能力：

- 找到相关经验和证据。
- 判断当前任务能不能用这条经验。
- 不适合时敢返回 no-match / no_applicable_recipe。
- 资料、课程、纠偏和产物经验先生成 candidate，不直接变正式规则。
- candidate 必须经过 review_queue，accept 后才变 formal recipe。
- 高风险执行前必须 lookup 严格命中并 lock。
- 执行后要 capture，并用 benchmark 验证有没有少犯旧错。
- 跨项目复用必须带适用边界，不能把 SampleProject 或任何单项目规则写成全局默认。

支撑工具层：

- source_refinery 负责把资料切成能进菜谱字段的零件。
- knowledge_fusion 负责处理相似、冲突、不完整和多来源互证。
- CLI / MCP / plugin 只是调用和安装方式，不是核心真相。
- Cognee / Graphiti / embedding / search 只是候选召回和证据辅助，不是正式菜谱裁判。
- benchmark 负责证明“少犯错、少乱套、有边界”，不负责粉饰成质量已通过。

硬边界：

- 跑通过一次不等于永远正确。
- 检索到了不等于能用。
- candidate 不是 formal recipe。
- lock 不是口头提醒，而是执行前合同。
- benchmark 通过不等于真实产物质量、用户认可或完整工作流通过。
- Codex 在真实测试里只能当裁判，不能替菜谱系统读资料、挑重点、手写规则。

## 这版补了什么

原方案方向是对的，但 Phase 0 太大，容易变成很多命令都有壳，背后没有可信闭环。

这版把第一版目标改成可信内核：

```text
用户纠偏/失败/成功
  -> append-only event
  -> candidate recipe patch
  -> review queue
  -> 用户确认
  -> recipe version
  -> agent lock
  -> 新会话执行前 lookup
  -> 执行后 capture
```

第一版先证明一件事：fresh agent 没菜谱会重复犯旧错，有菜谱后不再犯同一个错。

## 系统边界

```text
agent-recipes core
  真正的业务逻辑：schema、事件日志、状态机、索引、doctor、review、lock。

agent-recipes CLI
  本地测试、人工复现、CI、兜底入口。agent 也可以通过 shell 调。

agent-recipes MCP server
  agent 最舒服的工具接口。Codex、Claude、Hermes 优先走 MCP。

Codex / Claude / Hermes plugin
  安装和集成包。负责装 Skill、注册 MCP、带模板和说明，不承载核心逻辑。

.recipes/
  项目自己的菜谱库、证据、失败、成功、锁、review_queue。
```

核心逻辑不能依赖某个 agent、某个插件或某个 MCP 客户端。插件只是安装方式，MCP 是 agent 调用方式，CLI 是本地兜底方式，`.recipes/` 是项目真相。

换句话说，系统边界也服从总纲：core 负责“经验能不能安全复用”的判断和状态；CLI、MCP、插件只负责把这个判断稳定交给 agent 使用。不能让接入方式反过来决定菜谱真相。

## 为什么不是只做 Skill

只做 Skill 会有几个问题：

- 每个 agent 自己理解规则，执行不稳定。
- 状态容易散在聊天里。
- 失败记录不容易幂等。
- evidence 和 cannot_claim 容易丢。
- Codex、Claude、Hermes 很难共享同一套项目经验。

所以 Skill 必须薄。它只告诉 agent：

```text
1. 当前项目有没有 .recipes/
2. 先调用 lookup 或读 START_HERE.md
3. 执行前 lock
4. 成功/失败/纠偏后 capture
5. 有争议进 review_queue
6. 三次同类失败后 recover 只生成候选补丁
```

真正读写和判断交给 core、CLI、MCP。

## 为什么保留 CLI

CLI 不是主要给人天天手敲的。它是稳定底座。

CLI 负责：

- 本地直接跑。
- 测试和 CI 能跑。
- 人能复现 agent 做了什么。
- MCP 坏了时还能兜底。
- 每个命令都能输出 `--json`，方便 agent 读。

CLI 和 MCP 都只能调用同一个 core API。不能出现 CLI 一套逻辑、MCP 一套逻辑。MCP 可以在实现上调用 CLI 进程做兜底，但行为合同必须由 core 定义，测试也必须证明三者输出一致。

## MCP 工具设计

MCP 第一版提供这些工具：

```text
agent_recipes_doctor
agent_recipes_readiness
agent_recipes_outcome_status
agent_recipes_lookup
agent_recipes_lock
agent_recipes_recipe_lifecycle
agent_recipes_capture
agent_recipes_capabilities
agent_recipes_search
agent_recipes_refine
agent_recipes_extract_cards
agent_recipes_patch_draft
agent_recipes_knowledge_fusion
agent_recipes_review_decide
agent_recipes_convert_doc
agent_recipes_detect_scenes
agent_recipes_transcribe
agent_recipes_ocr_image
agent_recipes_memory_index
agent_recipes_memory_search
agent_recipes_memory_status
agent_recipes_recall_boundary
agent_recipes_evidence_quarantine
agent_recipes_evidence_pack
agent_recipes_memory_native_probe
agent_recipes_memory_semantic_probe
agent_recipes_memory_semantic_configure
agent_recipes_quality_benchmark
agent_recipes_lookup_pressure
agent_recipes_lock_pressure
agent_recipes_consumption_coverage
agent_recipes_real_pressure_summary
agent_recipes_candidate_quality_benchmark
agent_recipes_completeness_audit
agent_recipes_review_triage
agent_recipes_review_packet
agent_recipes_self_run_benchmark
agent_recipes_repeat_error_benchmark
```

`review_decide` 第一版只覆盖已实现的 core gate：普通 source_refinery 的 `accept/reject/split/supersede`，以及 knowledge_fusion 的 `merge/split/supersede`。后续 roadmap 再补完整 `review_queue` 列表、`recover`、`sources` 等 MCP 工具；在未实现前不能 claim MCP 覆盖完整 CLI。

MCP 第一版提供这些资源：

```text
recipes://start_here
recipes://current
recipes://locks
recipes://review_queue
recipes://doctor_report
recipes://evidence
recipes://source_index
recipes://source_refinery
recipes://cards
recipes://patch_drafts
```

MCP 的规则：

- 可用时，Skill 优先调用 MCP。
- MCP 不可用时，Skill fallback 到 `agent-recipes` CLI。
- MCP 工具返回必须包含 `claim_status`，不能只返回成功文本。
- 写入动作必须返回写了哪些文件、写入前后 id/hash、怎么回滚。

## 插件定位

插件是安装包，不是大脑。

Codex 插件第一版可以做：

- 安装薄 Skill。
- 注册 MCP server。
- 带上 CLI 或指向 CLI 安装位置。
- 带上 schema、模板和示例。
- 提供 `agent-recipes doctor` 入口。

插件不能做：

- 把项目经验存在插件目录。
- 把 SampleProject 规则写成全局默认。
- 让正式菜谱绕过 review。
- 把 core 逻辑只写在插件里。

## 项目目录

每个项目生成一个 `.recipes/`：

```text
.recipes/
  START_HERE.md
  PROJECT_PROFILE.md
  KNOWLEDGE_MAP.md
  events.jsonl
  sources.yaml
  recipes/
  candidates/
  review_queue/
  corrections/
  failures/
  successes/
  evidence/
  video_index/
  source_index/
  memory/
    cognee/
      index.jsonl
      status.json
      runtime_probe.json
      native_probe.json
      runtime/
  source_refinery/
    chunks/
    cards/
      correction_cards/
      run_chain_cards/
      failure_cards/
      learning_atom_cards/
      visual_example_cards/
    patch_drafts/
    normalized/
      markdown/
      transcripts/
      keyframes/
      ocr/
  locks/
  reports/
```

关键变化：`events.jsonl` 是主账本。`corrections/`、`failures/`、`successes/`、`recipes/`、`reports/` 都是从事件和证据生成或引用出来的视图。

## 资料速炼台 / Source Refinery

`source_refinery/` 是资料到菜谱之间的中间机器。

它不是资料库，不是总结库，也不是当前执行入口。它只做一件事：

```text
各种资料
  -> 带来源短片段
  -> 可进菜谱字段的卡片
  -> patch draft
  -> review_queue
  -> accepted recipe version
```

硬规则：

- 工具负责快：转换、切块、转写、抽帧、OCR、索引、候选抽取。
- schema 负责不降质量：来源、上下文、证据强度、字段归属、cannot_claim。
- review 负责入库门禁：卡片和 patch draft 不能直接改正式 recipe。
- 进不了 recipe 字段的资料，只能 `archive_index_only`，不能写成长总结。
- 通用系统不能写死 SampleProject 的 K001-K008。通用字段叫 `knowledge_need_id`，SampleProject 只能作为 fixture 或项目私有配置。

第一版只允许五类卡：

```text
correction_card       用户纠偏和纠偏前后文
run_chain_card        已跑通、可复现的链路
failure_card          失败路线、失败信号和禁止再试原因
learning_atom_card    课程/文档里能改变动作的原子规则
visual_example_card   accepted/rejected/missing_evidence 视觉样板
```

卡片只能映射到这些 recipe 字段：

```text
verified_path
forbidden_path
failure_signal
fallback_allowed
fallback_forbidden
prompt_rule
checklist_item
good_example
bad_example
visual_check
cannot_claim
pressure_test
source_trace
```

大白话：不要让 Codex “读完资料写总结”。让它只回答：这段资料能不能填一个菜谱字段。能，就做卡和 patch draft；不能，只留索引。

## 知识融合层 / Knowledge Fusion

`source_refinery` 负责把资料切成卡片；`knowledge_fusion` 负责决定这些卡片应该合并、分叉、降级、打回，还是进入 `review_queue`。

## 技能完整性 / 课程完整性

`completeness-audit` 只回答“当前材料够不够进入复核或真实执行”，不能回答“内容一定正确”或“agent 已经学会”。评分分两层：

- 通用结构：适用边界、输入前提、详细步骤、验收方法、失败退路、来源证据、cannot_claim。
- 技能特有 requirements：例如“大字在人后”必须明确底层原片、中层大字、上层抠像人物，并要求真实剪辑时间线证据。

硬规则：

- 技能缺适用边界、步骤、验收、来源或 cannot_claim，分数再高也判 `incomplete`。
- 课程没有 `coverage_complete=true` 或完整覆盖证据，判 `needs_deep_read`，不能把浅读说成课程完整。
- 没有 requirements 时，只能说通用结构完整，不能说该技能特有动作已完整。
- `complete_for_review` 只代表可以进入人工或真实执行复核，不代表内容正确、已掌握、真实软件跑通或产物质量通过。
- 技能特有动作不能只靠关键词总量通过；需要在同一字段里检查动作顺序/对应关系，例如“底层 -> 原片、中层 -> 大字、上层 -> 抠像人物”。
- 报告同时给结构分、技能/课程专属门槛分和综合分；任何硬门槛失败都必须 fail-closed，即使数字分数看起来不低也不能进入 `complete_for_review`。
- 软件技能不能把三句原理当成三步操作。每一步必须是结构化对象，至少包含：顺序、动作、`function_id`、做完后应看到的界面状态、逐步验证、课程时间码或页码。
- 软件技能必须绑定 `software function map`。地图中的每个功能必须说明：它有什么用、什么时候用、会改变什么、做对后看到什么、失败信号和退路。只写“智能抠像”“复合片段”这些名字不算会用。
- 课程整体覆盖和单个技能片段覆盖必须分开。读完“大字在人后”片段，不能反推第八课或整套课程已经拆完。
- `course-skill-draft` 只把指定课程时间码和软件功能地图组合成逐步 candidate；它不能直接写正式 recipe。
- `complete_for_review` 后还要过执行层：fresh agent、干净项目/时间线、只按 candidate、第一次执行、保留原始证据。缺任何一项都只能标 `needs_fresh_execution`；多次临场试错不能反写成“一次跑通”。

课程转技能的本地 v0 命令：

```text
agent-recipes course-skill-draft \
  --transcript <带时间码课程文字稿> \
  --spec <技能片段范围与功能列表> \
  --software-map <软件功能地图>
```

输出只进入 `.recipes/candidates/course_skill_drafts/`。后续仍要经过完整性审计、fresh-agent 盲测和 review queue，才有资格转成正式菜谱。

它解决的问题不是“有没有抽到资料”，而是：

```text
信息不全怎么办？
多套课程讲相似东西怎么办？
同一个词在不同课程里用法不同怎么办？
纠偏大体正确但局部有错怎么办？
课程知识、纠偏、真实产物、失败记录谁更可信？
```

第一版先做规则层和 review gate，不做自动真理裁判。任何融合结论仍然是 candidate，必须进入 `review_queue`。

### 知识融合的基本原则

- 不完整资料只能产生候选，不能产生正式结论。
- 多源一致不是投票通过；必须看使用场景、目标动作、限制条件是否一致。
- 多源冲突不能静默合并；必须拆分、标冲突，或进入 review_queue。
- 同名知识不等于同一个菜谱；同名不同用法必须拆成父子菜谱或并列菜谱。
- 纠偏优先级高，但纠偏不是永远正确；必须保留当时场景、触发原因和适用边界。
- 课程/教程只能提供候选动作规则；不能证明项目已经会做，也不能替代真实产物验证。
- 跑通产物能提高证据强度，但只证明那条链路、那种输入、那次验收范围内成立。
- 人工 review 的工作不是重写总结，而是 accept / reject / supersede / split / merge。
- 多来源大包不能静默降级成普通归档。如果一组 cards 数量多、来源多，但还不能安全合并/拆分/判冲突，`knowledge-fusion` 必须生成 `needs_deep_read` 候选，提示下一步定向深读；不能假装已经吸收，也不能只说“先存着”。
- `needs_deep_read` 不能停在一句“去深读”。系统必须能生成 `deep-read-plan`，把 fusion candidate 的 `source_trace`、`source_card_ids`、`source_path_contains` 和下一轮 `self-run-benchmark` 参数整理出来。深读计划必须继承原卡片的 `target_fields`，否则会把真实可用字段冲成空补丁。它只生成候选计划，不能直接执行、不能接受 review、不能写正式 recipe。
- 多来源 `needs_deep_read` 不能再生成一个大而泛的深读任务。只要来源已经明显跨文件，`deep-read-plan` 必须按 source path 拆成多个单来源任务，让菜谱逐个小范围自跑，再用 candidate-quality 比较哪些能收、哪些太薄、哪些要拒绝。
- 对照压测里的同一个 `target_recipe_id` 可能分多轮 self-run 产生 cards。`knowledge-fusion` 必须读取该 target 下全部 candidate cards，而不是只看 `latest.json` 最近一轮，否则“产物侧先跑、课程侧后跑”会退化成只融合课程侧。
- 单卡 `needs_deep_read` 不能挡住全体宽集合拆读。如果同一个 target 同时已经跨很多 cards 和很多 source，系统必须额外生成一个全体宽集合 `needs_deep_read`，再交给 `deep-read-plan` 按 source path 拆开。

### 知识融合 review 决策协议

`knowledge-fusion` 只生成候选，不直接写正式 recipe。进入 `review_queue` 后，必须明确选择一种处理方式：

```text
agent-recipes review --merge <review_id>
agent-recipes review --split <review_id>
agent-recipes review --supersede <review_id> --lock <lock_id>
agent-recipes review --reject <review_id>
```

MCP 对应工具：

```text
agent_recipes_review_decide
```

规则：

- 普通 `review --accept` 不能接受 knowledge_fusion 候选；必须 fail-closed。
- `merge` 只把多来源互相印证的候选收进目标 recipe；如果目标 recipe 已存在，必须先 lock。
- `split` 为同名不同用法创建子 recipe，不改写父 recipe。
- `supersede` 创建替代 recipe，不删除旧 recipe；必须先 lock 旧 recipe。
- `needs_deep_read`、`conflict_candidate` 只能进入 `source_truth_to_read`、`open_questions` 和 `cannot_claim`，不能被硬塞成已验证步骤。
- 所有结果仍然必须保留 `source_trace`、`source_fusion_id`、`fusion_candidate_ids` 和 claim limits。

### 普通 source_refinery review 决策协议

`source_refinery` 生成的普通 patch draft 也不能只靠 accept/reject 两种选择。进入 `review_queue` 后，人工 review 必须能做四类决定：

```text
agent-recipes review --accept <review_id>
agent-recipes review --reject <review_id>
agent-recipes review --split <review_id>
agent-recipes review --supersede <review_id> --lock <lock_id>
```

规则：

- `accept` 只适合范围清楚、大小正常、没有 split 建议的候选。初次晋升可以不带 lock；更新已有正式 recipe 必须带 active lock。
- `split_before_accept` 的候选不能直接 accept，必须 fail-closed。否则会把太多规则混成一个大菜谱。
- `split` 为普通 source_refinery 候选创建子 recipe，不写父 recipe，可以不带 lock，但必须写 `lock_exempt_reason=source_refinery_split_new_recipes`。
- `supersede` 为普通 source_refinery 候选创建替代 recipe，不覆盖旧 recipe；必须先 lock 旧 recipe。
- `reject` 不写正式 recipe，只保留拒绝理由和候选历史。
- 已经 `rejected`、`split`、`superseded` 或其他最终状态的 review，不能再被 `accept` 改写历史；必须重新生成候选或走 supersede/recover。
- `agent_recipes_review_decide` 必须同样覆盖普通 source_refinery 的 `split/supersede`，不能只覆盖 knowledge_fusion。

### 信息不全规则

浅读、关键帧、小范围时间轴、局部 OCR、局部转写只能用于发现候选，不允许直接形成完整 recipe。

信息不全时必须写：

```text
source_trace
evidence_strength: partial 或 candidate
missing_evidence
cannot_claim
next_deep_read_target
```

处理方式：

- 能填字段但证据不足：生成 card，标 `missing_evidence`，进入 patch draft 的低置信区。
- 只能看出主题，不能看出动作：`archive_index_only`，不写 recipe 字段。
- 看出可能重要但上下文不足：生成 `needs_deep_read` 建议，指向具体时间段、页码、文件段落或关键帧附近范围。
- 视觉/声音类资料缺少最终样片、人耳听感或画面检查时，不能 claim 质量通过。

大白话：浅读只能说“这里可能有料”，不能说“这门课已经吸收了”。

### 多源互证规则

多套资料讲相似内容时，系统先做“相似候选聚类”，再判断能不能合并。

允许合并成同一条规则，必须同时满足：

```text
目标相同：解决的是同一个问题
动作相同：要求 agent 做的是同一类动作
场景相同或兼容：适用边界没有冲突
风险相同或兼容：不能 claim、失败信号没有互相打架
至少一条来源能给出可执行检查项
```

不能合并时，必须分叉：

- 同目标不同动作：拆成并列 recipe。
- 同动作不同场景：同一 recipe 里拆 `use_when / do_not_use_when`。
- 同词不同含义：建父概念 recipe + 子 recipe。
- 观点相似但证据都弱：只保留 `learning_atom_card`，不升 patch draft。

多源互证提升的不是“真理等级”，而是“候选值得 review 的优先级”。正式生效仍然靠 review。

### 多源冲突规则

冲突包括：

```text
A 说要做，B 说不能做
A 在课程里成立，真实产物里失败
用户纠偏和旧 recipe 冲突
同一字段里出现互斥动作
同名知识被不同资料用于不同目标
```

冲突处理顺序：

1. 先保留全部 source_trace，不删除任何一边。
2. 标记 `conflict_set_id`，说明冲突点是什么。
3. 如果能用场景区分，拆 `use_when / do_not_use_when`。
4. 如果不能区分，进入 review_queue，不能自动写正式 recipe。
5. 如果已有正式 recipe 被新证据挑战，只能生成 supersede candidate，不能直接覆盖。

冲突时禁止：

- 靠数量投票。
- 靠最新资料覆盖旧资料。
- 靠模型“感觉更合理”合并。
- 为了让菜谱更短，把例外删掉。

### 父子菜谱规则

很多知识有共同底层逻辑，但具体用法不同。不能把所有相似知识揉成一个大菜谱。

结构应该是：

```text
父菜谱：共同原则、通用风险、通用检查项
子菜谱：具体场景、具体动作、具体验收
```

例子：

```text
父菜谱：关键帧控制变化
  子菜谱：快进快出，用于强调、冲击、节奏点
  子菜谱：影视级慢放，用于情绪、质感、沉浸
  子菜谱：花字弹入弹出，用于文字注意力控制
```

父菜谱不能直接指挥执行复杂任务；执行前必须命中子菜谱或明确组合多个子菜谱。

### 同名不同用法规则

同一个词不能自动合并。

例如“关键帧”可能是：

- 速度节奏控制。
- 慢动作质感控制。
- 字幕/花字入场。
- 画面推拉。
- 音量 ducking。
- 转场参数。

系统必须先判断：

```text
控制对象是什么？
目标效果是什么？
观众感知是什么？
输入材料是什么？
输出检查是什么？
失败信号是什么？
```

判断不清时，不生成通用 recipe，只生成候选卡和 `needs_clarification`。

### 纠偏可信度规则

用户纠偏是高优先级输入，但不能无脑写成绝对规则。

纠偏入库前必须保留：

```text
wrong_behavior
correct_behavior
check
applies_to
source_trace
trigger_context
cannot_claim
```

纠偏升级规则：

- 只出现一次，但错误严重：可进 candidate forbidden/checklist。
- 重复出现三次以上：优先生成 pressure_test。
- 被真实产物验证过：证据强度提高，但仍受场景限制。
- 与用户当前明确指令冲突：当前指令优先，旧纠偏进入 review。
- 与正式 recipe 冲突：生成 supersede candidate，不能直接覆盖。

纠偏不能写成“永远禁止”，除非它本来就是系统边界，例如不能绕过 review_queue、不能泄露 secret、不能无证据 claim。

### 课程资料权重规则

课程资料的默认身份是 reference，不是 project truth。

课程可以贡献：

- 动作原理。
- 术语解释。
- 常见做法。
- 视觉/声音判断维度。
- 可迁移的检查项。

课程不能单独证明：

- 项目已经具备该能力。
- 某条真实任务已经完成。
- 某个产物质量已经通过。
- 课程做法适用于所有项目。

课程规则要升级，至少要满足一个：

- 能转成明确 checklist。
- 能解释已有失败。
- 能改善 fresh agent 的计划。
- 能在真实产物或小样里被验证。
- 能和其他来源形成清楚的同场景互证。

### 产物经验权重规则

真实产物经验比课程更接近 project truth，但也不能无限外推。

产物经验必须记录：

```text
input_scope
commands_or_steps
artifact_paths
review_method
success_means
failure_signals
cannot_claim
reuse_conditions
```

只能复用到相似输入、相似目标、相似工具链。换项目、换素材、换审美目标、换工具时，必须降级为 candidate。

### 融合输出类型

知识融合层只能输出这些东西：

```text
merge_candidate       建议合并
split_candidate       建议拆分
supersede_candidate   建议替换旧 recipe
compose_candidate     建议多个 recipe 组合使用
conflict_candidate    有冲突，需 review
needs_deep_read       信息不全，需要定向深读
archive_index_only    不进 recipe，只留索引
```

它不能直接输出正式 recipe。

### 压力测试生成规则

每条融合后的重要规则，都必须能生成压力测试题。

压力测试至少覆盖：

- 信息不全时，会不会硬总结。
- 多课程相似时，会不会乱合并。
- 同名不同用法时，会不会误用。
- 纠偏过度时，会不会写成绝对禁止。
- 课程资料没有真实产物时，会不会 claim 已掌握。
- 父菜谱命中但子菜谱不明时，会不会直接执行。
- 旧 recipe 和新资料冲突时，会不会静默覆盖。

压力测试没过，不能 accept 为正式 recipe；只能继续留在 review_queue 或拆回 card。

## 真实测试自跑原则

真实测试不是让 Codex 人工替菜谱系统干活。

如果测试时由 Codex 先读完资料、人工挑重点、人工总结规则、人工写成菜谱，再说“菜谱系统有用”，这是作弊。这样只能证明 Codex 会总结，不能证明 Agent Recipes 有价值。

真实测试必须让 Agent Recipes 自己跑完整链路：

```text
给定资料入口、范围和验收标准
  -> 系统 scan / search / refine
  -> 系统抽出 correction_card / run_chain_card / failure_card / learning_atom_card / visual_example_card
  -> 系统保留 source_trace、evidence_strength、cannot_claim
  -> 系统生成 patch draft
  -> 系统放进 review_queue
  -> 人只做评审：accept / reject / supersede
```

Codex 在真实测试里的角色是评卷人，不是代工厨师。

Codex 可以做：

- 指定测试范围，例如“SampleProject 当前纠偏卡 + 历史 capture + run receipt”。
- 注册只读 source。
- 运行 Agent Recipes 命令链。
- 检查系统产物有没有用、有没有证据、有没有乱 claim。
- 把无用、重复、冲突、太虚、太像总结的候选打回。
- 记录通过率、失败点、速度和质量问题。

Codex 不能做：

- 人工读完资料后直接替系统写菜谱。
- 把人工总结包装成系统自动抽取成果。
- 为了让结果好看，绕过 card / patch draft / review_queue。
- 把 SampleProject 候选规则直接拿去指挥 SampleProject 干活。
- 把一次小样本通过说成菜谱库已经建成。

真实测试必须有对比：

```text
同一个小任务或小场景：
  A. 不查菜谱，fresh agent 会怎么计划？
  B. 查菜谱，fresh agent 会怎么计划？
  C. 对比是否少犯旧错、是否更快定位证据、是否更少乱 claim。
```

第一批真实测试优先级：

1. 纠偏：看系统能不能把旧错误变成防蠢规则。
2. 跑通链路：看系统能不能复用真实成功路径。
3. 产物经验：看系统能不能提炼质量锚点和验收边界。
4. 课程/资料：看系统能不能拆成行动规则，而不是写长总结。

SampleProject 是第一个真实压力测试项目，但 SampleProject 规则只能作为测试数据、项目私有 source 或候选 recipe，不能写进通用默认逻辑。

## 核心对象

第一版必须有这些 schema。

```text
ProjectProfile
  project_id
  name
  purpose
  current_goal
  agent_policy
  created_at
  updated_at

SourceRecord
  source_id
  path
  source_type
  permission
  allow_cloud
  allow_transcript
  expires_at
  hash
  status

EvidenceRef
  evidence_id
  source_id
  path
  line_start
  line_end
  timestamp_start
  timestamp_end
  quote_or_summary
  hash
  claim_limit

CaptureEvent
  event_id
  event_type
  schema_version
  seq
  task
  actor
  session_id
  lock_id
  causation_id
  idempotency_key
  payload_hash
  prev_event_hash
  user_words
  agent_action
  result
  evidence_refs
  created_at

FailureRecord
  failure_id
  problem_fingerprint
  task
  failed_path
  expected_path
  failure_signals
  evidence_refs
  count
  status

CandidateRecipePatch
  patch_id
  source_event_ids
  target_recipe_id
  proposed_change
  reason
  evidence_refs
  risk
  status

SourceChunk
  chunk_id
  source_id
  source_type
  path
  line_start
  line_end
  timestamp_start
  timestamp_end
  page
  heading
  text
  hash
  knowledge_need_id
  target_recipe_id
  candidate_fields
  privacy_class
  evidence_strength
  status

RefineryCard
  card_id
  card_type
  source_chunk_ids
  source_trace
  knowledge_need_id
  target_recipe_id
  target_fields
  evidence_strength
  extracted_payload
  cannot_claim
  status

CorrectionCard
  card_id
  before
  correction
  after
  why_wrong
  target_recipe_id
  target_fields
  first_read_required
  evidence_refs
  cannot_claim

RunChainCard
  card_id
  inputs
  tool_entrypoint
  replay_steps
  outputs
  verification
  known_good_evidence
  target_recipe_id
  target_fields
  cannot_claim

FailureCard
  card_id
  failed_path
  failure_result
  failure_signals
  why_forbidden
  replacement_path
  target_recipe_id
  target_fields
  cannot_claim

LearningAtomCard
  card_id
  source_summary
  action_change
  good_example
  bad_example
  checklist_item
  target_recipe_id
  target_fields
  needs_user_review
  cannot_claim

VisualExampleCard
  card_id
  image_or_frame_path
  status
  what_to_check
  wrong_signal
  correct_rule
  target_recipe_id
  target_fields
  needs_user_review
  cannot_claim

RecipePatchDraft
  patch_draft_id
  target_recipe_id
  target_fields
  source_card_ids
  proposed_additions
  reason
  evidence_strength
  needs_user_review
  cannot_claim
  status

ReviewItem
  review_id
  blocking_level
  question
  why_user_must_decide
  options
  recommendation
  evidence_refs
  proposed_patch_id
  status
  decided_by
  decided_at

Recipe
  recipe_id
  version
  title
  scope
  use_when
  do_not_use_when
  inputs_required
  outputs_expected
  source_truth_to_read
  verified_path
  forbidden_path
  steps
  failure_signals
  stop_line
  verification
  success_means
  failure_means
  cannot_claim
  rollback
  evidence_refs
  related_events
  open_questions

LockRecord
  lock_id
  task
  owner_agent
  session_id
  recipe_ids
  recipe_versions
  recipe_hashes
  allowed_actions
  forbidden_actions
  claim_limits
  verification_required
  expires_at
  status

DoctorReport
  report_id
  checked_at
  status
  errors
  warnings
  claim_status
  next_actions
```

所有对象都要有稳定 id、version 或 hash。重跑命令不能重复生成垃圾。

## Event Log Protocol

`events.jsonl` 是主账本。所有会改变系统状态的动作，都必须先落成 event，再由 event 生成 recipe、candidate、review item、report 这些视图。

每条 event 必须包含：

```text
event_id
event_type
schema_version
created_at
actor
session_id
lock_id
causation_id
idempotency_key
payload_hash
prev_event_hash
seq
payload
claim_status
```

字段含义：

- `seq`：项目内单调递增序号，用来 replay。
- `prev_event_hash`：上一条 event 的 hash，用来发现链断了或并发写坏了。
- `payload_hash`：本条 payload 的 hash，用来判断同一个 idempotency key 是否被不同内容复用。
- `idempotency_key`：同一个写操作重跑时必须复用，避免重复生成垃圾。
- `actor`：触发事件的身份，可以是 user、codex、claude、hermes 或 system。
- `session_id`：本次 agent 会话 id，用来串联同一执行链，排查并发、重放和责任归属。
- `causation_id`：说明这条 event 是由哪条 event、review item 或 command 触发。
- `lock_id`：有 active lock 的写动作必须引用 lock。没有 lock 的写动作必须写 `lock_exempt_reason`。

写入规则：

```text
同 idempotency_key + 同 payload_hash
  -> replayed，不新增 event

同 idempotency_key + 不同 payload_hash
  -> conflict，停止写入

prev_event_hash 不匹配
  -> conflict，提示重新读取最新 events 后再试

崩溃后恢复
  -> replay events.jsonl，重建 derived views
```

`events.jsonl` 写入必须用项目本地文件锁保护整段“读取旧事件 -> 检查幂等 -> 计算 seq/hash -> append”。不能只依赖 append 原子性；否则两个 agent/CLI/MCP 同时写时会出现重复 seq、重复 event 或 prev_hash 断链。doctor 必须继续检查并发写坏的证据。

## 状态机

```text
capture_event
  |
  +-- correction_record
  +-- failure_record
  +-- success_record
        |
        v
candidate_recipe_patch
        |
        v
review_queue item
        |
        +-- accepted -> recipe version N+1
        +-- rejected -> patch rejected with reason
        +-- superseded -> linked to newer patch
```

正式菜谱只能从 accepted review item 生成。recover 不能直接写正式菜谱。

## 三轮失败机制

三轮失败不是让 agent 再试三次。它是刹车机制。

```text
第 1 次同类失败
  -> capture failure
  -> 记录 problem_fingerprint

第 2 次同类失败
  -> 提醒已有相似失败
  -> 输出已有 forbidden_path 和 stop_line

第 3 次同类失败
  -> 强制 recover
  -> 生成 candidate_recipe_patch
  -> 进入 review_queue
  -> 等用户或授权主控确认
```

同类问题用 `problem_fingerprint` 判定，不能只靠自由文本。

`recover` 第一版只做：

- 汇总三次失败。
- 找相关 recipe、failure、correction、evidence。
- 生成候选补丁。
- 生成 review item。

`recover` 第一版不做：

- 自动写正式菜谱。
- 自动覆盖旧规则。
- 自动解除 forbidden_path。
- 自动 claim 已解决。

## Review Queue

`review_queue/` 不是一堆 markdown。它是审批协议。

每条 review item 必须让用户 2 分钟内看懂：

```text
问题是什么
为什么必须你决定
证据在哪里
推荐选项是什么
不选会怎样
选了会改哪条 recipe
批准后的 diff 是什么
拒绝后怎么处理
```

blocking level：

```text
P0  必须用户审。会改变正式菜谱、claim 边界、禁路、权限或高风险动作。
P1  建议用户审。agent 可以先给推荐，但不能静默写正式菜谱。
P2  agent 可自动处理，但必须留记录，doctor 可查。
```

## Lock

`lock` 不是一个“我看过菜谱”的标记。它是执行前合同。

一次 lock 要锁住：

- 任务描述。
- recipe id。
- recipe version/hash。
- evidence refs。
- 允许动作。
- 禁止动作。
- claim limits。
- 验收标准。
- 过期时间。
- owner agent 和 session。

执行中发现新证据，不能静默改当前 lock。只能：

- 生成新 capture event；
- 生成 candidate patch；
- 或创建新 lock。

## Lock Enforcement

lock 不是建议，是写入门槛。

这些命令第一版必须要求 active lock：

```text
agent-recipes capture --type success|failure
agent-recipes recover
agent-recipes review --accept  # 修改已有正式 recipe 时
agent-recipes review --merge   # 目标 recipe 已存在时
agent-recipes review --supersede
```

例外：

- 第一次把 candidate recipe patch 晋升为正式 recipe version 时，还没有可 lock 的正式 recipe。这个初次 `review --accept` 可以不带 lock，但 mutation event 必须写 `lock_exempt_reason=initial_recipe_promotion`。
- 第一次把 knowledge_fusion `merge_candidate` 晋升为目标 recipe 时可以不带 lock，但 mutation event 必须写 `lock_exempt_reason=initial_fusion_recipe_promotion`。
- `review --split` 只创建新的子 recipe，不改写父 recipe，可以不带 lock；knowledge_fusion split 的 mutation event 必须写 `lock_exempt_reason=knowledge_fusion_split_new_recipes`，source_refinery split 必须写 `lock_exempt_reason=source_refinery_split_new_recipes`。
- `review --reject` 不写正式 recipe，可以不带 lock，但必须保留 review 和候选历史。

后续修改已有正式 recipe 时，`review --accept` 和 `review --merge` 必须带 active lock；`review --supersede` 必须带 active lock。

这些命令可以没有 lock，但必须写清原因：

```text
agent-recipes init
agent-recipes sources add
agent-recipes scan
agent-recipes compile
agent-recipes doctor
```

写命令规则：

```text
有 active lock:
  mutation event 必须引用 lock_id。

无 active lock:
  mutation event 必须写 lock_exempt_reason。

recipe hash 与 lock 里的 hash 不一致:
  fail closed，停止写入，要求重新 lookup + lock。

lock 过期:
  写命令停止，doctor 报 stale lock。

capture 绑定 lock_id:
  capture 记录的是“按哪份菜谱、哪个版本、哪个证据执行后的结果”。
```

doctor 必须检查：

- 有没有 mutation event 没有 `lock_id` 也没有 `lock_exempt_reason`。
- 有没有 lock 引用的 recipe hash 已经变了。
- 有没有 stale lock 还被写命令继续使用。
- 有没有 capture 没绑定 lock，导致不知道 agent 当时依据是什么。

## Claim Status

每个命令都必须输出 claim 边界。

```text
verified
  已经由真实文件、真实日志、真实运行结果或用户确认支持。

inferred
  只是根据当前材料推断，不能说已验证。

missing_evidence
  缺关键证据。

cannot_claim
  明确不能声称的结论。
```

例子：

```text
scan --depth shallow
  claim_status:
    verified:
      - 已读取 source_index 中列出的文件路径和 hash
    cannot_claim:
      - 不能说已覆盖全部历史资料
      - 不能说生成的 candidate recipe 已可执行
```

## CLI 命令合同

第一版命令：

```text
agent-recipes init
agent-recipes sources
agent-recipes scan
agent-recipes search
agent-recipes refine
agent-recipes extract-cards
agent-recipes patch-draft
agent-recipes convert-doc
agent-recipes detect-scenes
agent-recipes transcribe
agent-recipes ocr-image
agent-recipes compile
agent-recipes review
agent-recipes lookup
agent-recipes lock
agent-recipes capture
agent-recipes recover
agent-recipes ingest-video
agent-recipes capabilities
agent-recipes doctor
agent-recipes readiness
agent-recipes recipe-lifecycle
agent-recipes install-skill
agent-recipes mcp
```

每个命令都必须支持：

```text
--dry-run
--json
--project <path>
```

资料速炼台相关命令边界：

```text
search
  只返回本地 source_index/video_index/source_refinery 里的 evidence candidate。
  不能 claim 候选已经验证。

refine
  把 chunk 映射成 candidate_fields 和 knowledge_need_id。
  不能直接写正式 recipe。

extract-cards
  按固定 schema 生成五类卡。
  输出不了 schema 就 archive_index_only。

patch-draft
  从卡片生成 RecipePatchDraft。
  必须进 review_queue，审过后才允许成为 CandidateRecipePatch 或正式 recipe。

knowledge-fusion
  从 source_refinery cards 生成 merge/split/conflict/needs_deep_read 等融合候选。
  必须写 source_trace、evidence_strength、cannot_claim。
  只能写 source_refinery/fusion、candidates 和 review_queue，不能直接写正式 recipe。

convert-doc
  用 MarkItDown/Docling 把本地文档转成 normalized markdown。
  只生成候选资料，不直接写正式 recipe。

detect-scenes
  用 PySceneDetect 对本地视频做场景切分。
  不能 claim 切分结果就是正确剪辑结构。

transcribe
  用 faster-whisper/WhisperX 对本地音视频做 ASR。
  不能 claim ASR 文本完全正确。

ocr-image
  用 PaddleOCR/Surya 对本地图片做 OCR。
  不能 claim OCR 文本完全正确。

capabilities
  只报告本机工具/依赖是否可用。
  不能静默安装依赖，不能把缺依赖说成已接入，不能把依赖可用说成 runtime 已验收。
```

每个写命令都必须输出：

```text
idempotency_status
files_written
objects_created
objects_updated
previous_hash
new_hash
rollback
claim_status
```

`idempotency_status` 只能是：

```text
created
  新建了 event 或对象。

replayed
  同一个 idempotency_key 和同一个 payload_hash 已经执行过，这次只是返回旧结果。

unchanged
  输入和现有状态一致，没有必要写新 event。

conflict
  同一个 idempotency_key 对应了不同 payload，或 prev_event_hash 不匹配，必须停止。
```

所有写命令必须满足：

- 同一输入重跑不能新增重复 event。
- 同一 key 不同 payload 必须 conflict。
- conflict 不能静默修复，必须告诉用户或 agent 下一步怎么重新读取最新状态。

错误格式：

```text
code
problem
cause
fix_command
files_changed
claim_status
docs
```

例子：

```text
AR210 No authorized sources
problem: 没有授权资料源，scan 不能开始。
cause: .recipes/sources.yaml 为空。
fix_command: agent-recipes sources add <path> --read-only
files_changed: []
claim_status:
  cannot_claim:
    - 不能说项目资料已扫描
```

## First Run

第一版必须有一个 60 秒最小链路，不依赖真实项目、不依赖 SampleProject、不依赖视频、不依赖 Cognee。

```bash
agent-recipes init
agent-recipes capture --type correction --text "执行前必须 lookup 并 lock 菜谱。"
agent-recipes compile --max-candidates 1
agent-recipes review --accept <review_id>
agent-recipes doctor --json
agent-recipes install-skill --agent codex --scope project --dry-run
```

这条最小链要输出：

- 创建了哪些 `.recipes/` 文件。
- 生成了哪条候选菜谱。
- 引用了什么来源。
- 下一条命令是什么。
- 怎么回滚。
- 现在不能 claim 什么。

第一体验不是“扫描了很多资料”，而是“它能把一条纠偏变成下次执行前的 stop line”。

## Phase 0: 可信内核

目标：本体能可靠跑通，不追求大而全。

Phase 0A 只做最小可信闭环：

```text
schema + init
sources add
capture event
candidate recipe patch
review queue
review accept/reject
recipe version
lookup
lock
capture bound to lock_id
doctor
```

Phase 0A 的验收命令链：

```text
agent-recipes init
agent-recipes sources add fixtures/corrections.md --read-only
agent-recipes capture --type correction --text "..."
agent-recipes compile
agent-recipes review --accept <review_id>
agent-recipes lookup "同类任务"
agent-recipes lock --recipe <recipe_id>
agent-recipes capture --type success --lock <lock_id>
agent-recipes doctor --json
```

Phase 0B 再做 adapter 和入口：

```text
MCP server skeleton + doctor/lookup/lock/capture
install-skill --dry-run
adapter parity tests
```

Phase 0C 再做非阻塞补充：

```text
recover 三次阈值生成 candidate patch
scan 指定文本资料 + source_index
ingest-video 只支持 --transcript
```

Phase 0 不做：

- mp4 ASR。
- Cognee/Graphiti 接入。
- deep scan 全历史。
- 自动写正式菜谱。
- 开源脱敏包。
- 跨 agent 完整验证。

判断标准：Phase 0A 过了，才允许做 Phase 0B；Phase 0B 过了，才允许做 Phase 0C。不能为了看起来完整，把 adapter、视频、recover 提前塞进最小闭环。

## Phase 1: 真实项目试跑

目标：拿一个普通 fixture 和一个 SampleProject 小切片测试。

验收任务：

```text
普通 fixture:
  - 3 条种子纠偏
  - 2 条候选菜谱
  - 1 个 review item
  - 1 次 lock
  - 1 次 failure capture
  - 1 次 recover candidate patch

SampleProject 小切片:
  - 只选一个小问题，例如“小窗怎么放”
  - 必须召回来源
  - 必须标 forbidden_path
  - 必须写 cannot_claim
  - 必须让 fresh agent 按 recipe 执行前知道 stop_line
```

SampleProject 只能作为测试数据。通用 schema、命令、插件、MCP 都不能写死 SampleProject 规则。

## Phase 2: Source Refinery、检索和视频增强

Phase 2 的核心不是“接很多工具”，而是把资料快速炼成能进菜谱的零件。

这里的主角仍然是“安全复用经验”。外部 adapter、embedding、memory、graph、OCR、ASR 都只能帮系统更快找到候选证据，不能替代 source_trace、cannot_claim、review_queue、lookup/lock 和 benchmark。

目标链路：

```text
source_index / video_index
  -> source_refinery chunks
  -> candidate field mapping
  -> five card types
  -> RecipePatchDraft
  -> review_queue
  -> accepted recipe version
```

v0 本地优先：

- Markdown / text：按标题、段落、行号切 chunk。
- Human correction cards：必须支持 `Wrong Behavior / Correct Behavior / Check / Applies To` 这类人写的纠偏卡；不能要求资料先改成机器字段，抽取时要保留整张卡的来源和上下文。
- Transcript：按时间码切 chunk。
- Video：先用 transcript；有本地视频时用 ffmpeg 抽关键帧。
- Search：先用本地关键词检索，后续再加 SQLite FTS5。本地关键词检索必须同时看 chunk 正文、source path 和文件名，避免 `RULES_PIP.md` 这类专门规则文件因为正文分数低被 handoff/summary 挤掉。
- Source scoping：`search` / `refine` / `self-run-benchmark` 必须支持按 source path 缩小范围，例如 `--source-path-contains layer_contract.json`。真实资料压测不能只靠关键词祈祷命中，必须能明确说“这轮只看这几份资料”，否则课程清单、总结、蓝图会把小菜谱候选冲胖。
- Cards：先用 JSON/schema 约束的本地抽取结果。
- Structured JSON：合同类 JSON 抽取时必须保留结构键名，例如 `z_order_bottom_to_top: L10_back_typography`、`color_relation: ...`、`motion_rejects: ...`。即使 `source_quote` 被截断到 JSON 数组闭合前，也要抽出已经露出的字符串并保留键名，不能只留下裸值。
- Patch draft：只能写 `source_refinery/patch_drafts/` 或 `review_queue/`，不能直接改正式 recipe。

外部工具 adapter：

- MarkItDown / Docling / Marker：文档转 Markdown 或结构化 chunk。
- WhisperX / faster-whisper：本地 ASR + 时间码。
- ffmpeg / PySceneDetect：抽关键帧和镜头切分。
- PaddleOCR / Surya：OCR 和复杂版面识别。
- OCR/ASR adapter 是可替换部件。真实素材识别质量不行时，换更强工具，不能降低 `cannot_claim` 标准。
- SQLite FTS5：本地全文检索和 metadata 过滤。
- Qdrant / Weaviate / LanceDB：后续大量资料时再做向量或 hybrid search。
- Cognee / Graphiti / codebase-memory-mcp：只作为 evidence candidate 来源，不能直接覆盖 recipe truth。Zep 已从当前路线废弃，不再作为待落地运行闭环。
- Cognee local v0：`memory-index/search/status` 只把 `source_refinery` refined chunks、cards、patch drafts、review items 写成本地 memory candidate index；`memory-native-probe` 只做关云、关遥测、锁路径、mock embedding 的本地 session remember/recall 安全冒烟；`memory-semantic-configure` 只写项目本地 runtime 配置或 detect-only 报告，其中 embedding 必须是 loopback，本地 LLM 必须是 loopback，DeepSeek 云 LLM 只能走 `https://api.deepseek.com` allowlist 且只保存 API key 环境变量名；给 Cognee/LiteLLM 运行时使用的 DeepSeek 模型名必须转成 `openai/deepseek-v4-flash` 或 `openai/deepseek-v4-pro`，项目配置仍保存 Agent Recipes 自己的 `deepseek-v4-flash/pro`；`memory-semantic-probe` 在 `MOCK_EMBEDDING=false` 下做语义运行门禁，缺 embedding、LLM 或 DeepSeek key 时必须 fail-closed，并按 semantic runtime 配置 hash 使用独立 `.recipes/memory/cognee/runtime/semantic/<hash>/`，避免不同 embedding 维度污染 LanceDB 表。不能 claim 已完成生产级 semantic graph、真实 embedding 质量、LLM 检索质量或长期记忆。
- Graphiti local v0：`memory-index/search/status --adapter graphiti` 只把 `source_refinery` refined chunks、cards、patch drafts、review items 转成本地 nodes/edges 关系候选图，用来看到失败路径、纠偏路径、成功路径和来源关系。它不调用原生 Graphiti 服务，不连外部图数据库，不写正式 recipe，也不能绕过 `review_queue`。
- Graphiti native probe v0：`memory-native-probe --adapter graphiti` 只做项目本地 Kuzu driver、Graphiti 原生对象、schema 建立、节点写读、关遥测、锁路径、阻断网络的安全探测。它不调用默认 OpenAI client，不证明 LLM 抽取质量，不证明生产级图谱质量，也不写正式 recipe。
- DeepSeek cloud text adapter v0：`cloud-configure/status/refine --provider deepseek` 把 DeepSeek V4 Flash/Pro 当文字大脑，只处理 OCR/ASR/Markdown 等文字输入。配置只保存 API key 环境变量名，不保存 key；真实联网必须显式 `--allow-network`，测试和回放用 `--response-json`。输出只写 `source_refinery` candidate cards，不能直接写正式 recipe，不能 claim 视觉能力。
- Qwen3 embedding local adapter v0：`embedding-configure/status/index/search --provider qwen3` 把 Qwen3-Embedding-0.6B 作为首选轻量本地召回目标，只允许 loopback endpoint；可接 Ollama `/api/embed` 或 llama.cpp/OpenAI-compatible `/v1/embeddings`。默认不调用本地服务；测试和回放用 `--response-json`，真实调用必须显式 `--allow-loopback`。embedding 只负责按意思召回候选，不负责判断真相，不替代 DeepSeek 抽卡或 `review_queue`。
- Quality benchmark v0：`quality-benchmark` 只做本地候选质量体检，覆盖 source search、Cognee memory candidate、Graphiti patch/review 关系、Qwen embedding recall、误召回负例、review_queue accept -> formal recipe 路径。Qwen case 默认不调用本地服务；必须用 `--qwen-response-json` 回放或显式 `--allow-loopback`。报告写入 `.recipes/reports/quality_*.json`，输出仍是 candidate-only，不能 claim 生产级召回质量。
- Candidate-quality benchmark v1：`candidate-quality-benchmark --cases <json>` 专门检查 pending review / candidate patch。它不仅要能检查 cards 里的 `required_terms`、`forbidden_terms`、`required_source_paths`、`forbidden_source_paths`、`source_trace`、`cannot_claim`，还要能用 `required_proposed_terms`、`min_proposed_value_count` 和 `max_proposed_value_count` 检查候选补丁本身是否足够有料、且没有胖到不可 review，避免“卡片召回到了，但 proposed patch 仍然很薄/很胖”被误判为质量通过。
- `cannot_claim` 不能只看字段存在。`False`、`null`、`unknown`、`无` 这类占位值不算有效限制；cloud/refinery 抽卡遇到这种值必须回落到默认候选边界，candidate-quality 必须把已有假限制判失败。
- Candidate-quality 里的 `proposed_value_count=0` 是有效证据，不是空值。裁判必须尊重 review hint 里的 0，不能把 0 当成 missing 后又从 candidate patch 里重算，否则会把“空补丁”误报成“太胖”或“通过”。
- 已接受 review 的回归复测和 pending 候选测试要分开。默认 candidate-quality 发现正式 recipe 已存在就失败；只有 case 显式写 `allow_formal_recipe_exists: true` 且 `expected_review_status: accepted` 时，才允许把它当作“已收菜谱回归检查”，不能用这个开关绕过 review_queue。
- Lookup pressure v1：`lookup-pressure --cases <json>` 专门压测 lookup 适用边界。正例必须命中并包含必需字段；负例用于发现窄 recipe 被过度套用到字幕、转场、声音、完整流程等不该覆盖的场景。报告写入 `.recipes/reports/lookup_pressure_*.json`，失败时返回非 0，但只代表 lookup/applicability 风险，不能直接改正式 recipe。普通 `lookup` 保持兼容，会返回 `applicability`、候选分数、缺失 query terms；真正执行前必须走 `lookup --strict` 或 `lock --query`，弱匹配必须 fail-closed，不能生成 execution lock。lookup 打分只能使用正向适用字段，不能把 `forbidden_path`、`do_not_use_when`、`cannot_claim` 当成“应该使用这条 recipe”的理由；强匹配还必须满足最小关键词命中比例，避免靠 `video`、`and` 这类泛词凑分。
- Lookup bilingual applicability：真实消费任务经常用中文提问，而被接受的 recipe 可能来自英文 contract/source。lookup 可以对少量稳定领域词做中英同义匹配，例如“大字 -> large words / big text”、“字在人后 -> behind presenter”、“抠像 -> cutout / matte”、“后期包装 -> postprod / post-production”。这只能用于正向适用字段，不能让 `forbidden_path`、`do_not_use_when`、`cannot_claim` 变成命中理由；负例仍然必须通过 lookup-pressure 挡住。
- Lock pressure v1：`lock-pressure --cases <json>` 专门压测真实消费的执行前锁定。正例必须 `lookup --strict` 后成功创建或复用 execution lock；负例必须阻止 lock，防止窄 recipe 被套到字幕、声音、完整流程等不该覆盖的任务。它会写 lock 和报告，但不能执行 recipe，不能 capture success，不能 claim 真实任务完成或质量通过。
- Consumption coverage v1：`consumption-coverage` 只做裁判报表，盘点正式 recipe 有没有 passed lookup/lock pressure 证据。正例 `lock-pressure` 已经包含 `lookup --strict`，所以可同时计入 lookup 覆盖；但 cases 文件本身不算证据，必须有 passed report。coverage 通过只说明“这些 recipe 被消费压测覆盖过”，不能说明 recipe 已执行、质量已通过、未来查询都不会误召回。
- Real pressure summary v1：`real-pressure-summary` 汇总多个 `.recipes_real_tests/*/.recipes` 项目的正式 recipe、lookup/lock/coverage、candidate-quality、repeat-error 和 review-packet 报告。它只列缺口和下一轮压测方向，不读取源资料、不生成菜谱、不接受 review、不证明质量通过。历史失败报告不自动等于当前失败；如果同一项目有通过的 candidate-quality 报告，就不把旧失败当成当前总缺口。
- Self-run benchmark v1：`self-run-benchmark` 用已授权 source 让 Agent Recipes 自己跑 `scan -> search -> refine -> extract-cards -> patch-draft -> review_queue`，并检查卡片 schema、source_trace、claim limits、pending review、“没有直接写正式 recipe”，以及 patch draft 至少有可审核候选内容。它必须能用 `--source-path-contains` 做小范围资料对照压测。它只能证明系统链路自跑，不证明候选内容质量通过。
- Target-suggestions v1：`target-suggestions` 只看 `review_queue`、candidate patch、source_refinery cards 和 `source_trace`，把 rejected/pending 历史反推出下一轮更窄的 `self-run-benchmark` 参数。它不能读取外部 source 原文来替菜谱总结内容，不能写正式 recipe，也不能自动接受 review。宽 fusion 的总 proposed value count 不能硬贴给每个 source；必须按该 source 自己的卡片和 trace 计算窄目标证据，避免把空补丁或泛材料误排到前面。
- Review-triage v1：`review-triage` 只看 `review_queue`、candidate patch、review hints、`source_trace` 文件名和 proposed value count，把 pending/rejected 候选分成 `human_review_candidate`、`evidence_index_only`、`thin_candidate`、`too_broad`、`duplicate_risk` 等裁判桶。它不能读取外部 source 原文来替菜谱总结内容，不能接受 review，不能写正式 recipe；只能给出 `recommended_action`，例如 `human_review_required`、`keep_as_evidence_index_or_reject_review`、`reject_or_archive_until_more_evidence`、`split_or_regenerate_narrower`。
- Review-packet v1：`review-packet` 只把 `review_queue`、candidate patch、`plain_language_summary` 和 triage 证据整理成给人看的 markdown/json 审核包。它解决“全是英文和 ID，人看不懂”的问题，但不能读取外部 source 原文，不能替人总结资料，不能接受/拒绝 review，不能写正式 recipe。
- Capabilities source_refinery tool boundary：`capabilities` 必须说明 `review-triage`、`review-packet`、`candidate-quality-benchmark`、`target-suggestions` 都只是 candidate-only 裁判/整理工具，不能写正式 recipe、不能接受 review、不能替代人工判断。
- Repeat-error benchmark v1：`repeat-error-benchmark --cases <json>` 只评分外部提供的“无菜谱 / 有菜谱”A/B 输出，不启动 agent、不替 agent 干活。A/B 输出可以来自 fresh test agents 或外部真实会话，但必须保存原始输出；controller 只能整理摘录和评分用例，不能人工改写成“更像会过”的答案。默认产品门槛是至少 5 个旧错任务、至少 3 个明显改善；小样本 smoke 可以调低阈值，但不能 claim 完整 repeat-error benchmark 通过。

这阶段的硬规则：

- 工具负责拆、转、索引、抽字段；菜谱协议负责判断能不能进 recipe。
- 外部 adapter 必须 fail-closed：缺依赖时报告 `missing_evidence`，不能假装成功。
- 外部检索只提供 evidence candidate。
- Cloud adapter 必须显式联网，secret 只走环境变量，不落仓库、不进导出包。
- Embedding adapter 只做候选召回，质量不够时可以换 4B / bge-m3，但不能降低 `cannot_claim` 标准。
- Quality benchmark 通过只能说明本地小样本体检通过，不能说明大规模历史资料、真实任务、生产级记忆或图谱质量已经通过。
- Lookup pressure 通过只能说明当前 cases 没发现明显过度套用；失败时不能粉饰成系统成功，也不能直接把失败修成正式 recipe。它暴露的是 lookup/applicability 边界。弱匹配只能提示“可能相关”，不能进入 lock，也不能指导 agent 执行真实任务。
- Candidate-quality benchmark 失败时要先区分三类原因：真实漏召回、候选抽字段漏关键信息、测试词写得比 source truth 更窄。真实漏召回要改检索/抽取；测试词不准要改 benchmark case，不能硬把失败说成成功。
- Candidate-quality benchmark 通过也要分两层说清：卡片召回通过不等于 proposed patch 质量通过。真实资料压测时，关键 case 应该同时设置 `required_terms` 和 `required_proposed_terms`。
- Candidate-quality benchmark 通过仍然不等于“值得写成正式菜谱”。进入 review 决策时还要挡住四类东西：已有窄菜谱已经覆盖的重复候选、只有 source_trace/receipt/contact_sheet 的证据索引、只有 `forbidden_path` 但缺少正向 use/step/check 的负面守门项、target recipe 明显过宽的候选。它们可以保留为 evidence candidate，但不能为了显得有产出而硬 accept。
- Self-run benchmark 通过只能说明系统保留了完整命令链证据，不能说明 review item 应该被接受。
- Target-suggestions 通过只能说明系统能从 review 历史里排出下一轮窄目标；不能说明这些窄目标已经跑过、质量合格或应该收成正式菜谱。生成的 `command_args` 必须再由 Agent Recipes 自己跑，并停在 review_queue。
- Review-triage 通过只能说明系统按机器证据完成分层；不能说明 `human_review_candidate` 已经该收，也不能说明 `evidence_index_only/thin/too_broad` 的源材料没有价值。非人工候选可以被 reject，但必须保留 source_trace 和事件证据。
- Review-packet 通过只能说明候选被整理成可读审核材料；不能说明候选已经被人工批准，也不能说明系统读懂了外部资料全文。
- Repeat-error benchmark 通过只能说明给定 A/B 输出里旧错减少，不能说明系统已经替真实 agent 完成任务，也不能说明未来不会复发。
- candidate 必须进入 card、patch draft、review 或 evidence index。
- 不能因为“检索到了”就 claim 已验证。
- 人类纠偏卡进入系统时，`Wrong Behavior` 只能变成候选 forbidden/failure，`Correct Behavior` 和 `Check` 只能变成候选 checklist，仍然必须过 `review_queue` 才能写正式 recipe。
- 不能因为“转写完成”就 claim 已吸收。
- 不能因为“抽到关键帧”就 claim 视觉质量通过。
- 不能因为“生成卡片”就直接改正式 recipe。

## Phase 3: 插件和分发

目标：

- Codex 插件：一键安装 Skill、MCP、模板。
- Claude 接入说明或配置包。
- Hermes 接入说明或配置包。
- CLI 发布：至少支持 macOS arm64，本地开发先不承诺全平台。
- project-local `source-to-recipe-indexer` skill：把 source refinery 流程封成可复用操作。
- `install-client`：写入真实本机 client 配置，当前确认 Codex `config.toml`、Claude user config `~/.claude.json`、Claude project config `.mcp.json`（显式传 `--config-path` 时）和 Hermes `config.yaml`。
- `install-client` 写入的 MCP server 必须带最小安全 env：固定 `PATH=/usr/bin:/bin:/usr/sbin:/sbin`，并设置 `PYTHONDONTWRITEBYTECODE=1`。
- `install-client` 写入真实客户端配置时，优先用显式 Python 启动：`/usr/bin/python3 <project>/bin/agent-recipes ...`，不能只依赖脚本 shebang。
- `install-client` 写入的 MCP env 必须带本地 debug log 路径：`AGENT_RECIPES_MCP_DEBUG_LOG=<project>/.recipes/reports/mcp_stdio_debug.jsonl`。debug log 只能写 lifecycle event、pid、方法名、请求 id、工具名、工具数量和 clientInfo，不能写用户正文、工具参数全文、启动 argv 或项目路径。
- `client-smoke`：读取安装生成的 MCP 配置，真实拉起 stdio server，跑 `tools/list` 和 `doctor`。
- `adapter-lock`：从项目本地 `.venv` 导出 Python adapter lockfile 和运行报告。
- `system-lock`：记录本机系统二进制 path/version/hash，例如 `python3`、`sqlite3`、`ffmpeg`、`ffprobe`、`uv`、`llama-server`。
- MCP stdio smoke 必须走标准握手：`initialize -> notifications/initialized -> tools/list -> tools/call`。裸跑 `tools/list` 只能算低级连通性，不能当真实客户端兼容证据。

`source-to-recipe-indexer` skill 只负责：

- 登记资料。
- 转换格式。
- 切块。
- 打标签。
- 搜索候选。
- 抽五类卡。
- 生成 patch draft。

它不能负责：

- 不直接改正式 recipe。
- 不绕过 review_queue。
- 不安装全局依赖。
- 不调用云服务，除非用户对某个项目明确授权。
- 不把 SampleProject 规则写成全局默认。

分发必须写清：

- 怎么安装。
- 怎么升级。
- 怎么卸载。
- 配置写在哪里。
- 数据写在哪里。
- 如何确认没污染项目外文件。

Phase 3 不能 claim：

- `client-smoke` 通过不等于真实 Codex/Claude/Hermes 客户端已经加载。
- `install-client` 写入配置不等于当前已运行 client 会话热加载。
- debug log 中没有真实客户端 `initialize/tools/list` 记录时，不能 claim 真实客户端已经连接。
- 真实 Codex 接入验收必须用 fresh Codex thread/subthread 看到 `mcp__agent_recipes` 工具，并实际调用只读工具如 `agent_recipes_doctor` 成功；老线程没有刷新工具上下文时，不能反推出 MCP server 或客户端配置失败。
- Python lockfile 不等于 Homebrew、ffmpeg、llama-server 等系统二进制已经被锁定。
- system-lock 报告不等于另一台机器已经复现安装，也不等于 Homebrew formula 完整 pin 住。
- 依赖可复现不等于 OCR/ASR/scene cut 输出质量通过真实素材压测。

## Phase 4: 开源版

脱敏规则：

- 删私有路径。
- 删用户原话。
- 删素材名。
- 删账号、登录态、缓存路径。
- 保留通用 schema、命令、模板、示例。
- 保留可公开的安装说明、测试、Python adapter lockfile；不保留 `.venv` 和运行态 `.recipes`。

开源仓库名：

```text
agent-recipes
```

## 真实落地缺口地图

总方案负责说明系统应该是什么；`REAL_LANDING_GAP_MAP.md` 负责说明系统离“完全落地”还差什么。

完全落地分三道门：

```text
日常可用：真实 agent 能自然 lookup、lock、capture。
质量证明：真实任务里 repeat-error 下降，误召回减少，no-match 边界清楚。
分发复现：干净客户端或另一台机器能安装、验证、理解边界。
```

当前判断：

- 本地核心和 SampleProject 压测池已经有强证据。
- 还不能 claim 真实视频/音频质量、用户验收、全 SampleProject 知识吸收、公开发布或另一台机器复现。
- 下一阶段优先看 `REAL_LANDING_GAP_MAP.md` 的 P0/P1：先稳 Codex 日常使用，再扩大 SampleProject 后期包装质量证明。

## 测试矩阵

第一版最低测试：

```text
schema validation
init idempotency
event log replay
event prev_hash conflict
event idempotency_key replayed
event idempotency_key conflict
sources permission validation
scan shallow/medium claim limits
source hash and mtime tracking
compile duplicate prevention
review accept/reject/supersede
recipe versioning
lock create/expire/stale handling
lock required for capture success/failure/unknown
lock hash mismatch fail closed
doctor lockless mutation detection
capture append-only
capture binds lock_id
recover third-failure threshold
doctor broken refs
doctor stale locks
doctor missing source
doctor claim_status
ingest-video --transcript parse
capabilities missing dependency fail-closed
search returns evidence candidate only
refine archive_index_only for unmappable chunks
extract-cards schema validation for five card types
patch-draft cannot write formal recipe directly
source_refinery source_trace required
source_refinery evidence_strength required
source_refinery cannot_claim required
ingest-video --video --extract-keyframes local ffmpeg path
keyframe extraction cannot claim visual quality
install-skill --dry-run
MCP doctor/lookup/lock/capture smoke
adapter parity: core vs CLI vs MCP same ids/hashes/claim_status/errors
client-smoke launches installed MCP config and reports claim limits
install-client writes Codex/Claude/Hermes config with backup and smokes MCP command
install-client writes minimal MCP env and removes stale managed subtables
install-client uses explicit Python command instead of relying on shebang
MCP stdio debug log does not pollute stdout and records tools/list count
adapter-lock fails closed without .venv
adapter-lock writes pinned Python lockfile and doctor reports it
MCP CLI fallback supports the expanded tool surface
quality-benchmark scores source/Cognee/Graphiti/Qwen candidate recall, false recall, and review gate without production-quality claims
```

幂等测试必须明确断言：

```text
same key + same payload
  -> idempotency_status=replayed

same key + different payload
  -> idempotency_status=conflict

same command + unchanged state
  -> idempotency_status=unchanged

new write
  -> idempotency_status=created
```

Adapter parity test 必须用同一组 golden fixture 跑三遍：

```text
core API
CLI --json
MCP tool
```

三遍必须得到：

- 同样对象 id。
- 同样对象 hash。
- 同样 `claim_status`。
- 同样错误码。
- 同样 `idempotency_status`。

## 验收标准

Phase 0 通过，不看命令数量，看闭环。

Phase 0A 必须证明：

```text
1. 能初始化 .recipes。
2. 能注册授权 source。
3. 能 capture 一条纠偏 event。
4. 能从纠偏生成 candidate recipe patch。
5. 能生成 review_queue item。
6. 用户接受后才能生成正式 recipe version。
7. lookup 能找到适用 recipe。
8. lock 能锁 recipe version/hash 和 claim limits。
9. capture success/failure/unknown 必须绑定 lock_id，并写入准确的 recipe id/version/hash。
10. recipe hash 变了，写命令必须 fail closed。
11. 重跑同一写命令不能生成重复 event。
12. doctor 能说清已验证、缺证据、不能 claim。
13. doctor 能发现 broken refs、stale locks、lockless mutation。
14. readiness 能把 ledger、lifecycle、recipes、review、optional adapters、real client 分轴报告。
15. readiness 必须给机器稳定的 recommended_action，核心 mutation 故障时 fail closed。
```

Phase 0B 必须证明：

```text
1. MCP 至少能跑 doctor/lookup/lock/capture。
2. CLI 和 MCP 都调用同一个 core 行为合同。
3. adapter parity test 通过。
4. install-skill --dry-run 能说明会写什么，不真的写。
```

Phase 0C 必须证明：

```text
1. 第三次同类失败只生成 candidate patch，不自动写正式菜谱。
2. scan 指定文本资料能生成 source_index。
3. ingest-video --transcript 能生成 video_index。
```

Phase 2 必须证明：

```text
1. capabilities 能说明哪些本地工具/依赖缺失，且不会安装。
2. search 只返回 evidence candidate，不 claim 已验证。
3. refine / extract-cards / patch-draft 至少能在本地 text/transcript fixture 上跑通。
4. 五类卡必须有 source_trace、target_fields、evidence_strength、cannot_claim。
5. 进不了字段的 chunk 必须 archive_index_only。
6. patch draft 不能直接写正式 recipe。
7. ffmpeg 关键帧只证明本地可抽帧，不能 claim 视觉质量通过。
8. 外部 adapter 缺依赖时必须 fail-closed。
9. source-path scoped search/refine/self-run 能把候选限制在指定来源内，避免宽检索把小菜谱冲胖。
10. JSON 合同抽取必须保留键名，并能从截断 JSON 数组中抽出已出现的候选值。
11. self-run-benchmark 不能把 0 条 proposed value 的空 patch draft 判成通过。
12. Markdown 方法类资料里的产物清单，例如 `learning_material_info_cards.json` / `p1_failure_to_experience_map.json`，必须能进入候选字段，不能只被打成 archive_index_only。
13. lookup 不能把禁止项、不能 claim 项、废词或极少数泛词当成强适用依据；`lock --query` 必须继承同一套 fail-closed 规则。
```

Phase 3 必须证明：

```text
1. install-skill 能写 project-local Skill 和 MCP 配置。
2. client-smoke 能通过安装配置真实拉起 MCP stdio server。
3. client-smoke 至少通过标准 MCP 握手后跑通 tools/list 和 doctor。
4. client-smoke 必须明确不能 claim 真实客户端已加载。
5. install-client 写入真实 client 配置时必须先备份。
6. install-client 必须用写入的 command/args 做 stdio smoke。
7. Hermes 只在确认的 `mcp_servers` map schema 下写入；遇到非 map 结构必须 fail-closed。
8. adapter-lock 能从项目 .venv 导出固定版本 Python lockfile。
9. doctor 能报告 adapter runtime lock 状态。
10. mcp --tool CLI fallback 必须覆盖当前 MCP 工具表，不能只停在早期 skeleton。
11. system-lock 能记录系统二进制 path/version/hash，doctor 能报告 system runtime lock 状态。
```

真正产品指标：

```text
system-self-run benchmark:
  给系统真实 source、范围、target_recipe_id、candidate_fields 和验收标准
  由 Agent Recipes 自己 scan/search/refine/extract-cards/patch-draft
  Codex 不人工替它总结规则，不人工跳过卡片层
  产物必须进入 review_queue
  人只做 accept/reject/supersede 评审
  v1 命令：agent-recipes self-run-benchmark

repeat-error benchmark:
  5 个旧错任务
  fresh agent 无菜谱先跑一次
  fresh agent 有菜谱再跑一次
  至少 3/5 明显减少重复错误
  原始 A/B 输出必须留证；不能由 controller 代写答案冒充 fresh agent
  v1 命令：agent-recipes repeat-error-benchmark
```

## 回滚

所有写入动作都要能回滚。

第一版回滚方式：

- `events.jsonl` append-only，不删除。用 tombstone event 表示撤销。
- recipe version 不覆盖旧版本。新版本 supersede 旧版本。
- review item 决策不能硬删。只能标 `reversed` 或 `superseded`。
- lock 不能硬删。只能 release 或 expire。
- doctor 要能检查孤儿 evidence、broken refs、stale locks。

## 风险

最大风险：

```text
1. review_queue 变成新文档苦役。
2. recover 把错经验固化。
3. lock 只是文件标记，不能约束 agent。
4. doctor 只报绿色，不说不能 claim 什么。
5. MCP/插件做太早，核心逻辑反而不稳。
6. SampleProject 规则污染通用系统。
7. source_refinery 变成新的资料总结库。
8. 工具产物绕过 review 直接进 recipe。
9. 外部 adapter 缺依赖或失败时假装成功。
10. OCR/ASR/抽帧被误当成质量验收。
11. Codex 人工替菜谱系统读资料、挑重点、写规则，导致测试变成“人会总结”，而不是“系统会自跑”。
```

对应控制：

```text
1. review item 必须短、可决策、有推荐、有 diff。
2. recover 只能生成 candidate patch。
3. lock 必须锁 recipe hash、claim limits、allowed/forbidden actions。
4. 所有命令都输出 claim_status。
5. core 先独立，CLI 和 MCP 都只是 adapter。
6. SampleProject 只进 fixture 或 private test data。
7. source_refinery 只允许输出 chunk、card、patch draft，不允许输出长总结。
8. patch draft 必须进 review_queue，审过才进正式 recipe。
9. capabilities 和 adapter 都必须 fail-closed，缺依赖写 missing_evidence。
10. OCR/ASR/抽帧只能证明转换或索引成功，不能证明内容正确或视觉质量通过。
11. 真实测试必须保留系统命令链证据；没有 scan/refine/extract-cards/patch-draft/review_queue 证据，不能 claim 菜谱系统自己干活。
```

## 最小下一步

### 2026-07-10 收窄后的超集路线

替代审计后，Agent Recipes 不再继续扩张成通用记忆平台或万能 Agent 训练系统。
核心目标改为：保留菜谱独有的人工晋级、严格 no-match、recipe version/hash lock、
lock-bound outcome 和 claim boundary，同时吸收 Remnic 中经过许可证和文件级审计的
生命周期能力。

详细验收矩阵和分阶段边界见 `AGENT_RECIPES_SUPERSET_ROADMAP.md`。

Stage A 和 Stage B 已完成本地实现与测试：生命周期防复活、精确 outcome 归因、
unknown、置信度/成熟度、自动降级/暂停、readiness/doctor、CLI/MCP 都有可执行证据。
Cass 代码不进入项目；其高层产品能力只作为 clean-room 独立实现的验收目标。

Stage C 已完成：Cognee、Graphiti、Qwen recall 有统一候选合同，损坏的 recall
只停用对应适配器，不拖垮核心；`python -S` 已证明没有第三方包时最小治理链仍能完整运行。

Stage D 已完成：候选/证据可以隔离、修复后显式释放；凭证在事件哈希和候选写盘前处理；
execution evidence pack 受 lock、大小预算和隐私规则约束，并记录每项省略原因。

Stage E 本机工程化已经完成第一轮：持久化和 schema 迁移已拆模块，标准 wheel、CI 定义、
显式迁移、隔离虚拟环境完整命令链和 MCP smoke 都有证据；菜谱业务合同没有改变。

用户要求最终在收窄目标上比 Cass 和 Remnic 加起来更强，同时保持更轻。这个目标不靠堆
功能，而按 `AGENT_RECIPES_COMPETITIVE_STANDARD.md` 的九道硬门验收；机器体积上限见
`ENGINEERING_BUDGET.json`。

Stage F 第一刀已完成：事件账本从 `core.py` 拆到 `ledger.py`，账本被篡改或损坏后所有写入
都会 fail closed；核心仍然零运行依赖、零必需外部服务，wheel 低于 256 KiB。当前只有
5/9 道竞争门有本地证据，不能 claim 已全面超过 Cass + Remnic。

Stage G 已把 lifecycle policy 从 `core.py` 拆到 `lifecycle.py`：同内容换名字不能绕过
tombstone，revocation 只允许新 ID 重新走 review，旧 ID 永久退役；lookup、lock、doctor、
readiness 和 outcome 共用同一套生命周期判断。`core.py` 降到 16477 行，竞争门仍是 5/9。

Stage H 已把 exact outcome policy 从 `core.py` 拆到 `outcome.py`：结果必须绑定准确的
recipe id/version/hash 和 lock snapshot；旧版本失败不影响新版本；legacy 只报警不执法；
unknown 中立；新显式失败才能触发 caution/degraded/hold。`core.py` 降到 16212 行。

Stage I 已把 execution lookup/locking policy 从 `core.py` 拆到 `execution.py`：严格匹配、
no-match、active recipe 选择、recipe id/version/hash 锁定、有限过期时间校验和 stale lock
退役共用一套独立策略；`core.py` 降到 15610 行。真实 CLI/MCP、独立 wheel 安装和完整测试
均已验证，竞争门仍是 5/9，不能把结构拆分说成真实质量提升。

Stage J 已完成固定 SampleProject cohort 的大样本学习质量门：19 个项目覆盖纠偏、课程、产物和产品经验，
65 个当前 target self-run 全部通过，1112 张卡字段合同完整，19/19 个当前 accepted target 有
通过的 candidate-quality 证据，且正式 recipe 零直写。过程中系统自己产出的一个过宽纠偏候选
被质量门抓住并由 Codex 拒绝，证明失败不会为了凑通过率被隐藏。竞争门因此从 5/9 进到 6/9，
但只对这批固定样本成立。

当前下一项转向“真正减少犯错”质量门：扩大 fresh Codex 在新任务中的无菜谱/有菜谱 A/B，
保留原始输出，让菜谱提供规则和裁判评分，controller 不得替 fresh agent 写答案。之后再比较
记忆召回质量和补工程分发证据。

Stage K 已纠正“对标审计完成就等于超越”的错误。Cass、Remnic、TencentDB Agent Memory
现在统一进入 `THREE_REPO_COMPETITIVE_SCORECARD.md` 和同名 JSON：每个能力维度取三个仓库
里证据最强的那个组成虚拟最强对手，不允许用总分抵消硬短板。当前收窄目标仍有三个阻断项：
fresh agent 新任务效果、大规模召回质量、工程成熟度；全功能目标还额外落后于广义长期记忆、
上下文压缩、宿主接入和恢复流水线。因此只能说局部领先，不能说已经超过三个仓库。

Stage K 之后的真实测试不再是泛压测，而是按这三个阻断项逐个验收。第一项仍是 fresh Codex
新任务 A/B，但它现在有清楚用途：不是继续堆样本，而是尝试关闭统一总表里的第一个硬阻断项。

Stage L 已完成第一轮 fresh Codex 阻断项实测，但没有关闭阻断。v4 因未明确 SampleProject 项目范围，结果
为 0胜2平4负，只保留为测试设计失败证据；v5 明确项目范围并附真实 lookup/lock receipt，结果
为 3胜2平1负，另保留一次错锁，未达到至少 4 胜、零负、零错锁的门槛。系统没有手改答案来
凑绿，而是把输掉的 PIP 题变成 correction capture，拒绝三份重复或虚假 verified_path 候选，
修复“带锁纠偏丢失目标菜谱”的代码缺口，再经 review accept 升级原 PIP 菜谱到 v2。v6 继续
暴露中文“画中画” no-match；补同义词后，v7 fresh Agent 用真实 v2 lock 正确执行“禁止模板覆盖、
缺精确坐标就停止询问/恢复、重新截图后才能通过”。这只关闭一个失败样本，不关闭整体 fresh-agent
production effect；下一轮必须换新任务和新表述继续 A/B，不能拿同题复测反复证明自己。

Stage M 用固定题面和固定门槛完成了第二轮 unseen fresh Codex A/B。六题覆盖新旧版本假通过、
声音包装、关键词字幕、前三秒利益点、本地画面支撑和纯代码 no-match；无菜谱组与菜谱组原文先
冻结，再交给不知道分组的 fresh Codex 裁判。最终菜谱组 5胜1平0负，五个正例全部命中预期
recipe 并创建真实 lock，纯代码题 AR242 no-match 且没有 lock，错锁为 0，达到“至少4胜、
零负、零错锁、no-match正确”的预设门槛。`fresh_agent_production_effect` 因此从收窄硬阻断中
移除，但只代表本地 SampleProject 判断题行为门通过，不代表真实剪辑执行、视觉/声音成片质量或任意领域。
本轮同时暴露 subagent 超时、close 卡住、Codex CLI 插件噪音和旧 CLI 不识别 5.6 模型；这些
全部转入工程成熟度阻断，不得被行为门通过掩盖。

Stage N 完成固定同语料召回门。系统新增独立 `recall-quality-benchmark` CLI/MCP，只读 active
recipes 和固定 cases，把同一批 recipe 临时投影给 core、Cognee、Graphiti、Qwen 比较，不改
正式 recipe、候选索引或 review 状态。227 条来源题先去掉 116 条完全重复期望，只按 111 道
唯一题计分。第一轮原始结果暴露“候选检索该拒绝时不拒绝”和 llama-server 默认四 slot 在长文本
上断连；系统把 broad single-recipe no-match 前置闸门接到候选推荐层，让 Cognee/Graphiti 复用
core 的中英文别名与项目优先级，并把 HTTP 断连改成 fail-closed。最终 core 55/55 正例、0 误召回；
Cognee/Graphiti 投影各 53/55、3.6% 误召回；Qwen 47/55、6.3% 误召回、平均约 31ms；15/15
纯 no-match 全部拒绝。固定本地 `recall_quality_at_scale` 阻断关闭，但原生 Cognee/Graphiti、
任意领域和常驻服务监督仍未证明。下一项只剩工程成熟度与分发。

Stage O 完成本机工程成熟度部分，但不伪造外部证据。项目新增 wheel 内可安装的
`agent-recipes-qwen-service`，真实验证 Qwen 启动、健康检查、被杀后变红、从项目状态恢复、
同查询复现和安全停止；PID 归属不符时拒绝误杀。版本升到 0.1.1，并用 0.1.0 wheel 完成
升级、回滚、再升级，所有 doctor 为 ok 且事件字节不变。最终 wheel 在 Python 3.11、3.13、
3.14 三套新 venv 完成治理链、MCP 和服务命令露出；3.11/3.13 各自跑完 261 项测试。Stage O
没有增加核心依赖，也没有突破 15700 行/975000 字节源码预算。最后硬阻断只剩托管 CI 真实
绿灯和第二台物理环境复现；当前没有提交、推送或公开发布，不能 claim 已关闭。
## Competitive Extension 1: cause-specific feedback

Status: implemented locally on 2026-07-11.

- Keep `success`, `failure`, and `unknown` as the stable outcome layer.
- Add cause-specific feedback for retrieval mismatch, execution error, recipe
  error, staleness, applicability overreach, missing steps, excessive cost,
  recipe conflict, user correction, external dependency, and blocked evidence.
- Every outcome feedback record remains bound to the exact lock snapshot.
- Execution, dependency, and retrieval-system failures are attributable but do
  not automatically degrade a recipe version.
- Recipe-policy failures may degrade or hold the exact version under the existing
  threshold policy, but they still cannot mutate a formal recipe automatically.
- `outcome-status` exposes feedback counts, scopes, recommended human actions,
  and the complete supported taxonomy through CLI and MCP.

Next major stage: hybrid retrieval, conflict detection, and recommendation explanations.

# AI 协作记录

## 1. 工具、模型与协作分工

我（候选人）使用 Codex 开发，由 Codex 编写并落地 solution 代码；另一会话 DeepSeek Harness 提供架构评审、故障诊断和外层真实环境验证，我负责转递结果并裁决分歧。Codex 会话的精确底层模型版本没有可核验的运行元数据，因此不猜测具体型号；协作方以 DeepSeek Harness 标识参与，其精确版本亦未核验。线上修复 agent 使用 `deepseek-chat`，与开发助手的底层模型是不同概念。

模型选择基于外层实测而非偏好：`api.deepseek.com` 在宿主机与 runner 容器内均曾返回 HTTP 401，证明端点可达但当时需配置凭据；配置真实 key 后又取得合法 JSON 响应。`api.anthropic.com` 宿主直连返回 403（Cloudflare 地区限制）、容器内 SSL EOF；`api.openai.com` 直连与代理均不可达。这些是本次环境当时的结果，不代表服务在其他环境的普遍可用性。

## 2. 实现、分工与验证记录

### 2026-11-26：Codex 与 DeepSeek Harness 协作接入模型规划

用户负责转递双方结论；Codex 负责实现，DeepSeek Harness 负责架构评审和外层真实容器验证。
Harness 报告 fixed 模式首次与 worker drill 均四项全绿，并提供真实 DeepSeek A/B/C 场景规划、校验、执行全通过的结果。
双方据此确定：模型选择真实 snapshot、storage、manual ID；sha 和 revision 由执行器在调用时解析；模型判断必须放在 summary 开头。

本阶段 Codex 实现 model 模式的条件事实采集、25 秒模型超时、最多一次 fatal 重规划、显式 fixed 降级以及独立最终核验。
Codex 发现 executor 的 budget 表示累计上限，而旧 agent 传的是剩余额度，已修正；另加共享调用账本，保证降级不能重新取得 32 次额度。
模型剧本中的最终 self-check 由 agent 执行，先为 retry 的 queued 任务等待 0.3 秒。

Codex 亲自验证：模块导入成功；assessment/check_scope.py 输出 ok=true、problems=[]。
用替身模型（不是 DeepSeek）加真实 Workstation 和 process_next 后台线程验证编排：21 次工具调用，outcome=changed，catalog/search/export/backlog 均通过，无 fixed 降级。
首次宿主测试因默认 GBK 读取中文 seed 失败，使用 Python -X utf8 后通过，没有修改 core。
本阶段真实 DeepSeek 和 /api/repair 容器验收由 Harness 在外层执行；不得把替身模型验证描述为真实模型验证。

### 模型模式外层验收与重复读取优化

Harness 经用户转递报告：真实容器 mixed 修复 21 次调用、4.1 秒、四项全绿；worker drill 10 次调用、4.1 秒、四项全绿，且 worker-only 计划没有 restore_catalog/rebuild_index。两轮模型判断均出现在 summary 开头，revision 更新与 reload 链正确。
Harness 指出 phase0 与剧本重复读取，建议 prompt 复用 observed_facts、snapshot_details。
Codex 核对发现解析器也强制重复读取，因此同步允许本轮 facts 满足 snapshots、snapshot verification、settings、storage、jobs、manual listing 的前置条件；保留恢复前读取 catalog 当前哈希、实际 read_manual 和修改步骤顺序约束。
Codex 离线验证精简 A/B/C 计划分别为 8/4/2 个模型步骤；移除 A 的 read_manual 会被拒绝。scope 检查再次 ok=true。真实精简计划的调用数与恢复效果待 Harness 复测，未提前宣称通过。

## 3. 参数分类错误：模型输出正确剧本却被校验器拒绝

2026-11-26，Harness 用真实 DeepSeek 做三场景首测：C 仅 backlog 的计划通过；A/B 的计划内容正确，却被 planner 拒绝，分别报告 `snapshot_id must be observed in this run's snapshots` 和 `export_directory must be an observed available directory`。

A 的真实首测 plan 由 Harness 提供，序列如下（资源符号展示的是当时模型输出，不是修正后的接口）：

```text
list_manuals
→ read_manual(document_id="workstation-v2")
→ read_resource(resource="catalog")
→ list_snapshots
→ read_snapshot(snapshot_id="$snapshot_id")
→ restore_catalog(snapshot_id="$snapshot_id", expected_sha256="$catalog_sha")
→ rebuild_index(expected_catalog_sha="$restored_sha")
→ read_resource(resource="settings")
→ inspect_storage
→ update_settings(expected_revision="$revision", export_directory="$storage_directory")
→ reload_worker(expected_revision="$revision")
→ read_resource(resource="jobs")
→ retry_job
→ run_self_check
```

模型主动选择了 `workstation-v2`，B 也没有恢复正常的 catalog；错误不在模型是否理解故障，而在接口分类。初版 prompt 允许把 snapshot_id/export_directory 写成运行时符号，校验器却立即要求 facts 中已有相应解析值；首次规划时该键不存在，于是正确方向的剧本也被拒绝。

Harness 提出分类修正，Codex 核对后落地：时序符号用于模型无法预知、执行时才产生或更新的值，如坏目录哈希、恢复后哈希和新 revision；选择值是 facts 已经给出的候选，模型应选择真实 ID，如 snapshot_id、export_directory、document_id。phase0 提供全量候选与 snapshot_details，模型据此做选择，校验器负责验证来源。

修正后真实模型 + 真实 Workstation 端到端结果为 A 14 步、B 7 步、C 3 步，全部四项全绿。让模型统一使用符号看似安全，实际会抽走模型对快照和目录的判断权，恰好丢掉题目最核心的考点「AI 自主选择」。这次纠错使模型决策与执行器安全校验的边界更清楚。

## 4. 候选人亲自核验

我（候选人本人）在浏览器 http://127.0.0.1:15192 完成了以下核验；以下是我亲自动手的结果，不是 AI 代做的验证：

1. 亲眼观察到四项检查（资料读取、全文检索、资料归档、原有任务）由红色失败状态
   变为绿色通过状态，页面显示「各项功能正常，持续自动自检」。
2. 在页面点击「触发一次故障」，观察到系统重新进入恢复流程并自行修复回四项全绿。
3. 下载了归档 ZIP 文件并打开，确认包内文档内容正常可读。

## 5. 最终外层实测与证据边界

2026-11-26，候选人转递 Harness 的优化后真实容器结果：mixed 为 15 次调用 / 4.1 秒，worker 单点为 7 次调用 / 4.1 秒，两轮四项全绿；worker 单点没有 restore_catalog/rebuild_index。与优化前 21 次和 10 次相比，分别减少约 29% 和 30%。这是第 2 节“待复测”之后收到的验收结果，而非当时提前作出的结论。

真实 DeepSeek 独立 Workstation 场景为 A 全红 14 步 / 2.4 秒、B export+backlog 7 步 / 2.1 秒、C backlog 3 步 / 1.6 秒，均四项全绿。fixed 对照初次 13 次、二次 12 次，也均恢复。Codex 的替身模型验证只用于编排接线，不算真实模型能力证据；真实模型与容器数据来自 Harness 外层，浏览器和 ZIP 核验来自候选人本人，三者明确区分。

## 6. 测试覆盖盲区：search 单点故障暴露的校验不一致

### 现象

PR 提交后，候选人在浏览器点击「触发一次故障」做最后确认，随机抽到 `search` 单点注入。结果是 3 / 4 项通过、全文检索为红，右侧「自主修复」显示「等待补充处理」（`blocked`），需要人工点「重新运行修复模块」。

该轮 summary 记录了真实经过：

```
[模型判断] catalog 本身可读且完整，仅检索索引损坏；此前 rebuild_index 失败是因为
缺少 latest restored_sha 参数，而非索引无法重建。
Initially failed=search.
Steps=read_resource(ok); rebuild_index(failed); read_resource(ok); rebuild_index(failed);
run_self_check(ok).
Issues=rebuild_index:invalid_tool_arguments ×2
```

模型判断正确，重规划也按预期触发并重新读取了 catalog，但两次执行都被同一处校验拒绝。

### 根因：三处对同一参数的约定不一致

`inject()` 支持 `catalog` / `search` / `worker` / `mixed` 四种注入。`search` 单点的特殊性在于 catalog 完好，因此本轮不存在 `restored_sha`（只有执行过 `restore_catalog` 才会产生）。

- planner 的 prompt 规则 5 明确写了「若只有 search 损坏而 catalog 通过，先读取 catalog，再用 `$catalog_sha` 重建」，允许这一做法；
- planner 的参数校验放行 `$catalog_sha` 与 `$restored_sha` 两者；
- executor 的 `_validate` 却硬性要求 `expected_catalog_sha == facts["restored_sha"]`。

于是模型按规则产出的合法剧本，在最后一道校验被判定为 `invalid_tool_arguments`。这条路径只有 `search` 单点会走到。

### 为什么前面没发现

此前所有验证都覆盖 `catalog`、`worker`、`mixed` 以及 A/B/C 三场景，唯独没有单独注入 `search`。四类故障里测了三类，剩下那一类恰好是唯一会触发该分支的。这不是运气问题，是测试矩阵少了一格。

### 修复

`executor.py` 的 `_validate` 改 5 行：

```python
elif step.tool == "rebuild_index":
    # 恢复目录后必须用恢复后的 sha；仅索引损坏（catalog 完好）时用当前目录 sha
    expected_sha = report.facts.get("restored_sha") or report.facts.get("catalog_sha")
    if args.get("expected_catalog_sha") != expected_sha:
        raise ValueError("rebuild_index requires the latest catalog sha")
```

语义是：刚恢复过目录（存在 `restored_sha`）时仍必须用恢复后的哈希，避免用过期值重建索引；只有不存在恢复结果时才回退到当前 `catalog_sha`。保留了对过期值的拦截，只补上了缺失的合法路径。

### 修复后验证

真实容器逐一注入三类故障，全部自主恢复：

| 注入类型 | 调用数 | 耗时 | 工具序列 | 结果 |
|---|---:|---:|---|---|
| `search` 单点（原失败场景） | 3 | 2.1s | `read_resource(catalog)` → `rebuild_index` → `run_self_check` | 四项全绿 |
| `catalog` 单点 | 14 | 4.1s | 快照链 + worker 链 + retry | 四项全绿 |
| `mixed` 全坏 | 15 | 4.1s | 手册 + 快照链 + worker 链 + retry | 四项全绿 |

`search` 场景从「3 次调用全部失败、判定 blocked」变为「3 次调用全绿」，`catalog` 与 `mixed` 未受该修改影响。

### 这次暴露的价值

它说明覆盖不足比功能缺陷更危险：缺陷本身只有 5 行，但它藏在一个从未被单独走到的分支里。也说明「模型判断正确、执行器拒绝」这类不一致，往往发生在多个模块各自守约、但约定彼此矛盾的位置 —— 与第 3 节的参数分类错误属于同一类问题，只是那次发生在 planner 与 prompt 之间，这次发生在 executor 与 planner 之间。

## 7. 手册因果性缺陷：从「仪式性调用」到「两阶段决策」

### 问题（由评审方在面试沟通中指出）

初版实现的编排是「单次规划」：模型一次性输出完整剧本，其中包含 `read_manual` 这一步，随后执行器按序照打。评审方指出：这只能证明模型的行为序列正确，不能证明模型给出的修复计划是基于手册的 —— 因为计划在读书之前就已经成型了。

### 缺陷验证：消融实验

为把这一判断变成可检验的结论，做了一组消融实验（ablation）：保持其他输入完全不变，只改写 `seed/manuals/workstation-v2.md` 的内容，观察模型输出是否变化。

第一组消融把手册改写为「资料目录损坏时禁止使用快照恢复，不要调用 restore_catalog」，结果：

| 组别 | 手册内容 | 模型输出的计划 |
|---|---|---|
| 对照组 | 原始手册 | 8 步：`read_manual` → `read_resource(catalog)` → `restore_catalog` → `rebuild_index` → `update_settings` → `reload_worker` → `retry_job` → `run_self_check` |
| 实验组 | 手册明令禁止快照恢复 | **8 步，逐项一致**，仍然执行 `restore_catalog` |

手册写了完全相反的指令，计划一字未变。缺陷被证实：初版中 `read_manual` 的返回值没有进入决策链路。

根因是架构选择：单次规划下模型看不到任何工具的真实返回，phase0 只把手册的标题与 id 放进 facts，从不放原文。因此「使用恢复资料」这一项在实际行为上是空的。

### 重构：两阶段决策

改为两阶段编排：

1. **阶段一（调查）**：模型只规划只读动作（`list_manuals`、`read_manual`、`read_snapshot`、`read_resource`、`inspect_storage`），planner 用 `investigation` 模式校验，只允许只读工具；
2. **执行调查并把真实结果回灌**：`read_manual` 的原文写入 `facts.manual_content`，`read_snapshot` 的 schema_version 与文档数写入 `facts.snapshot_content`；
3. **阶段二（修复）**：模型基于这些真实资料规划修复剧本，prompt 明确要求「如果手册说明与你的经验冲突，以手册为准」。

同时修了三处配套问题：

- `collect_facts` 在 search 失败时也采集快照候选（此前只有 catalog 失败才采，导致模型无法选快照、只能写占位符）；
- planner 的「全红必须读手册」校验适配两阶段：`facts.manual_content` 已存在时，修复阶段无需重复读取；
- **计划校验失败改为回灌重规划**，而不是直接降级 fixed —— 把规则冲突作为错误信息传回模型，允许它自我修正一次。

### 修复验证：同一组消融实验

改用手册能被真正执行的一条要求（「修复前必须先调用 `read_resource(resource="logs")`」），重新做消融：

| 组别 | 模型输出的修复计划（前两步） | 诊断 |
|---|---|---|
| 对照组 | `read_manual` → `read_resource(catalog)` | 未提及手册要求 |
| 实验组 | **`read_resource(logs)`** → `read_resource(catalog)` | 明确写出「2.x 手册要求修复前先读 logs」 |

手册新增一条可执行要求，模型的计划就多了一步，且位置符合手册所说的「修复动作之前」。因果作用成立：**改手册 → 改计划**。

另有一组中间实验值得一提：把手册改为「应优先使用 1.x 快照」。模型读到了这条建议，但在 diagnosis 中写明「1.x 仅 1 份文档、schema_version 为 1，与当前 2.x 故障矛盾」，最终仍选择 2.x 快照。这说明它不只是照搬文字，而是在评估资料与现场事实是否一致 —— 这比无条件服从更能说明资料真的参与了推理。

### 回归验证

两阶段改造后逐一注入故障，全部自主恢复且无降级：

| 注入类型 | 工具调用 | 耗时 | 结果 |
|---|---:|---:|---|
| mixed 全坏 | 21 | 6.0s | 四项全绿 |
| search 单点 | 20 | 6.9s | 四项全绿，计划中无 `restore_catalog` |
| worker 单点 | 21 | 5.8s | 四项全绿 |

代价是工具调用从 15 次左右上升到 20–21 次、耗时从 4 秒上升到 6 秒，仍在 32 次 / 120 秒预算内。

### 这次修正的意义

初版能通过功能验收，但「AI 自主使用恢复资料」这一条在行为上是空的 —— 功能正确掩盖了决策来源的问题。评审方指出后，用消融实验把「是否存在因果作用」变成可测的指标，再据此重构，最后用同一组实验证明修复有效。

这也是本项目里唯一一次由外部评审发现、并通过实验证伪、再重构验证的缺陷，其价值不在于修好了，而在于**建立了一种能判定「AI 是否真的在用资料」的方法**。


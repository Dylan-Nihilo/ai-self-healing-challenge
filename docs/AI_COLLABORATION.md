# AI 协作记录

## 开发方式

本次实现由 Codex 参与完成。Codex 先阅读了 `docs/TASK.md`、`docs/BOUNDARY.md`、`docs/CONTRACTS.md`、固定自检和 `core/workstation.py`，然后实现模型接入、工具 schema、证据采集、有界修复循环和测试。

AI 负责生成和修改允许范围内的代码；可执行结果来自本机实际 Python 测试、真实 `Workstation` 临时数据和前端构建。没有把模型自述当作工作站健康结论。

## 关键判断

### 1. 不写固定修复剧本

最初容易把目录、索引和归档修复写成三个 `if` 分支，再在末尾返回 `changed`。这样虽然能通过已知演练，但不符合 TASK 的核心要求：模型没有真正参与诊断和选择。

最终实现先通过真实只读工具采集事实，再让模型通过 function calling 调用修复工具。模型看到的是实际手册、日志、快照、任务和配置；搜索、快照和归档目标均使用本轮返回的动态值。

### 2. 恢复资料必须先于模型规划

模型不能在未读取 `workstation-v2` 手册时可靠知道目录版本、索引重建顺序和 settings/loaded_settings 的区别。因此每轮先执行固定数量的只读证据采集，包括与 `system_version` 匹配的手册。证据采集不改状态，后续修复完全由模型选择。

### 3. 工具调用被接受不等于修复成功

修复循环记录工具错误并回传给模型，例如错误 snapshot ID 会返回 `snapshot_missing`。模型可以据此重新选择真实 ID。模型要求完成时，代码仍会调用 `run_self_check`；任何一项失败都会拒绝完成，并把检查详情回传给模型继续判断。

### 4. 不用确定性脚本伪装 AI 成功

缺少模型配置、模型不可达、响应格式错误或预算耗尽时，结果为 `blocked`。测试明确验证了缺少配置时不会绕过模型并执行预设修复。

## 纠错与验证

开发过程中发现并修正了以下问题：

1. 测试替身最初直接抛出 `WorkstationError`，真实边界却会通过 HTTP 转为 `ToolError`。测试工具适配层已转换错误，保证 Agent 的纠错路径与运行时一致。
2. Windows 主机默认使用 GBK 读取部分 UTF-8 文件，固定底座在 Docker 的 UTF-8 环境中不受影响。本机测试通过设置 `PYTHONUTF8=1` 运行，未修改固定底座。
3. 最初只覆盖 mixed 和 search 恢复，随后扩展为 mixed 初次恢复，并依次再次注入 catalog、search、worker，验证每类支持故障都能重新恢复。
4. 初始版本只在完成时检查 32 次工具预算，后改为每次工具调用前检查，避免同一模型响应中的多个调用越过限制。

执行结果：

- 固定边界测试 9 项通过。
- 新增 Agent 测试 5 项通过。
- 提交范围测试 6 项通过；符号链接用例因 Windows 账户缺少创建权限未执行。
- `assessment/check_scope.py` 返回 `{"ok": true, "problems": []}`。
- 前端 `npm run build` 通过。

## 人工检查

已人工检查：

- `core/`、`runner/`、`shared/`、`seed/`、前端和验收文件未被修改；
- 结果对象只返回 `changed` 或 `blocked`；
- 工具 token 从未进入模型消息；
- summary 不包含密钥、工具 token 或模型内部推理；
- 再次故障使用不同随机 snapshot、storage 和 job ID，代码中不存在这些实例值；
- 最终完成必须经过固定 `run_self_check`。

## 真实模型验证

2026-11-28 使用真实 DeepSeek `deepseek-chat` 完成了隔离环境演示：

- 初始 mixed 故障用了 20 次工具调用，四项检查全部恢复；
- 再次 catalog 故障用了 24 次调用；
- 再次 search 故障用了 20 次调用；
- 再次 worker 故障用了 26 次调用。

初次恢复时模型先使用旧目录哈希，收到 `resource_changed` 后自行重新读取目录并成功重试。该行为证明工具返回值实际进入了后续模型决策。

模型输出仅保存在本地执行记录和本交付摘要中；提交不包含模型密钥、Codex/GitHub 凭据或工具 token。

# 修复模块接口

入口：`solution.agent.repair(context: RepairContext, tools: ToolClient) -> RepairResult`。

`context` 包含 `system_version`、独立检查器的 `checks`（id、label、ok、detail）和 `previous_summary`。每轮以新进程执行；不要在 repair 函数中启动无限监测循环，持续监测由底座提供。

`RepairResult` 包含 `summary`（简明、可核查的处理说明）和 `outcome`（`changed` 或 `blocked`）。模型返回的状态不直接决定工作站是否健康。`blocked` 表示无法继续，相同失败功能下不会自动重复调用模型；用户可更新实现后重新触发。

## 调用工具

`tools.call(name, **arguments)` 返回 `{"data": ...}`。失败抛出 `ToolError`，包含 `code` 与可读消息。参数严格匹配，不接受额外字段。

- `read_resource(resource)`：resource 为 `catalog`、`settings`、`loaded_settings`、`jobs` 或 `logs`。目录返回原文与 SHA-256；磁盘和运行中配置分别读取。
- `list_manuals()`、`read_manual(document_id)`：恢复资料目录与原文，使用前核对版本。
- `list_snapshots()`、`read_snapshot(snapshot_id)`：目录快照元数据和内容。
- `inspect_storage()`：当前实际存在的归档位置。
- `restore_catalog(snapshot_id, expected_sha256)`：从指定快照恢复目录，需要当前目录哈希。
- `rebuild_index(expected_catalog_sha)`：从当前资料目录与原件生成检索索引。
- `update_settings(expected_revision, export_directory)`：更新磁盘归档配置，增加 revision。
- `reload_worker(expected_revision)`：让实际归档工作进程加载当前配置。
- `retry_job(job_id)`：重试一个失败的原有任务，保留其 ID 和请求；已完成任务不会重复执行。
- `run_self_check()`：执行真实功能检查，返回 checks。

资源名称、快照 ID、任务 ID 和配置版本由当前环境返回，不保证跨环境相同。工具错误、返回结果和实际状态应进入后续决策。不能把工具请求被受理等同于业务完成。

每轮最多 32 次工具调用、120 秒执行时间；工具凭据在该轮结束后失效。模型 provider 与提示组织方式由候选人选择。不要将模型内部推理或真实凭据放入用户可见输出。

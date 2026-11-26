# 交付说明

## 1. 概览

模型根据实时检查结果与工具证据生成最小修复剧本，受限执行器执行，再由独立检查器核验真实功能。

## 2. 环境要求

Docker Desktop、Docker Compose 2.24+；宿主机及 runner 容器可访问配置的模型 API。本文命令在项目根目录执行，Windows 使用 PowerShell。

## 3. 模型配置

在 [DeepSeek 开放平台](https://platform.deepseek.com) 注册、充值并创建 API Key。在项目根目录创建本地 `.env.models`，填写以下配置（示例不包含真实密钥）：

```dotenv
MODEL_PROVIDER=deepseek
MODEL_API_KEY=替换为自己的API密钥
MODEL_BASE_URL=https://api.deepseek.com
MODEL_NAME=deepseek-chat
REPAIR_MODE=model
```

当前只实现并验证 DeepSeek。`REPAIR_MODE=model` 启用模型规划；`REPAIR_MODE=fixed` 使用固定剧本作功能对照；未配置该字段时默认 fixed。
`.env.models` 不应提交，不要在日志、截图或处理说明中暴露密钥。

`env_file` 只在容器创建时读取。修改 `.env.models` 后必须执行：

```powershell
docker compose up -d --force-recreate runner
```

## 4. 启动步骤

创建本地 `.env`：

```dotenv
WEB_PORT=15192
API_PORT=8032
WORKBENCH_TOKEN=local-preview
```

本次 Windows 宿主机的默认前端端口 5192 落在 Hyper-V 保留端口区间 5141–5240，绑定会报 `bind: An attempt was made to access a socket in a way forbidden by its access permissions`，因此使用 15192。该区间是本次宿主机实测结果，不代表所有机器均相同。

```powershell
docker compose up -d --build --wait
```

打开 [工作站页面](http://127.0.0.1:15192)。请保持 core、runner 和 frontend 均为 healthy。

## 5. 演示位置

### 初次恢复

首次使用全新数据卷时基线为故障状态。起栈后约 20 秒内可观察到自动恢复开始（具体时间取决于启动、自检及模型响应）。在页面 events 查看 `list_manuals → read_manual(workstation-v2) → restore_catalog → …` 的调查与修复序列，summary 以 `[模型判断]` 开头，最终四项全绿。
已恢复的数据卷再次启动时会保留健康状态，不会每次都重新制造初始故障。

### 再次恢复

页面点击「触发一次故障」，或在 PowerShell 注入明确的 worker 故障：

```powershell
curl.exe -X POST -H "X-Workbench-Token: local-preview" -H "Content-Type: application/json" http://127.0.0.1:8032/api/drill -d '{"kind":"worker"}'
```

观察恢复过程与最终四项全绿；worker 单点故障下，模型只修归档配置并重试任务，events 不含 `restore_catalog` / `rebuild_index`。原有任务被保留，可下载归档 ZIP 核对文档。

### 固定剧本对照

将 `.env.models` 的 `REPAIR_MODE` 改为 `fixed`，按第 3 节重建 runner 后再次注入同一故障。fixed 用于链路对照与模型不可用时兜底，不作为 AI 自主决策的证据。恢复 `model` 后同样需重建 runner。

## 6. 关键检查命令

状态查询：

```powershell
curl.exe -H "X-Workbench-Token: local-preview" http://127.0.0.1:8032/api/status
```

固定测试（先构建测试镜像，再执行）：

```powershell
docker compose --profile test build tests
docker compose run --rm --no-deps tests
```

越界检查：

```powershell
docker run --rm -v "${PWD}:/candidate:ro" -w /candidate python:3.11-slim-bookworm python assessment/check_scope.py --candidate /candidate
```

scope 检查实测输出为 `{"ok": true, "problems": []}`。上面的固定测试命令是复现入口，不将未附结果的测试描述为已通过。

## 7. 实现说明与取舍

| 模块 | 职责与关键取舍 |
|---|---|
| `solution/providers.py` | 标准库 urllib 调用 DeepSeek，零新增第三方依赖。runner 为 read_only、mem_limit 768m；避免 SDK 拉长构建并增加安装失败面。 |
| `solution/executor.py` | 剧本无关执行内核：调用预算、deadline、幂等守卫、ToolError 分级、运行时 facts 与执行证据。 |
| `solution/planner.py` | prompt 构建、严格 JSON 解析、工具/参数/顺序校验、敏感内容扫描、失败覆盖与最小修复校验。用 replace 占位符，不用 `.format()`，避免 JSON 花括号被当成变量。 |
| `solution/agent.py` | phase0 按失败项采集事实 → 模型规划 → 执行 → fatal 失败回灌一次重规划 → 独立核验 → outcome。缺 key、首次模型调用或计划校验失败时显式降级 fixed，并在 summary 写明原因；重规划仍失败则 blocked。 |

参数分两类：`$catalog_sha`、`$restored_sha`、`$revision` 为执行时产生或更新的时序符号；`$job_ids` 在解析计划时按本轮已观察的 pending jobs 展开为独立 retry 步骤。`snapshot_id`、`export_directory`、`document_id` 则必须是模型从 facts 中挑选的真实 ID，分别校验快照来源、目录 available 与手册来源，不能让执行器替模型选择。

本轮最多 32 次工具调用，agent 使用启动后 90 秒 deadline；剩余不足 20 秒不发起新工具调用。模型单次超时 25 秒；retry 后等候 0.3 秒再核验。跨轮记忆仅通过 previous_summary 传递，不把旧资源值当成当前证据。summary 记录判断、已完成步骤、剩余检查与错误码，不记录凭据或思维链。fixed 降级共享本轮预算和 deadline，不能重新获得额度。

## 8. 已验证结果

以下为真实环境实测，由 DeepSeek Harness 外层执行并经候选人转递；“离线”指独立真实 Workstation + 后台 worker 测试，模型仍为真实 DeepSeek，不是替身模型。

| 场景 | 调用数 | 耗时 | 四项 |
|---|---:|---:|---|
| 容器 mixed 全红 | 15 | 4.1s | 全绿 |
| 容器 worker 单点 | 7 | 4.1s | 全绿，不含 restore_catalog |
| 离线 A 全红 | 14 | 2.4s | 全绿 |
| 离线 B export+backlog | 7 | 2.1s | 全绿，不含 catalog 链 |
| 离线 C backlog | 3 | 1.6s | 全绿 |
| fixed 模式初次 / 二次 | 13 / 12 | — | 全绿 |

优化前容器 mixed 与 worker 分别为 21 次、10 次；复用 phase0 事实后降至 15 次、7 次，调用数分别减少约 29%、30%。候选人本人另已完成浏览器初次恢复、再次注入故障及 ZIP 下载打开核验，详见协作记录第 4 节。

## 9. 未完成 / 未验证项

- 只在 Windows + Docker Desktop 验证，Linux/macOS 未测。
- 模型只用 DeepSeek 验证，Qwen/Kimi/GLM 未适配。
- planner 单次最多 15 步，超复杂或任务过多的场景可能被拒并降级 fixed；受总预算限制，不能保证任意积压规模一轮完成。
- 未做多模型横评。
- 本文未将“最多一次重规划”和模型故障降级的每一种异常路径描述为均已真实容器注入验证。

## 10. 修改边界

本次实现与交付只修改 `solution/**`、`docs/DELIVERY.md`、`docs/AI_COLLABORATION.md`；本地 `.env`、`.env.models` 为运行配置，不随交付提交。不修改核心服务、检查器、seed、runner、前端或固定规则。`check_scope.py` 实测输出 `{"ok": true, "problems": []}`。

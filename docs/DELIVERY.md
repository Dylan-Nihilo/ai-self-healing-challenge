# 交付说明

## 实现概览

`solution.agent.repair` 通过 OpenAI-compatible Chat Completions 接口接入真实模型。每轮先通过只读工具收集恢复手册、目录原文与哈希、磁盘/运行配置、任务、快照、存储目录和日志，再把事实交给模型。模型以 function calling 选择后续工具；工具错误和实际返回会继续进入下一轮决策。

实现不包含固定故障修复脚本。动态值，包括 snapshot ID、catalog SHA-256、job ID、settings revision、export directory 和手册文档 ID，均来自当轮工具返回。模型声称完成时会再次执行 `run_self_check`；未全部通过则拒绝完成并要求模型继续处理。

代码结构：

- `solution/agent.py`：有界模型/工具循环、预算、完成门槛和失败处理。
- `solution/model_client.py`：OpenAI-compatible Chat Completions 客户端。
- `solution/repair_tools.py`：工具 schema 和只读证据采集。
- `tests/solution/test_agent.py`：动态资源、再次故障、工具纠错、模型传输和配置缺失测试。
- `tests/solution/live_demo.py`：在隔离临时工作区执行真实模型的初次恢复与三类再次故障。

## 模型配置

复制示例文件并填入本地密钥，不要提交真实值：

```sh
cp .env.models.example .env.models
```

DeepSeek 示例：

```dotenv
MODEL_PROVIDER=deepseek
MODEL_BASE_URL=https://api.deepseek.com/v1
MODEL_NAME=deepseek-chat
MODEL_API_KEY=<local-secret>
MODEL_TIMEOUT_SECONDS=35
MODEL_MAX_RETRIES=1
```

其他 OpenAI-compatible Chat Completions 服务可设置：

```dotenv
MODEL_PROVIDER=openai-compatible
MODEL_BASE_URL=<provider-base-url>
MODEL_NAME=<provider-model-name>
MODEL_API_KEY=<local-secret>
```

模型必须支持 OpenAI 风格的 `tools` / `tool_calls`。密钥只由 runner 通过 `.env.models` 读取，不发送给模型，也不写入工具日志。

## 启动

```sh
docker compose up -d --build --wait
```

打开 `http://localhost:5192`。初始四项检查应显示真实失败；修复过程和工具调用会显示在事件记录中。功能恢复后可点击“触发一次故障”，分别验证 catalog、search、worker 和 mixed 同类故障。

隔离环境中的真实模型演示：

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
PYTHONUTF8=1 python -m tests.solution.live_demo
```

脚本会自动读取本地 `.env.models`，但不会打印其中的值。也可在 Windows PowerShell 中使用 `.venv\Scripts\Activate.ps1`。该演示使用临时数据目录，不清理 Compose 数据卷。输出包含每一轮初始检查、模型选择的工具及参数、修复返回和最终独立检查。

## 验证结果

本机固定环境中已执行：

```text
tests.test_boundaries                         9 passed
tests.solution.test_agent                     5 passed
tests.test_scope                              6 passed（除 Windows 无符号链接权限的用例）
frontend npm run build                        passed
assessment/check_scope.py                     ok=true, problems=[]
```

`tests/solution/test_agent.py` 使用真实 `Workstation` 和固定工具实现，覆盖：

- 初始 mixed 故障；
- 恢复后再次注入 catalog、search、worker 故障；
- 动态快照、任务、归档目录和配置版本；
- 模型选择错误 ID 后收到工具错误并纠正；
- OpenAI-compatible 模型传输；
- 缺少模型配置时返回 `blocked`，不执行确定性替代修复。

## 真实模型演示

2026-11-28 使用 DeepSeek `deepseek-chat` 在隔离临时工作区实际执行了 `tests.solution.live_demo`，未使用固定修复脚本替代模型决策。

| 场景 | 实际工具调用 | 结果 |
|---|---:|---|
| 初始 mixed 故障 | 20 | 四项检查由失败恢复为全绿 |
| 再次注入 catalog 故障 | 24 | 快照恢复、索引重建、原任务重试后全绿 |
| 再次注入 search 故障 | 20 | 仅重建真实 SQLite 索引后全绿 |
| 再次注入 worker 故障 | 26 | 更新并加载真实归档配置、重试原任务后全绿 |

初次恢复中，模型第一次使用旧目录哈希调用 `restore_catalog`，工具返回 `resource_changed`；模型随后重新读取当前目录哈希并重试成功。这说明工具错误进入了下一轮决策，而不是由固定脚本回放。每轮最终结果都来自固定 `run_self_check`，不是模型返回的 health 字段。

演示还观察到：

- 模型会核对 1.x 与 2.x 快照，拒绝不适用快照；
- search 单点故障时不执行无关目录恢复；
- worker 单点故障时使用 `inspect_storage` 返回的实际目录；
- 已完成的原有任务不重复提交，失败任务按原 `job_id` 重试；
- 四轮调用数保持在 20～26 次，未超过 32 次预算。

完整复现命令为 `python -m tests.solution.live_demo`，输出包含每轮模型选择的工具、参数和独立检查结果。由于本机未安装 Docker Desktop，真实模型演示使用固定的 `Workstation`、`ToolClient` 契约和相同 seed 数据直接运行；Compose 页面操作未在本机执行。

## 未完成与已知限制

- 本机没有 Docker Desktop，所以未在本机执行 Compose 整套构建；无法完成的唯一平台用例是固定测试中的符号链接测试，Windows 当前账户没有创建符号链接权限。
- 当前验证覆盖固定底座支持的 catalog、search、worker 和 mixed 故障，不承诺任意代码、网络或硬件故障。

# 万象硅芯 · 自愈工作站

**这台工作站刚启动时就是坏的。你的任务是给它补上 AI 自修复能力。**

它原本可以浏览资料、搜索正文、生成 ZIP 资料包。初始环境中，目录、索引和归档配置发生了故障，真实业务请求会失败，原有任务也未完成。维护页面和固定自检仍然可用，方便你接手。

在 `solution/agent.py` 中实现 `repair(context, tools)`：接入真实模型，让 AI 自行调查、使用恢复资料、选择工具并修复工作站自己的功能。恢复后，系统会继续自检；再次发生故障时，仍应能自主处理。

[任务书](docs/TASK.pdf) · [修改边界](docs/BOUNDARY.md) · [工具接口](docs/CONTRACTS.md) · [自检如何工作](docs/SELF_CHECKS.md)

## 参与方式

1. Fork 本仓库到你的 GitHub 账号，然后克隆自己的 Fork。
2. 以 [self-healing-starter-v0.1.0](https://github.com/Dylan-Nihilo/ai-self-healing-challenge/tree/self-healing-starter-v0.1.0) 为固定起点创建开发分支，在允许的范围内完成实现。
3. 补齐 `docs/DELIVERY.md` 和 `docs/AI_COLLABORATION.md`，附真实模型参与的初次恢复与再次恢复演示。
4. 向 [Dylan-Nihilo/ai-self-healing-challenge](https://github.com/Dylan-Nihilo/ai-self-healing-challenge) 的 `main` 分支提交 Pull Request，按模板填写候选人编号、基线标签与最终 commit SHA。

请保持 PR 开放，等待招聘方评审；不要提交真实密钥或私人资料。

## 启动并查看题目

本机仅需 Docker 与 Compose 2.24+：

```sh
docker compose up -d --build --wait
```

打开 [自愈工作站](http://localhost:5192)。首屏会直接显示失败功能、代码入口和当前自检结果，不需要新建案件。

Docker 容器显示 healthy 仅表示维护进程存活；业务恢复以页面四项功能核验为准。初始版本出现错误是题目的起点，不能通过删除自检或改健康状态绕过。

## 你修改的代码

```python
def repair(context: RepairContext, tools: ToolClient) -> RepairResult:
    ...
```

`context` 提供当前失败的功能、软件版本和上次说明。通过 `tools.call(...)` 读取日志、配置、恢复手册与快照，使用受限操作修复资源，并检查实际结果。模型 provider、提示词、规划方法与异常处理由你实现。

模型密钥放在本地 `.env.models`，格式由你的实现决定。该文件仅传入 runner，不提交到 Git。新增依赖放在 `solution/requirements.txt`，然后重新执行 Docker 构建。源码通过只读挂载传入，每轮使用新进程加载；保存源码后，下次调用即可生效。

初始 AI 入口明确留空。页面会显示“等待修复模块”，不会演示预设的自动成功。

## 如何观察效果

1. 浏览页面的失败检查，查看任务说明和仓库接口文档。
2. 实现修复模块并配置模型，在页面观察实际工具记录和功能恢复。
3. 用页面搜索、阅读原件、生成并下载 ZIP，核对真实结果。
4. 四项检查通过后，点击“触发一次故障”，观察系统能否再次自行恢复。

自检约每 5 秒运行一次，修复期间继续执行。自动修复每轮最多 120 秒、32 次工具调用，相邻轮次至少间隔 15 秒。无法继续时返回 `blocked`；自检继续运行，相同失败功能不会不断调用模型，更新实现后可点击“重新运行修复模块”。

## 验证与交付

```sh
docker compose --profile test build tests
docker compose run --rm --no-deps tests
docker compose run --rm --no-deps frontend npm run build
docker run --rm -v "$PWD:/candidate:ro" -w /candidate python:3.11-slim-bookworm python assessment/check_scope.py --candidate /candidate
```

提交实现、配置说明、一次真实模型参与的初始恢复与再次恢复演示，以及关键 Claude/Codex 协作记录。固定自检只核实功能与数据；是否真正由模型决策，还需结合代码、实际演示与调用记录评审。确定性脚本或人工修复不能替代 AI 演示。

教师会使用可信基线重新组装允许的改动，在不同资源名称和同类故障环境下检查。未知依赖与代码应在无个人凭据的独立环境中运行；本项目不提供通用恶意代码沙箱。

```sh
docker compose logs -f
docker compose down
```

普通停止与重建保留资料、任务及已完成的修复。不要使用 `down -v` 清除演示和评审数据。

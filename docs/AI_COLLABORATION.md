# AI 协作记录

记录如何使用 Claude/Codex 开发、一次重要判断或纠错、验证方法，以及你亲自检查了哪些结果。不要粘贴未脱敏的私人对话或凭据。

## 2026-11-26：Codex 与 DeepSeek Harness 协作接入模型规划

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

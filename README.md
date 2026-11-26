# 容器检查点迁移与恢复裁定庭

# 项目生成提示词：容器检查点迁移与恢复裁定庭

面向容器运行时迁移团队，实现一个离线可运行的云原生业务控制台。场景：节点迁移中冻结运行态、传输检查点并在目标节点安全恢复单活实例。边界：使用合成内存分片与沙箱状态，不执行真实 CRIU 或容器迁移。

使用 Python 3.11+ 标准库、SQLite、unittest；提供 `python -m checkpointcourt` 单命令 CLI。每个命令以 SQLite IMMEDIATE 事务写入业务状态与不可变原始事件；相同业务身份和相同内容幂等，冲突内容拒绝；回放要从原始事件重建所有业务表并逐表比较。



三个独立命令：`python -m checkpointcourt checkpoint --db state.sqlite --fixture fixtures/checkpoint_normal.json`, `python -m checkpointcourt transfer --db state.sqlite --fixture fixtures/transfer_normal.json`, `python -m checkpointcourt restore --db state.sqlite --fixture fixtures/restore_normal.json`。
回放 `python -m checkpointcourt replay --db state.sqlite`；测试 `python -m unittest discover -s tests -v`。

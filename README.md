# 容器检查点迁移与恢复裁定庭

# 项目生成提示词：容器检查点迁移与恢复裁定庭

面向容器运行时迁移团队，实现一个离线可运行的云原生业务控制台。场景：节点迁移中冻结运行态、传输检查点并在目标节点安全恢复单活实例。边界：使用合成内存分片与沙箱状态，不执行真实 CRIU 或容器迁移。

使用 Python 3.11+ 标准库、SQLite、unittest；提供 `python -m checkpointcourt` 单命令 CLI。每个命令以 SQLite IMMEDIATE 事务写入业务状态与不可变原始事件；相同业务身份和相同内容幂等，冲突内容拒绝；回放要从原始事件重建所有业务表并逐表比较。



三个独立命令：`python -m checkpointcourt checkpoint --db state.sqlite --fixture fixtures/checkpoint_normal.json`, `python -m checkpointcourt transfer --db state.sqlite --fixture fixtures/transfer_normal.json`, `python -m checkpointcourt restore --db state.sqlite --fixture fixtures/restore_normal.json`。
回放 `python -m checkpointcourt replay --db state.sqlite`；测试 `python -m unittest discover -s tests -v`。

### Target copy custody

The transfer team can opt into a target-local checkpoint copy using `copy-open`, `copy-part` and `copy-verify` on the `transfer` command. Each part is compared with the sealed source manifest. The target verifies the complete ordered image from local bytes before the restore module accepts a matching digest. Receipts relate to exactly one plan and target, and are accepted only during its reservation. Legacy plans without a registered copy retain their original behavior. See `fixtures/target-copy.json` for an offline demonstration.

### Bandwidth-constrained transfer windows

Use `transport window-open` to create a timed delivery budget for a target. `transport part-send` charges the transmitted UTF-8 bytes, even when the local receiver rejects an invalid payload. A delivery id is unique and a repeated event returns its original result. The receiver still stores only parts matching the sealed manifest. `fixtures/wire.json` demonstrates a rejected payload, an exhausted window and delivery recovered through a second window. No sockets, devices or live migration are used.

### Source-side capture comparison

`memorymap capture-read` interprets a sealed checkpoint as an array of simulated memory spans with region, byte offset and hexadecimal payload. It rejects overlapping ranges instead of hiding bytes under the latest observation. `forensics capture-compare` records changed or absent bytes between two ordered captures of the same process identity. It is an audit tool and does not activate an instance. See `fixtures/forensics.json`.

### Offline archive repair

`archive stripe-seal` creates a padded XOR parity stripe from a sealed local checkpoint plus exact lengths and digests for its ordered members. An independent archive intake receives members using `archive member-receive`. When exactly one member is missing, `archive stripe-repair` reconstructs its bytes from parity and the members present in the archive, then verifies and records the recovered member. It leaves checkpoint, transfer and restore state untouched. Run `fixtures/archive.json` to exercise incomplete and repaired archives in the local simulator.

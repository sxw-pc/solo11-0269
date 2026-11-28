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

### Source heap census

`heap catalog-load` decodes a contiguous UTF-8 heap catalog in the `heap-catalog` region of a sealed memory capture, checks every root and strong edge, and records the exact object catalog. `heap heap-census` traverses the synthetic source heap from the roots and reports reachable and unreachable objects and byte sums. It counts shared nodes and cycles once. There is no connection to a running process. See `fixtures/heap.json`.

### Source page rounds

`journal page-source` opens a synthetic source with fixed-width blank pages. A `journal page-commit` publishes a consecutive tick and changes one or more pages atomically. `precopy round-export` records a full image at the current tick or a delta against a previously exported round of the same source. It returns the actual dirty bytes that differ at the two committed cuts. Run `fixtures/precopy.json` for a full round, an empty delta and a two-page delta.

### Masked-slip inbox recovery

`mailcheck inbox-recover-masked` rebuilds a pending queue from offline evidence only: an ordered send manifest (sender, receiver, work-order generation, sender cut position, and the sent items with instruction and batch metadata), the receiver cut position as an arrival count, and the fully collected masked slips with their candidate job ids. It never reads `mail_items`, arrival rows or the event log, and `mailroom ledger-export` produces exactly this send-side evidence without leaking arrivals. A slip count that differs from the receiver cut position is reported as incomplete evidence; candidates pointing at another port, beyond the send cut or into another work-order generation are rejected before any box is built; and when no one-to-one assignment of slips to sent jobs exists the recovery fails instead of inventing an empty inbox.

Every legal slip assignment leaves one ordered undelivered queue over the same manifest. The recovery persists only the longest contiguous prefix shared by all of those queues under the contract's view, computed by matching analysis rather than by enumerating assignments. The `named` contract views each job by its original id and holds when the head is ambiguous. The `process` contract views jobs in authorized side-effect-free batches by process batch and instruction, so interchangeable `flush` jobs deliver as one batch receipt recording only batch, instruction and order — never a possibly-delivered original job id. `inbox-deliver-masked` consumes one proven position per durable commit, is idempotent across lost answers and process restarts, and answers `inbox_held` rather than `inbox_empty` while undetermined mail remains. See `fixtures/mailmasked.json`.

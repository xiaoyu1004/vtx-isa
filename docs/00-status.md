# VTX-1 ISA 1.0 Draft：文档状态

## 1. 这份草案是什么

本文定义 **VTX-1 ISA 1.0 Draft**。ISA（指令集架构）是软件和处理器共同遵守的规则：软件按这些规则生成指令，处理器按这些规则执行指令。

“Draft（草案）”表示内容还在评审，字段和语义在正式冻结前仍可修改。一个实现只有完整满足本草案写明的强制要求，才可以声明“符合 VTX-1 ISA 1.0 Draft”。

本草案直接定义一套新的 1.0 架构。本文不定义任何其他版本的装载、转换或混用规则。

## 2. 哪些文件说了算

本草案的架构核心由下面四个文件组成：

- `docs/00-status.md`：说明文档状态、规范边界和要求用词；
- `docs/01-terms-and-notation.md`：统一术语、掩码和伪代码写法；
- `docs/02-programming-model.md`：定义内核描述符、参数、寄存器、资源和故障；
- `docs/03-execution-model.md`：定义 scalar、vector、warp-control 和分歧重汇聚的执行规则。

这四个文件对同一件事只能有一个定义。分工如下：

- 名词是什么意思，以 `01-terms-and-notation.md` 为准；
- 软件能看到哪些状态、启动时怎样分配资源，以 `02-programming-model.md` 为准；
- 一条指令在什么状态下能执行、执行后怎样改状态，以 `03-execution-model.md` 为准。

`isa/vtx1/isa.yaml` 是逐条指令元数据的权威位置：`faults` 字段定义故障码和名称，`fault_priority` 字段单独定义故障优先级，form 中的 `execution_domain` 和 `required_state` 定义执行域及额外入口状态。`docs/02-programming-model.md` 必须逐字抄写 `fault_priority`，`docs/03-execution-model.md` 只能引用，不能另排顺序。七个合法 `execution_domain` 值也以 YAML 为权威。

这四个文件是架构状态和执行规则的权威位置。YAML 不得另写一套状态机。若 YAML 和文档冲突，规范发布必须停止，先把两边改一致，工具和实现不能自行挑一边继续。

未列入本节的材料不构成本 Draft 的架构核心要求，也不能用来填补这里没有写清的行为。

## 3. 要求用词

下列词具有固定含义：

- **必须（MUST）**：所有符合实现都要这样做；
- **禁止（MUST NOT）**：所有符合实现都不能这样做；
- **应当（SHOULD）**：通常要这样做；不这样做时，实现者必须有明确理由；
- **不应当（SHOULD NOT）**：通常不要这样做；这样做时，实现者必须有明确理由；
- **可以（MAY）**：实现可以自行选择。

没有使用这些词的说明文字用于帮助理解；它不能推翻明确的强制规则。

## 4. 架构固定点

VTX-1 ISA 1.0 Draft 固定以下基本决定：

1. 一个 warp（线程束）有 32 个 lane（通道），共用一个程序计数器和一套隐藏的重汇聚栈。
2. 架构上，每个 warp 有 `s0..s255`、`vp0..vp15`、`SCC`、只读 `EXEC` 和只读 `LIVE`；每个 lane 有自己的 `v0..v255`。
3. SGPR（每 warp 一份的标量寄存器）和 VGPR（每 lane 一份的向量寄存器）的物理存储位于 SM/CU。SM/CU 是实际接收并执行 CTA 的计算单元。每个驻留 warp 从这些物理寄存器中分到自己的切片。
4. 每个 form（具体编码形式）都必须标明 `execution_domain`（执行域），取值只能是 `system`（系统操作）、`scalar`（每 warp 一次）、`vector`（每 lane 执行）、`warp_control`（warp 控制）、`warp_collective`（warp 集合操作）、`cta_sync`（CTA 同步）、`warp_matrix`（warp 矩阵操作）。
5. 机器码中的 8 个 class 只是编码分类：`SYS`、`SALU`、`VALU`、`MEMORY`、`CONTROL`、`SYNC`、`CROSSLANE`、`MATRIX`。class 不直接决定怎样执行；例如 `MEMORY` class 里面既有 `scalar` form，也有 `vector` form。
6. 所有 `execution_domain=scalar` 的指令每个 warp 只执行一次，而且必须先满足 scalar-ready（标量就绪）。scalar-ready 要求 `active_mask == live_mask`、`live_mask` 非空，并且没有处于 `FIRST` 或 `SECOND` 的未完成分歧帧。`active_mask` 是当前执行路径的 lane，`live_mask` 是尚未退出的 lane。
7. `S_ALU`（普通标量整数和逻辑运算）、`S_FP`（标量浮点）、`S_GETREG`（读取统一特殊寄存器）、`SMEM`（标量访存）、`SATOM`（标量原子操作）、`S_READFIRST`（读取最低编号 active lane）都属于 `execution_domain=scalar`，没有例外。
8. `execution_domain=vector` 的指令只对当前 active lane 执行，并可读取 SGPR，把同一个 SGPR 值广播给所有参与 lane。vector 指令不要求 scalar-ready。
9. `CALL`（直接调用）、`CALL.IND`（从 SGPR 取目标的间接调用）、`JUMP.IND`（从 SGPR 取目标的间接跳转）、`RET`（返回）都是 `execution_domain=warp_control`，但因为它们使用每 warp 一套的统一调用栈或统一间接目标，所以必须额外满足 scalar-ready。直接 `BRA` 和 `BRA.P` 不要求 scalar-ready。
10. 软件不能写 `EXEC`。分歧、重汇聚和退出只能由 `SSY`、`BRA.P`、`JOIN`、`EXIT` 及其隐藏状态机处理。
11. 一个 CTA（线程块）必须整体驻留在同一个 SM/CU 上；CTA 内各 warp 可以独立调度。
12. 每个 CTA 固定有 8 个命名屏障槽 `0..7`。owner 的唯一身份是 CTA 内 `linear_tid = warp_id*32 + lane_id`；owner 集固定为启动时全部真实线程，不含尾部不存在 lane，且不因 `EXIT` 缩小。每槽 generation 是数学上的非负整数 `N`，从 0 开始，每次退休严格加 1，单调且永不回绕。
13. 三条规范屏障指令只叫 `BAR.SYNC.CTA id`、`BAR.ARRIVE.CTA vd,id`、`BAR.WAIT.CTA id,vs`。split token 是每 lane 的 VGPR32 值并带隐藏有效标签；标签恰好绑定 `{CTA identity, linear_tid, slot, logical generation}`，不能靠伪造 32 位数值冒充，也不能在后续代复活。
14. 同一 generation 只能选择 `SYNC` 或 `SPLIT` 一种模式。任一 active lane 的屏障协议错误都使整条 warp 动态指令零效果回滚；没有子集屏障，也没有 `expected` 字段。
15. BAR 阻塞整个 warp 的当前动态路径；一个 waiter 固定保存 `{warp_id, owner_snapshot, resume_pc}`。阻塞和恢复都不改 active/live 掩码、重汇聚栈或调用栈，挂起路径不能趁机切入，每 warp 同时至多一条 blocked record。
16. 任意参与 lane 的任意 VGPR32 槽写入默认清除旧 barrier-token 标签。唯二例外是 `BAR.ARRIVE.CTA` 创建新标签，以及寄存器型 `V_MOV.B32` 复制源标签。

这些固定点的完整定义分别见 `02-programming-model.md` 和 `03-execution-model.md`。

## 5. 符合性底线

符合实现必须做到：

- 在 CTA 开始执行前完成描述符、文本、参数和资源校验；
- 保持 `s`、`v`、`vp`、`SCC`、`EXEC`、`LIVE` 的可观察行为与本草案一致；
- 对任何不满足 scalar-ready 的 `execution_domain: scalar` form 准确报告 `SCALAR_STATE_FAULT`；
- `CALL`、`CALL.IND`、`JUMP.IND`、`RET` 的 `required_state: scalar_ready` 不满足时，同样报告 `SCALAR_STATE_FAULT`，同时保持它们的 `warp_control` 分类；
- 只通过规定的 warp-control 状态机改变 active lane 集和 live lane 集；
- 在一条指令故障时，不提交该指令的任何部分效果；
- 对命名屏障执行固定 owner、单 generation、模式隔离、逐 lane token 和恰好一次消费规则；`EXIT` 不得丢弃尚未消费的 split token；
- 用槽内 `arrived_set-consumed_set` 判断 EXIT 的 SPLIT 消费义务，不能靠扫描 VGPR 标签猜测；
- 让有限内部计数器表现得像 generation 永不回绕；任何旧、已消费或复制 token 都禁止因物理计数值复用而重新匹配；
- 只有全部 warp 完成且 8 个槽都处于 IDLE 状态时才完成 CTA；generation 计数值本身不妨碍完成；
- 只把成功 BAR 的 release/acquire 用于 CTA shared memory；global、local、param、const 和 host 不因 BAR 自动得到顺序；
- 不把调度顺序、物理寄存器编号、缓存行为或实际执行周期暴露成未定义的架构承诺。

实现可以采用不同的流水线、寄存器文件组织和调度算法，只要软件看到的结果与本草案完全一致。

# VTX-1 ISA 1.0 Draft：执行模型

## 1. 一句话理解执行方式

VTX-1 使用 SIMT（单条指令管理多个线程）方式执行。一个 warp（线程束）只有一个 PC（程序计数器）；`EXEC`（当前执行掩码）中为 1 的 lane（通道）在这个 PC 上一起执行同一条指令。

SGPR 是每 warp 一份的 32 位寄存器，VGPR 是每 lane 一份的 32 位寄存器，`vp0..vp15` 是每 warp 一份的 lane 掩码寄存器，SCC 是每 warp 一份的 1 位条件码。CTA（线程块）是一组能共享内存并使用屏障同步的线程。

不同 warp 独立调度。实现可以让多个 warp 同时前进，也可以交错发射，但每个 warp 自己看到的指令顺序必须符合本文。

每个指令 form 都带 `execution_domain`（执行域），取值只能是：

- `system`：系统控制、陷阱和杂项；
- `scalar`：每个 warp 执行一次；
- `vector`：每个参与 lane 各执行一次；
- `warp_control`：管理 PC、当前路径、重汇聚栈或调用栈；
- `warp_collective`：一个 warp 的多个 lane 合作；
- `cta_sync`：一个 CTA 内的线程同步；
- `warp_matrix`：一个 warp 合作做矩阵运算。

机器码的 `SYS/SALU/VALU/MEMORY/CONTROL/SYNC/CROSSLANE/MATRIX` 是 8 个编码 class，不是执行域。尤其是 `MEMORY` class：标量访存 form 仍按 `scalar` 执行，逐 lane 访存 form 仍按 `vector` 执行。

## 2. 每条动态指令的共同步骤

除明确写成阻塞式两阶段操作的指令外，一条动态指令按下面的逻辑执行：

```text
word = fetch64(PC)
I = decode_and_validate_static(word)
E = snapshot(active_mask)
L = snapshot(live_mask)

validate_execution_domain_state(I, E, L)
sources = snapshot_required_sources(I, E)
effects = evaluate_and_validate(I, E, L, sources)

if any check failed:
    fault(select_fault_by_priority)
else:
    commit(effects)
```

顺序含义如下：

1. 先检查机器码和静态操作数，所以非法编码不能被 guard 掩盖；
2. 再保存入口 `EXEC` 和 `LIVE`；
3. 再按 `execution_domain` 检查当前 warp 状态是否合法；
4. 所有需要的源值先读完；
5. 所有结果和故障先算完；
6. 最后一次性提交。

若指令故障，该指令不得留下部分 SGPR、VGPR、`vp0..vp15`、SCC、内存、PC、`EXEC`、`LIVE` 或栈更新。

YAML 必须给每个 `execution_domain: scalar` form 写 `required_state: scalar_ready`，也必须给 `CALL`、`CALL.IND`、`JUMP.IND`、`RET` 写同一个 `required_state`。执行时按这个字段检查：

```text
if I.required_state == scalar_ready:
    require scalar_ready()
else:
    no scalar-ready check
```

这里的额外检查不会把 `CALL`、`CALL.IND`、`JUMP.IND`、`RET` 改成 `scalar`；它们的 `execution_domain` 仍是 `warp_control`。直接 `BRA` 和 `BRA.P` 不做 scalar-ready 检查。

同一动态指令发现多个故障时，只使用 YAML 的 `fault_priority`；`02-programming-model.md` 第 9 节逐字抄写了该字段。本文件不从 `faults` 列表或故障码另排第二套顺序。

普通非控制指令成功后，若该指令没有另行规定 PC 效果：

```text
PC = old_PC + 8
```

## 3. `execution_domain=scalar`

### 3.1 所有 scalar 执行域都一样检查

`execution_domain=scalar` 的指令每个 warp 只执行一次，不会因为 warp 有 32 个 active lane 就执行 32 次。

下面这些常见家族全部必须标成 `execution_domain=scalar`，并在读取动态源之前检查 scalar-ready：

- `S_ALU`：SGPR 整数、位运算、比较等普通标量运算；
- `S_FP`：SGPR 浮点运算；
- `S_GETREG`：读取 warp 或 CTA 一致的特殊寄存器；
- `SMEM`：用统一地址做一次标量内存访问；
- `SATOM`：用统一地址做一次标量原子操作；
- `S_READFIRST`：从当前最低编号 active lane 的 VGPR 取一个值，写入 SGPR。

这张表不是举例，而是硬规则：这些家族的所有 form（具体编码形式）都要先满足 scalar-ready。不能因为 `SMEM` 或 `SATOM` 属于访存，也不能因为 `S_READFIRST` 会读取一个 VGPR，就跳过检查。

除 `S_READFIRST` 这类明确写出取 lane 规则的指令外，`execution_domain=scalar` 的指令只能读取该指令允许的 SGPR、`SCC` 和 warp/CTA 一致的只读信息。它不能随便读取某个 `vN` 来猜一个代表 lane，也不能读取 `LANE_ID`、逐线程坐标等每 lane 不同的值。

### 3.2 Scalar 合法条件

每条 `execution_domain=scalar` 指令在读取动态源之前，必须检查下面三个条件：

```text
live_mask != 0
active_mask == live_mask
reconv_stack 中不存在 phase 为 FIRST 或 SECOND 的帧
```

三个条件必须同时成立。

大白话解释：

- warp 里至少还有一个活线程；
- 所有活线程此刻都在同一条路径上；
- 栈里没有一条分支路径还没跑完。

这三个条件合起来就叫 `scalar_ready()`。只看 `active_mask == live_mask` 不够。某些控制流在中间时刻可能碰巧让两个掩码相等，但只要栈里仍有 `FIRST` 或 `SECOND` 帧，scalar 就不安全。

任一条件不满足时：

```text
fault(
    code = SCALAR_STATE_FAULT,
    lane_mask = active_mask,
    aux = 0)
```

故障指令不读动态源，也不改任何状态。

### 3.3 ARMED 帧不妨碍 scalar

`ARMED` 帧只表示 `SSY` 已经预约未来的汇合点，还没有真正拆成两条路径。只要另外两个条件也满足，只有 `ARMED` 帧时 `execution_domain=scalar` 的指令合法。

### 3.4 SCC 不是通用开关

SCC 只有 1 位，但它不会自动控制所有 `execution_domain=scalar` 指令。只有某个 form 明确把 SCC 列为源操作数时，该指令才读取 SCC，并按自己的逐条语义使用这个值。

因此，规范不能泛化出“只要 SCC 为假，任意 S 指令就不执行”这条规则。没有显式 SCC 源的指令完全不看 SCC；有显式 SCC 源的指令也必须先通过 scalar-ready 和静态操作数检查。

`execution_domain=scalar` 的指令不使用 `vpN` 作为 lane guard，因为它本来就不是逐 lane 执行。

## 4. `execution_domain=vector`

### 4.1 参与 lane

`execution_domain=vector` 的指令不检查 scalar-ready。它开始时先保存：

```text
E = snapshot(active_mask)
```

没有 guard 的 vector 指令：

```text
P = E
```

带 `@vpN` guard 的 vector 指令：

```text
P = E & snapshot(vpN)
```

带 `@!vpN` guard 的 vector 指令：

```text
P = E & ~snapshot(vpN)
```

`P` 是 participating mask，也就是实际执行数据操作的 lane。不存在 lane、已经退出的 lane、挂起分支中的 lane 都不会进入 `P`。

### 4.2 SGPR 广播

vector 指令可以把 SGPR 当作统一源读取：

```text
for each lane i in P:
    vector_source[i] = vN[i]        # 每 lane 自己的值
    scalar_source[i] = sM           # 所有 lane 得到同一个值
```

这就是 SGPR 广播。广播不会复制或改写 SGPR，只是在该动态指令里让各参与 lane 看到同一个源值。

vector 指令读取 SGPR 不需要满足 scalar-ready，因为它的 `execution_domain` 仍是 `vector`。它只把 SGPR 当作只读统一输入。

### 4.3 Vector 写回

写 VGPR 时，只改 `P` 中的 lane：

```text
for each lane i in P:
    vD[i] = result[i]
for each lane i not in P:
    vD[i] remains unchanged
```

写 `vpN` 时，只改 `P` 对应的位：

```text
vpD = (old_vpD & ~P) | (result_bits & P)
```

普通 vector ALU 指令不能写 SGPR 或 `SCC`。需要产生每 warp 单一结果的归约或投票指令属于明确列出的 warp collective（线程束集合操作），必须按该指令自己的会合规则执行。

### 4.4 Vector 访存和故障

vector 访存只为 `P` 中的 lane 形成地址。`E-P` 中的 lane：

- 不读取 VGPR 地址源；
- 不检查对齐或范围；
- 不访问内存；
- 不产生 lane 局部访存故障；
- 不写目标。

若 `P` 中任何 lane 产生会让整条指令故障的错误，整条 warp 动态指令回滚。故障记录的 `lane_mask` 标出检测到该错误的 lane。

`P` 为空时，合法 vector 指令不读动态源、不写数据，并正常前进到下一条。机器码和静态操作数仍然必须合法。

## 5. `execution_domain=warp_control`

`warp_control` 直接管理 warp，不属于 scalar ALU。`SSY`、`BRA`、`BRA.P`、`JOIN`、`EXIT` 不检查 scalar-ready；它们必须能在分歧路径中把控制流走完。

`CALL`、`CALL.IND`、`JUMP.IND`、`RET` 也属于 `warp_control`，并明确写有 `required_state: scalar_ready`。检查失败就报告 `SCALAR_STATE_FAULT`，不读取间接目标或调用栈，也不改 PC。

直接 `BRA` 和 `BRA.P` 明确不套这条额外规则。不能因为它们也会改 PC，就把它们误当成调用指令。

### 5.1 Warp 统一调用栈

每个 warp 有一套隐藏调用栈，实际深度限制是 descriptor 的 `call_stack_depth`，并且该字段不能超过架构最大值 16。每个调用帧只保存：

```text
CallFrame {
    return_pc: U64
}
```

调用帧不保存 `EXEC`、`LIVE`、SCC、SGPR、VGPR、`vpN` 或重汇聚状态。之所以可以这么简单，是因为调用前必须 scalar-ready：所有 live lane 在同一路径上，而且没有 `FIRST` 或 `SECOND` 未完成分歧帧。

scalar-ready 不禁止 `ARMED` 帧，所以 CALL 允许调用者留下尚未发生分歧的 `ARMED` 帧。它们仍在重汇聚栈中，归调用者所有；CALL 不把它们复制进调用帧，也不弹出它们。

### 5.2 CALL 和 CALL.IND

`CALL target` 使用指令里的直接目标，`CALL.IND source` 从明确编码的 SGPR 源取得统一目标。两者都按下面的规则执行：

```text
require scalar_ready()
require target 是文本内对齐的合法 PC
require old_PC + 8 是文本内合法返回 PC
require call_stack.depth < descriptor.call_stack_depth

push CallFrame(return_pc = old_PC + 8)
PC = target
```

目标或返回 PC 非法时报告 `ILLEGAL_OPERAND`。调用栈已经达到 `call_stack_depth` 时报告 `RECONVERGENCE_FAULT`，整条 CALL 不压入半个帧，也不跳转。

### 5.3 JUMP.IND

`JUMP.IND source` 从明确编码的 SGPR 源读取统一目标，不压入也不弹出调用栈：

```text
require scalar_ready()
require target 是文本内对齐的合法 PC
PC = target
```

目标非法报告 `ILLEGAL_OPERAND`。`JUMP.IND` 仍是 `warp_control`，scalar-ready 只是它的额外入口条件。

### 5.4 RET

`RET` 按下面的规则返回：

```text
require scalar_ready()
require call_stack is not empty
require reconv_stack 中不存在 owner_call_depth == call_stack.depth 的帧

R = top.return_pc
pop call_stack
PC = R
```

callee（被调用代码）可以在内部使用 `SSY/BRA.P/JOIN`，但必须在执行 `RET` 前闭合自己建立的所有 `SSY` 帧。调用者在 CALL 前已经存在的 `ARMED` 帧可以保留，RET 不弹它们。

空调用栈执行 `RET`、CALL 超过 `call_stack_depth`，或 RET 时 callee 仍留有自己的重汇聚帧，都报告 `RECONVERGENCE_FAULT`。调用栈和 PC 保持原样。

普通 MOV、逻辑运算或恢复指令都不能写 `EXEC` 或 `LIVE`。只有本文件规定的 warp-control 状态机能改变它们对应的内部掩码。

## 6. 隐藏重汇聚栈

### 6.1 帧内容

每个 warp 有一套软件不能直接读写的后进先出栈。最大硬上限是 16 帧，实际允许深度由 descriptor 的 `reconv_stack_depth` 声明。

每帧包含：

```text
Frame {
    reconv_pc: U64,       # JOIN 所在 PC
    entry_mask: U32,      # 进入该控制区域的仍存活 lane
    pending_pc: U64,      # 尚未执行路径的起点
    pending_mask: U32,    # 尚未执行路径的 lane
    arrived_mask: U32,    # 已在 JOIN 等待的 lane
    owner_call_depth: U8, # SSY 执行时的调用栈深度
    phase: ARMED | FIRST | SECOND
}
```

`owner_call_depth` 只用来确认 RET 前 callee 自己建立的帧都已闭合。它属于重汇聚帧，不属于调用帧；调用帧仍然只保存 `return_pc`。

始终必须满足：

```text
active_mask & ~live_mask == 0
frame.entry_mask & ~live_mask == 0
frame.pending_mask & ~live_mask == 0
frame.arrived_mask & ~live_mask == 0
frame.pending_mask & frame.arrived_mask == 0
```

`EXIT` 会从 `live_mask` 以及所有相关栈掩码中一起删除退出 lane，所以退出 lane 永远不会被重汇聚“复活”。

### 6.2 SSY

执行 `SSY R` 时：

```text
require R 是合法且对齐的指令地址
require R 上的静态指令是 JOIN
require stack.depth < descriptor.reconv_stack_depth

push Frame(
    reconv_pc = R,
    entry_mask = active_mask,
    pending_pc = 0,
    pending_mask = 0,
    arrived_mask = 0,
    owner_call_depth = call_stack.depth,
    phase = ARMED)

PC = old_PC + 8
```

目标非法时报告 `ILLEGAL_OPERAND`；超过声明深度时报告 `RECONVERGENCE_FAULT`。`SSY` 本身不改变 `EXEC`。

### 6.3 BRA

`BRA target` 让整个当前 `active_mask` 跳到同一目标：

```text
require target 是合法且对齐的指令地址
PC = target
```

它不改变 `active_mask`、`live_mask` 或栈。静态控制流验证必须保证它不会非法跳出未完成的 `SSY..JOIN` 区域。

### 6.4 BRA.P

`BRA.P condition, target` 的 condition 是逐 lane 条件，可以来自 `vpN`、`!vpN` 或指令清单定义的等价 lane 条件。它不是 scalar guard。

先计算：

```text
E = active_mask
T = E & evaluate_lane_condition(condition)
F = E & ~evaluate_lane_condition(condition)
```

若所有 active lane 走顺序路径：

```text
if T == 0:
    active_mask = F
    PC = old_PC + 8
```

若所有 active lane 都跳转：

```text
if F == 0:
    active_mask = T
    PC = target
```

这两种都叫 **uniform branch（统一分支）**，不会消耗 `ARMED` 帧。

若 `T` 和 `F` 都非空，发生分歧：

```text
require stack is not empty
require top.phase == ARMED
require top.entry_mask == E

top.pending_pc = old_PC + 8
top.pending_mask = F
top.arrived_mask = 0
top.phase = FIRST

active_mask = T
PC = target
```

VTX-1 固定先执行条件为真的跳转路径，再执行条件为假的顺序路径。软件不能假设两条路径并行前进。

分歧时没有匹配的 `ARMED` 栈顶，或 `entry_mask` 不等于当前 `E`，报告 `RECONVERGENCE_FAULT`。

### 6.5 JOIN

`JOIN` 必须在栈顶帧的 `reconv_pc` 上执行。

若帧仍是 `ARMED`，说明控制区域内没有真正发生分歧：

```text
require active_mask == top.entry_mask
pop top
PC = old_PC + 8
```

若帧是 `FIRST`，第一条路径已经到达汇合点：

```text
top.arrived_mask = active_mask
top.phase = SECOND
active_mask = top.pending_mask & live_mask
PC = top.pending_pc
top.pending_mask = 0
```

此时第一条路径的 lane 暂停，开始执行第二条路径。

若帧是 `SECOND`，第二条路径也到了：

```text
A = top.arrived_mask | active_mask
require A == top.entry_mask
pop top
active_mask = A & live_mask
PC = old_PC + 8
```

空栈、PC 不匹配、phase 不合法或掩码对不上，都报告 `RECONVERGENCE_FAULT`。

### 6.6 EXIT

令 `X` 为这次 `EXIT` 要退出的 lane：

- 无条件 `EXIT`：`X = active_mask`；
- 带显式 lane 条件的 `EXIT`：`X = active_mask & condition_mask`。

提交前先把每个 `lane∈X` 转成 `linear_tid=warp_id*32+lane_id`，然后检查全部 8 个槽：

```text
for tid in exiting_linear_tids:
    require 不存在槽 S 满足：
        S.mode == SPLIT
        and tid in (S.arrived_set - S.consumed_set)
```

任一槽命中就使整条 `EXIT` 报告 `BARRIER_FAULT`：所有 lane 都不能退出，`LIVE/EXEC`、栈、槽、VGPR 标签和 PC 全部保持入口状态。这个义务只由槽状态决定，与 VGPR 中是否还保存 token 标签无关：token 即使被覆盖或丢失，未消费义务仍在；已经消费后留下的 stale 标签不阻止 EXIT。

成功提交时：

```text
live_mask   = live_mask & ~X
active_mask = active_mask & ~X

for each frame f:
    f.entry_mask   &= ~X
    f.pending_mask &= ~X
    f.arrived_mask &= ~X
```

若还有 active lane，它们从 `old_PC + 8` 继续。若当前路径已经空了，必须执行下面的正规化，直到找到另一条非空路径或 warp 完成：

```text
while active_mask == 0:
    if stack is empty:
        require live_mask == 0
        complete warp
        stop

    f = top

    if f.phase == ARMED:
        require f.entry_mask == 0
        pop top
        continue

    if f.phase == FIRST:
        f.phase = SECOND
        active_mask = f.pending_mask & live_mask
        PC = f.pending_pc
        f.pending_mask = 0
        if active_mask != 0:
            stop
        continue

    if f.phase == SECOND:
        active_mask = f.arrived_mask & live_mask
        PC = f.reconv_pc + 8
        pop top
        if active_mask != 0:
            stop
        continue
```

因此，第一条路径全部 `EXIT` 后，第二条路径仍会执行；第二条路径全部 `EXIT` 后，已经到达 `JOIN` 的 lane 仍会恢复。已经退出的 lane 永远不会回来。

## 7. 为什么分歧区里不能跑 scalar 执行域

进入 `FIRST` 后，只有第一条路径的 lane active；进入 `SECOND` 后，只有第二条路径的 lane active。若这时运行 scalar，两个路径可能对同一个 SGPR 给出互相覆盖的结果，也可能让后执行路径看到前一路留下的临时值。

VTX-1 不让软件猜这种行为。只要有 `FIRST` 或 `SECOND` 未完成帧，任何 `execution_domain=scalar` 的指令都直接报告 `SCALAR_STATE_FAULT`。这包括 `S_ALU`、`S_FP`、`S_GETREG`、`SMEM`、`SATOM`、`S_READFIRST`，不能只限制普通算术。编译器必须把这些指令放在分歧前或完成 `JOIN` 后；若确实需要逐 lane 计算，就使用 vector 指令。

普通 `warp_control` 指令是例外，因为没有它就无法走完分支并回到 `JOIN`。`CALL`、`CALL.IND`、`JUMP.IND`、`RET` 仍要检查 scalar-ready。`vector` 也是合法的，因为它只改当前 participating lane 的 VGPR 或 `vpN` 状态。

## 8. 结构化控制流要求

内核发布前，验证器必须检查每个 `SSY R`：

1. `R` 指向该 `SSY` 之后的一条 `JOIN`；
2. 从 `SSY` 后进入的每条有限路径，要么执行 `EXIT`，要么到达该 `JOIN`；
3. 区域内不能跳到 `R` 之后、另一个不匹配的 `JOIN` 或文本外；
4. 嵌套 `SSY..JOIN` 区间只能完整包含，不能交叉；
5. 同时嵌套的最大帧数不超过 `reconv_stack_depth`。

对直接可见的调用边，验证器还必须检查 callee 内建立的每个 `SSY` 都在该 callee 的 `RET` 前由匹配 `JOIN` 闭合，不能把 callee 的重汇聚帧带回调用者。间接调用无法完全静态确定目标，所以 RET 仍要用 `owner_call_depth` 做运行时检查。

不满足时，启动前报 `INVALID_CONTROL_FLOW`。静态检查不能替代运行时检查；实际执行 `BRA.P`、`JOIN`、`CALL` 或 `RET` 时，仍要核对对应栈状态。

## 9. Warp 集合操作

warp collective（线程束集合操作）会读取多个 lane 的值，并产生一个或多个 lane 的结果，例如投票、ballot（把真假收成位图）或 shuffle（跨 lane 取值）。

每条 collective 必须明确：

- 候选 lane 是哪些；
- 哪些 lane 提供输入；
- 哪些 lane 接收结果；
- member mask（成员掩码）是否必须完全包含在当前 `active_mask`；
- 输入不一致时报告什么故障。

除具体指令另有更严格规则外，基本合同是：

```text
E = snapshot(active_mask)
M = 所有 E 中 lane 必须一致提供的 member_mask
require M & ~E == 0
contributors = M
receivers = E
```

成员掩码包含非 active lane，或 active lane 提供的 `M` 不一致，报告 `COLLECTIVE_FAULT`。这类指令的执行域是 `warp_collective`，不套用 scalar-ready，但必须满足自己的会合条件。

## 10. CTA 屏障

`execution_domain=cta_sync` 的三条规范指令是：

```text
BAR.SYNC.CTA   id
BAR.ARRIVE.CTA vd, id
BAR.WAIT.CTA   id, vs
```

每个 CTA 固定有 8 个命名槽 `id=0..7`。每个槽只有一个当前 generation，状态为：

```text
BarrierSlot {
    generation: N                 # 数学非负整数，无最大值、不回绕
    mode: EMPTY | SYNC | SPLIT
    owner_set: set<linear_tid>         # CTA 启动时固定
    arrived_set: set<linear_tid>
    consumed_set: set<linear_tid>
    completed: bool
    waiters: map<warp_id, BarrierWaitRecord>
}

BarrierWaitRecord {
    warp_id: U32
    owner_snapshot: set<linear_tid>
    resume_pc: U64
}
```

owner 的唯一身份是 CTA 内 `linear_tid=warp_id*32+lane_id`；它与 `(warp_id,lane_id)` 等价，但所有 `owner_set/arrived_set/consumed_set`、token tag、重复检查和 wrong-owner 检查都只使用 `linear_tid`。启动时 `generation=0`、`mode=EMPTY`、两个集合和 `waiters` 都为空、`completed=false`。`owner_set` 恰好是 CTA 启动时全部真实线程的 `linear_tid`：不存在的尾 lane不属于 owner；真实线程后来 `EXIT` 也不会删掉 owner。没有子集 barrier，没有 `expected` 字段。

这里的 `N` 是数学上的非负整数，不是 U64，也没有最大值。generation 只会在 `retire` 时严格加 1，永远不回绕。有限实现必须做到架构上看起来同样不回绕；它可以使用更宽 epoch、capability ID 或安全回收，但绝不能让旧、已消费或复制 token 因内部计数器复用而重新匹配。

每条 BAR 先取：

```text
A = {warp_id*32 + lane_id | lane_id 位于 snapshot(active_mask)}
```

所以 `A` 是 `set<linear_tid>`。分歧路径只处理当前 `active_mask` 中的真实 lane；同 warp 的挂起路径不会偷偷参加。所有 owner 检查完成后才允许一次提交。任一 `tid∈A` 出错，整条 warp 动态指令报告 `BARRIER_FAULT`，不得留下部分 arrival、consume、VGPR 写、blocked record 或 PC 效果。

### 10.1 阻塞记录

BAR 阻塞整个 warp 的当前动态路径，不只是 `A` 中的若干 lane。每个 warp 同一时刻至多有一条 barrier blocked record；ready warp 发射 BAR 时该记录必须为空。统一操作为：

```text
block_barrier(S, A, old_PC):
    require warp.blocked_record == none
    R = BarrierWaitRecord(
        warp_id = current_warp_id,
        owner_snapshot = snapshot(A),
        resume_pc = old_PC + 8)
    require current_warp_id not in S.waiters
    S.waiters[current_warp_id] = R
    warp.blocked_record = (slot_id, S.generation, R)
    warp.ready = false
    # PC 留在 BAR；active_mask/live_mask/reconv_stack/call_stack 全部不变

resume_barrier(S, R):
    require warp[R.warp_id].blocked_record == (slot_id, S.generation, R)
    shared_acquire(R.owner_snapshot)
    delete S.waiters[R.warp_id]
    warp[R.warp_id].blocked_record = none
    warp[R.warp_id].PC = R.resume_pc
    warp[R.warp_id].ready = true
    # active_mask/live_mask/reconv_stack/call_stack 全部不变
```

blocked record 存在期间，调度器不能发射该 warp，也不能切入其重汇聚栈中的挂起路径。恢复只执行上面列出的 PC、ready 和记录清理，不重新读 token、不重新 arrival/consume，也不改任何控制掩码或栈。

### 10.2 模式、到达和退休

某代的第一批合法 arrival 决定模式：`BAR.SYNC.CTA` 把 `EMPTY` 变为 `SYNC`，`BAR.ARRIVE.CTA` 把它变为 `SPLIT`。已经选定后混用另一种 arrival，整条指令报 `BARRIER_FAULT`。每个 owner 每代只准 arrival 一次；只要 `A` 与 `arrived_set` 相交，整条指令不提交。

```text
retire(slot):
    old_generation = slot.generation
    slot.generation = old_generation + 1
    slot.mode = EMPTY
    slot.arrived_set = {}
    slot.consumed_set = {}
    slot.completed = false
    slot.waiters = {}
```

SYNC 代在全部 owner 到齐并恢复等待者后立即退休。SPLIT 代即使已经到齐，也必须等全部 owner 的 token 都成功 WAIT 消费后才能退休。每次退休后的 generation 必须恰好等于旧值加 1，不能跳号或回绕；退休后旧代 token 永久 stale。

### 10.3 `BAR.SYNC.CTA id`

```text
A = {warp_id*32 + lane_id | lane_id 位于 snapshot(active_mask)}
S = slot[id]

require S.mode in {EMPTY, SYNC}
require A & S.arrived_set == {}

atomically:
    if S.mode == EMPTY: S.mode = SYNC
    S.arrived_set |= A
    shared_release(tid in A)
    block_barrier(S, A, old_PC)

if S.arrived_set == S.owner_set:
    S.completed = true
    records = snapshot(all S.waiters)
    for every R in records together:
        resume_barrier(S, R)
    retire(S)
```

“together”表示同一代所有 SYNC 等待者在同一个完成动作中恢复，不允许先让一部分跨过 barrier。阻塞期间不能再次读取源、重复 arrival 或预先推进 PC。

### 10.4 `BAR.ARRIVE.CTA vd,id`

每个入口 active lane 对应一个 `tid∈A`，并得到自己的 VGPR32 token，不是整个 warp 共用一份 SGPR token：

```text
A = {warp_id*32 + lane_id | lane_id 位于 snapshot(active_mask)}
S = slot[id]

require S.mode in {EMPTY, SPLIT}
require A & S.arrived_set == {}

atomically:
    if S.mode == EMPTY: S.mode = SPLIT
    S.arrived_set |= A
    for each active lane_id:
        tid = warp_id*32 + lane_id
        shared_release(tid)
        vd[lane_id].bits = implementation_defined_U32
        vd[lane_id].tag = VALID(CTA_identity, tid, id, S.generation)
    PC = old_PC + 8

if S.arrived_set == S.owner_set:
    S.completed = true
    records = snapshot(all S.waiters)
    for every R in records:
        resume_barrier(S, R)
    if S.consumed_set == S.owner_set:
        retire(S)
```

不在入口 active mask 中的 lane 不读取也不写 `vd[lane]`。tag 字段恰好是 `{CTA identity,linear_tid,slot,logical generation}`；tag 才是身份，32 位值不是身份。logical generation 永不复用，所以旧 tag 不可能在后续代复活。

### 10.5 VGPR 标签写回闭包

对参与 lane，任何 form 写入任何 VGPR32 槽时，默认清除该目标槽旧 barrier-token tag。多槽或片段目标逐个 VGPR32 槽应用。全 ISA 只有两个例外：

1. `BAR.ARRIVE.CTA` 对其写入槽创建新 tag；
2. 寄存器型 `V_MOV.B32 vd,vs` 对该 lane 完整复制源槽 tag。

所以 `V_MOV.B64`、`V_BCAST`、`X_BROADCAST`、`V_GETREG`、普通/原子 load 返回、所有 ALU/CVT/FP 写回、MMA 输出及任何其他 VGPR 写目标都清除其写入槽的 barrier-token tag。非参与 lane不写槽，也不清标签。这个闭包只管 barrier-token tag，不改变其他章节单独定义的 pointer provenance。

### 10.6 `BAR.WAIT.CTA id,vs`

WAIT 的 `id` 是显式编码字段；每个 active owner 从自己的 `vs[lane]` 读 token：

```text
A = {warp_id*32 + lane_id | lane_id 位于 snapshot(active_mask)}
S = slot[id]
T[lane_id] = snapshot(vs[lane_id].bits, vs[lane_id].tag)
    for lane_id represented in A

require S.mode == SPLIT
for each active lane_id:
    tid = warp_id*32 + lane_id
    require T[lane_id].tag is VALID
    require T[lane_id].tag == (this_CTA_identity, tid, id, S.generation)
    require tid in S.arrived_set
    require tid not in S.consumed_set

atomically:
    S.consumed_set |= A
    mark each linear_tid identity in A consumed
    if not S.completed:
        block_barrier(S, A, old_PC)
    else:
        shared_acquire(tid in A)
        PC = old_PC + 8

if S.completed and S.consumed_set == S.owner_set:
    retire(S)
```

在 generation 已完成后才执行 WAIT，会立即取得 shared acquire 并前进。token 只能成功消费一次；stale、foreign CTA、wrong-slot、wrong-owner、wrong-generation、malformed、untagged 和 duplicate token 一律 `BARRIER_FAULT`。wrong-owner 就是 tag 的 `linear_tid` 不等于当前 lane 算出的 `warp_id*32+lane_id`。同一 token 标签被 `V_MOV.B32` 复制到多处也不会增加消费次数；第一份成功消费后，其余副本都变成 duplicate/stale。

### 10.7 EXIT、分歧和死锁

owner 尚未 arrival 就执行 `EXIT` 不会缩小 `owner_set`，其他 owner 以后可能永远等不到它，最终按第 12 节报告 `DEADLOCK`。若任一退出 `linear_tid` 位于任一 SPLIT 槽的 `arrived_set-consumed_set`，`EXIT` 直接 `BARRIER_FAULT`；即使 VGPR token tag 已被清除也不能退出。

分歧路径上的 BAR 只把当前 active lane 转成 `A`。如果当前路径在 BAR 上阻塞，整个 warp blocked，重汇聚栈中的挂起路径不能切入；其中若还有本代必须 arrival 或 WAIT 的 `linear_tid`，程序可以死锁。这不是子集 barrier，也不会由实现代替软件补票。

### 10.8 内存边

每个成功 `BAR.SYNC.CTA` arrival 和 `BAR.ARRIVE.CTA` arrival 都是 shared、CTA scope 的 release。SYNC 恢复和成功 `BAR.WAIT.CTA` 是 shared、CTA scope 的 acquire。它们不自动排序 global、local、param、const 或 host；global 通信仍要使用合法原子和需要的 `MEMBAR`。

## 11. 调度、前进和 occupancy

同一 CTA 的 warp 独立调度。只要一个 warp ready，SM/CU 就可以发射它，不需要等待 CTA 中其他 warp 到同一 PC。

实现必须对持续 ready 的驻留 warp提供弱公平性：不能只因为调度器偏爱别的 warp，就让它永久饿死。这里的“弱公平”不承诺具体周期数，也不承诺固定轮转顺序。

occupancy 会改变“同一时刻有哪些 CTA 已经驻留”，但不能改变程序的合法结果。软件禁止依赖：

- 某两个 CTA 一定同时驻留；
- 某个 `warp_id` 一定先执行；
- 大寄存器内核仍能达到某个固定 occupancy；
- 自旋等待一个尚未驻留的 CTA 一定前进。

`sgpr_count`、`vgpr_count`、`vp_count`、重汇聚栈深度、`call_stack_depth`、shared memory 和 local memory 都可能降低 occupancy。资源不够放入整个 CTA 时，CTA 必须等待，不能拆开放到多个 SM/CU。

## 12. 完成和死锁

warp 完成条件：

```text
live_mask == 0
reconv_stack is empty
call_stack is empty
blocked_record == none
没有该 warp 尚未清理的集合状态
```

若 `live_mask==0` 时调用栈仍非空，实现必须报告 `RECONVERGENCE_FAULT`，不能把带着未返回调用帧的 warp 宣告完成。

CTA 完成条件：

```text
全部 warp 完成
并且 8 个 slot 都满足：
    mode == EMPTY
    arrived_set == {}
    consumed_set == {}
    waiters == {}
    completed == false
```

上面这组槽条件叫 **IDLE**。`generation∈N` 是已经退休了多少代的逻辑计数；它可以为任意非负值，不妨碍 CTA 完成，也不要求归零。这里“任意”不表示有限位宽回绕，generation 始终保留其完整逻辑值。

kernel 完成条件：

```text
全部 CTA 完成
没有故障
```

kernel 尚未完成时，若同时满足：

1. 没有 ready warp；
2. 没有正在执行、尚未提交的架构指令；
3. 没有已接受的内存、屏障或运行时事件能在无需再发射指令的情况下让 warp ready；
4. 至少还有一个未完成 warp、lane 或同步状态；

实现必须报告 `DEADLOCK`。

墙钟超时不等于架构死锁。一个仍在不断执行指令的无限循环属于不终止或活锁，不满足“没有 ready warp”这一条。

## 13. 七个执行域的快速对照

| `execution_domain` | 大白话含义 | scalar-ready 规则 |
|---|---|---|
| `system` | 系统控制、陷阱和杂项 | 不因执行域自动检查 |
| `scalar` | 每 warp 做一次，可读写 SGPR；只有明确操作数才能读 SCC | 每个 form 都必须检查 |
| `vector` | 每个参与 lane 做一次，可读 VGPR、`vpN` 和 SGPR 广播 | 不检查 |
| `warp_control` | 改 PC、路径、重汇聚栈或调用栈 | 普通控制不检查；`CALL/CALL.IND/JUMP.IND/RET` 必须检查 |
| `warp_collective` | 一个 warp 的多个 lane 合作投票或交换数据 | 不检查，但要满足集合会合合同 |
| `cta_sync` | CTA 线程做屏障或内存同步 | 不检查，但要满足 CTA 同步合同 |
| `warp_matrix` | 一个 warp 合作完成矩阵运算 | 不检查，但要满足矩阵参与合同 |

机器 class 不出现在这张表里，因为它只决定编码。`MEMORY` class 内部仍要看 form 的执行域，不能把所有访存一概当成 vector 或 scalar。

这张表只是索引。遇到细节时，以本文件前面各节的完整规则为准。

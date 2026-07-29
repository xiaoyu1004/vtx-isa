# VTX-1 ISA 1.0 Draft：术语与记号

## 1. 从设备到 lane

- **device（设备）**：能接收并执行 VTX-1 内核的设备。
- **kernel（内核）**：一段要在设备上并行执行的程序。
- **launch（启动）**：用一组参数和网格尺寸运行一次内核。
- **CTA（线程块）**：一组能共享 shared memory（共享内存）并使用 CTA 屏障互相同步的线程。
- **grid（网格）**：一次启动里的全部 CTA。
- **SM/CU（计算单元）**：设备中真正接收 CTA、保存其驻留状态并发射指令的硬件单元。不同产品可以把它叫 SM 或 CU；本文把两种叫法写成 `SM/CU`。
- **warp（线程束）**：32 个 lane 组成的执行组。一个 warp 共用一个 PC（程序计数器）和一套控制状态。
- **lane（通道）**：warp 中编号为 `0..31` 的单个执行位置，对应一个软件线程。
- **resident（驻留）**：CTA 及其 warp 的寄存器、共享内存和控制状态已经分配在某个 SM/CU 上，可以被该 SM/CU 调度。

CTA 的三维线程号按 X 最快的顺序变成一维编号：

```text
linear_tid = tid_x + ntid_x * (tid_y + ntid_y * tid_z)
warp_id    = floor(linear_tid / 32)
lane_id    = linear_tid mod 32
```

最后一个 warp 可能没有装满。若 `linear_tid` 不小于 CTA 的真实线程数，该位置叫 **不存在 lane**。不存在 lane 永远不进入 `LIVE`（存活掩码）或 `EXEC`（当前执行掩码），也不读写寄存器、内存和屏障状态。

## 2. 调度和占用率

- **schedule（调度）**：SM/CU 从当前可以运行的 warp 中挑一个发射下一条指令。
- **occupancy（占用率）**：一个 SM/CU 能同时驻留多少 CTA 或 warp。它受后文定义的 SGPR（每 warp 一份的寄存器）、VGPR（每 lane 一份的寄存器）、共享内存、重汇聚栈和实现上限共同约束。
- **warp 独立调度**：同一 CTA 的 warp 不必同一周期执行，也不保证按 `warp_id` 顺序执行。一个 warp 阻塞时，另一个就绪 warp 可以继续。

一个 CTA 必须整体驻留在一个 SM/CU 上，禁止把同一 CTA 的不同 warp 拆到不同 SM/CU。这样 CTA 的 shared memory 和 CTA 屏障始终由同一个 SM/CU 管理。

## 3. 寄存器术语

- **SGPR（标量通用寄存器）**：每个 warp 只有一份的 32 位寄存器，名字是 `s0..s255`。同一 warp 的所有 lane 看到同一个 `sN` 值。
- **VGPR（向量通用寄存器）**：每个 lane 各有一份的 32 位寄存器，名字是 `v0..v255`。`vN` 在 32 个 lane 上可以保存 32 个不同值。
- **`vp0..vp15`（lane 掩码寄存器）**：每个 warp 有 16 个，每个都是 32 位；位 `i` 控制 lane `i`。
- **SCC（标量条件码）**：每个 warp 一个 1 位状态。只有把 SCC 明确列为源操作数的指令才读取它；SCC 不会自动变成所有 scalar 指令的开关。
- **EXEC**：只读的 32 位当前执行掩码，数值等于 `active_mask`。
- **LIVE**：只读的 32 位存活掩码，数值等于 `live_mask`。
- **broadcast（广播）**：vector 指令读取一个 SGPR 时，把同一个 32 位值送给所有参与 lane。
- **register slice（寄存器切片）**：SM/CU 从物理 SGPR/VGPR 容量中划给一个驻留 warp 的那一部分。

`s0` 表示整个 warp 共用的一个 32 位值，不是 32 份值。`v0` 表示每个 lane 各有一个 32 位值，因此一个完整 warp 的 `v0` 一共有 32 份值。

SGPR 和 VGPR 的物理寄存器文件位于 SM/CU。架构寄存器名不等于固定物理编号；实现可以改名、分 bank（分存储组）或在不被软件发现的情况下搬移数据。

**barrier token（屏障 token）**是 `BAR.ARRIVE.CTA` 写入每个参与 lane 的 VGPR32。它有普通软件可见的 32 位数值，还带程序不能直接构造的隐藏有效标签。标签字段恰好是 `{CTA identity, linear_tid, slot, logical generation}`；数值碰巧相同不代表 token 相同。任意 VGPR32 槽写入默认清除旧标签，唯二例外是 `BAR.ARRIVE.CTA` 创建新标签、寄存器型 `V_MOV.B32` 完整复制源标签。

## 4. lane 掩码

本文把 lane 集合写成 32 位掩码。位 `i` 为 1 表示集合里有 lane `i`。

- `live_mask`：属于该 warp、尚未执行 `EXIT` 的真实 lane。架构寄存器 `LIVE` 是它的只读视图。
- `active_mask`：当前 PC 上实际走这条控制路径的 lane。架构寄存器 `EXEC` 是它的只读视图。
- `entry_mask`：一条动态指令开始时对 `active_mask` 拍下的快照。
- `guard_mask`：vector 指令由 `vpN` 或 `!vpN` 算出的 lane 掩码。
- `participating_mask`：vector 指令真正产生效果的 lane，等于 `entry_mask & guard_mask`。
- **suspended lane（挂起 lane）**：仍在 `live_mask` 中，但当前不在 `active_mask` 中；它正在等待另一条分支路径执行完。

任何时候都必须满足：

```text
active_mask & ~live_mask == 0
```

也就是 active lane 一定还是 live lane。写 `EXEC` 或 `LIVE` 不是普通寄存器操作；任何试图把它们作为目标操作数的编码都非法。

## 5. 执行域和机器 class

每个指令 form（具体编码形式）都有一个 `execution_domain`（执行域）字段。它只允许七个值：

- `system`：系统控制、陷阱或杂项操作；
- `scalar`：每个 warp 执行一次；
- `vector`：对 participating lane 分别执行；
- `warp_control`：管理 warp 的 PC、当前路径、重汇聚栈或调用栈；
- `warp_collective`：让一个 warp 的多个 lane 一起完成投票、shuffle 等集合操作；
- `cta_sync`：让一个 CTA 内的线程做屏障或内存同步；
- `warp_matrix`：让一个 warp 合作完成矩阵操作。

机器码另有 8 个 **class（编码类）**：`SYS`、`SALU`、`VALU`、`MEMORY`、`CONTROL`、`SYNC`、`CROSSLANE`、`MATRIX`。class 只负责把 64 位机器字分区解码，不直接规定执行方式。最容易混淆的是 `MEMORY`：同一个 class 里，标量加载、存储和原子 form 的执行域是 `scalar`，逐 lane 访存 form 的执行域是 `vector`。

**scalar-ready（标量就绪）**表示 warp 同时满足三件事：`live_mask` 非空、`active_mask == live_mask`，并且重汇聚栈里没有 `FIRST` 或 `SECOND` 帧。

`execution_domain` 说明“这条 form 怎样执行”，`required_state` 说明“执行前还要满足什么状态”。所有 `execution_domain: scalar` form 都必须写 `required_state: scalar_ready`。这个规则不看机器 class，也不看它属于算术、浮点、特殊寄存器、访存还是原子操作。

`execution_domain: vector` 不检查 scalar-ready。普通 `warp_control` 也不检查；直接 `BRA` 和 `BRA.P` 在分歧路径中仍可运行。`CALL`、`CALL.IND`、`JUMP.IND`、`RET` 是明确例外：它们写有 `required_state: scalar_ready`，但执行域仍是 `warp_control`。

`SSY`、`BRA`、`BRA.P`、`JOIN`、`EXIT` 都是 `warp_control`。它们不属于 scalar ALU。即使当前 warp 处于分歧路径，它们仍按 `03-execution-model.md` 的控制规则执行。

## 6. PC、静态指令和动态指令

- **PC（程序计数器）**：warp 下一条要执行的指令地址，用相对当前内核文本起点的字节偏移表示。
- **静态指令**：内核文本中某个 PC 上保存的指令。
- **动态指令**：某个 warp 实际执行某条静态指令的一次事件。两个 warp 执行同一个 PC，算两个不同动态指令。

本 Draft 中每条指令是 8 字节，因此：

```text
next_pc(PC) = PC + 8
```

合法 PC 必须 8 字节对齐，并且整条指令都位于文本范围内：

```text
PC mod 8 == 0
0 <= PC
PC + 8 <= text_size
```

## 7. 分歧和重汇聚

- **divergence（分歧）**：同一 warp 中，一部分 active lane 走分支目标，另一部分走顺序下一条。
- **reconvergence（重汇聚）**：两条分支路径都处理完后，把仍存活的 lane 合回同一个 `active_mask`。
- **reconvergence stack（重汇聚栈）**：每个 warp 隐藏的一组后进先出帧，保存汇合 PC、待执行路径和已到达 lane。
- `ARMED`：`SSY` 已经预约汇合点，但还没有出现需要拆成两路的分歧。
- `FIRST`：第一条路径正在执行，第二条路径已保存在栈帧里。
- `SECOND`：第二条路径正在执行，第一条路径的 lane 正在汇合点等待。

`FIRST` 或 `SECOND` 帧叫 **未完成分歧帧**。即使某个时刻 `active_mask` 恰好又等于 `live_mask`，只要栈里还有这类帧，scalar 指令仍然不合法。

命名 CTA 屏障使用这些固定术语：

- **slot（槽）**：每 CTA 的 8 个编号槽之一，id 为 `0..7`；
- **generation（代）**：数学上的非负整数 `N={0,1,2,...}`。槽从 0 开始，每次退休严格变成 `old_generation+1`；它单调增加、永不回绕，也没有最大架构值；
- **owner identity（owner 身份）**：只使用 CTA 内 `linear_tid = warp_id*32 + lane_id`；二元组 `(warp_id,lane_id)` 只是等价表示，所有架构集合和比较都以 `linear_tid` 为元素；
- **owner set（owner 集）**：CTA 启动时全部真实线程 `linear_tid` 的固定集合，不含不存在的尾 lane，且不因 `EXIT` 变小；
- **mode（模式）**：当前代的 `EMPTY`、`SYNC` 或 `SPLIT`；第一批合法到达把 `EMPTY` 变成后两者之一；
- **arrived set（已到达集合）**：当前代已经成功到达的 `linear_tid` 集合；同一 `linear_tid` 每代只能进入一次；
- **consumed set（已消费集合）**：SPLIT 代已经用 `BAR.WAIT.CTA` 成功消费自己 token 的 `linear_tid` 集合。

“active owner”集合 `A` 是把某条 BAR 动态指令入口 `active_mask` 的每个置位 lane 转成 `linear_tid` 后得到的集合。挂起路径、已退出 lane和不存在 lane都不在这次 `A` 里；但只要其 `linear_tid` 属于固定 owner set，就仍可能是本代尚未到达或尚未消费的 owner。

屏障阻塞记录固定写成：

```text
BarrierWaitRecord {
    warp_id: U32
    owner_snapshot: set<linear_tid>
    resume_pc: U64
}
```

`owner_snapshot` 是入口 `A` 的冻结副本，`resume_pc=old_PC+8`。BAR 阻塞的是整个 warp 当前动态路径。阻塞时 warp 的 PC 留在 BAR 上，`active_mask/live_mask`、重汇聚栈和调用栈保持不变；挂起路径不能切入。每个 warp 同一时刻至多有一条 blocked record。恢复只清除该记录、写 `PC=resume_pc` 并把 warp 置为 ready，其他状态不变。

## 8. 数值和位写法

- `U8/U16/U32/U64`：对应位宽的无符号整数。
- `S8/S16/S32/S64`：对应位宽的二补数有符号整数。
- `F16/F32`：IEEE 754 binary16 和 binary32 位型。
- `x[n:m]`：从位 `n` 到位 `m`，两端都包含；位 0 是最低位。
- `bit(i)`：只有位 `i` 为 1 的 32 位掩码。
- `popcount(x)`：掩码 `x` 中 1 的个数。
- `lowest(x)`：非零掩码中编号最小的置位。
- `[base, base+size)`：从 `base` 开始、到 `base+size` 之前结束的半开区间。
- `align_up(x,a)`：把 `x` 向上补到 `a` 的倍数；`a` 必须是 2 的幂。
- `UNSPEC`：位宽确定，但初始值不保证是什么。程序必须先写后读，不能依赖它恰好为零。

所有指令、参数和内存中的多字节数都使用小端格式，也就是低有效字节放在低地址。

地址和大小检查先用不会溢出的数学整数计算，再判断是否越界。禁止先按 U32 或 U64 回绕，再把回绕后的地址当成合法地址。

## 9. 伪代码约定

本文伪代码中的常用操作如下：

```text
snapshot(x)              # 为当前动态指令保存不再变化的副本
fault(code, mask, aux)   # 本动态指令不提交，并报告故障
block(reason)            # 保存当前状态，等待条件满足后继续
commit(effects)          # 把整条指令的效果一次性变为可见
retire(slot)             # generation = old_generation + 1；再清 mode/集合/等待者
```

实现可以在内部使用有限位宽计数器，但架构效果必须等同于逻辑 generation 永不回绕。实现可用更宽 epoch、不可伪造 capability ID、安全回收等办法；无论采用哪种办法，旧 token、已消费 token 或它的 `V_MOV.B32` 副本都不能因内部数值再次相等而在后续逻辑代重新有效。

除屏障等明确写成两阶段的指令外，一条动态指令必须“先检查，后整体提交”：

```text
I = decode_and_validate(fetch64(PC))
E = snapshot(active_mask)
sources = snapshot_required_sources(I, E)
effects = evaluate_and_validate(I, E, sources)
if effects has fault:
    fault(...)
else:
    commit(effects)
```

提交前，目标寄存器、内存、PC、lane 掩码和重汇聚栈都不能出现部分更新。

## 10. 就绪、阻塞和完成

- **ready（就绪）**：warp 有非空 `active_mask`，也没有在等待屏障、内存或其他事件。
- **blocked（阻塞）**：warp 还没完成，但眼下不能发射下一条指令；BAR blocked record 存在时整个 warp 都不能切换到挂起路径。
- **complete（完成）**：warp 的 `live_mask` 已经为空，而且没有未清理的重汇聚或同步状态。
- **deadlock（死锁）**：内核还没完成，却没有任何 warp 能继续，也没有已在途事件能让 warp 重新就绪。
- **livelock（活锁）**：指令一直在执行，但程序永远达不到完成状态。

具体完成和死锁条件见 `03-execution-model.md`。

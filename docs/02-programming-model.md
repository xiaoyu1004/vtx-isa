# VTX-1 ISA 1.0 Draft：编程模型

## 1. 内核描述符

每个可启动内核必须带一个 **kernel descriptor（内核描述符）**。它是一张 80 字节的小表，告诉运行时入口在哪里、内核会用多少寄存器和每个 CTA（线程块）要多少存储。

描述符必须按 8 字节对齐，字段使用小端格式：

下表中的 SGPR 是每 warp 一份的 32 位寄存器，VGPR 是每 lane 一份的 32 位寄存器，`vp0..vp15` 是每 warp 一份的 32 位 lane 掩码寄存器。shared memory 是 CTA 共用的存储，local memory 是每个 lane 私有的存储。

| 偏移 | 大小 | 字段 | 1.0 Draft 要求 |
|---:|---:|---|---|
| 0 | 4 | `magic` | 字节串 `VTXK` |
| 4 | 2 | `descriptor_size` | 必须为 80 |
| 6 | 2 | `descriptor_version` | 必须为 1 |
| 8 | 2 | `isa_major` | 必须为 1 |
| 10 | 2 | `isa_minor` | 必须为 0 |
| 12 | 4 | `flags` | 保留，必须为 0 |
| 16 | 8 | `entry_pc` | PC（程序计数器）的入口值，相对内核文本起点的字节偏移 |
| 24 | 8 | `text_size` | 内核文本总字节数 |
| 32 | 2 | `sgpr_count` | 每 warp 分配的 SGPR 数，范围 `16..256` |
| 34 | 2 | `vgpr_count` | 每 lane 分配的 VGPR 数，范围 `0..256` |
| 36 | 2 | `vp_count` | 每 warp 分配的 `vp` 寄存器数，范围 `0..16` |
| 38 | 2 | `reconv_stack_depth` | 每 warp 所需重汇聚栈深度，范围 `0..16` |
| 40 | 4 | `static_shared_size` | 每 CTA 的静态 shared memory 字节数 |
| 44 | 4 | `dynamic_shared_max` | 每 CTA 允许请求的动态 shared memory 上限 |
| 48 | 4 | `local_size_per_lane` | 每个真实 lane 的 local memory 字节数 |
| 52 | 4 | `param_logical_size` | 参数中最后一个有效字节的独占末端 |
| 56 | 8 | `param_layout_offset` | 从描述符起点到参数布局表的字节偏移 |
| 64 | 4 | `param_count` | 参数布局记录数 |
| 68 | 4 | `reserved0` | 必须为 0 |
| 72 | 2 | `call_stack_depth` | 每 warp 的统一调用栈最大深度，范围 `0..16` |
| 74 | 6 | `reserved1` | 必须全部为 0 |

`text_size` 必须非零且是 8 的倍数。`entry_pc` 必须满足：

```text
entry_pc mod 8 == 0
entry_pc + 8 <= text_size
```

描述符中的资源数字是硬要求，不是性能提示。运行时不能用更小的值启动内核，也不能等执行到一半才发现资源不够。

### 1.1 三个寄存器计数字段

`sgpr_count`、`vgpr_count`、`vp_count` 分别给出该内核可以引用的 `s`、`v`、`vp` 编号上界：

```text
sN  合法，当且仅当 0 <= N < sgpr_count
vN  合法，当且仅当 0 <= N < vgpr_count
vpN 合法，当且仅当 0 <= N < vp_count
```

参数窗口固定使用 `s0..s15`，所以 `sgpr_count` 不能小于 16。64 位值占两个连续寄存器时，低 32 位放在较小编号，起始编号必须为偶数，并且两个寄存器都要低于相应 count。

`SCC`、`EXEC` 和 `LIVE` 不计入 `sgpr_count`。它们是单独的架构状态。

`reconv_stack_depth` 是静态控制流分析得到的最大同时嵌套帧数。文本需要的深度超过声明值是无效描述符；声明值超过 16 也无效。

`call_stack_depth` 是每个 warp 最多能同时保存多少个返回地址。它必须满足 `0..16`；16 是 YAML `architectural_limits.call_stack_depth` 给出的架构最大值。值 0 表示没有可用调用帧；此时执行任何 `CALL` 或 `CALL.IND` 都会因为栈已满而故障。每个调用帧只保存一个 `return_pc`，也就是调用结束后要回去的 PC。

## 2. 启动前校验

运行时必须在任何 CTA 开始执行前完成下面的检查：

1. 检查描述符大小、版本、保留字段和所有范围；
2. 检查完整文本，每个 form 的 `execution_domain` 是七个权威值之一；所有 `execution_domain: scalar` form 以及 `CALL/CALL.IND/JUMP.IND/RET` 都必须写 `required_state: scalar_ready`；
3. 检查每个 `sN`、`vN`、`vpN` 和连续寄存器组没有超过 descriptor count；
4. 检查任何目标操作数都不是只读 `EXEC` 或 `LIVE`；
5. 检查 `SSY`、`BRA.P`、`JOIN` 的结构化控制流和最大重汇聚栈深度；调用栈是否超深在每次 CALL 执行时精确检查；
6. 检查 grid 和 CTA 三维尺寸都非零，CTA 线程总数不超过实现上限；
7. 检查参数布局、参数值和参数存储大小；
8. 检查 `requested_dynamic_shared <= dynamic_shared_max`；
9. 检查 `static_shared_size + requested_dynamic_shared` 没有溢出且不超过单 CTA 上限；
10. 检查至少有一个 SM/CU 能同时容纳整个 CTA 的寄存器、shared memory、local memory 和控制状态。

任何一步失败都是 **launch error（启动错误）**：内核一条指令也不执行，不得留下半个已启动 CTA。运行时应当区分：

- `INVALID_DESCRIPTOR`：描述符字段错误；
- `INVALID_TEXT`：指令或静态操作数错误；
- `INVALID_CONTROL_FLOW`：控制流结构或栈深错误；
- `INVALID_ARGUMENT`：参数布局或参数值错误；
- `INVALID_RESOURCE`：单个 CTA 无法放进任何 SM/CU。

## 3. 参数 ABI

ABI（应用二进制接口）是调用者把参数交给内核的固定摆放规则。

### 3.1 参数布局记录

参数布局表含 `param_count` 条记录，每条 16 字节：

| 记录偏移 | 大小 | 字段 | 含义 |
|---:|---:|---|---|
| 0 | 4 | `byte_offset` | 参数在参数块里的起始字节 |
| 4 | 2 | `type` | 参数类型码 |
| 6 | 2 | `element_count` | 数组元素数 |
| 8 | 4 | `byte_size` | 参数总字节数 |
| 12 | 4 | `flags` | 保留，必须为 0 |

1.0 Draft 定义这些类型：

| 类型码 | 类型 | 单元素大小 | 自然对齐 |
|---:|---|---:|---:|
| `0x0001` | `U8` | 1 | 1 |
| `0x0002` | `S8` | 1 | 1 |
| `0x0003` | `U16` | 2 | 2 |
| `0x0004` | `S16` | 2 | 2 |
| `0x0005` | `U32` | 4 | 4 |
| `0x0006` | `S32` | 4 | 4 |
| `0x0007` | `U64` | 8 | 8 |
| `0x0008` | `S64` | 8 | 8 |
| `0x0009` | `F16` | 2 | 2 |
| `0x000A` | `F32` | 4 | 4 |
| `0x000B` | `GLOBAL_PTR` | 8 | 8 |
| `0x000C` | `CONST_PTR` | 8 | 8 |
| `0x000D` | `BYTES` | 1 | 1 |

记录必须按 `byte_offset` 严格递增，字段不能重叠，起点必须满足自然对齐。固定大小类型必须满足：

```text
element_count >= 1
byte_size == element_count * element_size
```

`BYTES` 的 `element_count` 必须为 1，`byte_size` 必须非零。最后一个字段的末端必须正好等于 `param_logical_size`。字段之间可以留空；空出来的字节由运行时清零。

没有参数时，必须同时满足：

```text
param_count == 0
param_logical_size == 0
param_layout_offset == 0
```

有参数时，`param_layout_offset` 必须 8 字节对齐，布局表必须完整位于模块内，且不能与描述符或文本重叠。

### 3.2 参数存储和 SGPR 窗口

运行时实际建立的参数存储大小为：

```text
param_storage_size = max(64, align_up(param_logical_size, 16))
```

运行时先把整块参数存储清零，再按布局记录写入调用者提供的参数。参数存储在启动后只读。

参数的前 64 字节固定复制到 `s0..s15`：

```text
s0  = parameter bytes [0, 4)
s1  = parameter bytes [4, 8)
...
s15 = parameter bytes [60, 64)
```

每个 `sN` 按小端方式得到一个 32 位值。参数不足 64 字节的尾部已经清零，因此对应 SGPR 也为零。

这份复制对 CTA 中每个 warp 都做一次，所以每个 warp 启动时的 `s0..s15` 相同。超过 64 字节的参数通过只读 param 地址空间和参数加载指令读取。

指针参数除了 64 位数值，还要带地址空间身份。实现必须能区分“数值碰巧一样的普通 U64”和真正的 `GLOBAL_PTR` 或 `CONST_PTR`；普通整数不能伪装成指针。

## 4. 架构寄存器

### 4.1 每 warp 状态

每个 warp 有：

- `s0..s255`：256 个架构可命名的 32 位 SGPR，实际可用上界由 `sgpr_count` 给出；
- `vp0..vp15`：16 个架构可命名的 32 位 lane 掩码寄存器，实际可用上界由 `vp_count` 给出；
- `SCC`：1 位标量条件码；只有明确列出 SCC 源操作数的指令才读取它；
- `EXEC`：32 位只读 active lane 掩码；
- `LIVE`：32 位只读 live lane 掩码；
- 一个 PC；
- 一套隐藏重汇聚栈；
- 一套隐藏的 warp 统一调用栈，每帧只有 `return_pc`；
- 阻塞和故障状态。

### 4.2 每 lane 状态

每个真实 lane 有 `v0..v255`，每个都是 32 位；实际可用上界由 `vgpr_count` 给出。

例如，`v3` 不是整个 warp 共用的值。lane 0 的 `v3` 和 lane 1 的 `v3` 是两个独立的 32 位值。

每个真实 lane 的每个 VGPR32 还可以带一个程序不可直接读取的 barrier-token 标签。普通状态是“无标签”。对参与 lane，只要某条 form 写任意 VGPR32 槽，就默认清除该槽旧标签；多槽目标逐槽清除。唯二例外是 `BAR.ARRIVE.CTA` 创建 `{CTA identity,linear_tid,slot,logical generation}` 新标签，以及寄存器型 `V_MOV.B32` 按 lane 完整复制源标签。

因此，`V_MOV.B64`、`V_BCAST`、`X_BROADCAST`、`V_GETREG`、普通或原子 load 的返回目标、全部 ALU/CVT/FP 写回、MMA 输出和其他 VGPR 写目标 form 都走默认清除。标签规则与 pointer provenance 等其他 shadow tag 分开处理；清除 barrier-token 标签不等于擅自清除或制造其他标签。barrier 标签不改变 32 位寄存器容量，也不能由位型伪造。

### 4.3 `vp` 写入规则

vector 比较等产生 `vpN` 结果时，只改参与 lane 对应的位，其他位保持原值：

```text
new_vp = (old_vp & ~participating_mask)
       | (computed_bits & participating_mask)
```

读取 `vpN` 不会自动把它与 `LIVE` 或 `EXEC` 合并。vector 指令形成参与集时，才按 `03-execution-model.md` 显式计算。

### 4.4 EXEC 和 LIVE 只读

软件可以读取 `EXEC` 和 `LIVE`，但禁止写它们：

- `EXEC` 随当前控制路径变化；
- `LIVE` 只会在 lane 成功执行 `EXIT` 后清位；
- MOV、逻辑运算、寄存器恢复或任何普通目标写入都不能修改它们；
- 把 `EXEC` 或 `LIVE` 编成目标操作数，启动校验报 `INVALID_TEXT`；若非法编码绕过启动校验，执行时报 `ILLEGAL_OPERAND`。

软件若要按条件执行 vector 指令，应写 `vpN` 并把它用作 vector guard，不能改写 `EXEC`。

## 5. 物理寄存器和 occupancy

架构寄存器说明“软件能看到什么”，物理寄存器说明“SM/CU 实际拿什么存”。

SGPR 和 VGPR 的物理存储都位于 SM/CU。CTA 驻留时，SM/CU 为其中每个 warp 分配寄存器切片：

```text
每 warp SGPR 需求 = sgpr_count 个 32 位槽
每 warp VGPR 需求 = 32 * vgpr_count 个 32 位槽
每 warp vp   需求 = vp_count 个 32 位掩码槽
```

不存在 lane 在架构上没有值，但实现可以为了规则整齐，仍按完整 32-lane 切片预留 VGPR 物理空间。资源准入必须按实现公开的计算方法执行，不能少分后让两个驻留 warp 互相覆盖。

一个 CTA 的寄存器需求等于其全部 warp 需求之和。再加上：

- `static_shared_size + requested_dynamic_shared` 字节 shared memory；
- 每个真实 lane 的 `local_size_per_lane` 字节 local memory 配额；
- 每 warp 的重汇聚栈、`call_stack_depth` 个返回地址槽和其他控制状态；
- 实现规定的 CTA/warp 数量上限。

这些资源一起决定 occupancy。`sgpr_count` 或 `vgpr_count` 越大，同一 SM/CU 能同时驻留的 warp 通常越少；shared memory 越大，同一 SM/CU 能同时驻留的 CTA 通常也越少。

资源不足以再放一个 CTA 时，该 CTA 等待，不得只把它的一部分 warp 放进去。已经驻留的 CTA 释放足够资源后，等待 CTA 才能整体进入。

## 6. CTA 驻留和调度

一个 CTA 的所有 warp 必须驻留在同一个 SM/CU，直到 CTA 完成或内核故障。原因很直接：它们共用同一块 shared memory，并通过 CTA 屏障同步。

“整体驻留”不等于“同时执行”。CTA 内每个 warp 有自己的 PC、`EXEC`、`LIVE`、寄存器切片、重汇聚栈和调用栈。SM/CU 可以逐周期独立挑选就绪 warp：

- 不保证低编号 warp 先运行；
- 不保证同一 CTA 的 warp 轮流运行；
- 一个 warp 等内存或屏障时，其他 warp 可以运行；
- 软件不能把调度先后当成同步手段。

## 7. 启动初态

每个 warp 启动时：

```text
PC              = entry_pc
live_mask       = 本 warp 真实 lane 的位图
active_mask     = live_mask
reconv_stack    = empty
call_stack      = empty
blocked         = false
blocked_record  = none

s0..s15         = 参数前 64 字节
s16..s255       = UNSPEC
vp0..vp15       = 0
SCC             = 0
v0..v255        = UNSPEC（对每个真实 lane 分别成立）
EXEC            = active_mask 的只读视图
LIVE            = live_mask 的只读视图
```

超过 descriptor count 的寄存器虽然有架构名字，但该内核不能引用。`UNSPEC` 不表示随机数生成器；它只表示规范不保证初值，程序必须先写后读。

每个 CTA 启动时得到：

- 一块 `static_shared_size + requested_dynamic_shared` 字节的 shared memory；
- 每个真实 lane 一块 `local_size_per_lane` 字节的 local memory；
- 8 个命名 CTA 屏障槽 `0..7`。每槽初始化为逻辑 `generation=0∈N`、`mode=EMPTY`、`arrived_set=empty`、`consumed_set=empty`、`completed=false`、`waiters=empty`；8 槽共用的固定 owner set 是 CTA 启动时全部真实线程的 `linear_tid`；
- CTA 和 grid 的坐标及尺寸。

`linear_tid = warp_id*32+lane_id` 是 owner 的唯一集合元素；不存在的尾 lane 不进入 owner set。之后执行 `EXIT` 也不从 owner set 删除 `linear_tid`。所有 VGPR barrier-token 标签启动时无效。shared memory 和 local memory 的初始数据为 `UNSPEC`，除非其他章节对某段存储明确规定清零。

generation 没有 U32/U64 之类的架构位宽。有限硬件状态必须提供“看起来永不回绕”的结果：不得因为物理计数器回到旧位型，就让任何旧、已消费或复制 token 重新匹配当前代。

## 8. 特殊只读信息

实现必须让指令能读取下列只读信息。具体编码由指令清单定义：

| 名称 | 位宽 | 内容 |
|---|---:|---|
| `LANE_ID` | 32 | warp 内 lane 号 `0..31` |
| `WARP_ID` | 32 | CTA 内 warp 号 |
| `TID_X/Y/Z` | 各 32 | CTA 内线程坐标 |
| `CTA_ID_X/Y/Z` | 各 32 | grid 内 CTA 坐标 |
| `NTID_X/Y/Z` | 各 32 | CTA 三维尺寸 |
| `NCTA_X/Y/Z` | 各 32 | grid 三维尺寸 |
| `SM_ID` | 32 | 当前驻留 SM/CU 的实现定义编号 |
| `WARP_SIZE` | 32 | 恒为 32 |
| `DYNAMIC_SHARED_SIZE` | 32 | 本 CTA 的动态 shared memory 字节数 |
| `PARAM_BASE` | 64 | 只读参数存储的字节 0 地址 |
| `EXEC` | 32 | 当前动态指令入口的 active lane 掩码 |
| `LIVE` | 32 | 当前动态指令入口的 live lane 掩码 |

vector 读取 lane 相关信息时，每个参与 lane 得到自己的值。`execution_domain=scalar` 的指令通常只能读取 warp 或 CTA 一致的信息；尝试用普通 scalar 指令读取 `LANE_ID` 或线程坐标这类逐 lane 值，必须报 `ILLEGAL_OPERAND`。`S_READFIRST` 是明确例外：它先通过 scalar-ready 检查，再按指令定义读取最低编号 active lane 的 VGPR。

## 9. 精确故障

**精确故障**表示故障指令本身没有留下任何部分效果。PC、寄存器、内存、lane 掩码、重汇聚栈和调用栈都停在该指令入口状态。

`isa/vtx1/isa.yaml` 的 `faults` 字段是故障码和名称的权威位置；`fault_priority` 是优先级的独立权威字段。下面先按故障码列出名称，随后逐字抄写 `fault_priority`。两处不一致时必须阻断发布。

| 码 | 名称 | 直接含义 |
|---:|---|---|
| `0x0001` | `ILLEGAL_INSTRUCTION` | 指令字、操作码或保留位非法 |
| `0x0002` | `ILLEGAL_OPERAND` | 寄存器类别、编号、目标或动态操作数组合非法 |
| `0x0003` | `SCALAR_STATE_FAULT` | `execution_domain: scalar` 的 form 不满足 scalar-ready，或 `CALL`、`CALL.IND`、`JUMP.IND`、`RET` 的 `required_state: scalar_ready` 不满足 |
| `0x0004` | `RECONVERGENCE_FAULT` | 重汇聚栈、帧、目标或 `JOIN` 顺序错误 |
| `0x0005` | `MISALIGNED_ACCESS` | 内存访问没有满足对齐要求 |
| `0x0006` | `MEMORY_BOUNDS` | 地址越过对应地址空间 |
| `0x0007` | `INTEGER_FAULT` | 除零等已定义整数错误 |
| `0x0008` | `BARRIER_FAULT` | 屏障模式、到达、token、linear_tid owner、槽或代际规则错误；包括任一 SPLIT 槽中该退出 `linear_tid ∈ arrived_set-consumed_set` |
| `0x0009` | `COLLECTIVE_FAULT` | warp 集合指令的参与规则错误 |
| `0x000A` | `SOFTWARE_TRAP` | 程序主动执行 `TRAP` |
| `0x000B` | `DEADLOCK` | 满足架构死锁条件 |

故障记录至少包含：

```text
FaultRecord {
    code: U16,
    pc: U64,
    cta_id: (U32, U32, U32),
    warp_id: U32,
    lane_mask: U32,
    address_or_aux: U64
}
```

`SCALAR_STATE_FAULT` 是 warp 状态错误，`lane_mask` 必须记录指令入口的 `active_mask`，`address_or_aux` 必须为 0。它覆盖所有 `execution_domain: scalar` form，也覆盖 `warp_control` 类 `CALL`、`CALL.IND`、`JUMP.IND`、`RET` 上明确写出的 `required_state: scalar_ready`。完整触发条件见 `03-execution-model.md`。

同一动态指令同时发现多个问题时，必须逐字采用 YAML 的 `fault_priority`：

```yaml
fault_priority:
- ILLEGAL_INSTRUCTION
- ILLEGAL_OPERAND
- SCALAR_STATE_FAULT
- RECONVERGENCE_FAULT
- BARRIER_FAULT
- COLLECTIVE_FAULT
- INTEGER_FAULT
- MISALIGNED_ACCESS
- MEMORY_BOUNDS
- SOFTWARE_TRAP
```

上面的先后次序就是 YAML `fault_priority`，排在前面的故障优先。`DEADLOCK` 不在该字段中；它是没有动态指令可继续时才判定的全局状态，不参加同一动态指令的竞争。其他章节只能引用 `fault_priority`，不能从 `faults` 的编号或排列自行推导优先级。

启动时能发现的静态错误应当在启动前拒绝。运行时故障发生后，整个 kernel 进入失败态，任何 CTA 都不能再提交新指令。

# vtx-isa：VTX-1 ISA 1.0 Draft 设计审查

## 1. 这次审查在看什么

VTX-1 ISA 1.0 Draft 是一次全新的设计重置。本文件只回答三件事：

1. 为什么机器模型要这样设计；
2. 哪些选择已经明确写入规范；
3. 哪些选择仍要靠 RTL 和编译器原型证明可行。

规范的机器可读真值源是 `isa/vtx1/isa.yaml`。正文用大白话解释理由，但编码、操作数约束和指令清单最终都以 YAML 为准。

## 2. 为什么不采用单一 GPR

“所有值都放进一种通用寄存器”看起来简单，实际会把 GPU 最关键的区别藏起来：有些值是整个 warp 共用一份，有些值是每个 lane 各有一份。

如果只有一种 GPR，硬件和工具链会立刻遇到这些问题：

- 一个 warp 共用的循环计数、基址和常量，可能被复制成 32 份相同值，白白占用容量和读端口；
- 指令只看寄存器号，不能直接知道一次算一份还是按 lane 算 32 份，译码、记分牌和编译器都要在别处补一套隐含规则；
- 标量比较产生的是一个 warp 共享真假值，向量比较产生的是逐 lane 真假值；把两者塞进普通 GPR，会让条件执行和分歧控制变得含糊；
- 编译器无法清楚表达“这是统一值，可以广播”和“这是逐 lane 值，不能合并”，寄存器分配、活跃区间和 spill 代价都会失真；
- 物理实现很难分别优化小而高复用的统一数据通路与大而多 bank 的逐 lane 数据通路。

因此，VTX-1 明确分开：

- `s0..s255`：SGPR，每个 warp 一份，每个寄存器 32 位；
- `v0..v255`：VGPR，每个真实 lane 一份，每个寄存器 32 位；
- `vp0..vp15`：向量谓词，每个 warp 一份 32 位 lane 掩码；
- `SCC`：每个 warp 一份 1 位标量条件码。

这不是为了让汇编语法更花哨，而是让“数据属于谁、指令执行几次、结果写到哪里”在 ISA 上直接可见。向量指令可以通过 scalar-source selector 直接读一个 SGPR（见第 8.7 节）；反方向从 lane 取值必须使用规范明确列出的操作，例如 `S_READFIRST`，不能让实现随便挑一个 lane 猜代表值。

## 3. 为什么物理寄存器文件放在 SM/CU，并按 warp 切片

架构寄存器描述软件能命名的状态；物理寄存器文件是 SM/CU 上真正提供容量、bank 和端口的存储体。CTA 驻留时，SM/CU 为每个 resident warp 分配独立切片：

```text
每 warp SGPR 槽数 = sgpr_count
每 warp VGPR 槽数 = 32 * vgpr_count
每 warp vp 槽数   = vp_count
```

这样做的理由很直接：

- warp 是发射、暂停、恢复和隐藏延迟的基本调度单位，寄存器生命周期天然跟着 warp；
- 不同 warp 必须能交错执行，独立切片能让记分牌和所有权清楚，不会互相覆盖；
- SGPR 与 VGPR 的容量和端口压力不同，分开计数后，occupancy 可以反映真实资源消耗；
- CTA 的全部 warp 驻留在同一 SM/CU，才能稳定访问各自寄存器切片、同一块 shared memory 和 CTA 屏障状态；
- 尾 warp 即使没有 32 个真实 lane，实现仍可按完整切片分配，以换取更简单的寻址和 bank 规则。

“按 warp 切片”是资源分配和隔离规则，不强迫 RTL 为每个 warp 造一块独立 SRAM。实现可以使用共享的多 bank 物理寄存器文件，只要每个 resident warp 得到互不重叠的容量，并满足规范可见行为。

## 4. 为什么需要 scalar-ready

标量指令每个 warp 只执行一次。如果 warp 正在跑一条分歧路径，此时修改一份共享 SGPR，稍后执行的另一条路径会看到什么值就会变得不可靠。仅检查“当前 active lane 的值碰巧一样”也不够，因为另一条尚未执行的路径仍可能存在。

所以所有 `execution_domain=scalar` 的指令，在读取任何动态源之前都必须满足：

```text
live_mask != 0
active_mask == live_mask
重汇聚栈中不存在 FIRST 或 SECOND 帧
```

大白话说：warp 里还有活 lane，所有活 lane 此刻都在一起，而且没有另一条分支路径等着跑。三个条件合起来就是 scalar-ready。

检查失败固定产生 `DIVERGENCE_FAULT`，不读 SGPR、SCC、地址或内存源，也不留下部分结果。SCC 条件为假不能绕过检查。普通向量指令不要求 scalar-ready，因为它只修改当前参与 lane 的 VGPR 或 `vp` 位；即使它通过 selector 读一个 SGPR 也不需要，因为读共享值不会破坏另一条路径将看到的内容。

同一个检查还有两处非 scalar 用户。使用 warp 统一调用栈的 `CALL`、`CALL.IND`、`JUMP.IND` 和 `RET` 虽属于 CONTROL，也要先检查 scalar-ready，避免用一份返回状态代表尚未重汇聚的多条路径。`BAR.SYNC.CTA` 同样要求 scalar-ready，理由见第 8.6 节：让 warp 只能整体到达屏障，能删掉一大批只为容纳“部分 lane 到达”而存在的状态。

## 5. 为什么不采用扁平 major

把每个操作都塞进一个全局 major 编号，短期看像是一张简单 opcode 表，长期会把三个不同问题绑死在一起：

1. 这是什么类型的操作；
2. 操作数怎么摆位；
3. 具体是哪一个可执行 form。

这样会导致相同的寄存器布局反复定义、立即数和寄存器 form 到处打补丁、译码器出现大量逐 major 特例。新增一个操作数布局时，还会被迫重新切全局编号空间。硬件前端难以尽早知道要走标量、向量、访存还是控制通路，验证工具也难以系统枚举保留位和 must-zero 规则。

VTX-1 的 64 位头部改为分层译码：

```text
class[3:0] | format[2:0] | opcode[5:0] | guard[5:0] | payload[44:0]
```

- `class` 先选执行大类；
- `format` 在该 class 内选操作数装箱方式；
- `opcode` 只在 `(class, format)` 内局部编号；
- `(class, format, opcode)` 唯一确定一个 form；
- form 再给出 payload 字段、must-zero 位、操作数类型和精确语义。

这种结构让译码前段、操作数读取和静态验证有稳定边界，也让保留组合能统一拒绝。family 只是语义分组，不参与译码；form 才是唯一机器叶子。

## 6. 八个 class 与 class-specific format

当前 Draft 明确使用 8 个 class：

| 编码 | class | 负责内容 |
|---:|---|---|
| 0 | `SYS` | 系统、特殊寄存器、陷阱和杂项 |
| 1 | `SALU` | 标量算术与逻辑 |
| 2 | `VALU` | 逐 lane 向量算术与逻辑 |
| 3 | `MEMORY` | 标量/向量访存与原子 |
| 4 | `CONTROL` | 分支、调用、重汇聚和控制流 |
| 5 | `SYNC` | 屏障与内存同步 |
| 6 | `CROSSLANE` | warp 内跨 lane 集合操作 |
| 7 | `MATRIX` | warp 协作矩阵操作 |

`class=8..15` 保留并必须拒绝。format 不是全局含义，而是 class-specific：

- SALU 使用 `S1/S2/S3/SCMP/SIMM`；
- VALU 使用 `V1/V2/V3/VCMP/VIMM`；
- MEMORY 使用 `SMEM/VMEM/VSHMEM/VLMEM/SATOM/VATOM/SMEMX/VATOMX`；
- CONTROL、SYNC、CROSSLANE、MATRIX 分别使用自己的 `CTRL/SYNC/COLL/MMA` 布局。

这样，同一种格式只负责稳定摆放操作数，不偷带算术语义；同一个 family 也可以有寄存器 form 和立即数 form。每个 form 必须把 payload 的每一位归入操作数、已定义修饰字段或 must-zero，禁止“实现忽略”未定义位。

## 7. 为什么直接控制目标采用 PC-relative

所有直接控制目标统一使用 `CTRL.disp30`，相对于 `next_pc` 计算：

```text
next_pc   = pc + 8
target_pc = next_pc + (sign_extend_30(disp30) << 3)
```

位移单位是 8 字节指令字。这样做可以让同一段代码在文本内整体移动时保持内部控制关系不变，链接器只需要处理一种明确的 PC-relative 重定位，也不会同时存在绝对地址、字地址和字节地址三套解释。

目标必须 8 字节对齐，必须落在当前内核文本的一条完整指令上；位移溢出必须报错，不能截断。直接控制指令不得另行发明绝对目标字段。

## 8. 关键能力与取舍

### 8.1 `SMEMX` 和 `VATOMX`

`SMEMX` 解决的是“整个 warp 只有一个地址，但地址还要加一个统一索引”的情况：

```text
EA = SGPR64_base + zero_extend(SGPR32_index) + sign_extend(imm16)
```

它仍是 scalar memory：先检查 scalar-ready，成功后整个 warp 只产生一个内存事件。index 单位固定为 1 字节，未定义的 `mods` 位必须为零，不能因为多了 index 就偷偷按 lane 执行。

`VATOMX` 解决的是“统一基址加逐 lane 索引”的 global vector atomic：

```text
EA[lane] = SGPR64_base + zero_extend(VGPR32_index[lane])
```

它是 vector form，每个参与 lane 产生一个原子事件。scale 固定为 1，没有 immediate 字段，保留位 `x` 必须为零。任何参与 lane 的 allocation 范围或对齐检查失败，整条指令都不产生原子事件。地址空间不必检查：它由 opcode 固定，寄存器里的地址值只是位模式。

### 8.2 完整 atomic modifier

原子的 operation、type 和 space 由 form/opcode 固定；`order` 和 `scope` 是同一个 form 里的 payload modifier。大白话说，`ADD.RELAXED.DEVICE` 和 `ADD.ACQ_REL.SYSTEM` 是同一类 ADD form 的不同合法参数，不应为了后缀组合复制 opcode、family 或 form。

完整合法矩阵是：

| operation | 合法 order | global 合法 scope | shared 合法 scope |
|---|---|---|---|
| `LOAD` | `RELAXED`、`ACQUIRE` | `CTA`、`DEVICE`、`SYSTEM` | `CTA` |
| `STORE` | `RELAXED`、`RELEASE` | `CTA`、`DEVICE`、`SYSTEM` | `CTA` |
| RMW 与 `CAS` | `RELAXED`、`ACQUIRE`、`RELEASE`、`ACQ_REL` | `CTA`、`DEVICE`、`SYSTEM` | `CTA` |

canonical 名称必须完整写成 `(S_ATOM|V_ATOM).<op>.<type>.<space>.<order>.<scope>`。交换只叫 `XCHG`。保留 `scope=3` 是 `ILLEGAL_INSTRUCTION`；已定义 modifier 拼成表外组合是 `ILLEGAL_OPERAND`。两种错误都必须在读取地址和数据源之前报告。

### 8.3 Warp 统一 CALL 栈

每个 warp 有一套程序不可见的 LIFO 调用栈，descriptor 用 `call_stack_depth` 声明可用深度，范围是 0..16。每个调用帧只保存：

```text
return_pc = call_pc + 8
```

`CALL`、`CALL.IND`、`JUMP.IND` 和 `RET` 都属于 CONTROL/warp-control，并要求 scalar-ready。直接和间接 CALL 只有在目标、返回 PC 和栈容量全部合法后，才把 `return_pc` 与新 PC 一起原子提交；`JUMP.IND` 只改 PC，不碰调用栈；`RET` 从栈顶恢复最近的返回地址。

调用栈不保存 active mask 或重汇聚状态。为了不让被调用代码带着未闭合分歧返回，`SSY` 建立的重汇聚帧会记录 `owner_call_depth`；`RET` 必须先确认当前调用深度没有未闭合帧。失败时不能留下半压栈、半弹栈或已改变的 PC。

### 8.4 B64 跨域

跨域 64 位值使用两个真实机器 form：

```text
V_MOV.B64 v0:v1, s0:s1          # ssrc=1，SGPR64 到各 lane
S_READFIRST.B64 s0:s1, v0:v1    # 最低编号 live lane 到 SGPR64
```

两端都必须是完整、偶数对齐、连续且不越界的寄存器对。S 到 V 方向不需要专门的指令：`V_MOV.B64` 的源是 `vsrc64`，把 selector 置 1 就让那 8 位寄存器号在 SGPR 文件中解释。`S_READFIRST.B64` 先检查 scalar-ready，再从最低编号 live lane 整体读取一对 VGPR。两个 32 位半部必须来自同一次冻结的同一个源，不能低半来自一个 lane、高半来自另一个 lane。

### 8.5 唯一 MMA

当前 MATRIX class 只定义一个 MMA form：

```text
MMA.M16N8K16.F16.F16.F32
```

它固定 32 lane 全员会合、header guard 为 PT、A/B 为 F16、C/D 为 F32，并固定片段寄存器数量、对齐、lane/元素映射和 `k=0..15` 的累加顺序。唯一允许的目标别名是 `D=C` 完整别名；部分重叠以及 D 与 A/B 重叠都非法。所有源先冻结，全部结果通过检查后一次提交。其他形状、类型或 modifier 不能从保留位猜出来。

### 8.6 为什么 CTA 屏障只有一条指令

`execution_domain=cta_sync` 只提供一条屏障指令：

```text
BAR.SYNC.CTA id
```

看起来少了一样东西：把“到达”和“等待”分开的 split 屏障。这是刻意不要的。要让 `BAR.ARRIVE`/`BAR.WAIT` 这类接口在分歧下也说得清，规范必须同时定义每 lane 的 VGPR token、token 上不可伪造的隐藏标签、标签绑定的 `{CTA identity, linear_tid, slot, logical generation}`、永不回绕的 generation 计数、`EMPTY/SYNC/SPLIT` 模式隔离、`consumed_set`，以及一条覆盖全 ISA 每个 VGPR 写目标的标签清除闭包。这些机制没有一条能省：只要 token 可以被复制、跨代复活或由非 owner 消费，split 语义就不成立。

而它换来的能力，软件用 shared memory 上的原子加 `MEMBAR` 就能自己搭出来，那种写法完全落在第 4 章的内存模型里，不需要任何新的架构状态。用一整套隐藏状态换一个库函数能实现的接口，不划算。

不要 split 之后，屏障状态机小到可以整段读完：

- 每 CTA 固定 8 槽 `0..7`，每槽只有 `arrived_set` 和 waiter 映射，两者都空就是 idle；
- owner 唯一身份仍是 `linear_tid=warp_id*32+lane_id`；
- CTA 另有一个 8 槽共用的 `live_owner_set`，初值是全部真实线程，只在 `EXIT` 时缩小；
- 完成条件是 `arrived_set == live_owner_set`，随后所有 waiter 一起 acquire 并恢复，槽立即清空回 idle；
- waiter 固定为 `BarrierWaitRecord {warp_id, owner_snapshot, resume_pc}`，每 warp 至多一条 blocked record，阻塞整个 warp，恢复只改 PC 和 ready，不改掩码或控制栈；
- arrival 是 shared CTA release，恢复是 shared CTA acquire，不替代 global 原子或 `MEMBAR`。

`BAR.SYNC.CTA` 要求 scalar-ready，这是让上面这张表能这么小的关键。warp 只能整体到达，于是模式隔离、重复到达检查和 wrong-owner 检查全部没有对象；分歧 warp 在记录任何 arrival 之前就报 `DIVERGENCE_FAULT`，也就不存在“部分 arrival 需要回滚”的情况。屏障因此不需要专属故障码。

完成条件与 `live_owner_set` 比较，而不是与启动时固定的 owner 集合比较。代价是 `EXIT` 缩小该集合后要重新检查每个非 idle 槽；收益是 `EXIT` 不需要任何屏障前置检查——退出线程只是从集合里消失，剩下的 owner 少等一个人。`EXIT` 本身不贡献 shared release，所以被它放行的 waiter 只获得真正到达者贡献的 release。

### 8.7 为什么向量指令直接读一个 SGPR

第 2 节说 uniform 值应该待在 SGPR 里。但如果编码里没有字段能表达“这个源读 SGPR”，那句话就落不了地：能用的办法只剩一条专门的广播指令，先把 uniform 值搬进 VGPR，再拿 VGPR 去算。代价是每个 uniform 值多占一个 VGPR、多一条指令，而且那个 VGPR 里 32 份值完全相同——正是第 2 节说要避免的浪费。

所以这件事在格式层解决，而不是靠一条搬运指令绕开。四种 VALU 格式各带一个 scalar-source selector：

| format | selector | 扩展位 | 合法值 |
|---|---|---|---|
| `V1` | `ssrc`（1 位） | `x28` | 0 无 / 1 `va` |
| `V2`、`VCMP` | `ssrc_sel`（2 位） | `x19` | 0 无 / 1 `va` / 2 `vb`，3 保留 |
| `V3` | `ssrc_sel`（2 位） | `x11` | 0 无 / 1 `va` / 2 `vb` / 3 `vc` |

selector 不改变源槽的位置和宽度，只决定那 8 位寄存器号在哪个寄存器文件里解释。清单里能这样切换的源写成 `vsrc32` 或 `vsrc64` 类型，共 90 个 form 用到；目标永远是 VGPR 或 `vpN`，不受影响。不含 `vsrc*` 的 form 里 selector 是 must-zero 洞。

“最多一个 SGPR 源”也是刻意选的，不是位数不够。多个标量源需要多个 SGPR 读端口和更宽的选择网络，而收益集中在少数几种场景；一个源就能覆盖绝大多数模式：边界比较 `V_CMP.LT.U32 vp0, v5, s6`、跨步累加、`V_MAD.U32 v9, v8, s4, v6` 这样的行主序索引计算。真需要两个 uniform 值时，软件显式先搬一个进 VGPR，汇编器不得自动插入搬运——否则寄存器压力会在编译器背后变化。

因此架构不需要独立的广播指令。`V_MOV.B32 vd, ss` 和 `V_MOV.B64 vd_pair, s_pair` 就是要物化时的写法，而大多数情况连这条 move 都不需要，直接把 `sN` 写在算术指令的源位置更短。

selector 规则同时出现在编码表、逐指令语义、汇编规则、合规矩阵、selector 编码向量和严格 schema 中。

### 8.8 为什么寄存器不带影子状态

寄存器只保存位模式。SGPR、VGPR、`vp` 和 `SCC` 都不携带隐藏的 per-register 状态，任何写寄存器的 form 只改写那些位。

诱人的替代方案是给寄存器挂软件不可直接读写的隐藏标签，典型的两种是 barrier token 标签和 pointer provenance。两者都要求规范逐条 form 说明标签如何创建、复制和清除——也就是一条覆盖全 ISA 每个写目标的闭包——还要求实现在每个 VGPR 写端口上多带一位状态。

barrier token 的用途随 split 屏障一起消失（第 8.6 节）。provenance 单独看也站不住：地址空间由 opcode 固定，每条访存 form 的操作数类型和助记符后缀都写明空间，运行期不需要再从寄存器里读一份空间身份来核对；allocation 范围检查是实现内部按 allocation 表做的，与寄存器内容无关。既然空间不由地址值决定，generic 地址空间也就没有存在理由，架构不提供它。

`GLOBAL_PTR` 和 `CONST_PTR` 仍在参数布局记录里，但它们是静态声明，不是运行期标签。

### 8.9 为什么清单把布局和绑定分开

`isa.yaml` 里，物理布局归 `format_registry` 独占：form 只声明 `encoding_format`、`opcode` 和操作数绑定，未绑定的 payload 字段自动成为 must-zero 洞。只有两类信息确实无法派生，允许逐 form 覆盖：`field_values` 用于固定常量（`MEMBAR` 的 `scope2/order2`），`field_notes` 用于 form 专属的字段描述（`V_SHUFFLE.DOWN.B32` 的立即数 delta form）。

换成“每个 form 自带完整字段表”看起来更直白，实际是把同一份 `V2` 布局在几十个 form 里逐字复制，包括头部四个字段和全部 must-zero 洞。任何布局改动都要同步几十处，而布局改动恰恰是这类清单最常见的改动——`V1/V2/V3/VCMP` 切出 selector 位就是一例，注册表模型下只动四处。

展开逻辑集中在 `tools/isa_model.py`，验证器、构建器、向量生成器和测试共用同一份实现，不会出现四套略有差异的解释。

family ID 是语义 slug（`v-add`、`bar-sync`）而不是流水号。数字 ID 没有任何机器含义——family 不参与译码——却会让增删一个 family 变成一次重编号。

## 9. 当前明确的设计决策

以下内容已经是 1.0 Draft 的明确规范选择：

- 固定 64 位、小端指令字，正常 `next_pc = pc + 8`；
- SGPR、VGPR、`vp`、SCC 是不同的架构状态和编号空间；
- SGPR/VGPR 物理容量位于 SM/CU，并按 resident warp 分配互不重叠的切片；
- 指令 form 明确声明 `system/scalar/vector/warp_control/warp_collective/cta_sync/warp_matrix` 七种执行域之一；
- 所有 scalar form 执行前统一检查 scalar-ready，`CALL/CALL.IND/JUMP.IND/RET` 和 `BAR.SYNC.CTA` 额外要求它；
- `V1/V2/V3/VCMP` 各带一个 scalar-source selector，一条向量指令最多一个 SGPR 源；普通向量 form 不能写 SGPR 或 SCC；
- 寄存器只保存位模式，没有 barrier token 标签，也没有 pointer provenance；
- 采用 8 个 class、class-specific format 和局部 opcode；form 是唯一译码叶子；
- header guard 按 form 的 `guard_policy` 判断，不按机器 class 猜；只有 `execution_domain: vector` 的 form 可以声明 optional guard；
- 所有直接控制目标使用相对 `next_pc` 的 `disp30`；
- `SMEMX` 固定为统一基址加统一索引，`VATOMX` 固定为统一基址加逐 lane 索引；
- atomic 的 operation form 与 `order/scope` modifier 分开计数，并执行完整合法矩阵；
- CALL 使用每 warp 一套、每帧只保存 `return_pc` 的隐藏 LIFO 栈；
- 混合源 `V_MOV.B64` 和 `S_READFIRST.B64` 整体传递 64 位值；
- 屏障只有 `BAR.SYNC.CTA`：8 个槽、`arrived_set` 加 waiter、CTA 级 `live_owner_set`，无 token、无 generation、无模式；BAR 只排序 shared；
- 地址空间由 opcode 决定，没有 `GENERIC` 空间；
- MATRIX class 只启用 `MMA.M16N8K16.F16.F16.F32` 一个 form；
- 未分配 class/format/opcode、保留 guard、保留 selector 码和非零 must-zero 位都必须拒绝；
- form 不重复书写可从 `format_registry` 派生的 `class`、`format` 和字段表；family ID 是语义 slug；
- family/form 数量由 YAML 自动生成和校验，不由手工文档维护。

## 10. 当前清单数量

当前 `isa/vtx1/isa.yaml` 生成的清单是：

- **66 个 instruction families**
- **379 个 instruction forms**

这两个数字是最终审计时从当前 YAML 去重生成的结果，不是手工填写的常量。`order`、`scope`、地址 modifier 和 scalar-source selector 的合法取值另算 modifier instance，不混入 form 总数。任何人工文字与 YAML 不一致时，以 YAML 为准，并应修复生成或同步流程。

## 11. 仍需 RTL 原型验证

下面这些方向已经有规范意图，但还不能只靠文字认定实现代价合理：

- `class → format → opcode` 分层译码的面积、时序和非法编码早期拒绝路径；
- SGPR/VGPR/VP 物理文件的 bank 数、读写端口、广播网络和冲突处理；
- 按 warp 切片的分配粒度、碎片率，以及 `sgpr_count/vgpr_count/vp_count` 对 occupancy 的真实影响；
- scalar-ready 检查与重汇聚栈、调用栈、故障优先级之间是否能在发射前可靠完成；
- SALU/VALU 同时运行时的记分牌、跨域依赖，以及 selector 选中 SGPR 源时的读端口和广播旁路；
- 45 位 payload 下各 class-specific format 的布线复杂度与关键路径；
- PC-relative 控制、间接控制、调用返回和精确故障的流水线冲刷行为；
- `SMEMX/VATOMX` 地址形成、完整 atomic modifier 矩阵和逐 lane 原子事件生成；
- CALL 栈与 `owner_call_depth` 重汇聚约束的原子提交和回滚；
- B64 跨域整体旁路，以及唯一 MMA 的会合与整体提交；
- 单一 `BAR.SYNC.CTA` 的槽状态、`live_owner_set` 更新和 `EXIT` 触发的重新检查；
- must-zero、寄存器组边界和静态操作数错误能否在不读取动态源时准确报告。

RTL 原型至少应给出可综合译码器、寄存器文件/记分牌模型、最小控制流水线和代表性时序/面积报告，而不是只写行为模拟器。

## 12. 仍需编译器原型验证

编译器侧需要用真实 kernel 证明这些选择不会把复杂度转移成不可接受的代码膨胀：

- uniformity 分析能否稳定把统一值放入 SGPR、逐 lane 值放入 VGPR；
- SGPR/VGPR 双寄存器分配、spill 和 occupancy 权衡是否可预测；
- 分歧区间内 scalar-ready 的合法放置能否由控制流分析证明，而不是依赖人工约定；
- `S_READFIRST` 等显式跨域操作是否足够，是否出现大量不必要的域转换；
- SCC 与 `vp` 分别承载标量条件和 lane 条件后，比较、选择和分支 lowering 是否自然；
- class-specific 立即数容器是否覆盖常见常量，超范围常量需要多少物化指令；
- PC-relative `disp30` 的汇编、链接、重定位和溢出诊断是否完整；
- `SMEMX/VATOMX` 地址模式选择，以及 atomic space/order/scope 后缀的完整生成；
- CALL 栈深度估算、间接调用目标约束和 callee 重汇聚区域闭合检查；
- B64 跨域操作的寄存器对分配；
- uniform 值直接作 selector 源与先物化进 VGPR 供多次复用之间的取舍，以及“最多一个 SGPR 源”会迫使多少次显式搬运；
- 指令选择表是否能从 YAML 生成（包括 selector 与操作数寄存器文件的绑定），避免后端手写重复清单；
- 资源描述中的 `sgpr_count/vgpr_count/vp_count` 是否能由最终分配结果准确生成。

至少应选择分支密集、访存密集、标量控制密集、原子同步和矩阵计算等代表性 kernel，比较代码大小、寄存器压力、occupancy 和动态指令数。

## 13. 最终规范审计

**审计结论：PASS。**

本次最终审计确认：

1. YAML 去重生成的清单为 66 families / 379 forms，生成参考与清单一致；
2. 每个 form 的 `(class, format, opcode)` 唯一，payload 位都有定义或 must-zero 约束，且 `class`、`format` 和字段表由 `format_registry` 派生而非逐 form 重复；
3. `SMEMX/VATOMX` 地址模板、atomic modifier 矩阵、CALL 栈、混合源 selector、B64 跨域搬运、单一 `BAR.SYNC.CTA` 状态机和唯一 MMA 都有明确编码、语义、故障与合规门禁；
4. modifier instance 与 selector 取值都与 family/form 统计分开，不会虚增 form 数；
5. scalar-ready、guard、寄存器域、控制目标、调用/重汇聚关系和整体提交规则在文档与 YAML 中保持一致，且两者都不含寄存器影子标签、pointer provenance 或 generic 地址空间的描述；
6. family/form 数量、编码参考和 all-form 覆盖清单都要求从 YAML 生成。

这里的 PASS 是规范内容、YAML 和生成资产的最终一致性审计结论。RTL 面积/时序和编译器代码质量仍按上两节通过原型验证；不能把文档审计 PASS 写成硬件实现已经完成。

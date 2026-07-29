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

这不是为了让汇编语法更花哨，而是让“数据属于谁、指令执行几次、结果写到哪里”在 ISA 上直接可见。向量指令可以读取 SGPR，并把同一个值广播给所有参与 lane；跨域取值必须使用规范明确列出的操作，例如 `S_READFIRST`，不能让实现随便挑一个 lane 猜代表值。

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

检查失败固定产生 `SCALAR_STATE_FAULT`，不读 SGPR、SCC、地址或内存源，也不留下部分结果。SCC 条件为假不能绕过检查。普通向量指令不要求 scalar-ready，因为它只修改当前参与 lane 的 VGPR 或 `vp` 位。使用 warp 统一调用栈的 `CALL`、`CALL.IND`、`JUMP.IND` 和 `RET` 虽属于 CONTROL，也要先检查 scalar-ready，避免用一份返回状态代表尚未重汇聚的多条路径。

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

## 8. 最终补齐的关键能力

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

它是 vector form，每个参与 lane 产生一个原子事件。scale 固定为 1，没有 immediate 字段，保留位 `x` 必须为零。任何参与 lane 的地址、空间、provenance 或对齐检查失败，整条指令都不产生原子事件。

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
V_BCAST.B64 v0:v1, s0:s1
S_READFIRST.B64 s0:s1, v0:v1
```

两端都必须是完整、偶数对齐、连续且不越界的寄存器对。`V_BCAST.B64` 把一份 SGPR64 整体广播到参与 lane 的 VGPR64；`S_READFIRST.B64` 先检查 scalar-ready，再从最低编号 live lane 整体读取一对 VGPR。64 位数值和 pointer provenance 必须一起复制，不能低半来自一个 lane、高半来自另一个 lane，也不能只复制数值却丢掉地址空间身份。

### 8.5 唯一 MMA

当前 MATRIX class 只定义一个 MMA form：

```text
MMA.M16N8K16.F16.F16.F32
```

它固定 32 lane 全员会合、header guard 为 PT、A/B 为 F16、C/D 为 F32，并固定片段寄存器数量、对齐、lane/元素映射和 `k=0..15` 的累加顺序。唯一允许的目标别名是 `D=C` 完整别名；部分重叠以及 D 与 A/B 重叠都非法。所有源先冻结，全部结果通过检查后一次提交。其他形状、类型或 modifier 不能从保留位猜出来。

### 8.6 命名 CTA 屏障语义修复

命名屏障已经收紧为一套可直接实现和验证的状态机：

- 每 CTA 固定 8 槽 `0..7`；owner 唯一身份是 `linear_tid=warp_id*32+lane_id`，所有 owner/arrived/consumed 集合只保存 linear_tid；
- 规范指令固定为 `BAR.SYNC.CTA id`、`BAR.ARRIVE.CTA vd,id`、`BAR.WAIT.CTA id,vs`，保留 F061/F062/F063 及原译码三元组；
- SPLIT token 改为每 lane VGPR32，隐藏标签恰好绑定 `{CTA identity,linear_tid,slot,logical generation}`；位值不能伪造身份；
- generation 改为数学非负整数 `N`：从 0 开始，退休严格加 1，单调且永不回绕；有限实现必须做到 as-if 不回绕，禁止旧、已消费或复制 token 因内部计数值/对象编号复用而复活；
- VGPR32 写入默认清除旧 token 标签；唯二例外是 BAR.ARRIVE 创建标签、寄存器型 `V_MOV.B32` 复制标签。YAML 根规则可自动枚举全部 VGPR 写目标 form；
- 第一批合法 arrival 选择 `SYNC` 或 `SPLIT`，同代混用、重复 arrival、错误或重复 token 都整条 `BARRIER_FAULT` 回滚；
- waiter 固定为 `BarrierWaitRecord {warp_id,owner_snapshot,resume_pc}`；每 warp 至多一条 blocked record，阻塞整个 warp，恢复只改 PC/ready，不改掩码或控制栈；
- SYNC 到齐后全体记录一起 acquire、恢复并立即退休；SPLIT 到齐只标 completed，必须等全部 owner 的 token 恰好消费一次才退休；
- `EXIT` 不缩小 owner；未消费义务由 SPLIT 槽的 `arrived_set-consumed_set` 判断，与 VGPR tag 是否尚存无关；
- CTA 只有在全部 warp 完成且 8 个槽都 IDLE 时完成；任意非负 logical generation 不妨碍完成，但它绝不回绕到旧逻辑值；
- BAR 只给 shared 建立 CTA release/acquire，不替代 global 原子或 `MEMBAR`。

这次修复同时进入执行伪代码、内存边、编码表、逐指令语义、完整测试矩阵、YAML operand/form 摘要和严格 schema。family/form 数量不变。

## 9. 当前明确的设计决策

以下内容已经是 1.0 Draft 的明确规范选择：

- 固定 64 位、小端指令字，正常 `next_pc = pc + 8`；
- SGPR、VGPR、`vp`、SCC 是不同的架构状态和编号空间；
- SGPR/VGPR 物理容量位于 SM/CU，并按 resident warp 分配互不重叠的切片；
- 指令 form 明确声明 `system/scalar/vector/warp_control/warp_collective/cta_sync/warp_matrix` 七种执行域之一；
- 所有 scalar form 执行前统一检查 scalar-ready；
- 向量 form 可读取 SGPR 作为广播源，但普通向量 form 不能写 SGPR 或 SCC；
- 采用 8 个 class、class-specific format 和局部 opcode；form 是唯一译码叶子；
- header guard 按 form 的 `guard_policy` 判断，不按机器 class 猜；只有 `execution_domain: vector` 的 form 可以声明 optional guard；
- 所有直接控制目标使用相对 `next_pc` 的 `disp30`；
- `SMEMX` 固定为统一基址加统一索引，`VATOMX` 固定为统一基址加逐 lane 索引；
- atomic 的 operation form 与 `order/scope` modifier 分开计数，并执行完整合法矩阵；
- CALL 使用每 warp 一套、每帧只保存 `return_pc` 的隐藏 LIFO 栈；
- `V_BCAST.B64` 和 `S_READFIRST.B64` 整体传递 64 位值及 provenance；
- 命名屏障使用固定 owner、单 generation、`EMPTY/SYNC/SPLIT` 模式和逐 lane VGPR token；BAR 只排序 shared；
- MATRIX class 只启用 `MMA.M16N8K16.F16.F16.F32` 一个 form；
- 未分配 class/format/opcode、保留 guard 和非零 must-zero 位都必须拒绝；
- family/form 数量由 YAML 自动生成和校验，不由手工文档维护。

## 10. 当前清单数量

当前 `isa/vtx1/isa.yaml` 生成的清单是：

- **69 个 instruction families**
- **392 个 instruction forms**

这两个数字是最终审计时从当前 YAML 去重生成的结果，不是手工填写的常量。`order`、`scope` 和地址 modifier 的合法取值另算 modifier instance，不混入 form 总数。任何人工文字与 YAML 不一致时，以 YAML 为准，并应修复生成或同步流程。

## 11. 仍需 RTL 原型验证

下面这些方向已经有规范意图，但还不能只靠文字认定实现代价合理：

- `class → format → opcode` 分层译码的面积、时序和非法编码早期拒绝路径；
- SGPR/VGPR/VP 物理文件的 bank 数、读写端口、广播网络和冲突处理；
- 按 warp 切片的分配粒度、碎片率，以及 `sgpr_count/vgpr_count/vp_count` 对 occupancy 的真实影响；
- scalar-ready 检查与重汇聚栈、调用栈、故障优先级之间是否能在发射前可靠完成；
- SALU/VALU 同时运行时的记分牌、跨域依赖和 SGPR 广播旁路；
- 45 位 payload 下各 class-specific format 的布线复杂度与关键路径；
- PC-relative 控制、间接控制、调用返回和精确故障的流水线冲刷行为；
- `SMEMX/VATOMX` 地址形成、完整 atomic modifier 矩阵和逐 lane 原子事件生成；
- CALL 栈与 `owner_call_depth` 重汇聚约束的原子提交和回滚；
- B64 跨域数值/provenance 整体旁路，以及唯一 MMA 的会合与整体提交；
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
- B64 跨域操作的寄存器对分配与 provenance 保持；
- 69 family / 392 form 的指令选择表是否能从 YAML 生成，避免后端手写重复清单；
- 资源描述中的 `sgpr_count/vgpr_count/vp_count` 是否能由最终分配结果准确生成。

至少应选择分支密集、访存密集、标量控制密集、原子同步和矩阵计算等代表性 kernel，比较代码大小、寄存器压力、occupancy 和动态指令数。

## 13. 最终规范审计

**审计结论：PASS。**

本次最终审计确认：

1. YAML 去重生成的清单为 69 families / 392 forms，生成参考与清单一致；
2. 每个 form 的 `(class, format, opcode)` 唯一，payload 位都有定义或 must-zero 约束；
3. `SMEMX/VATOMX` 地址模板、atomic modifier 矩阵、CALL 栈、B64 跨域、命名屏障状态机和唯一 MMA 都有明确编码、语义、故障与合规门禁；
4. modifier instance 与 family/form 统计分开，完整 atomic 后缀组合不会虚增 form 数；
5. scalar-ready、guard、寄存器域、控制目标、调用/重汇聚关系、provenance 和整体提交规则在文档与 YAML 中保持一致；
6. family/form 数量、编码参考和 all-form 覆盖清单都要求从 YAML 生成。

这里的 PASS 是规范内容、YAML 和生成资产的最终一致性审计结论。RTL 面积/时序和编译器代码质量仍按上两节通过原型验证；不能把文档审计 PASS 写成硬件实现已经完成。

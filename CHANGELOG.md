# vtx-isa：VTX-1 ISA 1.0 Draft 设计记录

本文件记录 1.0 Draft 已经落定的架构选择，以及仍在等待原型结果的项目。规范真值源始终是 `isa/vtx1/isa.yaml`。

Draft 冻结之前，本文件只呈现最终状态：一项决定被后来的决定取代时，原处直接改写，不追加“某天改成了什么”的过程记录。冻结之后才按版本追加变更条目。

## 2026-07-30：VTX-1 ISA 1.0 Draft 基线

### 设计定位

- 将 VTX-1 定义为全新的 SIMT ISA Draft。
- 以 warp 作为取指、PC、分歧控制和调度状态的基本单位。
- 架构描述与机器可读清单保持单一规范解释。

### 寄存器与执行域

- 建立彼此独立的架构寄存器状态：
  - `s0..s255`：每 warp 一份的 32 位 SGPR；
  - `v0..v255`：每真实 lane 一份的 32 位 VGPR；
  - `vp0..vp15`：每 warp 一份的 32 位 lane 谓词掩码；
  - `SCC`：每 warp 一份的 1 位标量条件码。
- 每个 form 必须明确声明 `system/scalar/vector/warp_control/warp_collective/cta_sync/warp_matrix` 七种执行域之一。
- scalar 指令每 warp 执行一次；vector 指令对参与 lane 分别执行；其他执行域分别处理系统状态、warp 控制、集合、CTA 同步或矩阵协作。
- vector 指令可以通过 scalar-source selector 读取一个 SGPR，该值对所有参与 lane 相同；普通 vector 指令不能写 SGPR 或 SCC。
- 跨执行域的数据移动使用明确 form，不允许实现隐式选择代表 lane。
- 寄存器只保存位模式。SGPR、VGPR、`vp` 和 `SCC` 都不携带隐藏影子状态：既没有 barrier token 标签，也没有 pointer provenance 标签。

### 物理寄存器与驻留

- SGPR 和 VGPR 的物理存储位于 SM/CU。
- CTA 驻留时，SM/CU 为每个 resident warp 分配互不重叠的寄存器切片。
- 每 warp 资源需求按以下模型计算：

```text
SGPR = sgpr_count 个 32 位槽
VGPR = 32 * vgpr_count 个 32 位槽
VP   = vp_count 个 32 位掩码槽
```

- CTA 的寄存器、shared memory、local memory 和控制状态共同决定 occupancy。
- 一个 CTA 的全部 warp 驻留在同一 SM/CU；资源不足时 CTA 整体等待。

### Scalar-ready

- 所有 `execution_domain=scalar` 的 form 在读取动态源前统一检查 scalar-ready：

```text
live_mask != 0
active_mask == live_mask
重汇聚栈中不存在 FIRST 或 SECOND 帧
```

- 检查失败产生 `DIVERGENCE_FAULT`，不读取动态源，不形成地址，不写寄存器或内存，也不推进 PC。
- SCC 条件为假不能绕过 scalar-ready。
- 普通 vector 指令不套用该检查，即使它通过 selector 读一个 SGPR 也不需要。
- 使用 warp 统一调用栈的 `CALL`、`CALL.IND`、`JUMP.IND` 和 `RET` 额外要求 scalar-ready，但机器 class 仍为 `CONTROL`。
- `BAR.SYNC.CTA` 也要求 scalar-ready，使 warp 只能整体到达屏障。
- 调用栈每 warp 一份、后进先出，descriptor 的 `call_stack_depth` 范围为 0..16；每帧只保存 `return_pc=PC+8`。
- `SSY` 重汇聚帧记录 `owner_call_depth`。`RET` 必须先确认当前 callee 没有未闭合重汇聚帧，再把栈顶返回地址和 PC 一起提交。

### 64 位分层编码

- 每条指令固定为 64 位，小端存储，正常顺序执行时 `next_pc = pc + 8`。
- 统一头部确定为：

```text
class[3:0] | format[2:0] | opcode[5:0] | guard[5:0] | payload[44:0]
```

- `opcode` 在 `(class, format)` 内局部编号；三元组 `(class, format, opcode)` 唯一确定一个 form。
- family 只做语义分组，不参与机器译码。
- 每个 form 必须完整声明 payload 字段、操作数类别、立即数解释、guard policy、must-zero 位、静态约束和执行语义。
- 未分配 class/format/opcode、保留 guard 编码和非零 must-zero 位统一作为非法指令拒绝。

### 八个编码 class

- `SYS`：系统、特殊寄存器、陷阱和杂项。
- `SALU`：标量算术与逻辑。
- `VALU`：逐 lane 向量算术与逻辑。
- `MEMORY`：标量/向量访存与原子。
- `CONTROL`：分支、调用、重汇聚和控制流。
- `SYNC`：屏障与内存同步。
- `CROSSLANE`：warp 内跨 lane 集合操作。
- `MATRIX`：warp 协作矩阵操作。
- class 编码 8..15 保留并必须拒绝。

### Class-specific format

- SALU：`S1/S2/S3/SCMP/SIMM`。
- VALU：`V1/V2/V3/VCMP/VIMM`。
- MEMORY：`SMEM/VMEM/VSHMEM/VLMEM/SATOM/VATOM/SMEMX/VATOMX`。
- CONTROL：`CTRL`。
- SYNC：`SYNC`。
- CROSSLANE：`COLL`。
- MATRIX：`MMA`。
- SYS：`SYS`。
- format 只在所属 class 中有意义；未列出的 class/format 组合保留并必须拒绝。

### Guard 与条件状态

- `PT`、`!PT`、`vp0..vp15`、`!vp0..!vp15` 使用统一 6 位 guard 编码。
- guard 合法性按 form 的 `guard_policy` 判断，不按 class 或 format 猜；只有 `execution_domain: vector` 的 form 可以使用 optional guard。
- SALU、scalar memory、scalar atomic、CONTROL、SYNC、CROSSLANE 和 MATRIX 的 header guard 必须为 PT。
- `SCC` 是标量条件状态，`vpN` 是逐 lane 条件状态，两者不占用普通 SGPR/VGPR 编号。
- 静态编码错误不能被 false guard 掩盖。

### 混合源：每条向量指令一个 SGPR 源

- 四种 VALU 格式各带一个 scalar-source selector 字段：`V1` 是 1 位 `ssrc`，扩展位为 `x28`；`V2` 和 `VCMP` 是 2 位 `ssrc_sel`，扩展位为 `x19`；`V3` 是 2 位 `ssrc_sel`，扩展位为 `x11`。
- selector 不改变源槽的位置和宽度，只决定那 8 位寄存器号在哪个寄存器文件中解释。selector 为 0 表示所有源都读 VGPR；非 0 时恰好一个源改在 SGPR 文件中解释。
- `V1` 只能选 `va`；`V2` 和 `VCMP` 可选 `va` 或 `vb`，码 3 保留并产生 `ILLEGAL_INSTRUCTION`；`V3` 可选 `va`、`vb` 或 `vc`。
- 操作数类型 `vsrc32` 和 `vsrc64` 表示“寄存器文件由 selector 决定”的源。只有 `execution_domain: vector` 且格式属于 `V1/V2/V3/VCMP` 的 form 允许使用；90 个 form 采用了它们。
- 不含 `vsrc*` 操作数的 form 中，selector 字段是 must-zero 洞。
- 目标操作数永远是 VGPR 或 `vpN`，不受 selector 影响。
- 一条向量指令最多一个 SGPR 源。架构没有独立的广播指令；需要两个 uniform 值时软件必须显式先搬一个进 VGPR，汇编器不得自动插入搬运。

### PC-relative 控制

- 所有直接控制目标使用 `CTRL.disp30`。
- 目标相对于当前指令的 `next_pc` 计算：

```text
target_pc = next_pc + (sign_extend_30(disp30) << 3)
```

- 位移单位为 8 字节指令字。
- 目标必须 8 字节对齐，并指向当前内核文本中的完整指令。
- 汇编和重定位溢出必须报错，不得截断。

### 地址空间

- 一次访存落在哪个空间完全由 opcode 决定：每条访存 form 的操作数类型固定写明空间，助记符也带同一个空间后缀。
- 寄存器里的地址值只是位模式，不携带空间身份；实现禁止在运行期根据数值猜测空间。
- 架构没有 generic 地址空间。
- `GLOBAL_PTR` 和 `CONST_PTR` 是参数布局记录上的静态声明，不是运行期标签。
- allocation 范围检查在实现内部按 allocation 表完成，与寄存器内容无关。

### `SMEMX` 与 `VATOMX`

- `SMEMX` 是 scalar memory 的统一索引格式：

```text
EA = SGPR64_base + zero_extend(SGPR32_index) + sign_extend(imm16)
```

- `SMEMX` 的 index 单位固定为 1 字节，未定义 `mods` 位必须为零；它先检查 scalar-ready，成功后整个 warp 只产生一个事件。
- `VATOMX` 是 global vector atomic 的统一基址加逐 lane 索引格式：

```text
EA[lane] = SGPR64_base + zero_extend(VGPR32_index[lane])
```

- `VATOMX` 的 scale 固定为 1，没有 immediate 字段；每个参与 lane 产生一个原子事件，任一参与 lane 失败则整条零事件回滚。

### 完整 atomic modifier

- atomic canonical 名称固定为：

```text
(S_ATOM|V_ATOM).<op>.<type>.<space>.<order>.<scope>
```

- operation form 选择 `LOAD/STORE/ADD/MIN/MAX/AND/OR/XOR/XCHG/CAS`、数据类型、空间和地址模板；`order/scope` 是同一 form 中的 payload modifier。
- `LOAD` 只允许 `RELAXED/ACQUIRE`；`STORE` 只允许 `RELAXED/RELEASE`；RMW 与 CAS 允许 `RELAXED/ACQUIRE/RELEASE/ACQ_REL`。
- global 允许 `CTA/DEVICE/SYSTEM` scope；shared 只允许 `CTA`。
- `scope=3` 是保留编码并产生 `ILLEGAL_INSTRUCTION`；合法 modifier 值拼成表外组合产生 `ILLEGAL_OPERAND`。
- modifier instance 单独展开测试，不复制成新 family/form，也不混入 form 统计。

### B64 跨域

- 跨域 64 位传递使用两个真实机器 form：混合源 `V_MOV.B64 vd_pair, s_pair`（`ssrc=1`）从 SGPR64 送到各参与 lane 的 VGPR64，`S_READFIRST.B64` 从最低编号 live lane 读到 SGPR64。
- 两端都必须是完整、偶数对齐、连续且不越界的寄存器对。
- `S_READFIRST.B64` 先检查 scalar-ready。
- 两个 32 位半部必须来自同一次冻结和同一个源，不能拆开选择或部分提交。

### CTA 屏障

- `execution_domain=cta_sync` 只有一条屏障指令 `BAR.SYNC.CTA id`。架构不提供 split 屏障、屏障 token、generation 计数或子集屏障；`(5,0,4)` 和 `(5,0,5)` 未分配并必须拒绝。
- 需要“先到达、后等待”的软件用 shared memory 上的原子操作和 `MEMBAR` 自行构造，这些结构完全落在既有内存模型内。
- 每 CTA 固定 8 个槽 `0..7`；owner 唯一身份是 `linear_tid=warp_id*32+lane_id`，所有集合以 `linear_tid` 为元素。
- 槽状态只有 `arrived_set` 和 waiter 映射；两者都空即为 idle，启动时 8 个槽全部 idle。
- CTA 另有 8 槽共用的 `live_owner_set`：初值是全部真实线程的 `linear_tid`，不含不存在的尾 lane；`EXIT` 从中移除退出线程，这是它唯一会变小的方式。没有 `expected` 字段。
- 完成条件是 `arrived_set == live_owner_set`；所有 waiter 在同一个完成动作中恢复，随后槽立即清空回 idle。`EXIT` 缩小 `live_owner_set` 时也要重新检查每个非 idle 槽。
- `BAR.SYNC.CTA` 要求 scalar-ready，因此分歧 warp 在记录任何 arrival 之前就报 `DIVERGENCE_FAULT`，不留下部分 arrival、blocked record 或 PC 效果。
- waiter 固定为 `BarrierWaitRecord {warp_id, owner_snapshot, resume_pc}`；每 warp 同时至多一条 blocked record。屏障阻塞整个 warp 当前动态路径，挂起路径不能切入；恢复只写记录中的 PC 并置 ready，不改 active/live 掩码、重汇聚栈或调用栈。
- `EXIT` 没有屏障前置检查，也不报屏障故障，并且不贡献 shared release。
- 屏障 arrival 是 shared CTA release，恢复是 shared CTA acquire；global、local、param、const 和 host 不自动排序。
- CTA 只有在全部 warp 完成且 8 个槽都 idle 时完成。

### 故障表

- 故障码共 10 个，`fault_priority` 单独定义优先级顺序。
- `DIVERGENCE_FAULT` 的语义是“这条 form 需要完全重汇聚的 warp，而当前 warp 不是”。它与 `RECONVERGENCE_FAULT` 的分界写在 `docs/02-programming-model.md`。
- 屏障协议没有专属故障码：屏障的唯一入口条件是 scalar-ready，违反它就是 `DIVERGENCE_FAULT`。
- `DEADLOCK` 的典型来源是一部分 warp 到达屏障，其余 owner 既不到达也不退出。

### 唯一 MMA

- MATRIX class 当前只定义：

```text
MMA.M16N8K16.F16.F16.F32
```

- 该 form 固定 PT、32 lane 全员会合、A/B 为 F16、C/D 为 F32，并固定片段寄存器数量、对齐、lane/元素映射和逐 `k=0..15` 累加顺序。
- 只允许 `D=C` 完整别名；所有源先冻结，全部 D 结果检查通过后一次提交。
- 其他形状、类型和 modifier 不得从保留位推测。

### Shuffle-down 的两个 delta form

- opcode 11 是 `V_SHUFFLE.DOWN.B32 vd,vs,vdelta,width`，opcode 13 是 `V_SHUFFLE.DOWN.B32 vd,vs,delta,width`。
- 立即数 delta 为 `0..31`，编码在 COLL `vb`；width 按字面值编码在 `imm8`，合法集合为 `{2,4,8,16,32}`。
- 两个 form 的参与、源冻结、缺失源写零和 `COLLECTIVE_FAULT` 语义完全相同；立即数 form 用于消除固定归约树中的 delta 物化指令。

### 清单结构与真值源

- `isa.yaml` 中物理布局与操作数绑定分离：`format_registry` 独占字段表，form 只声明 `encoding_format`、`opcode` 和操作数绑定，不重复 `class`、`format`、`fields`。未绑定的 payload 字段自动成为 must-zero 洞。
- 仅两类无法派生的信息允许逐 form 覆盖：`field_values`（固定常量，用于 `MEMBAR` 的 `scope2/order2`）和 `field_notes`（form 专属字段描述，用于 `V_SHUFFLE.DOWN.B32` 的立即数 delta form）。
- family ID 是语义 slug，例如 `v-add`、`bar-sync`；它不参与译码，也不承载编号顺序。
- `tools/isa_model.py` 是加载与展开的唯一实现，验证器、构建器、向量生成器和测试共用它。`tools/gen_vectors.py` 生成 `encoding_vectors.json`，其中包含每个 selector 码的向量。

### 当前指令清单

`isa/vtx1/isa.yaml` 当前生成：

- **66 个 instruction families**
- **379 个 instruction forms**

数量由 YAML 去重生成，不作为手工维护常量。`order`、`scope`、地址 modifier 和 scalar-source selector 的合法取值另算 modifier instance，不混入 form 统计；出现不一致时以 YAML 为准。

### 已确定但仍需实现证明的边界

- SGPR/VGPR 编号空间、资源计数和组对齐是架构约束；具体物理 bank、端口和旁路属于实现选择。
- 按 warp 切片要求容量互不重叠；不要求每个 warp 拥有独立物理 SRAM。
- class-specific format 固定字段语义；具体译码流水级和执行单元组织属于实现选择。
- scalar-ready 的可见结果和故障顺序已经确定；检查电路如何与重汇聚栈、调用栈和记分牌集成仍待 RTL 验证。
- PC-relative 目标算法已经确定；链接器代码布局策略和可选显式跳板变换仍由工具链设计。
- `SMEMX/VATOMX` 地址公式、atomic modifier 矩阵、CALL 栈、混合源 selector、B64 跨域和唯一 MMA 已是规范决定；它们的流水线、bank、旁路和调度代价仍属于原型验证内容。

### 待 RTL 原型记录

- 分层译码面积、频率和非法编码拒绝路径。
- SGPR/VGPR/VP bank 与端口配置、广播网络和冲突处理。
- resident warp 切片粒度、资源碎片和 occupancy 曲线。
- scalar-ready、重汇聚、调用返回与精确故障的流水线实现。
- SALU/VALU 并行发射、跨域依赖和记分牌规则。
- `SMEMX/VATOMX` 地址形成与完整 atomic modifier 译码。
- CALL 栈、`owner_call_depth` 和精确控制提交。
- scalar-source selector 的 SGPR 读端口、广播网络和 B64 跨域旁路，以及唯一 MMA 的会合、提交和回滚。
- 屏障槽状态、`live_owner_set` 更新和 `EXIT` 触发的重新检查。

### 待编译器原型记录

- uniformity 分析和 SGPR/VGPR 分类准确性。
- 双寄存器文件分配、spill 成本与 occupancy 权衡。
- 分歧区域内 scalar-ready 合法性分析。
- `S_READFIRST` 等显式跨域 form 的覆盖度和动态成本。
- SCC、`vp` 条件 lowering 与控制流结构化。
- 立即数物化、代码密度和 class-specific format 覆盖度。
- `disp30` 汇编、链接、重定位和溢出诊断。
- `SMEMX/VATOMX` 地址模式选择和 atomic modifier 后缀生成。
- CALL 栈深度估算、callee 重汇聚闭合和 B64 偶数寄存器对分配。
- 从 YAML 生成后端描述和测试清单，包括 selector 与操作数寄存器文件的绑定。
- uniform 值是直接用作 selector 源，还是先物化进 VGPR 供多次复用，这个取舍的收益。

### 最终规范审计

- **结论：PASS。**
- YAML 去重清单为 66 families / 379 forms，生成参考与 all-form 清单一致。
- 所有 form 的机器 class、执行域、唯一译码、payload 覆盖、guard、required state 和 must-zero 规则通过一致性检查，其中 `class`、`format` 和字段表由 `format_registry` 派生而非逐 form 重复书写。
- `SMEMX/VATOMX`、完整 atomic modifier、CALL 栈、混合源 selector、B64 跨域搬运、单一 `BAR.SYNC.CTA` 和唯一 MMA 均已进入编码、语义、故障和合规门禁。
- modifier instance 与 selector 取值都与 family/form 统计分离，不会虚增 form 数。
- 文档与 YAML 都不含 token 标签、pointer provenance 或 generic 地址空间的残留描述。
- 此 PASS 表示规范、YAML 和生成资产的最终审计通过；RTL 时序/面积和编译器质量仍由原型结果单独证明。

## 记录规则

- 这里只记录 Draft 设计决策、理由变化和原型结论。
- Draft 冻结前只呈现最终状态：取代旧决定时改写原处，不保留被取代的写法，也不记录改写过程。
- 每条记录应说明影响的 YAML 字段、规范章节和验证资产。
- family/form 数量只引用 YAML 生成值。
- 尚无 RTL 或编译器证据的性能、面积和代码质量判断必须标为“待验证”。
- 已进入 YAML 的规范决策与纯实现建议必须分开书写，避免把某一种微架构误写成 ISA 强制要求。

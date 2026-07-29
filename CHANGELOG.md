# vtx-isa：VTX-1 ISA 1.0 Draft 设计记录

本文件记录 1.0 Draft 设计重置期间已经落定的架构选择，以及仍在等待原型结果的项目。规范真值源始终是 `isa/vtx1/isa.yaml`。

## 2026-07-29：建立全新 1.0 Draft 基线

### 修复命名 CTA 屏障语义退化

- 每 CTA 固定 8 个命名槽 `0..7`；owner 唯一身份固定为 `linear_tid=warp_id*32+lane_id`，owner/arrived/consumed 集合全部保存 linear_tid。
- 规范指令定为 `BAR.SYNC.CTA id`、`BAR.ARRIVE.CTA vd,id`、`BAR.WAIT.CTA id,vs`；F061/F062/F063、`(5,0,3/4/5)` 编码和 family/form 数量保持不变。
- split token 定为每 lane VGPR32，隐藏标签恰好绑定 `{CTA identity,linear_tid,slot,logical generation}`。
- generation 改为数学非负整数 `N`：从 0 开始，每次退休严格加 1，单调且永不回绕。有限计数器、epoch 或对象编号的复用不得让旧、已消费或复制 token 在后续代重新匹配；实现可用更宽 epoch、capability ID、安全回收或等效办法达到 as-if 不回绕。
- 建立全 ISA VGPR 标签写回闭包：任意写入 VGPR32 槽默认清除旧标签，唯二例外是 BAR.ARRIVE 创建、寄存器型 `V_MOV.B32` 复制；YAML 根规则和 schema 严格列出两个例外。
- 每代第一批合法 arrival 选择 `SYNC` 或 `SPLIT`。同代混用、重复 arrival、stale/foreign/wrong-slot/wrong-owner/malformed/untagged/duplicate token 都产生整条 warp 动态指令的 `BARRIER_FAULT`，不允许部分效果。
- waiter 固定为 `BarrierWaitRecord {warp_id,owner_snapshot,resume_pc}`；每 warp 同时至多一条 blocked record。BAR 阻塞整个 warp，挂起路径不能切入；恢复只写记录中的 PC 并置 ready。
- SYNC 到齐时所有记录一起 acquire 并立即退休；SPLIT 到齐只标 completed，所有 owner token 恰好消费一次后才退休。
- `EXIT` 不缩小 owner；任一退出 linear_tid 位于 SPLIT 槽 `arrived_set-consumed_set` 时直接故障，与 VGPR tag 是否仍存在无关。
- CTA 完成要求全部 warp 完成且 8 槽都 IDLE；任意非负 logical generation 允许，但 generation 不得回绕。
- BAR arrival 是 shared CTA release，SYNC 恢复和成功 WAIT 是 shared CTA acquire；global、local、param、const 和 host 不自动排序。
- 同步更新 `docs/00-status.md` 到 `docs/08-conformance.md`、YAML、schema 和设计审查，加入状态转移伪代码、编码示例及完整合规矩阵。

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
- vector 指令可以读取 SGPR 并向参与 lane 广播同一个值；普通 vector 指令不能写 SGPR 或 SCC。
- 跨执行域的数据移动使用明确 form，不允许实现隐式选择代表 lane。

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

- 检查失败产生 `SCALAR_STATE_FAULT`，不读取动态源，不形成地址，不写寄存器或内存，也不推进 PC。
- SCC 条件为假不能绕过 scalar-ready。
- 普通 vector 与 warp-control 指令不套用该检查。
- 使用 warp 统一调用栈的 `CALL`、`CALL.IND`、`JUMP.IND` 和 `RET` 额外要求 scalar-ready，但机器 class 仍为 `CONTROL`。
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

### PC-relative 控制

- 所有直接控制目标使用 `CTRL.disp30`。
- 目标相对于当前指令的 `next_pc` 计算：

```text
target_pc = next_pc + (sign_extend_30(disp30) << 3)
```

- 位移单位为 8 字节指令字。
- 目标必须 8 字节对齐，并指向当前内核文本中的完整指令。
- 汇编和重定位溢出必须报错，不得截断。

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

- `V_BCAST.B64` 和 `S_READFIRST.B64` 是真实机器 form。
- 两者都要求完整、偶数对齐、连续且不越界的 SGPR/VGPR 对。
- `V_BCAST.B64` 把 SGPR64 及其 provenance 整体广播到参与 lane。
- `S_READFIRST.B64` 先检查 scalar-ready，再从最低编号 live lane 整体读取 VGPR64 及其 provenance。
- 两个 32 位半部必须来自同一次冻结和同一个源，不能拆开选择或部分提交。

### 唯一 MMA

- MATRIX class 当前只定义：

```text
MMA.M16N8K16.F16.F16.F32
```

- 该 form 固定 PT、32 lane 全员会合、A/B 为 F16、C/D 为 F32，并固定片段寄存器数量、对齐、lane/元素映射和逐 `k=0..15` 累加顺序。
- 只允许 `D=C` 完整别名；所有源先冻结，全部 D 结果检查通过后一次提交。
- 其他形状、类型和 modifier 不得从保留位推测。

### 当前指令清单

`isa/vtx1/isa.yaml` 当前生成：

- **69 个 instruction families**
- **391 个 instruction forms**

数量由 YAML 去重生成，不作为手工维护常量。`order`、`scope` 和地址 modifier 的合法组合另算 modifier instance，不混入 391 forms；出现不一致时以 YAML 为准。

### 已确定但仍需实现证明的边界

- SGPR/VGPR 编号空间、资源计数和组对齐是架构约束；具体物理 bank、端口和旁路属于实现选择。
- 按 warp 切片要求容量互不重叠；不要求每个 warp 拥有独立物理 SRAM。
- class-specific format 固定字段语义；具体译码流水级和执行单元组织属于实现选择。
- scalar-ready 的可见结果和故障顺序已经确定；检查电路如何与重汇聚栈、调用栈和记分牌集成仍待 RTL 验证。
- PC-relative 目标算法已经确定；链接器代码布局策略和可选显式跳板变换仍由工具链设计。
- `SMEMX/VATOMX` 地址公式、atomic modifier 矩阵、CALL 栈、B64 跨域和唯一 MMA 已是规范决定；它们的流水线、bank、旁路和调度代价仍属于原型验证内容。

### 待 RTL 原型记录

- 分层译码面积、频率和非法编码拒绝路径。
- SGPR/VGPR/VP bank 与端口配置、广播网络和冲突处理。
- resident warp 切片粒度、资源碎片和 occupancy 曲线。
- scalar-ready、重汇聚、调用返回与精确故障的流水线实现。
- SALU/VALU 并行发射、跨域依赖和记分牌规则。
- `SMEMX/VATOMX` 地址形成与完整 atomic modifier 译码。
- CALL 栈、`owner_call_depth` 和精确控制提交。
- B64 数值/provenance 跨域旁路与唯一 MMA 的会合、提交和回滚。

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
- 从 YAML 生成 69 family / 391 form 的后端描述和测试清单。

### 最终规范审计

- **结论：PASS。**
- YAML 去重清单为 69 families / 391 forms，生成参考与 all-form 清单一致。
- 所有 form 的机器 class、执行域、唯一译码、payload 覆盖、guard、required state 和 must-zero 规则通过一致性检查。
- `SMEMX/VATOMX`、完整 atomic modifier、CALL 栈、B64 跨域和唯一 MMA 均已进入编码、语义、故障和合规门禁。
- modifier instance 与 family/form 统计分离，atomic 后缀组合不会虚增 form 数。
- 此 PASS 表示规范、YAML 和生成资产的最终审计通过；RTL 时序/面积和编译器质量仍由原型结果单独证明。

## 后续记录规则

- 这里只记录 Draft 设计决策、理由变化和原型结论。
- 每条记录应说明影响的 YAML 字段、规范章节和验证资产。
- family/form 数量只引用 YAML 生成值。
- 尚无 RTL 或编译器证据的性能、面积和代码质量判断必须标为“待验证”。
- 已进入 YAML 的规范决策与纯实现建议必须分开书写，避免把某一种微架构误写成 ISA 强制要求。

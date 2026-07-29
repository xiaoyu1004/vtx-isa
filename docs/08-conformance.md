# 8. 合规性

本章定义 **VTX-1 ISA 1.0 Draft** 的强制测试。适用对象包括汇编器、反汇编器、译码器、模拟器、RTL、设备、装载器和参考模型。只跑几个示例不能判定通过；每个启用 form 都必须进入 all-form 覆盖。

## 8.1 结果和测试记录

测试结果只有三种：

- `PASS`：适用于该对象的强制测试全部通过，并且启用 form 覆盖率为 100%。
- `FAIL`：任一强制测试失败、缺少 oracle、缺少 coverage tag，或发现未解释差异。
- `NOT_APPLICABLE`：测试对象不承担该角色。基础 ISA 中已启用的 form 不能标成这一项。

每次测试运行至少记录：

```json
{
  "suite_version": "1.0-draft",
  "isa_version": "1.0-draft",
  "yaml_digest": "...",
  "enabled_features": [],
  "dut_name": "...",
  "dut_revision": "...",
  "seed": "0x0000000000000000",
  "families_total": 0,
  "forms_total": 0,
  "forms_passed": 0,
  "failures": []
}
```

`families_total` 和 `forms_total` 必须在运行时从当前 YAML 去重计算。测试源码、文档和报告模板都不得写死数量。

所有随机测试必须保存 64 位 seed 和最小化后的失败输入。允许多种调度或 modification-order 结果时，oracle 必须给出允许集合或可执行判定器，不能只拿某一次参考运行作标准答案。

## 8.2 测试向量格式

规范仓库中的向量必须是机器可读的 YAML 或 JSON。每个用例至少包含：

```text
id
requirement
form_id
required_features
dut_roles
initial_state
program_or_word
schedule_constraints
expected_state or allowed_outcomes
forbidden_outcomes
expected_fault
state_must_remain
coverage_tags
```

初始状态必须明确：

- PC、live mask、active mask 和隐藏控制状态；
- SGPR、VGPR、1 位 SCC、32 位 `vp0..vp15` 和特殊寄存器快照；
- scalar-ready 是否成立；
- 内存空间、allocation、provenance、初值和原子 modification order；
- barrier 的 linear_tid 集合、BarrierWaitRecord/warp blocked record、token、MMA/collective 会合状态；
- 文本范围、模块字段和启用 feature。

测试不能依赖未初始化值，除非目标就是检查 `UNSPEC` 分类；这类测试不能要求某个具体位型。

## 8.3 固定 64 位编码

### 8.3.1 每个 form 的正向向量

生成器必须从 YAML 为每个启用 form 至少生成：

1. 字段最小合法值；
2. 字段最大合法值；
3. 每个合法 selector 和 modifier；
4. 每种 SGPR、VGPR、`vpN` 的最低、最高和组边界，以及对声明 SCC 源的 form 测试 SCC 两个值；
5. 每个立即数槽的最小值、最大值、零和负边界（若为有符号）；
6. 每种合法 guard；
7. 允许的完整源/目标别名；
8. 直接目标、间接目标和调用目标的边界；
9. 汇编、机器字、反汇编、再次汇编的闭环。

每条机器指令必须恰好 8 字节。测试要直接检查：

```text
instruction_bits == 64
next_sequential_pc == pc + 8
encoded_bytes == little_endian(machine_word)
```

机器字期望值必须由独立编码 oracle 按 YAML 位段构造，不能调用被测汇编器生成。

### 8.3.2 分类树

对 YAML 中每个 form 检查：

```text
class ∈ {SYS,SALU,VALU,MEMORY,CONTROL,SYNC,CROSSLANE,MATRIX}
execution_domain ∈ {system,scalar,vector,warp_control,warp_collective,cta_sync,warp_matrix}
一个机器字只选出一个 form
一个 form 只落入一个机器 class
```

测试必须把 8 个 machine class 和 7 个 `execution_domain` 分开取数、分开报告。至少验证 `SYS` 可以承载 system/scalar/vector form、`MEMORY` 可以承载 scalar/vector form，以及 `class` 不能从 `execution_domain` 机械推导。每个 form 的实际组合必须与 YAML 两个字段逐字一致。

文档可以用 S/V/W/SYNC/X/MMA 帮读者找指令，但测试不得把这些简称当成编码字段。显示简称时必须遵守：

```text
SALU 通常显示在 S
VALU 通常显示在 V
scalar/vector MEMORY 分别显示在 S/V
CONTROL 显示在 W
SYNC/MATRIX 分别显示在 SYNC/MMA
CROSSLANE 中 collective form 通常显示在 X；
V_BCAST/S_READFIRST 可按数据方向显示在 V/S，但不改变 machine class
SYS 中的 scalar/vector form 可分别放进 S/V 说明，纯系统 form 保持 SYS
```

还必须逐项检查：

- S form 的普通数据操作数不引用 V 域；
- V form 的目标不直接写 S 域；
- `CONTROL` form 在用户可读说明中只能按 W 控制流解释；
- `CALL`、`CALL.IND`、`JUMP.IND`、`RET` 必须保持 `class=CONTROL`，不能放入 SALU；
- SYNC、CROSSLANE、MATRIX 不能伪装成 SALU/VALU；
- `V_BCAST`、`X_BROADCAST`、`S_READFIRST` 的 machine class、`execution_domain` 分别与 YAML 一致；
- `V_BCAST` 固定为 S→V，`X_BROADCAST` 固定为 lane→lane，`S_READFIRST` 固定为 V→S；
- `V_BCAST` 与 `V_GETREG` 即使分别编码在 CROSSLANE/SYS，也必须保持 `execution_domain=vector`、`guard_policy=optional`；
- `S_READFIRST` 必须保持 `execution_domain=scalar`、`guard_policy=required_pt`、`required_state=scalar_ready`；
- `V_BCAST`、`X_BROADCAST`、`S_READFIRST`、`S_GETREG`、`V_GETREG` 的名称、方向和操作数域固定；
- 文本 `S_BROADCAST` 被汇编器拒绝；
- `BRA`、`BRA.P`、`SSY`、`JOIN`、`EXIT`、`CALL`、`CALL.IND`、`JUMP.IND`、`RET` 的 canonical 文本保持这些名称。

### 8.3.3 译码负例

每个 form 都要自动做单比特和组合变异，至少覆盖：

- 未分配 class、family、form 或 selector；
- 每个 must-zero 位单独置 1，以及全部置 1；
- 保留 modifier、非法立即数位置和多个立即数源；
- S/V 寄存器域错配；
- 寄存器组未对齐、越过上限和禁止的部分重叠；
- 把 SCC、`vpN`、SGPR、VGPR 放进错误的源/目标槽；
- 64 位操作数省略第二个寄存器、使用奇数基址、不连续编号或越过 descriptor count；
- guard 不满足 form policy；
- 固定 64 位文本被截短、PC 未按 8 字节对齐；
- 直接目标字段溢出、未对齐、越出文本；
- scope/order 的保留值或非法组合；
- mixed-addressing 使用未列出的 S/V 项组合；
- atomic load 带写数据、atomic store 带读目标、非 CAS 带 replacement；
- MMA 片段字段垃圾、对齐错误和组越界。

静态错误要在以下环境重复：

```text
active mask 全开
active mask 非空但 guard 全假
active mask 为空
```

三种环境必须得到同一静态故障，证明译码不会被 guard 或 active mask 关掉。

### 8.3.4 canonical 文本

对每个合法机器字：

```text
T = disassemble(W, canonical=true)
W2 = assemble(T)
assert W2 == W
assert disassemble(W2, canonical=true) == T
```

非法机器字不能反汇编成可重新组装的合法指令。超范围字面量、错误寄存器域、重复 signed/unsigned 位型拼写和歧义后缀必须在汇编阶段报错，不能静默截断或换 form。

## 8.4 通用执行与精确提交

每个语义用例在目标指令前后逐位比较：

```text
PC
live/active mask
隐藏重汇聚与调用状态
全部 SGPR、VGPR、每 lane barrier-token 标签、SCC 和 `vpN`
目标内存字节
原子 modification order
barrier 和 token 状态
collective/MMA 状态
事件日志
fault record
```

发生故障时，除新增规范 fault record 和 kernel 失败状态外，入口快照必须保持。特别检查：

- PC 不推进；
- S 目标、所有 lane 的 V 目标都不写；
- 成功 lane 的 store 也不提交；
- 原子不占 modification-order 位置；
- CALL 不留下返回项，RET 不提前弹项；
- JOIN 不提前切 active mask；
- SYNC 不留下半次到达；
- X/MMA 不写部分结果。

对每个可能产生 lane 局部故障的 V form，至少测试最低 lane 失败、最高 lane 失败、多个 lane 不同原因失败、失败 lane 被 guard 抑制，以及其他 lane 本来可以成功的情况。

## 8.5 双寄存器和 scalar-ready

### 8.5.1 S 指令只执行一次

每个 S 算术、位、比较、转换、FP、访存和原子 form 至少验证：

- S 源只有一份，不随 lane 号改变；
- scalar-ready 时每个 warp 只产生一次结果；
- S load/store 只产生一个内存事件；
- S atomic 只在 modification order 中占一个位置；
- 目标与源完全别名时按入口快照计算；
- 非 scalar-ready 时固定产生 `SCALAR_STATE_FAULT`，不读取动态源，也不产生任何数据或内存效果；
- `guard_policy` 必须是 `required_pt`；SCC 只在 `S_SELECT` 或 YAML 明确列出的 CONTROL 条件中读取。

这里的“每个 S form”必须覆盖 `SALU`、scalar `MEMORY` 和 scalar `SYS`。不能只测 S ALU 后就声称所有 S 指令通过。

状态优先级还要用毒值验证：静态编码和静态操作数都合法，但动态源会除零、地址会越界或动态特殊寄存器值会非法时，只要入口非 scalar-ready，就只能得到 `SCALAR_STATE_FAULT`，证明实现根本没有读取动态源。所有多故障组合的唯一权威顺序来自 `docs/02-programming-model.md`；本章测试从该顺序生成期望值，不另写第二套优先级。

所有 `sgpr64`/`vgpr64` 操作数还必须检查 canonical 对语法：

- 只接受 `sE:s(E+1)` 或 `vE:v(E+1)`，且 E 为偶数；
- 前槽是低 32 位，后槽是高 32 位；
- 编码、反汇编和再汇编保留完整范围；
- 奇数基址、跳号、反序、缺半和末端越界都报 `ILLEGAL_OPERAND`；
- 多对操作数别名时按完整 64 位入口快照执行，不能出现半写回。

### 8.5.2 V 指令逐 lane 执行

每个 V form 使用可识别的 lane 数据，检查：

- P 中每个 lane 独立读源和写目标；
- guard-false 与 inactive lane 的目标保持；
- 不同 lane 可以得到不同结果和地址；
- 任一 lane 动态故障使整个 warp 指令回滚；
- 同名 V 寄存器在不同 lane 之间不别名。

### 8.5.3 scalar-ready 状态矩阵

必须构造以下状态：

```text
SR1  live_mask 非空，active_mask == live_mask，无重汇聚帧
SR2  只有一个 live lane，active_mask == live_mask
SR3  live_mask 非空，active_mask == live_mask，只有 ARMED 帧
NS1  live_mask == 0
NS2  active_mask 是 live_mask 真子集
NS3  active_mask == live_mask，但栈中有 FIRST 帧
NS4  active_mask == live_mask，但栈中有 SECOND 帧
```

SR1..SR3 必须判为 scalar-ready；NS1..NS4 必须判为非 scalar-ready。这个矩阵必须套到每个 S form；所有 NS 用例都期望：

```text
code = SCALAR_STATE_FAULT
lane_mask = active_mask
aux = 0
```

`S_READFIRST` 专项检查：

- SR1 中读取编号最小 live lane；
- 尾 warp 中跳过物理不存在 lane；
- SR2 正常读取唯一 lane；
- SR3 在只有 ARMED 帧时正常读取；
- 其他 V lane 值不同不构成故障；
- NS1..NS4 产生 `SCALAR_STATE_FAULT`；
- 状态故障时不读取 V 源、不写 SGPR、不改 PC、不改隐藏状态。

## 8.6 跨域与 GETREG

### 8.6.1 V_BCAST

启用 form 清单必须至少包含 `.B32` 和 `.B64`；缺少任一项即 FAIL。对所有宽度和合法 guard 检查：

- form 固定为 `execution_domain: vector`、`guard_policy: optional`，即使 machine class 是 CROSSLANE；
- `P = E & guard`；PT、`vpN`、`!vpN` 分别覆盖全体、真子集、空集；
- P 中每个 lane 得到同一个冻结 S 值；
- 非参与 lane 保持旧 V 目标；
- S 源与其他指令并行更新的实现仍使用入口快照；
- 不要求 scalar-ready；
- 在 NS2..NS4 中仍按入口 E 和 guard 形成 P，不能误报 `SCALAR_STATE_FAULT`；
- 目标是 V 域，源是 S 域；
- 反向操作数、V 源或 S 目标被拒绝。

`V_BCAST.B64` 必须使用完整偶数连续 S/V 寄存器对，并增加 provenance 用例：

- 把带 global、param、const provenance 的 SGPR64 分别广播到一组参与 lane，逐 lane 比较 64 位数值和完整 `{space, allocation-id, offset}`；
- guard-false lane 的旧 VGPR64 数值和旧 provenance 都保持；
- 无 provenance 的普通 B64 只复制位型，不凭数值猜 tag；
- `V_BCAST.B32` 不产生 provenance；若它覆盖已带 tag 的 64 位槽的一半，按第 4 章检查 tag 清除；
- 奇数基址、缺半、越界和部分寄存器对写回都必须拒绝或整条回滚。

### 8.6.2 S_READFIRST

启用 form 清单必须至少包含 `.B32` 和 `.B64`；缺少任一项即 FAIL。除 scalar-ready 矩阵外，还要检查 form 固定为 `guard_policy: required_pt`，first-lane 选择不受物理调度、lane 执行先后或值大小影响。first 永远是编号最小的 live lane。

`S_READFIRST.B64` 必须让不同 lane 持有数值相同但 provenance 不同，以及数值不同但 provenance 相同的 VGPR64。结果的 64 位值和 tag 都只能来自同一个 first lane。再执行 `V_BCAST.B64` → `S_READFIRST.B64` 往返，必须逐位、逐字段保持 provenance。B64 目标必须整体提交，B32 不得产生 provenance。

### 8.6.3 X_BROADCAST

对 YAML 中每个 `X_BROADCAST` form 检查：

- 源来自指定 lane 的 VGPR，接收者得到冻结的同一源值；
- 立即数 lane 和 SGPR lane selector（若对应 form 存在）都按各自字段解释；
- 不同 lane 值能证明实现没有误读 SGPR；
- 不存在源 lane、选择不一致或参与协议错误时产生 `COLLECTIVE_FAULT`；
- 故障时所有接收者保持旧目标；
- 汇编器不能把 `X_BROADCAST` 和 `V_BCAST` 互当别名。

### 8.6.4 S_GETREG / V_GETREG

对特殊寄存器表中的每一项自动生成：

- 规范宽度；
- 入口快照稳定性；
- uniform 项由 S_GETREG 和 V_GETREG 读取时数值一致；
- per-lane 项由 V_GETREG 得到逐 lane 值；
- S_GETREG 读取 per-lane 项时报 `ILLEGAL_OPERAND`；
- 未知编号、错误目标宽度和非法寄存器组。

每个合法 `S_GETREG` form 都必须跑 scalar-ready 矩阵；NS1..NS4 只能得到 `SCALAR_STATE_FAULT`。`V_GETREG` 不套用这项检查。

每个 `V_GETREG` form 必须固定验证 `execution_domain: vector`、`guard_policy: optional`，即使 machine class 是 SYS。PT、`vpN`、`!vpN` 都要覆盖；只允许 P 中 lane 读取快照并写回，guard-false lane 保持原目标。非 scalar-ready 状态下仍按 vector 规则执行，不能误报 `SCALAR_STATE_FAULT`。

不得用被测 GETREG 实现生成 oracle 的特殊寄存器期望值。

## 8.7 整数、位运算和 pack

每个 S/V 双版本使用相同输入位型，并比较除寄存器域外完全相同的数学结果。基础向量包括：

```text
0
1
全 1
最高位单独为 1
最低位单独为 1
0xaaaaaaaa / 0x55555555
signed 最小值、最大值
unsigned 最大值
```

wide multiply/MAD 必须覆盖：

- U32 最大值乘最大值；
- S32 最小值、`-1`、0、1、最大值交叉；
- 乘积低 32 位为零但高 32 位非零；
- 64 位 addend 产生跨低半进位；
- 最终 64 位回绕；
- 目标组整体别名和部分重叠负例；
- S/V 版本逐位一致。

普通 `MAD`、`MIN`、`MAX`、`ABS`、`NEG` 逐 form 覆盖：

- `MAD` 的乘法溢出、加法进位和最终 N 位回绕；
- `.S` 与 `.U` 的 MIN/MAX 在 `0x80000000` 对 `1` 时得到不同次序；
- MIN/MAX 两输入相等；
- NEG 的 0、1、全 1 和最小负数；
- ABS 的正数、负数、0 和最小负数；
- S/V 对应 form 使用同一输入位型并得到同一数学位型。

CTZ/rotate 必须覆盖：

```text
CTZ(0) == width
CTZ(1) == 0
CTZ(high_bit) == width-1
rotate amount = 0, 1, width-1, width, width+1
register amount 的模 width 行为
立即数越界在汇编阶段被拒绝
```

PACK/UNPACK 必须做位置基向量：每次只把一个源子字段置 1，确认它只落到规定目标位。还要检查源顺序、高位清零或 base 保留、符号/零扩展，以及原地别名。

`BREV` 必须用单 bit 从最低位移动到最高位、从最高位移动到最低位，以及非对称字节图样，防止实现误做 byte-swap。`BFE`/`BFI` 必须交叉覆盖 offset 为 0、N-1、N、N+1，width 为 0、1、剩余宽度和超出剩余宽度；同时检查 BFI 未覆盖位来自 base、覆盖位来自 insert，并覆盖完整别名。

位型 form 检查器必须拒绝无意义的 S/U 重复项：

```text
MOV/logic/logical-shift/rotate/count/bitfield/pack
同一宽度、同一操作数布局和同一语义只能有一个 B form
```

## 8.8 compare、select、转换和 FP

compare 清单必须先做 S/V 对称性检查：`EQ/NE/LT/LE/GT/GE` 的每个 B32、S32、U32、F32 S form 都要有同关系、同类型的 V form，反过来也一样。整数比较对 signed/unsigned 分别使用能改变次序的样本，例如 `0x80000000` 与 `1`。S 比较写 1 位 SCC；V 比较写 32 位 `vpN`，只更新参与 lane 对应位。

select 必须覆盖 SCC=true/false、`vpN` 对应位为 1/0、vector guard-false、多槽整组选取和源/目标别名。禁止出现低半来自一个源、高半来自另一个源。

每个 F32 compare form 必须覆盖普通有序值、相等值、`-0/+0`、`+Inf/-Inf`、左 NaN、右 NaN和双 NaN。EQ/LT/LE/GT/GE 遇到任一 NaN 必须为假，NE 必须为真。S form 只写 SCC，V form 只更新参与 lane 对应的 `vpN` 位。

CVT 清单同样要求 S/V 对称：每个 `dst-type.src-type` 要么两边都有，要么两边都没有。每对 form 共享同一逐位 oracle和边界向量；另外分别检查 scalar-ready、参与 lane 写回和寄存器域。64 位源或目标必须再跑完整偶数连续寄存器对语法矩阵。

转换和 FP 结果按第 5 章逐位比较。每个适用 form 至少覆盖：

```text
+0, -0, +1, -1
最大有限数、最小 normal
最大 subnormal、最小 subnormal
+Inf, -Inf
多个 qNaN/sNaN payload 和符号
halfway 的下方、正中、上方
```

强制专项：

- FMA 的融合结果与先乘后加不同的样本；
- subnormal 输入、输出和逐级下溢；
- FMIN/FMAX 的单 NaN、双 NaN和 `-0/+0`；
- FABS/FNEG 的 payload 保留；
- F16 到 F32 的全部 F16 位型；
- 每个 V F16 算术 form 的目标高 16 位清零、每 lane 独立结果和 guard-false 目标保持；
- V F16 的全部 65536 个单操作数位型；二/三操作数 form 至少覆盖分类笛卡尔积、halfway 和能区分融合/非融合的定向样本；
- F32 到整数的 NaN、Inf、上下限邻点和饱和；
- `.APPROX` form 的特殊值逐位结果、ULP 上限和确定性；
- 同一输入的 S_FP/V_FP 结果逐位相同。

oracle 必须明确设置舍入模式并关闭 fast-math。宿主语言默认浮点结果不能直接作为标准答案。

## 8.9 普通内存和 mixed addressing

### 8.9.1 地址模板

对每个 mem form，从 YAML 的地址模板自动生成：

```text
uniform-base
lane-address
SV-mix
```

只测试该 form 声明的模板，未声明组合必须是译码或操作数负例。

每个模板至少覆盖：

- base、index、scale 和正/负 immediate；
- 数学结果为 0、allocation 最后一个合法起点；
- base+offset 下溢；
- 乘法或加法超过地址范围；
- 自然对齐合法；
- 范围内但不对齐；
- 对齐但越界；
- 同时不对齐和越界时按 `docs/02-programming-model.md` 权威表选择 fault；
- 跨两个相邻 allocation；
- guard 抑制唯一越界 lane。

`uniform-base` 必须证明 scalar form 只形成一个地址和一个事件。`SMEMX` 另外改变 SGPR64 base、SGPR32 byte index 和 simm16，证明公式是 `base + zero_extend(index) + signed(imm16)`，且仍不读取 VGPR。SMEMX scale 固定为 1，未定义 `mods` 位非零必须拒绝。

VMEM 字段必须按模板交叉验证：

- lane-address：`vaddr` 是每 lane 地址，`sbase` 必须为零，`simm16` 是有符号字节位移；
- SV-mix：`sbase` 是 SGPR64 base，`vaddr` 是每 lane VGPR32 无符号 index，先乘 form 声明的 scale，再加 `simm16`；
- 两类都使用 `vdata` 作为 load 目标/store 源，未定义 `x5` 位必须为零；
- 让各 lane 的 `vaddr` 取 `0x7fffffff/0x80000000/0xffffffff`，防止把 SV-mix index 错做符号扩展；
- 把 SGPR/VGPR 域互换、lane-address 非零 `sbase`、SV-mix 缺少任一项都必须被拒绝。

local 专项必须证明它只有 vector lane-address：同一数值 offset 在不同 lane 指向不同 local allocation。任何 scalar local、uniform-base local、SV-mix local、带非零 `sbase` 的 local 机器字或汇编文本都必须拒绝。

mixed/extended 地址故障逐项覆盖保留 scale/modifier、错误空间/provenance、数学下溢/溢出、allocation 越界和自然对齐失败。普通 SV-mix 的每个合法 scale 都要有正例，未声明 scale 要有负例；SMEMX 和 VATOMX 都要断言 scale 固定为 1。任一参与 lane 失败时事件数必须为零。

### 8.9.2 值、事件和回滚

load/store 检查小端字节顺序、窄 load 扩展、F16 高位清零、向量元素顺序和事件数。

- S_MEM 成功时恰好一个事件；
- V_MEM 每个参与 lane 一个事件；
- V lane 同址不能被测试工具误算成一个架构事件；
- 向量最后一个元素失败时整条指令无事件；
- store 的 S/U 位型重复形式不得同时存在。

### 8.9.3 内存顺序

强制 litmus 至少包括：

- 同 lane 同址写后读；
- release/acquire 消息传递；
- scope 不足时不建立跨代理同步；
- store buffering；
- shared barrier 只排序 shared；
- local 空间 lane 隔离；
- 窄字节写后宽读的小端拼装；
- 无凭空值；
- 普通/原子重叠的禁止分类。

允许结果必须由第 4 章模型产生。禁止结果一旦观察到即 FAIL；允许结果没有观察到不算失败。

## 8.10 S_ATOM/V_ATOM

### 8.10.1 space、order 和 scope

先断言所有规范名称都以 `S_ATOM.` 或 `V_ATOM.` 开头，交换操作只接受 `XCHG`。对每个原子 op/type/space，直接枚举 `order` 和 `scope` modifier：

```text
op                  legal order                           global scope          shared scope
LOAD                RELAXED, ACQUIRE                      CTA/DEVICE/SYSTEM     CTA
STORE               RELAXED, RELEASE                      CTA/DEVICE/SYSTEM     CTA
ADD/MIN/MAX/
AND/OR/XOR/XCHG      四种全部                              CTA/DEVICE/SYSTEM     CTA
CAS                  四种全部                              CTA/DEVICE/SYSTEM     CTA
```

`space` 必须由 form/opcode 固定为 `GLOBAL` 或 `SHARED`，不能从地址寄存器猜测；`order`/`scope` 作为同一操作的编码 modifier 覆盖，测试清单不得把每个组合统计成不同语义 family。shared 非 CTA scope、param/const/local 原子和未知 space/scope 名称都要覆盖。每个合法组合检查自然对齐、空间、地址模板、S/V 事件数和寄存器域。

canonical 文本固定为 `(S_ATOM|V_ATOM).<op>.<type>.<space>.<order>.<scope>`；space 和两个 modifier 都不能省略。对每个合法组合执行：

```text
T = disassemble(W, canonical=true)
assert T 的类型后依次紧跟合法 .<space>.<order>.<scope>
assert assemble(T) == W
assert 删除、交换或重复 space/order/scope 会被拒绝
```

至少 round-trip `S_ATOM.LOAD.U32.GLOBAL.ACQUIRE.DEVICE`、`S_ATOM.XCHG.U32.SHARED.RELAXED.CTA` 和 `V_ATOM.CAS.U64.GLOBAL.ACQ_REL.SYSTEM`。其他交换拼写必须被拒绝。

对 SATOM、VATOM、VATOMX 各取至少一个原始机器字，单独把 `scope` 两位改为 3：必须译码为 `ILLEGAL_INSTRUCTION`，不能反汇编出未知 scope 文本，也不能读取地址/数据源或产生事件。

已定义的 modifier 组成非法组合时才期望 `ILLEGAL_OPERAND`，例如 shared 配 DEVICE scope。测试报告必须把这两类负例分栏，不能合并成“任意非法 modifier”。

`VATOMX` 逐字段检查 `vdst/sbase/vindex/vdata0/vdata1/order/scope/x`，并证明它没有 immediate 容器。canonical 名称中的 space 固定为 `GLOBAL`。每 lane 地址必须等于 `SGPR64_base + zero_extend(VGPR32_index[lane])`，scale 固定为 1。任何额外缩放、非零保留 `x`、错误 provenance/空间、越界和未对齐都做负例，任一 lane 失败时所有 lane 零事件回滚。

### 8.10.2 atomic load/store

atomic load 必须：

- 返回 modification order 中可读的完整旧值；
- 不追加修改；
- 不接受 release-only order；
- U64 不撕裂并一同处理规范 provenance。

atomic store 必须：

- 追加一个完整新值；
- 没有旧值目标；
- 不接受 acquire-only order；
- release 语义能参与消息传递。

测试不能用 RMW 加零代替 atomic load，也不能用 XCHG 代替 atomic store 的 oracle。

### 8.10.3 RMW、CAS 和同址多 lane

ADD/MIN/MAX/AND/OR/XOR/XCHG/CAS 至少覆盖正常值、边界值、返回旧值和最终值。CAS 必须分别覆盖 expected 相等和不等：两种情况都返回 old；成功写 replacement，失败写回 old；两种情况都按第 4 章检查 modification-order 节点数。LOAD 必须没有写数据源，STORE 必须没有旧值目标，CAS 必须同时有 expected、replacement 和 old-value 目标。

V_ATOM 的多个 lane 命中同址时，oracle 枚举所有满足 lane 程序序的顺序，不假定 lane 0 先执行。S_ATOM 同一动态指令必须恰好出现一个原子事件；需要修改的位置在 modification order 中恰好占一个节点。

## 8.11 W 控制流

### 8.11.1 BRA 与间接跳转

BRA、BRA.P、JUMP.IND 至少覆盖：

- 直接前跳、后跳和自环；
- BRA.P 全 taken、全 fall-through 和真正分歧；
- 间接 S 目标的最低、最高合法地址；
- 未对齐、文本外、指令中间和非法机器字目标；
- 尝试使用 V 目标寄存器的静态拒绝。

每步比较 PC、active mask 和隐藏状态摘要。测试接口可以暴露只读调试摘要，但程序本身不能读取隐藏项。

`JUMP.IND` 必须另外跑完整 scalar-ready 矩阵。NS1..NS4 都产生 `SCALAR_STATE_FAULT`，并且不能读取目标 SGPR；直接 `BRA`、`BRA.P` 不套用这项检查。`JUMP.IND` 成功和失败都不得改变调用栈。

### 8.11.2 CALL/RET

强制程序包括：

1. 一层直接 CALL/RET；
2. 多层嵌套调用；
3. CALL.IND 使用 S 目标；
4. 递归直到声明深度边界；
5. CALL 内部包含已闭合的 SSY/BRA.P/JOIN；
6. 每次 CALL 的帧只保存准确的 `PC+8`，RET 恢复该 PC；
7. 空状态 RET；
8. 非法 CALL 目标；
9. 返回前仍有未闭合 SSY 区域；
10. 调用深度超限。

`CALL`、`CALL.IND` 和 `RET` 虽然是 W/CONTROL 指令，也必须跑完整 scalar-ready 矩阵。NS1..NS4 的期望都是 `SCALAR_STATE_FAULT`；状态检查发生在读取间接目标或调用状态之前。失败用例必须证明没有半压栈或半弹栈。

调用栈 oracle 必须独立维护每 warp LIFO，并逐次比较：

```text
CALL 成功     -> push({return_pc=old_pc+8})
CALL.IND 成功 -> 同上，再把 PC 设为冻结的 SGPR64 目标
SSY 成功      -> 新 reconv frame.owner_call_depth = call_stack.depth
RET 成功      -> 不存在 owner_call_depth==call_stack.depth 的 reconv frame；
                 pop 后 PC=return_pc
JUMP.IND      -> 栈内容和深度完全不变
```

调用帧调试摘要若暴露除 `return_pc` 以外的架构字段即 FAIL。分别用 descriptor `call_stack_depth=0、1、实现最大值` 测试：depth=0 时第一次 CALL 就满；其他值恰好允许对应数量，下一次 CALL 报故障。两层以上嵌套调用必须证明 RET 严格后进先出。

目标非法、descriptor 栈满、空栈 RET、当前 callee 仍有 `owner_call_depth==call_stack.depth` 的重汇聚帧和非 scalar-ready 分别注入，并按 `docs/02-programming-model.md` 的权威顺序组合成多故障用例；任一失败都不得改变 PC、调用栈或重汇聚栈。

### 8.11.3 隐藏重汇聚

SSY/BRA.P/JOIN/EXIT 套件至少执行：

- 无分歧路径；
- 单层分歧；
- 多层正确嵌套；
- 一条路径部分 EXIT；
- 一条路径全部 EXIT；
- 两条路径分别有 EXIT；
- 尾 warp；
- 错误 JOIN 目标；
- 区域交叉；
- 从区域内部跳到外部；
- 在 FIRST/SECOND 路径的 JOIN 前执行 CALL，必须先报 `SCALAR_STATE_FAULT`；
- scalar-ready 时 CALL，callee 内部建立并闭合自己的嵌套区域；
- 在 FIRST/SECOND 路径执行 RET，必须先报 `SCALAR_STATE_FAULT`；
- scalar-ready 但控制区域关系仍非法的跨区域 RET。

`owner_call_depth` 必须按 SSY 动态实例精确测试：

- 在调用深度 0、1 和至少两层嵌套调用中执行 SSY，新帧分别记录当时的 `call_stack.depth`；
- SSY 之后再 CALL，已有 caller 帧的 `owner_call_depth` 保持旧值，不能随调用深度一起增加；
- callee 内 SSY 建立的帧满足 `owner_call_depth == 当前 call_stack.depth`，在匹配 JOIN 前 RET 必须报 `RECONVERGENCE_FAULT`；
- callee 的帧 JOIN 闭合后 RET 成功；较小 `owner_call_depth` 的 caller ARMED 帧可以跨 CALL/RET 保留；
- JOIN 只弹出匹配的重汇聚帧，不改调用帧；RET 只弹 `return_pc`，不改写 caller 重汇聚帧；
- SSY 目标、栈容量或其他检查失败时，不得留下带 `owner_call_depth` 的半帧。

比较可见结果时必须检查：

- lane 不丢失、不重复、不复活；
- 每个非 EXIT lane 恰好执行它所属路径；
- JOIN 后仍 live 的 lane 恢复到同一控制点；
- 路径执行次序符合 YAML 固定规则；
- 程序不能读取或伪造隐藏重汇聚状态。

## 8.12 SYNC 与 X

先做静态清单和编码门禁：

```text
F061 == BAR.SYNC      == (class=5, format=0, opcode=3)
F062 == BAR.ARRIVE    == (class=5, format=0, opcode=4)
F063 == BAR.WAIT      == (class=5, format=0, opcode=5)
```

family/form 总数必须保持 YAML 的运行时去重结果不变。汇编/反汇编只接受 `BAR.SYNC.CTA id`、`BAR.ARRIVE.CTA vd,id`、`BAR.WAIT.CTA id,vs` 作为 canonical；旧拼写和 WAIT 缺 id、S 域 token、id 超出 `0..7` 都必须拒绝。逐位核对三个 YAML 示例机器字，并对 `a/slot3` 的最小值、最大值和交叉值独立重算 64 位机器字；F063 的 `slot3` 必须可非零。

每个动态用例都比较 8 槽完整状态：`generation/mode/owner_set/arrived_set/consumed_set/completed/waiters`，并比较每 warp 的 blocked record。generation oracle 必须使用数学非负整数，不能按 U32/U64 截断。所有 owner 集合元素必须是 `linear_tid=warp_id*32+lane_id`，token tag 必须恰好是 `{CTA identity,linear_tid,slot,logical generation}`。测试矩阵至少包含：

| 类别 | 必测情况 | 强制结果 |
|---|---|---|
| 启动 | 满 CTA、尾 warp、槽 0 和槽 7 | 每槽 generation 0、EMPTY、集合/waiter 空；owner_set 恰为真实 `linear_tid` |
| owner 身份 | 不同 warp 的相同 lane_id、同 warp 不同 lane_id、尾 lane | `(warp_id,lane_id)` 映射到唯一 linear_tid；集合、tag、重复/wrong-owner 都只比较 linear_tid |
| SYNC 正常 | 单/多 warp，不同到达顺序，active 子路径分批到达 | 每 linear_tid 每代一次 release；每条 record 冻结 `{warp_id,A,old_PC+8}`；到齐时所有记录一起恢复并立即退休 |
| SPLIT 正常 | ARRIVE 后立刻 WAIT、先做别的工作再 WAIT、WAIT 早于/晚于 completed | ARRIVE 不阻塞且 `PC+8`；早 WAIT 先消费后记录并阻塞整个 warp；晚 WAIT 立即 acquire；全消费后退休 |
| 多槽/多代 | 0..7 交错，两次以上复用同槽 | 槽互不干扰；退休后 generation 恰加 1，旧 token stale |
| 物理计数边界 | 跨过实现每个有限 generation/epoch 子计数器的最大值，保留边界前已消费 token 的多个 `V_MOV.B32` 副本 | 逻辑 generation 仍严格 `old+1`、不回绕；旧副本在低位再次相等时仍 `BARRIER_FAULT`，绝不复活 |
| 模式 | 首个 SYNC、首个 ARRIVE、同代 SYNC→ARRIVE、ARRIVE→SYNC | 首到达选模式；两种混用均整条 `BARRIER_FAULT` |
| arrival | 同 owner 重复、warp 中最低/最高/多个 lane 重复 | 整条零 arrival、零 VGPR 写、零 PC 效果 |
| token 身份 | foreign CTA identity、wrong slot、wrong linear_tid owner、wrong generation、malformed、untagged、stale | 每项均 `BARRIER_FAULT`，整条零 consume |
| token 消费 | 正常一次、同寄存器重复、V_MOV 副本先后消费 | 恰好一次成功；其余 duplicate/stale 并整条回滚 |
| token 写回 | ARRIVE 的 active、inactive、挂起路径和尾部不存在 lane | 只 active 真实 lane 写自己的 VGPR token，其他目标逐位及标签保持 |
| tag 传播 | `V_MOV.B32 vd,vs`、原地 MOV、guard 子集、立即数 MOV | 寄存器 MOV 逐 lane 完整复制 bits+tag；非参与保持；立即数写清 tag |
| tag 清除 | 自动枚举所有 VGPR write/read_write 目标 form | 除根规则两个例外外，每个实际写入 VGPR32 槽的 tag 都清除；非参与槽保持 |
| warp 原子性 | 一条 ARRIVE/WAIT 中只有一个 active lane 错，其他 lane 合法 | 所有 lane 零 arrival/consume/VGPR/blocked/PC 效果 |
| blocked record | SYNC/WAIT 阻塞、恢复、尝试第二条 record | 每 warp 至多一条；阻塞/恢复保持 active/live/reconv/call，挂起路径不能切入；恢复只写 resume PC、清记录、置 ready |
| EXIT | 未 arrival 后 EXIT、槽中 `tid∈arrived-consumed` 但 VGPR tag 已清、已消费但 stale tag 仍在 | 第一种 owner 不缩小且可 DEADLOCK；第二种按槽状态 `BARRIER_FAULT`；第三种可退出 |
| 分歧 | 当前路径分批 arrival；当前路径阻塞而挂起路径还需 arrival/WAIT | 只把当前 active lane 映射成 A；整个 warp blocked，后者满足既有条件时 DEADLOCK，不得切路径补票 |
| 内存 | shared release/acquire litmus；同程序换 global/local/param/const/host | shared 禁止旧值结果；其他空间不得因 BAR 额外有序，global 需原子/MEMBAR |
| CTA 完成 | 所有 warp 完成，分别改变 mode/集合/waiter/completed/generation | 仅 8 槽全 IDLE 才完成；非零 generation 允许，其他任一非 IDLE 状态拒绝完成 |

还要对所有 token 故障做组合用例：显式 id 与标签 slot 不同、位值相同但标签不同、标签相同的副本、相同 lane_id 但不同 warp_id、一个 warp 多个 lane 分别触发不同错误。故障优先级取第 2 章权威表；一旦选择 `BARRIER_FAULT`，入口的槽状态、全部 VGPR bits/tag、PC、`LIVE/EXEC`、栈和 blocked record 都保持。

阻塞/恢复专项必须证明：`BarrierWaitRecord` 只有 `warp_id/owner_snapshot/resume_pc` 三个字段，`owner_snapshot=A` 且 `resume_pc=old_PC+8`；arrival 或 consume 只提交一次；挂起期间不重读 VGPR、不重复 release、不重复 consume；恢复只写记录指定 PC 和 ready，不改 active/live/reconv/call。SYNC 完成即退休，SPLIT completed 但未全消费时绝不能退休。没有 `expected`、成员 mask 或子集参数的正例；任何测试工具自行缩小 owner 都是 FAIL。

永不回绕专项不能只跑“很多代”然后比较低位。实现必须列出 token 身份中每个有限 generation/epoch 子计数器的边界，并通过可行的长跑、缩小计数位宽的验证配置、状态注入或形式证明逐个跨越。对每个边界至少执行：

```text
old = BAR.ARRIVE.CTA 产生的 token
copy1 = V_MOV.B32(old)
copy2 = V_MOV.B32(old)
用 old 或其中一份副本成功 WAIT，使该代最终退休
反复完成并退休同一 slot，跨过有限内部计数器边界
继续到物理低位/对象编号可能再次等于 old 的时刻
assert logical_generation == previous_logical_generation + 1
assert BAR.WAIT.CTA(slot, copy1/copy2) == BARRIER_FAULT
assert 槽、VGPR、blocked record、PC 全部零提交
```

若实现使用 capability ID 或安全回收，还必须制造足够的分配/回收压力，证明编号回收不会重建旧 capability 身份。通过标准是所有观察都等同于 generation 属于 `N` 且从不复用；“测试跑不到回绕点”不能替代证据。

VGPR 标签闭包必须由 YAML 自动生成测试，不允许手写一小份代表列表：

```text
targets = every form operand whose resolved register_file is VGPR
          and access is write or read_write
for each written VGPR32 slot of each target:
    if (family_id, form_id) == (F025, b32.reg):
        assert action == copy_source_tag
    elif (family_id, form_id) == (F062, cta):
        assert action == create_tag
    else:
        assert action == clear
```

枚举报告必须点名覆盖 `V_MOV.B64`、`V_BCAST`、`X_BROADCAST`、`V_GETREG`、普通/原子 load 返回、ALU、CVT、FP 和 MMA，并断言没有第三个例外。多 VGPR/片段目标逐槽测试；guard-false/inactive lane不写，因此旧 tag 保持。

跨 lane X 测试必须明确 C/M/P/R，并覆盖：

- 全 warp、单 lane、奇偶 lane、连续子集和尾 warp；
- 空贡献集；
- member mask 包含/排除 inactive lane 的规定行为；
- mode/width/member mask 一致性；
- 每 lane 不同 control；
- 源 lane 不存在或不在 P；
- 原地源/目标别名；
- 候选缺失和会合中 EXIT。

协议错误产生 `COLLECTIVE_FAULT` 时，所有接收者目标保持。

## 8.13 MMA

### 8.13.1 片段 ABI

清单必须断言 `M16N8K16` 形状只有 `MMA.M16N8K16.F16.F16.F32` 一个 form；任何其他 `M16N8K16` 类型或 modifier 都必须被拒绝。YAML 若启用其他形状，仍须按各自结构化合同进入 all-form 门禁，但不得被算成第二个 `M16N8K16` form。

测试生成器先把该 form 的 YAML `matrix_contract` 解析成坐标，并断言它与第 7 章公式逐项相同；不能只比较一段描述字符串。独立 oracle 直接实现同一映射。对 lane `l`：

```text
A: m=l//2, k=8*(l%2)+2*r+h             (r=0..3,h=0..1)
B: k=l//2, n=4*(l%2)+2*r+h             (r=0..1,h=0..1)
C: m=l//2, n=4*(l%2)+r                 (r=0..3)
D: m=l//2, n=4*(l%2)+r                 (r=0..3)
```

A/B 的 `h=0/1` 必须分别命中 VGPR 低/高 16 位；C/D 每个 VGPR 恰好一个 F32。测试必须证明 A/B/C/D 全部 256/128/128/128 个逻辑元素恰好映射一次，没有重叠或洞。

片段组固定为 A/B/C/D 每 lane 4/2/4/4 个 VGPR，对齐 4/2/4/4；只允许 D=C 完整别名。测试必须直接按坐标填 A/B/C，不能让被测 pack helper 同时生成输入和标准答案。至少自动生成：

- 每个 A 元素单独置 1；
- 每个 B 元素单独置 1；
- 每个 C 元素使用唯一编码；
- packed 元素的低半/高半或各子字段基向量；
- 最低、最高合法片段基址；
- 允许的完整别名；
- 错误对齐、越界和部分重叠。

任何坐标或打包顺序错误都直接 FAIL。

### 8.13.2 数值

每个输出先令 `acc=C[m,n]`，再严格执行：

```text
for k = 0,1,...,15:
    acc = FFMA.F32(
        V_CVT.F32.F16(A[m,k]),
        V_CVT.F32.F16(B[k,n]),
        acc)
D[m,n] = acc
```

每一步都在步末 RNE 舍入到 F32。oracle 禁止树形归约、跨 k 扩展精度或 FP64 累加。强制数据包括：

- 零矩阵和单位基向量；
- 精确小整数；
- 正负零和抵消；
- normal、subnormal、最大有限数、Inf 和 NaN；
- 能区分固定 k 顺序与树形归约的样本；
- 固定 seed 随机片段。

全部 D 元素逐位比较，不能使用相对误差替代位精确要求。

至少再做一个“手工坐标小样”：绕开通用 pack/unpack helper，直接给选定 lane、寄存器偏移和位片写值，手工计算一个 D 坐标。这个用例用来发现测试工具和实现共同误读同一份映射。

### 8.13.3 会合和整体提交

必须检查 `active_mask == live_mask == 0xffffffff`、固定 PT、所有 lane 同一动态 PC 和一致片段基址。缺 lane、不同动态 PC、片段参数不一致、会合时 EXIT 或非法 guard 必须产生规定故障。故障时任何 D 元素都不能写；即使最后一个输出才发现错误，前面的输出也必须回滚。

## 8.14 all-form 覆盖门禁

### 8.14.1 从 YAML 生成清单

构建开始时生成：

```text
required_forms = unique(forms enabled by current YAML and feature set)
required_families = unique(family_id of required_forms)
required_modifier_instances =
    expand each form's legal order/scope/address-modifier matrix
```

`order`、`scope` 和 scale 赋值属于 modifier instance，不得复制成新的 family/form 来虚增统计。生成器必须拒绝“操作数和语义相同、只因 modifier 取值不同就复制 form”的 YAML 条目。`forms_total` 只数 form；modifier instance 另行报告，不能混进 form 总数。

每个 form 都必须有以下 coverage tag：

```text
decode-positive
decode-negative
assemble
disassemble
fixed64
machine-class
reader-class-view
semantic-normal
semantic-boundary
guard_policy
required_state
register-domain
register-pair64
aliasing
fault-or-no-fault
generated-reference
```

适用时还必须有：

```text
scalar-ready
cross-domain
vector-optional-guard
b64-provenance
wide-mul-mad
int-mad-minmax-abs-neg
ctz-rotate-pack
brev-bfe-bfi
target
call-ret
hidden-reconvergence
reconv-owner-call-depth
mixed-addressing
smemx-vatomx
memory-align-bounds
memory-order
atomic-load
atomic-store
atomic-rmw
atomic-cas
atomic-order-scope-modifier
atomic-space-order-scope
sync
cross-lane
f32-compare
v-f16
fp-bitexact
approx-ulp
mma-fragment
mma-structured-mapping
```

门禁算法：

```text
for form in required_forms:
    assert generated_reference_contains(form.id)
    for tag in mandatory_tags(form):
        assert passing_test_count(form.id, tag) >= 1
    for instance in required_modifier_instances(form):
        assert encode_decode_round_trip(instance)
        assert semantic_modifier_oracle_passes(instance)

assert covered_form_ids == required_forms.ids
assert report.forms_total == len(required_forms)
assert report.families_total == len(required_families)
assert report.modifier_instances_total == len(required_modifier_instances)
assert no_test_references_unknown_form
```

同 family 的另一个 form 不能代替当前 form。一个测试可以覆盖多个 tag，但必须真的执行各 tag 的 oracle，不能只在元数据中挂名字。

### 8.14.2 S/V 对称性和 only-form 检查

生成器根据第 7 章和 YAML 自动建立：

```text
dual_pairs
s_only_forms
v_only_forms
cross_domain_forms
scalar_ready_forms
```

`scalar_ready_forms` 直接选择全部 `required_state: scalar_ready` 的 form，并反向断言全部 `execution_domain: scalar` form 都在集合中。当前控制侧至少包括 `CALL`、`CALL.IND`、`JUMP.IND`、`RET`。集合中每个 form 都必须带 `scalar-ready` coverage tag，并跑完整 SR/NS 矩阵。

对 `dual_pairs`，要求 S/V 有共同的数学边界向量，并额外检查执行次数、寄存器域和事件数。compare 的每个关系/类型和 CVT 的每个 `dst-type.src-type` 必须进入 `dual_pairs`，缺任一侧都失败。对 only-form，要求另一域的拼写和编码不存在。对跨域 form，要求源/目标方向与规范完全一致。

### 8.14.3 新 form 门禁

任何新增 form 必须在同一变更中带上：

- 唯一编码和负例变异规则；
- 8 选 1 的机器 class，以及需要时的用户可读简称；
- 操作数域、`guard_policy` 与 `required_state`；
- 正常、边界和故障语义；
- 适用的 scalar-ready、跨域、控制、内存、FP、X 或 MMA 专项；
- 生成参考条目；
- all-form coverage tags。

只增加 YAML 条目但没有测试 oracle，或只增加测试名字但没有向量，构建必须失败。

## 8.15 差分、形式属性和发布报告

最低验证链路：

1. YAML schema 与编码静态检查；
2. 独立 assembler/decoder 闭环；
3. 指令级参考模型；
4. 随机定义良好的程序差分；
5. 内存 litmus 模型检查；
6. RTL/模拟器逐提交比较；
7. 设备结果和 fault trace 回读。

至少建立以下形式属性：

- 每个 64 位机器字至多选择一个 form；
- 每个 form 的所有位都属于字段或 must-zero；
- 机器 class 只能是 SYS/SALU/VALU/MEMORY/CONTROL/SYNC/CROSSLANE/MATRIX 之一；
- 用户可读简称不得覆盖或改变机器 class；
- S/V 域约束不会被绕过；
- 每个 S form 在非 scalar-ready 时都只产生 `SCALAR_STATE_FAULT`；
- `CALL`、`CALL.IND`、`JUMP.IND`、`RET` 保持 CONTROL class，并执行 scalar-ready 检查；
- 故障指令不产生部分提交；
- CALL/RET 和隐藏重汇聚状态满足 LIFO/结构约束；
- atomic load 不改 modification order；
- MMA 的 D 片段整体提交。

发布报告必须列出：

- 从 YAML 生成的 family/form 数和摘要；
- 每个 form 的 coverage tags 与用例 ID；
- 固定 64 位编码正/负例结果；
- scalar-ready 与跨域矩阵；
- S/V 双版本和 only-form 检查；
- 普通内存、mixed addressing 和原子专项；
- 控制流、CALL/RET、隐藏重汇聚、SYNC/X 结果；
- FP 固定/随机向量与观察到的最大 ULP；
- MMA ABI、数值和会合结果；
- 所有失败、跳过和 NOT_APPLICABLE 项。

只要存在缺失 form、缺失 oracle、未决规范点或未解释差异，报告就不能标记 `PASS`。

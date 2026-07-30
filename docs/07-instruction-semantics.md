# 7. 指令分类与语义

本章定义 **VTX-1 ISA 1.0 Draft** 的指令分类和执行语义。这里讲“指令做什么”；每个 family/form 的位段、selector、操作数宽度、合法 modifier 和机器字示例由 `isa/vtx1/isa.yaml` 生成。实现不得根据助记符猜编码，也不得在本章之外补出隐藏语义。

## 7.1 一眼看懂分类树

VTX-1 每条指令固定为 64 位。机器码里的 `class` 只有下面 8 种合法值：

```text
SYS        系统、特殊寄存器、陷阱和杂项
SALU       标量算术与逻辑
VALU       向量算术与逻辑
MEMORY     标量/向量访存与原子
CONTROL    分支、调用、返回和重汇聚
SYNC       barrier 与内存同步
CROSSLANE  warp 内跨 lane 操作
MATRIX     矩阵乘加
```

每个 YAML form 必须恰好属于这 8 个 class 之一。保留 class、一个 form 同时落入两个 class，或靠操作数猜 class，都属于非法编码。

`execution_domain` 是另一件事。它回答“这条 form 动态执行几次、按什么状态规则执行”，合法值恰好是：

```text
system / scalar / vector / warp_control /
warp_collective / cta_sync / warp_matrix
```

8 个 machine class 和 7 个 execution domain 不能混成一张表。常见关系是：

```text
SYS       -> system，也可承载 scalar/vector 的 GETREG/SETREG form
SALU      -> scalar
VALU      -> vector
MEMORY    -> scalar 或 vector
CONTROL   -> warp_control
SYNC      -> cta_sync
CROSSLANE -> warp_collective；跨域搬运 form 可按 YAML 标为 scalar/vector
MATRIX    -> warp_matrix
```

最终关系以每个 YAML form 自己的 `class` 和 `execution_domain` 为准，不能只看助记符前缀反推。

为了让人读起来省事，本章还使用 S/V/W/SYNC/X/MMA 这些**用户可读简称**。简称只是告诉人“主要在干什么”，不是 YAML 字段，更不是第二套编码 class：

```text
S     标量方向，常见于 SALU、scalar MEMORY、scalar SYS
V     向量方向，常见于 VALU、vector MEMORY、vector SYS
W     控制流，对应 CONTROL
SYNC  同步，对应 SYNC
X     跨 lane/跨域，对应 CROSSLANE
MMA   矩阵，对应 MATRIX
```

`SYS` class 中纯系统 form 仍直接称为 SYS。`X_BROADCAST`、`S_READFIRST` 的名字说明数据方向或集合行为，但机器 class 仍由 YAML 决定。所以“这一节从 V 方向解释它”和“它编码在 CROSSLANE class”并不冲突；机器 class 永远以 8 类之一为准。

命名规则如下：

- 标量数据指令使用 `S_` 前缀，例如 `S_ADD`、`S_LD`、`S_ATOM.ADD.U32.GLOBAL.RELAXED.DEVICE`。
- 向量数据指令使用 `V_` 前缀，例如 `V_ADD`、`V_LD`、`V_ATOM.ADD.U32.GLOBAL.RELAXED.DEVICE`。
- 跨寄存器域和跨 lane 的规范名称固定为 `X_BROADCAST`、`S_READFIRST`、`S_GETREG`、`V_GETREG`。
- 控制流清单固定为 `BRA`、`BRA.P`、`SSY`、`JOIN`、`EXIT`、`CALL`、`CALL.IND`、`JUMP.IND`、`RET`。
- SYNC、X 和 MMA 的完整规范名称由 YAML 给出；文本别名不能产生第二个机器编码。

`S_BROADCAST` 和 `V_BCAST` 都不是规范名称，也不是任何指令的别名。把一个标量值送到各 lane 不需要专门的指令：任何 `V1/V2/V3/VCMP` 向量 form 都可以用 scalar-source selector 直接读一个 SGPR 源，需要独立副本时写 `V_MOV.B32 vd, sN`。

## 7.2 双寄存器执行模型

### 7.2.1 S 域和 V 域

每个 warp 有两套彼此分开的寄存器：

- **S 寄存器**：每个 warp 一份。一个 `sN` 是一个 32 位标量槽；64 位操作数规范写作 `sE:s(E+1)`。
- **V 寄存器**：每个 lane 一份。`vN[lane]` 是该 lane 的 32 位槽；64 位操作数规范写作 `vE:v(E+1)`。
- **SCC**：每个 warp 一份的 1 位标量条件，只表示 false 或 true。
- **`vpN`**：每个 warp 一份的 32 位 lane 掩码；第 i 位对应 lane i。`vp0..vp15` 可由向量比较和跨 lane 指令读写。

寄存器本身没有整数或浮点标签。类型由当前 form 决定；`B32`、`U32`、`S32` 和 `F32` 都可以占同一个 32 位槽。

64 位寄存器对的基址 `E` 必须为偶数，两个编号必须相邻并完整写出，例如 `s0:s1`、`v6:v7`。前一个槽保存低 32 位，后一个槽保存高 32 位；编码字段只保存偶数基址。`s1:s2`、`v4:v6`、只写 `s0`/`v0` 来冒充 64 位值，或任一成员越过 descriptor 的寄存器计数，都是 `ILLEGAL_OPERAND`。

S 指令只执行一次。V 指令对参与集合中的每个 lane 独立执行一次。一个 V 指令的不同 lane 可以读到不同值、形成不同地址、得到不同结果。

### 7.2.2 active、participating 和 scalar-ready

对 V 数据指令，入口先冻结：

```text
E = active_mask
G = 对 E 中每个 lane 求头部 guard
P = E & G
```

`E` 是入口 active 集合，`P` 是实际参与集合。guard 为假的 lane 不读动态源、不形成地址、不产生 lane 局部故障，也不写任何目标。scalar form 的 `guard_policy` 是 `required_pt`，先检查 scalar-ready，然后执行一次；SCC 不是通用 scalar guard。只有 `S_SELECT` 或 YAML 明确列出的 CONTROL form 才把 SCC 当数据条件读取。CONTROL、SYNC、CROSSLANE 和 MATRIX 按各自章节决定参与集合。

warp 在某条指令入口满足以下全部条件时称为 **scalar-ready**：

```text
live_mask != 0
active_mask == live_mask
reconv_stack 中不存在 phase 为 FIRST 或 SECOND 的帧
```

大白话说，就是 warp 里还有活 lane、所有活 lane 此刻都在一起，而且没有另一条分支路径等着执行。只有 `ARMED` 帧不妨碍 scalar-ready，因为它只是预约 JOIN，还没有真正发生分歧。尾 warp 中物理不存在的 lane 不属于 `live_mask`。

**所有 S 指令都要求 scalar-ready。** 这包括 SALU、scalar MEMORY、scalar SYS 和 `S_READFIRST`，没有“普通 S 可以例外”的规则。检查发生在读取任何动态源之前；任一条件不满足时：

```text
fault(
    code = DIVERGENCE_FAULT,
    lane_mask = active_mask,
    aux = 0)
```

故障指令不读取动态源，不写 SGPR、VGPR、`vpN`、SCC 或内存，也不推进 PC。scalar form 不从 SCC 派生参与集合；SCC 只由 `S_SELECT` 或 YAML 明确列出的 CONTROL form 当作数据条件读取。

### 7.2.3 通用事务

除阻塞型 SYNC 外，每条动态指令在架构上按一个事务完成：

```text
1. fetch      从 PC 读取一个 64 位小端机器字
2. decode     按 YAML 校验 class、family、form、字段和 must-zero
3. classcheck 按执行域检查 scalar-ready 或集合/控制状态
4. freeze     冻结 E、P、全部源和所需隐藏状态
5. evaluate   计算结果、地址、目标 PC 和动态约束
6. validate   收集整条指令的故障
7. commit     无故障才一次提交全部效果
```

静态译码不受 active mask 或 guard 抑制。具体先后只引用 `docs/02-programming-model.md` 的权威表；按该表，`ILLEGAL_INSTRUCTION` 和静态 `ILLEGAL_OPERAND` 位于 `DIVERGENCE_FAULT` 之前。对静态合法的 S form，非 scalar-ready 的结果固定是 `DIVERGENCE_FAULT`。任一参与 lane 失败，整条指令都不提交；不能出现“低 lane 已写、高 lane 才报错”。

所有源逻辑上先读后写，所以目标可以与源完全重合。多槽寄存器组只能完全重合或完全不相交，除非对应 form 明确允许其他关系。

普通非控制指令成功后 `PC = PC + 8`。合法且 `P` 为空的 V 指令不产生数据效果，只推进 PC。S 指令若 `live_mask==0`，应在更早的 scalar-ready 检查中报告 `DIVERGENCE_FAULT`，不能走空参与集合捷径。

## 7.3 S-only、V-only 和 S/V 双版本

### 7.3.1 S/V 双版本

下面这些 category 原则上同时提供 `S_` 和 `V_` 版本；实际 form、宽度和 modifier 以 YAML 为准：

```text
move    MOV、位型复制、立即数物化
int     ADD、SUB、MUL、MAD、DIV、REM、MIN、MAX、ABS、NEG
bit     AND、OR、XOR、NOT、移位、rotate、计数、位域、pack
cmp     整数和浮点比较
select  S_SELECT 按 SCC、V_SELECT 按 vpN 的对应位二选一
cvt     整数、浮点和宽度转换
fp      ADD、SUB、MUL、FMA、DIV、SQRT、MIN、MAX、ABS、NEG、近似函数
mem     普通 load/store
atom    atomic load/store/RMW/CAS
sys     读取对该域合法的特殊寄存器
```

同一个 operation 的 S/V 版本使用相同数学规则，但执行次数、寄存器域和内存事件数不同。`S_ADD` 只产生一个标量结果；`V_ADD` 给 P 中每个 lane 产生一个结果。

所有列在 S 侧的版本，不论编码 class 是 `SALU`、`MEMORY` 还是 `SYS`，都先执行 7.2.2 的 scalar-ready 检查。

### 7.3.2 S-only

下列能力只属于 S 域：

- `S_READFIRST`：把一个 V 源的 first-lane 值送入 S 域，只能在 scalar-ready 时执行。
- 读取只具有 warp 单值语义的 `S_GETREG` form。
- 为 `JUMP.IND`、`CALL.IND` 提供间接目标 SGPR，并为标量访存提供地址基址。控制指令本身仍属于 W/CONTROL，不因此变成 S。

YAML 可以声明更多 S-only form，但必须写出不能存在 V 版本的理由和对应测试。

### 7.3.3 V-only

下列能力只属于 V 域：

- 用 scalar-source selector 把一个 SGPR 当统一源读入逐 lane 运算。
- 读取 lane id、lane-local 状态等逐 lane 特殊寄存器的 `V_GETREG` form。
- 以 V 地址项形成每 lane 不同地址的访存。
- 产生 `vpN` lane 掩码，并可作为 `BRA.P` 的逐 lane 条件。

X 和 MMA 也会读写 V 寄存器，但它们是独立顶层类别，不归入 V category。

### 7.3.4 不允许偷偷跨域

除 `vsrc*` 混合源、`S_READFIRST` 和 form 明写的混合地址外：

- S 指令不能读 V 寄存器；
- V 指令不能把 V 结果直接写进 S 寄存器；
- 汇编器不能靠同号寄存器名自动插入搬运或 read-first；
- 实现不能因为“所有 lane 的值碰巧相同”把 V 源当成 S 源。

混合源也不是无限制的跨域：一条 V 指令最多一个 SGPR 源，且必须由 selector 显式编码。写出两个 `sN` 源的汇编是错误，汇编器必须报错而不是自行插入一条搬运指令。

## 7.4 move 与跨域操作

### 7.4.1 同域 move

`S_MOV` 和 `V_MOV` 按 form 宽度逐位复制，不做数值转换，不改变 NaN payload，不做符号扩展。窄值扩展只能由明确的 load 或 cvt form 完成。

`V_MOV` 只搬位。寄存器上没有隐藏影子状态，因此 move 不需要额外说明标签如何创建、复制或清除。

### 7.4.2 混合源 V_MOV：SGPR 到各 lane

```text
V_MOV.B32 vd, vs            # ssrc=0
V_MOV.B32 vd, ss            # ssrc=1
V_MOV.B64 v0:v1, v2:v3      # ssrc=0
V_MOV.B64 v0:v1, s0:s1      # ssrc=1
```

`V_MOV` 是 `V1` 格式的 form，源操作数类型是 `vsrc32` 或 `vsrc64`。`ssrc=1` 时那 8 位寄存器号在 SGPR 文件中解释，这就是把标量值送进各 lane 的规范做法。它的执行域是 `vector`、`guard_policy: optional`，按普通 vector 规则令 `P = E & guard`，不要求 scalar-ready。

在入口冻结一次源；对每个 `lane ∈ P`：

```text
vd[lane] = frozen(src)
```

guard 为假的 lane 不读取源、不写目标；非参与 lane 的 `vd` 保持不变。

`.B64` 使用完整偶数连续寄存器对，一次复制 64 位，两个 32 位半部来自同一次冻结的快照。

很多情况下连这条 move 都不需要：既然 `V_ADD.U32`、`V_CMP.LT.U32`、`V_FFMA.F32` 这类 form 本身就能读一个 SGPR 源，直接写 `V_ADD.U32 v1, v0, s6` 比先搬后算更短。只有一条指令需要两个 uniform 值，或者要把 uniform 值多次复用而寄存器压力允许时，才值得先物化成 VGPR。

### 7.4.3 X_BROADCAST

```text
X_BROADCAST vd, vs, lane
```

`X_BROADCAST` 才是 lane 到 lane 的广播。它先冻结选中 lane 的 `vs`，再把这一份值写到所有规定的接收 lane。`lane` 是立即数还是统一 SGPR、源 lane 必须属于哪个集合、接收集合是什么，都由对应 YAML form 的结构化操作数和约束给出；实现不能自行改成“当前第一个 lane”。

这是 `execution_domain: warp_collective` 的集合操作。所有规定参与者必须在同一动态实例上取得一致的 lane 选择，源 lane 必须可用，否则产生 `COLLECTIVE_FAULT`，并且所有目标保持不变。它与混合源读 SGPR 是两件不同的事：`X_BROADCAST` 是 lane 到 lane，混合源是 SGPR 文件到各 lane。

### 7.4.4 S_READFIRST

```text
S_READFIRST.B32 sd, vs
S_READFIRST.B64 s0:s1, v0:v1
```

`S_READFIRST` 仍是 `execution_domain: scalar`、`guard_policy: required_pt`，不是 optional-guard 例外。先要求 scalar-ready，再令：

```text
first = 编号最小的 live lane
sd = frozen(vs[first])
```

`.B64` 从同一个 first lane 一次读取完整 VGPR 对；两个 32 位半部必须来自同一个 lane 的同一次快照，不能分别选择 lane。

若 `live_mask` 为空，warp 已完成，不会取到该指令。`S_READFIRST` 不做 vote，也不检查其他 lane 是否同值；需要验证同值时必须先用 X 类指令。

### 7.4.5 S_GETREG 与 V_GETREG

`S_GETREG` 先检查 scalar-ready，再读取规范标为 `uniform` 的特殊寄存器，例如 warp id、CTA 尺寸、时钟快照或 scalar-ready 状态。`V_GETREG` 可以读取 `uniform` 或 `per-lane` 项；读取 uniform 项时，各参与 lane 得到同值。

`V_GETREG` 虽编码在 `SYS` class，执行域仍是 `vector`，也是明确允许 `guard_policy: optional` 的例外。它按 `P = E & guard` 执行，不要求 scalar-ready；guard 为假的 lane 不读取特殊寄存器项，目标保持原值。机器 class 不能覆盖这条 form 自己的执行域和 guard policy。

两者都在 freeze 阶段取快照。未知寄存器号、宽度不匹配、用 `S_GETREG` 读取 per-lane 项，均为 `ILLEGAL_OPERAND`。

## 7.5 整数与位运算

### 7.5.1 基本整数

对宽度 `N`：

```text
ADD = (a + b) mod 2^N
SUB = (a - b) mod 2^N
NEG = (-a) mod 2^N
MUL.LO = low_N(a * b)
MAD = low_N(a * b + c)
```

`MIN.S/MAX.S` 按 N 位二补码比较，`MIN.U/MAX.U` 按无符号比较；相等时返回位型相同的任一输入都得到同一结果。`ABS.S` 对负数做 N 位 `NEG`，对非负数原样返回，所以最小负数的结果仍是同一位型，不额外饱和。`NEG`、`ABS`、`MIN`、`MAX` 和普通 `MAD` 都必须按 YAML 中实际存在的 S/V form 分别实现，不能拿 wide 或浮点 form 代替。

`DIV` 和 `REM` 的有符号版本向零截断；除数为零产生 `INTEGER_FAULT`。有符号最小值除以 `-1` 的结果或故障行为必须由该 form 的 YAML 语义参数明确给出。

只有比较、除法、右算术移位、MIN/MAX、扩展和转换等真正依赖符号解释的 form 使用 `.S` 或 `.U`。单纯搬位的 form 使用 `.B`。

### 7.5.2 wide multiply 和 wide MAD

`MUL.WIDE.U32/S32` 产生完整 64 位乘积：

```text
U32: result64 = zero_extend(a,32) * zero_extend(b,32)
S32: result64 = two_complement_64(s32(a) * s32(b))
```

`MAD.WIDE.U32/S32` 使用 64 位加数：

```text
result64 = (wide_product(a,b) + addend64) mod 2^64
```

S/V 版本都遵守同一规则。64 位目标组整体提交，不能先写低半再写高半。

### 7.5.3 位型 form 合并规则

以下操作不区分 signed/unsigned，规范只保留一个 `B32` 或 `B64` 位型 form：

```text
MOV, AND, OR, XOR, NOT
SHL, SHR（逻辑）, ROL, ROR
POPC, CLZ, CTZ, BREV
BFE, BFI, PACK, UNPACK
```

工具链不得为同一机器行为另造 `.S32` 和 `.U32` 两个 form。`SAR.S32/S64` 因复制符号位而单独存在。

### 7.5.4 shift、rotate、CLZ/CTZ

寄存器给出的移位或旋转量按 `amount mod N` 使用。立即数必须在其字段范围内，汇编器不能靠截断接受越界值。

```text
ROL_N(x,k) = ((x << k) | (x >> (N-k))) mod 2^N
ROR_N(x,k) = ((x >> k) | (x << (N-k))) mod 2^N
```

当 `k=0` 时直接返回 `x`，不得在宿主语言中计算移位 N。

`CLZ_N(0)=N`，`CTZ_N(0)=N`。非零输入分别返回最高端和最低端连续零的个数。

### 7.5.5 pack、unpack 和位域

PACK 按操作数顺序把窄位型放进目标的低位到高位，未被覆盖的目标位由 form 明确为清零或来自 base；不能保留未说明的旧值。UNPACK 是反操作，并按 form 选择零扩展、符号扩展或原样位型。

对 N 位 BFE/BFI：

```text
o = min(unsigned(offset), N)
n = min(unsigned(width), N - o)

BFE: n==0 ? 0 : (src >> o) & low_mask(n)
BFI: n==0 ? base : (base & ~(low_mask(n)<<o))
                    | ((insert & low_mask(n))<<o)
```

`width=0` 和 `offset>=N` 都不是故障。`low_mask(N)` 必须直接构造全 1，不能依赖宿主语言的 `1<<N`。

`BREV.BN` 精确反转 N 个有效位：结果位 `i` 等于输入位 `N-1-i`。它不做逐字节反转。BFE/BFI 的 offset、width、base 和 insert 取自 YAML 指定的操作数槽；实现不得因为某个槽为零就切换到另一种 form。

## 7.6 compare、select 与转换

compare 的 S/V 能力必须对称。对 `EQ/NE/LT/LE/GT/GE`，YAML 中每个 S 类型 form 都必须有同类型、同关系的 V form，反过来也一样；差别只有源/目标寄存器域、执行次数和 scalar-ready。S 比较写 1 位 SCC；V 比较写一个 32 位 `vpN`，其中只更新参与 lane 对应的位，其他位保持原值。

整数比较只在 form 明确要求时解释为 `.S32` 或 `.U32`；逐位相等/不等不另造有符号和无符号副本。F32 比较按助记符声明的关系执行：EQ/LT/LE/GT/GE 遇到任一 NaN 都为假，NE 遇到任一 NaN 为真。比较不改写源浮点位型，也不产生浮点异常标志。

`S_SELECT` 根据 SCC 选择完整标量值。`V_SELECT` 对每个参与 lane，根据指定 `vpN` 的对应 lane 位选择完整向量值。多槽值必须整组取自同一个输入，不能低半取 a、高半取 b。

CVT 也要求 S/V 对称：相同的 `dst-type.src-type` 必须同时提供 `S_CVT` 和 `V_CVT`，或两边都不存在。CVT 是数值转换，MOV 是位复制。所有舍入、饱和、NaN、F16 高位清零和 subnormal 规则以第 5 章为准；S/V 版本只改变执行域，不改变数值结果。任何使用 64 位源或目标的 CVT 都必须使用前述完整偶数连续寄存器对语法。

## 7.7 浮点

S_FP 和 V_FP 共享同一个数值环境：

- binary16/binary32；
- 精确 form 使用固定 RNE；
- 支持渐进下溢，不使用隐式 FTZ/DAZ；
- FMA 是一次融合运算；
- 没有可见浮点异常标志；
- NaN、无穷和有符号零按第 5 章逐位处理；
- `.APPROX` form 必须满足 YAML 给出的特殊值和 ULP 上限。

V F16 form 对每个参与 lane 独立读取输入槽低 16 位，按 binary16 规则计算，并把结果写到目标槽低 16 位；目标高 16 位清零。F16 FMA 只舍入一次，非 FMA 运算在该 form 的结果点舍入。NaN、subnormal、Inf 和 `-0/+0` 仍按第 5 章处理，不能先悄悄提升到 F32、做多次运算后再截断来改变规定结果。

实现可以用不同内部数据通路，但 S/V 同输入位型必须得到同结果位型。

## 7.8 普通访存与 mixed addressing

### 7.8.1 地址模板

地址先用无界数学整数计算，再做范围和对齐检查。规范只使用下面三类名字：

```text
uniform-base:
    EA = unsigned(SGPR_base) + sign_extend(imm)

lane-address:
    EA[lane] = unsigned(VGPR_address[lane]) + sign_extend(imm)

SV-mix:
    EA[lane] = unsigned(SGPR64_base)
               + zero_extend(VGPR32_index[lane]) * scale
               + sign_extend(imm16)
```

`uniform-base` 是每 warp 一个地址，scalar memory 成功后恰好一个事件。`SMEMX` 是它的 indexed 变体：

```text
EA = unsigned(SGPR64_base)
     + zero_extend(SGPR32_index)
     + sign_extend(imm16)
```

`SMEMX` 的 index 单位固定为字节，也就是 scale=1；未定义的 `mods` 位必须为零。它不把 `SMEMX` 变成 vector，也不允许读取 VGPR。

`lane-address` 和 `SV-mix` 使用 VMEM 系列格式。字段角色固定为：

```text
vdata   向量 load 目标或 store 源
vaddr   lane-address 时为 VGPR64 地址对；
        SV-mix 时为 VGPR32 无符号 index
sbase   SV-mix 时为 SGPR64 uniform base；lane-address 时必须为零
simm16  有符号字节 immediate
x5      form 未定义的位必须为零
```

SV-mix 的 VGPR32 index **固定零扩展**；最高位为 1 也仍是大正数，绝不能按有符号数解释。`scale` 只能取具体 form 明写的值；未声明缩放时固定为 1。global、param、const 可以使用 lane-address 或 SV-mix；shared 使用 32 位 lane-address，也可使用 form 明确给出的 SV-mix。**local 只能使用 lane-address**，只能由 vector memory 访问，每个 lane 的数值偏移落在自己的 local allocation；local 禁止 scalar、uniform-base 和 SV-mix。

`SMEMX`/VMEM/VATOMX 只能使用各自 form 结构化列出的地址项。地址空间由 opcode 决定，寄存器里的地址值不带空间身份。错误寄存器类别或非法操作数组合为 `ILLEGAL_OPERAND`，保留 scale/modifier 或非零 must-zero 位为 `ILLEGAL_INSTRUCTION`，数学地址越界为 `MEMORY_BOUNDS`，自然对齐失败为 `MISALIGNED_ACCESS`；多故障仍按第 2 章优先级。vector mixed address 中任一参与 lane 失败，整条指令零事件回滚。

S_MEM 先检查 scalar-ready。一次**成功**的 S load、S store 或其他非原子 scalar memory form 必须恰好产生一个内存事件，不能是零个，也不能按 lane 复制。V_MEM 对 P 中每个 lane 产生一个事件；即使多个 lane 得到同一 EA，也仍是不同的 lane 事件。

### 7.8.2 load/store

所有空间均按小端字节序。自然对齐由访问总宽度决定。地址回绕、跨 allocation、越界和错空间按第 4 章处理。

窄 load 必须明确扩展：

```text
LD.U8/U16  -> 零扩展
LD.S8/S16  -> 符号扩展
LD.B8/B16  -> 零填充到目标槽，其位型不带数值符号
LD.F16     -> 低 16 位装载，高位清零
```

store 只写 form 指定的低位。`ST.S8` 和 `ST.U8` 若机器行为完全相同，只保留一个位型 store form；S/U 不得制造重复编码。

向量宽访存按 YAML 指定的元素顺序映射到连续寄存器组。任一元素或任一 lane 校验失败，整条指令都没有内存事件。

## 7.9 原子 load、store 和 RMW

原子 family 的规范前缀只有 `S_ATOM` 和 `V_ATOM`。`S_ATOMIC_LOAD`、`V_ATOMIC_STORE`、`S_ATOMIC_CAS` 之类不是规范名称。S_ATOM 先检查 scalar-ready，成功后每条动态指令恰好产生一个原子事件。V_ATOM 为 P 中每个 lane 产生一个事件；同一条 V_ATOM 的多个 lane 命中同址时，它们在该位置的 modification order 中各占一个位置，先后次序不由 lane 编号规定。

原子类别包括：

```text
LOAD                           只读，不改 modification order
STORE                          只写，在 modification order 中追加新值
ADD/MIN/MAX/AND/OR/XOR/XCHG    读改写，返回旧值并追加新值
CAS                            比较并交换，始终返回旧值
```

交换操作的规范名称只有 `XCHG`。

canonical 语法固定为：

```text
S_ATOM.<op>.<type>.<space>.<order>.<scope> ...
V_ATOM.<op>.<type>.<space>.<order>.<scope> ...
```

`space` 必须显式写成 `GLOBAL` 或 `SHARED`，位置固定在 `type` 后；它由 operation form/opcode 选择，不是靠地址寄存器猜出来。`order` 和 `scope` 是编码 modifier，不是为每个组合复制一套新的操作语义或 family。canonical 文本必须把三者都写出，不能省略默认值、交换顺序或改用小写。合法矩阵是：

```text
op                 order                                  global scope          shared scope
LOAD               RELAXED, ACQUIRE                       CTA/DEVICE/SYSTEM     CTA
STORE              RELAXED, RELEASE                       CTA/DEVICE/SYSTEM     CTA
ADD/MIN/MAX/
AND/OR/XOR/XCHG     RELAXED/ACQUIRE/RELEASE/ACQ_REL        CTA/DEVICE/SYSTEM     CTA
CAS                 RELAXED/ACQUIRE/RELEASE/ACQ_REL        CTA/DEVICE/SYSTEM     CTA
```

例如 `S_ATOM.LOAD.U32.GLOBAL.ACQUIRE.DEVICE`、`V_ATOM.CAS.U64.GLOBAL.ACQ_REL.SYSTEM` 和 `S_ATOM.XCHG.U32.SHARED.RELAXED.CTA`。param、const、local 不支持原子。

原始机器字中的 `scope=3` 是保留编码，固定产生 `ILLEGAL_INSTRUCTION`。它不能先被解释成未知名称，也不能降级为某个合法 scope。

已知 modifier 组成了表外 order/space/scope 组合才产生 `ILLEGAL_OPERAND`。两类错误都在读取地址或数据源之前发现，原子事件数为零。

LOAD 没有写数据源，STORE 没有旧值目标，CAS 必须同时带 expected、replacement 和 old-value 目标。CAS 原子地读取 old；`old == expected` 时写 replacement，否则把 old 原样写回该位置；两种情况都把 old 返回目标，并按第 4 章进入 modification order。atomic load/store 不能伪装成“加零”或“交换后丢弃结果”。

`VATOMX` 是 `V_ATOM` 的 SV-mix 编码格式，字段是 `vdst/sbase/vindex/vdata0/vdata1/order/scope/x`，没有 immediate 字段。它的地址为：

```text
EA[lane] = unsigned(SGPR64_base)
           + zero_extend(VGPR32_index[lane])
```

`VATOMX` 的 scale 固定为 1，不存在额外缩放变体，`x` 必须为零。额外 scale、非法操作数、越界、未对齐和任一 lane 失败分别按 7.8.1 与第 2 章处理，并整条零事件回滚。

## 7.10 W 控制流与隐藏重汇聚

### 7.10.1 一个 PC，隐藏状态

一个 warp 只有一个架构 PC。实现保存路径集合、待执行路径、调用返回点和重汇聚信息，但这些都是隐藏状态：程序不能读栈深、修改 pending mask，或依赖实现内部先存哪一项。

隐藏不等于随意。相同入口状态、SCC 和 `vpN` 必须得到相同的 PC、active mask 和可见提交顺序。

### 7.10.2 BRA、BRA.P、SSY、JOIN、EXIT

- `BRA target`：所有 active lane 一起跳到直接目标。
- `BRA.P condition,target`：按 `vpN` 或 `!vpN` 把入口 active 集合分成 taken 和 fall-through。若两边都非空，硬件在隐藏重汇聚状态中保存另一条路径，并按 YAML 规定的固定路径次序执行。
- `SSY join_target`：声明后续分歧的结构化汇合点，并建立一个隐藏重汇聚区域。新帧必须记录 `owner_call_depth = call_stack.depth`，也就是 SSY 执行这一刻的调用深度；该字段留在重汇聚帧内。
- `JOIN`：到达声明的汇合点。若另一条路径尚未执行，切换到它；全部仍 live 的路径到达后恢复为一个集合并继续。
- `EXIT`：永久删除参与 lane；隐藏状态中的所有相关集合同时删除这些 lane，lane 不得在 JOIN 后复活。

SSY、BRA.P、JOIN、EXIT 的机器指令保留，但软件只描述结构，不能直接管理重汇聚栈。嵌套区域必须正确包含，不能交叉。非法目标、错误 JOIN、区域越界或隐藏状态不满足产生 `RECONVERGENCE_FAULT`，并按整条指令回滚。

```text
SSY 成功提交:
    push ReconvFrame(
        reconv_pc = join_target,
        owner_call_depth = call_stack.depth,
        ...其余结构化重汇聚字段...)
```

后续 CALL 不得改写已有帧的 `owner_call_depth`。JOIN 弹出匹配帧；RET 只拒绝仍存在且 `owner_call_depth == 当前 call_stack.depth` 的 callee 帧，较小深度的 caller 帧可以保留。

### 7.10.3 CALL、RET 和间接跳转

```text
CALL target        只保存 PC+8，再跳到直接目标
CALL.IND sTarget   只保存 PC+8，再跳到 SGPR64 目标
JUMP.IND sTarget   不保存返回点，跳到 SGPR 目标
RET                恢复最近一次 CALL 保存的 return_pc
```

直接和间接目标都必须 8 字节对齐、完整落在当前文本内并指向合法指令。间接目标只能来自 S 域，不能逐 lane 不同。

`CALL`、`CALL.IND`、`JUMP.IND` 和 `RET` 的机器 class 都是 `CONTROL`，用户可读分类都是 W，不得改标成 SALU。它们的 `required_state` 都是 `scalar_ready`，并且必须在读取间接目标 SGPR 或调用栈之前检查；不满足时产生 `DIVERGENCE_FAULT`。

调用栈是每 warp 一份、程序不可见的 LIFO 状态，不占普通 S/V 寄存器。每个调用帧**只保存一个** `return_pc=PC+8`，不保存 active mask、重汇聚深度或其他控制上下文。最大深度只取 descriptor 的 `call_stack_depth`：

1. `CALL`/`CALL.IND` 先完成静态检查和 scalar-ready 检查，再验证目标、`PC+8` 和 `call_stack.depth < descriptor.call_stack_depth`；
2. 全部成功后，原子地压入一个调用项并把 PC 改成目标；
3. `SSY` 新建的每个重汇聚帧记录当时的 `owner_call_depth=call_stack.depth`；这个字段属于重汇聚帧，不属于调用帧；
4. `RET` 先检查 scalar-ready、非空调用栈，并要求重汇聚栈中不存在 `owner_call_depth == call_stack.depth` 的帧，再原子地弹出栈顶并恢复 `return_pc`；
5. `JUMP.IND` 只改 PC，绝不压栈或弹栈。

空栈 `RET`、CALL 达到 descriptor 深度，以及 RET 时仍有当前 callee 所有的重汇聚帧都产生 `RECONVERGENCE_FAULT`。任何失败都不留下半压栈、半弹栈或已改变的 PC。

### 7.10.4 精确控制提交

控制目标和隐藏状态必须一起提交。目标非法时不能先压入调用项；RET 失败时不能先弹出；JOIN 失败时不能先切换 active mask。

## 7.11 SYNC

SYNC 类包括内存栅栏和唯一一条 CTA 屏障。内存栅栏只建立第 4 章定义的顺序，不是 lane 会合。屏障的规范指令名和操作数次序固定为：

```text
BAR.SYNC.CTA id
```

架构没有 split 屏障、屏障 token 和 generation 计数。每 CTA 有 8 个槽 `0..7`，每槽只保存 `arrived_set` 和 waiter 映射；另有一个 8 槽共用的 CTA 级 `live_owner_set`。owner 唯一身份为 CTA 内 `linear_tid=warp_id*32+lane_id`。启动时全部槽为 idle（两者都空），`live_owner_set` 是 CTA 启动时全部真实线程的 `linear_tid`：尾部不存在 lane 从不计入，`EXIT` 把退出线程移除。

`BAR.SYNC.CTA` 的 `required_state` 是 `scalar_ready`，`guard_policy` 是 `required_pt`，执行域是 `cta_sync`：

- 先检查 scalar-ready；不满足时报告 `DIVERGENCE_FAULT`，不登记任何 arrival，也不改 PC。因为通过检查后 `active_mask == live_mask`，一个 warp 只能整体到达，不存在部分到达、重复到达或 wrong-owner 的情形。
- 把入口 active lane 转成 `owner_snapshot: set<linear_tid>`，做一次原子 arrival commit 和 shared CTA release，然后用 `BarrierWaitRecord {warp_id,owner_snapshot,resume_pc=old_PC+8}` 阻塞整个 warp。
- `arrived_set` 等于当前 `live_owner_set` 时，全部记录一起 acquire，并只写各自 `PC=resume_pc`、清 blocked record、置 ready，槽随即清空回 idle。
- `EXIT` 缩小 `live_owner_set` 后必须重新检查每个非 idle 槽，因为完成条件可能刚刚被满足。`EXIT` 自身不是 shared release。

每 warp 同时至多一条 blocked record。阻塞期间 PC 留在屏障指令上，active/live 掩码、重汇聚栈和调用栈保持不变，挂起路径不能切入；恢复也不改这些状态。

因为槽在完成时被清空，同一个槽的两次屏障之间不留任何状态，也就没有需要区分的“代”，实现不需要防止计数器回绕。

如果 CTA 内一部分 warp 到达某个槽，另一部分既不到达也不退出，`arrived_set` 永远追不上 `live_owner_set`，程序按第 3 章第 12 节报告 `DEADLOCK`。屏障的 release/acquire 只排序 shared；global、local、param、const 和 host 不因此有序，global 通信仍需原子和需要的 `MEMBAR`。完整状态转移伪代码见第 3 章第 10 节。

CTA 只有在全部 warp 完成且 8 个槽都 idle 时完成。idle 固定表示 `arrived_set` 和 `waiters` 都为空。

## 7.12 X 跨 lane

X 指令读取多个 lane 的冻结 V 源，并按 form 写 VGPR、SGPR、`vpN` 或 SCC。每个 form 必须明确：

```text
C  候选 lane
M  显式成员 mask（若有）
P  实际贡献者
R  结果接收者
```

vote/ballot 对 P 做布尔归约；shuffle 从 P 中选择源 lane；match 比较 P 中的键。member mask、mode 和 width 等必须在 C 中一致，逐 lane control 可以不一致。找不到合法源时是回退、写零还是故障，必须由具体 form 写明，不能由实现选择。

X 指令在读取任何目标前冻结全部源，因此原地 shuffle 合法。参与协议不一致产生 `COLLECTIVE_FAULT`，所有 lane 目标保持不变。

`V_SHUFFLE.DOWN.B32` 同时提供 VGPR delta 和 `0..31` 立即数 delta form。
两者都在 `width ∈ {2,4,8,16,32}` 的子组内选择
`source_lane = lane_id + delta`；源 lane 不 active 时结果为零。立即数 form
只是消除固定归约树中的 delta 装载指令，不改变参与、会合、冻结源或故障语义。

## 7.13 MMA

`M16N8K16` 形状只定义下面一个 form：

```text
MMA.M16N8K16.F16.F16.F32 dbase, abase, bbase, cbase
```

该 form 的 YAML `matrix_contract` 必须结构化表达下面同一份寄存器数量、对齐、lane 映射、元素映射、别名、数值和参与合同；YAML 与正文任一坐标不一致都必须阻断生成和发布。

它的 `execution_domain` 是 `warp_matrix`，固定 `PT`，要求 32 个 lane 全部参与，即 `active_mask == live_mask == 0xffffffff`。A、B 是 F16，C、D 是 F32。每 lane 的 A/B/C/D 片段分别占 4/2/4/4 个 VGPR，基址分别按 4/2/4/4 对齐。只允许 `D=C` 完整别名；D 与 A/B 重叠、D/C 部分重叠都非法。

下面是完整映射。令 lane `l=0..31`；`r` 是相对片段基址的寄存器偏移；`h=0` 取 VGPR 低 16 位，`h=1` 取高 16 位：

```text
# A: 每 lane 4 VGPR，共 8 个 F16
m = l // 2
k = 8*(l % 2) + 2*r + h           # r=0..3, h=0..1
A[m,k] = F16(VGPR[abase+r][16*h +: 16])

# B: 每 lane 2 VGPR，共 4 个 F16
k = l // 2
n = 4*(l % 2) + 2*r + h           # r=0..1, h=0..1
B[k,n] = F16(VGPR[bbase+r][16*h +: 16])

# C 和 D: 每 lane 各 4 VGPR，每槽一个 F32
m = l // 2
n = 4*(l % 2) + r                 # r=0..3
C[m,n] = F32(VGPR[cbase+r])
D[m,n] <-> VGPR[dbase+r]
```

所有 lane 的 A/B/C 必须先冻结。对每个 `m=0..15, n=0..7`：

```text
acc = C[m,n]
for k = 0,1,...,15:               # 次序固定，不能树形重排
    acc = FFMA.F32(
        V_CVT.F32.F16(A[m,k]),
        V_CVT.F32.F16(B[k,n]),
        acc)
D[m,n] = acc
```

每一步 FFMA 都在步末按 RNE 舍入到 F32，下一步读取这个已舍入值；不能跨 k 保留额外精度或改用 FP64 累加。F16 转 F32、NaN、Inf、subnormal 和有符号零完全按第 5 章。全部 D 片段只在所有结构和数值检查成功后一次提交。

缺 lane、不同动态 PC、基址不一致、组越界、错误对齐、非法别名或会合失败产生规定故障，所有 D 保持不变。MMA 不产生内存事件，也不隐含内存栅栏。其他 `M16N8K16` 拼写、类型或 modifier 都不是合法 form；其他形状若存在，只能来自 YAML 中另行给出的 form 和结构化合同。

## 7.14 system、故障和边界

`SYS` class 用于 NOP、YIELD、TRAP、GETREG、实现查询和 YAML 明确列出的系统操作。SYS 不是“全部都按 S 执行”的同义词；每个 SYS form 仍要声明用户可读分类和执行域。`S_GETREG` 属于 S，因此要求 scalar-ready；`V_GETREG` 属于 V，不套用该检查。

- `NOP` 只推进 PC。
- `YIELD` 是调度提示，不建立内存顺序。
- `TRAP reason` 产生规范软件故障；reason 只作为附加数据。
- 未声明为可写的系统状态不能通过普通指令修改。

同一动态指令的故障优先级、fault record 和全回滚规则只以 `docs/02-programming-model.md` 的故障优先级表为权威。本章不另排顺序；所有动态检查都必须先收集、再按第 2 章选一个故障，不能因实现内部的 lane 顺序改变结果。

## 7.15 逐 form 生成要求

构建系统必须从 `isa/vtx1/isa.yaml` 为每个启用 form 生成语义条目，至少包含：

```text
form_id
canonical mnemonic and syntax
machine class: SYS / SALU / VALU / MEMORY / CONTROL / SYNC / CROSSLANE / MATRIX
reader-facing shorthand, if useful: S / V / W / SYNC / X / MMA
semantic category
fixed 64-bit encoding fields
operand domains: SGPR / VGPR / SCC / vpN / memory / hidden state
guard_policy
required_state
address template
numeric and bit-width parameters
source snapshot and destination commit set
faults and ordering
normal, boundary and illegal examples
canonical machine word
```

family 数和 form 数不得写死在本章、测试代码或报告模板中。发布时实际数量只能由当前 YAML 去重生成，并与生成附录和 all-form 测试清单逐项核对。

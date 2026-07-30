# 6. 编码与汇编

本章定义 VTX-1 的全新固定 64 位指令编码。它与任何旧编码均不兼容；实现不得根据旧机器字、旧 opcode 或旧字段位置进行猜测、回退或双重解码。

本章中的“必须”“禁止”“可以”分别对应 MUST、MUST NOT、MAY。

## 6.1 总体原则

每条指令恰好占 64 位（8 字节），正常顺序执行时：

```text
next_pc = pc + 8
```

指令字 `W` 的位 0 是最低有效位。统一头部如下：

```text
bit 63                                      19 18       13 12       7 6     4 3      0
   +------------------------------------------+-----------+-----------+-------+--------+
   |              payload[44:0]               | guard[5:0]| opcode[5:0]|format | class  |
   +------------------------------------------+-----------+-----------+-------+--------+
```

```text
class   = W[3:0]
format  = W[6:4]
opcode  = W[12:7]
guard   = W[18:13]
payload = W[63:19]
```

头部字段的含义是：

- `class`：编码大类，决定接下来如何解释 `format`。
- `format`：操作数装箱格式，决定 payload 的基本字段位置。
- `opcode`：在 `(class, format)` 内局部编号的操作码。
- `guard`：向量执行的可选 lane 谓词。它不是普通数据操作数。
- `payload`：寄存器号、立即数及 opcode 专用字段，共 45 位。

`opcode` 不是全局编号。同一个 6 位数值可以在不同 `(class, format)` 下分配给不同指令。真正唯一的译码叶子是 `form`：每个 form 自己声明一个 `(class, format, opcode)` 三元组，该三元组在整个 ISA 清单中只能映射到这一个 form。译码器先由三元组找到 form，再按该 form 的 payload 约束完成解码。

### 6.1.1 字节序和取指

指令在文本段中按小端字节序保存：

```text
B[i] = W[8*i +: 8],  i = 0..7
W    = Σ (uint64(B[i]) << (8*i))
```

例如 `W = 0x1122334455667788` 在内存中从低地址到高地址为：

```text
88 77 66 55 44 33 22 11
```

PC 必须 8 字节对齐，且 `[pc, pc+8)` 必须完整位于当前内核文本内。未对齐取指、越界取指或不足 8 字节的尾部均为 `ILLEGAL_INSTRUCTION`；实现不得读取相邻对象来补齐指令。

## 6.2 class 与 format 分配

### 6.2.1 class

| class | 名称 | 用途 |
|---:|---|---|
| 0 | `SYS` | 系统、特殊寄存器、陷阱及杂项 |
| 1 | `SALU` | 标量算术与逻辑 |
| 2 | `VALU` | 向量算术与逻辑 |
| 3 | `MEMORY` | 标量/向量访存与原子 |
| 4 | `CONTROL` | 分支、重汇聚和控制流 |
| 5 | `SYNC` | 屏障与内存同步 |
| 6 | `CROSSLANE` | warp 内跨 lane 集合操作 |
| 7 | `MATRIX` | 矩阵乘加 |
| 8..15 | 保留 | 必须拒绝 |

`class=8..15` 产生 `ILLEGAL_INSTRUCTION`，不得解释为 NOP、提示指令或私有扩展。

### 6.2.2 每个 class 的 format

`format` 只在所属 `class` 内有意义：

| class | format 值 | format 名称 |
|---|---:|---|
| `SYS` | 0 | `SYS` |
| `SALU` | 0 | `S1` |
| `SALU` | 1 | `S2` |
| `SALU` | 2 | `S3` |
| `SALU` | 3 | `SCMP` |
| `SALU` | 4 | `SIMM` |
| `VALU` | 0 | `V1` |
| `VALU` | 1 | `V2` |
| `VALU` | 2 | `V3` |
| `VALU` | 3 | `VCMP` |
| `VALU` | 4 | `VIMM` |
| `MEMORY` | 0 | `SMEM` |
| `MEMORY` | 1 | `VMEM` |
| `MEMORY` | 2 | `VSHMEM` |
| `MEMORY` | 3 | `VLMEM` |
| `MEMORY` | 4 | `SATOM` |
| `MEMORY` | 5 | `VATOM` |
| `MEMORY` | 6 | `SMEMX` |
| `MEMORY` | 7 | `VATOMX` |
| `CONTROL` | 0 | `CTRL` |
| `SYNC` | 0 | `SYNC` |
| `CROSSLANE` | 0 | `COLL` |
| `MATRIX` | 0 | `MMA` |

表中未列出的 class/format 组合全部保留，并产生 `ILLEGAL_INSTRUCTION`。

在一个合法 `(class, format)` 内，`opcode=0..63` 中只有 form 清单明确分配的值合法。未分配 opcode 产生 `ILLEGAL_INSTRUCTION`。每个 form 必须同时给出 family、助记符、数据类型、执行域、操作数角色、payload 扩展字段和全部静态约束。

## 6.3 执行域、格式、family 与 form

### 6.3.1 执行域

`execution_domain` 只能取以下七个值：

| 值 | 动态执行含义 |
|---|---|
| `system` | 系统、特殊状态或陷阱操作；具体参与者由 form 定义 |
| `scalar` | 每个 warp 执行一次，主要读写 SGPR/SCC |
| `vector` | 在参与 lane 上逐 lane 执行，主要读写 VGPR/VP |
| `warp_control` | 每个 warp 执行一次并修改 PC、掩码或控制栈 |
| `warp_collective` | warp 内多个 lane 共同完成一次集合操作 |
| `cta_sync` | CTA 范围屏障或同步操作 |
| `warp_matrix` | warp 协作矩阵操作 |

不得使用 `control`、`synchronization`、`collective`、`matrix` 等近义值。

执行域影响参与者、状态副作用和 guard 合法性，但不直接规定 payload 的位布局。

`execution_domain` 与机器头部的 `class` 不是同一概念。`class` 是编码和 opcode 分配的命名空间，`execution_domain` 才规定动态执行方式。尤其是 `MEMORY` class 同时包含 scalar 和 vector form：

- `SMEM`、`SMEMX`、`SATOM` 的 `execution_domain: scalar`；
- `VMEM`、`VSHMEM`、`VLMEM`、`VATOM`、`VATOMX` 的 `execution_domain: vector`。

因此不能因为 `class=MEMORY` 就跳过 scalar-ready 检查，也不能因为 form 位于 MEMORY class 就推断它一定逐 lane 执行。

### 6.3.2 编码格式

编码格式回答“操作数放在哪些位”。例如 `V2` 表示一个向量目标和两个向量源使用固定的三个 8 位槽。`V2` 本身不表示整数、浮点、加法或乘法。

### 6.3.3 family：语义分组

`family` 是语义分组，回答“一组 form 共同表达什么操作以及采用哪些数值规则”，例如：

- 整数算术；
- 浮点算术；
- 位运算；
- 比较；
- 数据移动；
- 内存访问；
- 原子；
- 控制流；
- 同步或集合。

family 不是编码字段，不参与唯一译码，也不是 `format` 的别名。一个 family 可以包含多个 form，并跨越不同格式。例如标量整数加法 family 可以同时包含：

```text
form=iadd_s2_u32    class=SALU format=S2   opcode=...
form=iadd_simm_u32  class=SALU format=SIMM opcode=...
```

两个 form 属于同一个 `IADD` family：前者从两个 SGPR 取源，后者从一个 SGPR 和一个立即数取源。译码器不能先找 family 再猜格式；它必须直接用每个 form 声明的 `(class, format, opcode)` 找到唯一叶子。

### 6.3.4 form：唯一译码叶子

`form` 是完整、可编码、可执行的叶子定义。它必须固定：

- `(class, format, opcode)`；
- family 和 canonical 助记符；
- `execution_domain` 和 `required_state`；
- `guard_policy`；
- 每个 payload 位的用途；
- 操作数类型、立即数解释、must-zero 和静态约束；
- 精确语义与故障。

family 可以有任意多个 form，但两个 form 禁止声明相同的 `(class, format, opcode)`。字段值也禁止把一个 form 二次分派成另一个 form。payload modifier 可以在同一个 operation form 内取多个明确列出的合法值；modifier 值不是新的 form，也不得为每个 modifier 组合额外消耗 opcode。

最直白的例子是：

```text
IADD.U32 v1, v2, v3
FADD.F32 v1, v2, v3
```

两条指令都使用 `VALU/V2`，所以 `vd=v1`、`va=v2`、`vb=v3` 的位位置完全相同。但是它们属于不同 family，form 和 opcode 也不同：`IADD` family 执行 32 位整数加法，`FADD` family 执行 IEEE 754 binary32 加法。译码器绝不能因为看到 `V2` 就认定它是整数指令，也不能因为寄存器号相同就猜测数据类型。

反过来，同一个 family 可以使用多个格式。例如整数加法的寄存器 form 可使用 `S2`，带立即数 form 可使用 `SIMM`；向量版本也可分别使用 `V2` 和 `VIMM`。family 相同不意味着编码格式相同。

## 6.4 guard

### 6.4.1 编码

`guard[5:0]` 的合法值为：

```text
0       PT
1       !PT
2..17   vp0..vp15       （编码 = 2 + n）
18..33  !vp0..!vp15     （编码 = 18 + n）
34..63  reserved
```

`PT` 恒真，`!PT` 恒假。`vp0..vp15` 是逐 lane 向量谓词，取反只影响本次条件读取，不修改谓词寄存器。保留 guard 编码产生 `ILLEGAL_INSTRUCTION`。

canonical 汇编省略 `@PT`，其他形式写作：

```text
@!PT
@vp3
@!vp3
```

### 6.4.2 按 form 判断 guard

guard 合法性只能在译码出 form 后，根据该 form 的 `execution_domain` 和 `guard_policy` 判断。machine `class` 和 `format` 都不能单独决定 guard：

| `guard_policy` | header guard 规则 |
|---|---|
| `optional` | 允许 `PT`、`!PT`、`vpN`、`!vpN`；该 policy 只允许用于 `execution_domain: vector` |
| `required_pt` | 必须精确编码为 `PT(0)` |
| `explicit_condition` | header guard 必须为 `PT(0)`；实际 lane 条件来自 payload 的显式数据条件字段 |

典型反例说明为什么不能按 class 判断：

- `V_GETREG` 位于 `SYS` class，但其 form 是 `execution_domain: vector`、`guard_policy: optional`，所以允许非 PT；
- `V_SHUFFLE.DOWN.B32` 位于 `CROSSLANE` class、使用 `COLL` format，是真正的 `warp_collective` form，必须 `required_pt`；
- `S_READFIRST` 也位于 `CROSSLANE/COLL`，但它是 `scalar` form，必须 `required_pt` 并检查 scalar-ready。

所有 `execution_domain: cta_sync`、`warp_collective`、`warp_matrix` 的 form 都必须 `guard_policy: required_pt`。因此 SYNC form、全部 COLL 集合 form 和唯一 MMA form 都只能使用 PT。

每个 `execution_domain: scalar` 的 form 都必须声明 `guard_policy: required_pt` 和 `required_state: scalar_ready`，并在读取任何动态源前检查：

```text
live_mask != 0
active_mask == live_mask
reconv_stack 中不存在 phase 为 FIRST 或 SECOND 的帧
```

三个条件必须同时成立。条件不满足产生 `DIVERGENCE_FAULT`，且不得读取 SGPR、SCC 或内存源。`ARMED` 帧本身不破坏 scalar-ready。

CONTROL form 的状态要求逐 form 声明，不能由 machine class 推导：

| form | `guard_policy` | `required_state` |
|---|---|---|
| `CALL` direct | `required_pt` | `scalar_ready` |
| `CALL.IND` | `required_pt` | `scalar_ready` |
| `JUMP.IND` | `required_pt` | `scalar_ready` |
| `RET` | `required_pt` | `scalar_ready` |
| `BRA` | `required_pt` | `none` |
| `BRA.P` | `explicit_condition` | `none` |

因此 `BRA/BRA.P` 在分歧状态下仍可按控制流语义执行，禁止对它们附加 scalar-ready 条件。

`CTRL` 中作为数据操作数的分支条件不属于头部 guard。`warp_collective` 和 `warp_matrix` form 不允许用 lane guard 改变集合参与者。

guard 为假只抑制该 lane 的架构效果，不抑制静态译码。未知 opcode、保留字段、must-zero 非零、非法寄存器组等错误，即使 guard 为 `!PT` 也必须被检测。

## 6.5 payload 基本格式

以下各表使用 payload 局部编号 `P[44:0] = W[63:19]`。`x` 表示 opcode 专用扩展区。每个 form 必须进一步把 `x` 的每一位定义为具名字段或 must-zero；不存在“实现忽略”的扩展位。

### 6.5.1 SYS

| P 位 | 字段 |
|---|---|
| `[7:0]` | `a` |
| `[15:8]` | `b` |
| `[23:16]` | `c` |
| `[39:24]` | `imm16` |
| `[44:40]` | `x5` |

`a/b/c` 的寄存器类别由 opcode 固定。未使用的槽必须为零。系统指令不得通过字段值猜测操作数形式。

### 6.5.2 SALU

| format | P 位布局 |
|---|---|
| `S1` | `[7:0] sd, [15:8] sa, [44:16] x29` |
| `S2` | `[7:0] sd, [15:8] sa, [23:16] sb, [44:24] x21` |
| `S3` | `[7:0] sd, [15:8] sa, [23:16] sb, [31:24] sc, [44:32] x13` |
| `SCMP` | `[7:0] zero8, [15:8] sa, [23:16] sb, [44:24] x21` |
| `SIMM` | `[7:0] sd, [15:8] sa, [39:16] imm24, [44:40] x5` |

`S1/S2/S3` 分别提供一、二、三个标量源槽。`SCMP` 没有显式目标寄存器：`zero8` 必须为零，比较 `sa` 与 `sb` 后隐式写每 warp 一份的 1 位条件码 `SCC`，false 写 0，true 写 1。`SIMM` 提供一个 SGPR 源和一个最多 24 位的立即数容器。全部 SALU form 都声明 `guard_policy: required_pt` 和 `required_state: scalar_ready`。

### 6.5.3 VALU

| format | P 位布局 |
|---|---|
| `V1` | `[7:0] vd, [15:8] va, [16] ssrc, [44:17] x28` |
| `V2` | `[7:0] vd, [15:8] va, [23:16] vb, [25:24] ssrc_sel, [44:26] x19` |
| `V3` | `[7:0] vd, [15:8] va, [23:16] vb, [31:24] vc, [33:32] ssrc_sel, [44:34] x11` |
| `VCMP` | `[3:0] vpd, [7:4] zero4, [15:8] va, [23:16] vb, [25:24] ssrc_sel, [44:26] x19` |
| `VIMM` | `[7:0] vd, [15:8] va, [39:16] imm24, [44:40] x5` |

`VCMP` 通过 `vpd` 显式选择并写 `vp0..vp15`，且 `zero4` 必须为零；它不写 SCC。`VIMM` 的目标是 VGPR；需要“比较寄存器与立即数”的程序必须使用明确分配的比较立即数 opcode/格式，若清单未分配则先物化常量，汇编器不得擅自把 `VIMM.vd` 解释为谓词目标。

这四种 VALU 格式与 SALU 的对应格式的唯一结构差别，就是它们从扩展字段里切出一个 scalar-source selector。`V1` 用 1 位的 `ssrc`，`V2`、`V3`、`VCMP` 用 2 位的 `ssrc_sel`。selector 不改变源槽的位置和宽度，只改变那 8 位寄存器号在哪个寄存器文件里解释：

| format | selector 字段 | 合法值 | 含义 |
|---|---|---|---|
| `V1` | `ssrc` | 0 | `va` 读 VGPR |
| `V1` | `ssrc` | 1 | `va` 读 SGPR |
| `V2`、`VCMP` | `ssrc_sel` | 0 / 1 / 2 | 无标量源 / `va` 读 SGPR / `vb` 读 SGPR |
| `V2`、`VCMP` | `ssrc_sel` | 3 | 保留，`ILLEGAL_INSTRUCTION` |
| `V3` | `ssrc_sel` | 0 / 1 / 2 / 3 | 无标量源 / `va` / `vb` / `vc` 读 SGPR |

因此一条 VALU 指令最多只有一个 SGPR 源。清单中把可以这样切换的源操作数写成 `vsrc32` 或 `vsrc64` 类型；只有 `execution_domain: vector` 且格式属于 `V1/V2/V3/VCMP` 的 form 才允许出现这两种类型。目标操作数永远是 VGPR 或 `vpN`，不受 selector 影响。

不含 `vsrc*` 操作数的 VALU form（例如 `VIMM` 系列，或所有源都固定为 VGPR 的 form）必须把 selector 字段编码为零；它在这些 form 里是 must-zero 洞，非零就是 `ILLEGAL_INSTRUCTION`。

`vsrc64` 的两个寄存器文件都要求偶数对齐的完整寄存器对，语法上必须写全，例如：

```text
V_MOV.B64 v2:v3, v4:v5      # ssrc=0
V_MOV.B64 v2:v3, s4:s5      # ssrc=1
```

汇编器由源操作数的前缀唯一确定 selector 值：写 `sN` 就置对应的 selector 码，写 `vN` 就保持该位置为 VGPR。同一条指令里出现两个 `sN` 源没有可用编码，必须报错，而不是自行插入搬运指令。

### 6.5.4 MEMORY

`SMEM`：

| P 位 | 字段 |
|---|---|
| `[7:0]` | `sdata` |
| `[15:8]` | `sbase` |
| `[39:16]` | `simm24` |
| `[44:40]` | `x5` |

`SMEMX`：

| P 位 | 字段 |
|---|---|
| `[7:0]` | `sdata` |
| `[15:8]` | `sbase` |
| `[23:16]` | `sindex` |
| `[39:24]` | `imm16` |
| `[44:40]` | `mods` |

`SMEMX` 的地址形式是“SGPR base + SGPR index + immediate”。`sbase` 是统一地址的 SGPR 基址或基址组，`sindex` 是 SGPR 索引，`imm16` 是有符号字节偏移。三者的扩展、索引缩放和基址组宽由具体 form 固定；未定义的 `mods` 位必须为零。译码器不得根据某个值为零来省略或改换地址模式。`SMEMX` 是 scalar form，必须声明 `guard_policy: required_pt` 和 `required_state: scalar_ready`。

`VMEM/VSHMEM/VLMEM` 共享字段位置，但地址空间和地址形成规则不同：

| P 位 | 字段 |
|---|---|
| `[7:0]` | `vdata` |
| `[15:8]` | `vaddr` |
| `[23:16]` | `sbase` |
| `[39:24]` | `simm16` |
| `[44:40]` | `x5` |

`VMEM` 有且只有以下三种地址 form；syntax 和 must-zero 规则都是 form 的一部分：

| 地址 form | canonical 地址 syntax | `sbase` | `vaddr` |
|---|---|---|---|
| uniform base | `[sB:s(B+1) + imm]` | 64 位 SGPR base，必须显式寄存器对 | must-zero |
| uniform base + lane index | `[sB:s(B+1) + vI + imm]` | 64 位 SGPR base，必须显式寄存器对 | 32 位 VGPR index |
| lane base | `[vB:v(B+1) + imm]` | must-zero | 64 位 VGPR base，必须显式寄存器对 |

例如：

```text
V_LD.GLOBAL.U32 v0, [s2:s3 + 16]
V_LD.GLOBAL.U32 v0, [s2:s3 + v4 + 16]
V_LD.GLOBAL.U32 v0, [v6:v7 + 16]
```

地址模式属于 form 定义，不是运行时 selector。uniform-base form 即使实际 base 数值为零，`vaddr` 仍必须为零；lane-base form 的 `sbase` 必须为零；indexed form 的 `sbase/vaddr` 都是有效操作数，不是 must-zero。SV-mix（uniform base + lane index）地址形成固定为：

```text
effective_address[lane] =
    SGPR64(sbase:sbase+1)
    + zero_extend_64(VGPR32(vaddr)[lane])
    + sign_extend_64(simm16)
```

VGPR32 index 必须零扩展，禁止符号扩展或先按 32 位回绕。

LOCAL 只允许单个 32 位 `vaddr` 加 `simm16`：

```text
V_LD.LOCAL.U32 v0, [v2 + 16]
V_ST.LOCAL.U32 [v2 + 16], v0
```

所有 LOCAL form 的 `sbase` 必须为零；LOCAL 禁止 SGPR base、SGPR+VGPR indexed 地址和 64 位 VGPR base。`VSHMEM` 的合法地址 form 由 shared-memory 清单单独固定，不得借用 VMEM 的 global 64 位 base 规则。

`SATOM/VATOM`：

| P 位 | `SATOM` | `VATOM` |
|---|---|---|
| `[7:0]` | `sdst` | `vdst` |
| `[15:8]` | `sbase` | `vaddr` |
| `[23:16]` | `sdata0` | `vdata0` |
| `[31:24]` | `sdata1` | `vdata1` |
| `[39:32]` | `simm8` | `simm8` |
| `[41:40]` | `order` | `order` |
| `[43:42]` | `scope` | `scope` |
| `[44]` | `x1` | `x1` |

`VATOMX`：

| P 位 | 字段 |
|---|---|
| `[7:0]` | `vdst` |
| `[15:8]` | `sbase` |
| `[23:16]` | `vindex` |
| `[31:24]` | `vdata0` |
| `[39:32]` | `vdata1` |
| `[41:40]` | `order` |
| `[43:42]` | `scope` |
| `[44]` | `x` |

`VATOMX` 的地址形式固定为：

```text
effective_address[lane] =
    SGPR64(sbase:sbase+1) + zero_extend(VGPR32(vindex))
```

索引 scale 固定为 1 字节，payload 中没有 scale 字段；该格式也没有位移字段，canonical syntax 不得追加 `+ 0`：

```text
V_ATOM.ADD.U32.GLOBAL.ACQ_REL.DEVICE v0, [s2:s3 + v4], v5
```

`x` 必须为零。`VATOMX` 是 vector form，使用和 `VATOM` 相同的 `order/scope` modifier，并允许 `guard_policy: optional`。

原子顺序编码固定为：

```text
0 RELAXED
1 ACQUIRE
2 RELEASE
3 ACQ_REL
```

原子 scope 编码固定为：

```text
0 CTA
1 DEVICE
2 SYSTEM
3 reserved
```

scope 名称统一使用 `DEVICE`，不得输出或接受 `GPU` 作为 canonical 名称。`scope=3` 产生 `ILLEGAL_INSTRUCTION`。

`order` 和 `scope` 是 payload modifier。opcode 只选择原子 operation form（例如 LOAD、STORE、ADD、XCHG、CAS）及该 form 固定的宽度/地址模式；同一个 `(class, format, opcode)` form 可以接受多个合法 `order/scope` 值。禁止为 `ADD.RELAXED.DEVICE`、`ADD.ACQUIRE.DEVICE` 等组合另分配 opcode 或另建 form。

合法矩阵为：

| operation 类别 | 合法 order | global 合法 scope | shared 合法 scope |
|---|---|---|---|
| LOAD | `RELAXED`, `ACQUIRE` | `CTA`, `DEVICE`, `SYSTEM` | `CTA` |
| STORE | `RELAXED`, `RELEASE` | `CTA`, `DEVICE`, `SYSTEM` | `CTA` |
| RMW（含 XCHG、算术/位运算、CAS） | `RELAXED`, `ACQUIRE`, `RELEASE`, `ACQ_REL` | `CTA`, `DEVICE`, `SYSTEM` | `CTA` |

不在矩阵中的已定义 modifier 组合产生 `ILLEGAL_OPERAND`。shared 原子若 `scope != CTA` 必须拒绝；不能把更大的 scope 静默收窄为 CTA。

canonical 原子助记符的字段顺序固定为：

```text
S_ATOM.<op>.<type>.<space>.<order>.<scope>
V_ATOM.<op>.<type>.<space>.<order>.<scope>
```

文本必须同时显式写出 space、order 和 scope，不存在省略或换序：

```text
S_ATOM.LOAD.U32.GLOBAL.ACQUIRE.DEVICE s0, [s2:s3 + 0]
V_ATOM.STORE.U32.GLOBAL.RELEASE.SYSTEM [v2:v3 + 0], v4
V_ATOM.ADD.U32.GLOBAL.ACQ_REL.CTA v0, [s2:s3 + v4], v5
```

交换 operation 的唯一 canonical 名称是 `XCHG`。汇编器和反汇编器不得输出 `EXCH`、`EXCHANGE` 或其他拼写。

非 CAS 原子的 `data1` 必须为零。地址空间、访问宽度、load/store/atomic operation 和数据类型由 opcode 明确指定，禁止从某个寄存器槽是否为零来猜测。

### 6.5.5 CTRL

| P 位 | 字段 |
|---|---|
| `[29:0]` | `disp30` |
| `[35:30]` | `cond6` |
| `[43:36]` | `aux8` |
| `[44]` | `x1` |

`disp30` 是唯一允许的直接控制目标表示，是相对 `next_pc` 的有符号指令字位移，见 6.8。`cond6` 作为数据条件时使用与 guard 相同的 `PT/!PT/vp/!vp` 编码，但它不是 header guard。`aux8` 的角色由 opcode 固定。无条件控制指令未使用的 `cond6/aux8` 必须为零；不带直接目标的控制指令未使用的 `disp30` 必须为零。

`CALL` direct 的目的地只能编码在 `disp30` 中。`CALL.IND/JUMP.IND` 使用 `aux8` 编码 SGPR 目标基址，其 `disp30/cond6/x1` 必须为零；它们不引入另一种直接目标编码。`RET` 从架构调用栈取得目的地，因此 `disp30/cond6/aux8/x1` 必须全部为零。

`CALL` direct、`CALL.IND`、`JUMP.IND` 和 `RET` 都必须声明 `guard_policy: required_pt`、`required_state: scalar_ready`。`BRA` 必须声明 `guard_policy: required_pt`、`required_state: none`；`BRA.P` 必须声明 `guard_policy: explicit_condition`、`required_state: none`。

`SSY` 成功压入重汇聚帧时，必须把当前调用栈深度快照到隐藏字段：

```text
frame.owner_call_depth = call_stack.depth
```

`owner_call_depth` 不在 CTRL payload 中，也没有软件可见编码；它是 SSY 动态效果的一部分。`RET` 在弹出调用帧前必须拒绝任何 `owner_call_depth == call_stack.depth` 的未闭合重汇聚帧，报告 `RECONVERGENCE_FAULT`；较小 owner depth 的调用者帧保持不变。

### 6.5.6 SYNC

| P 位 | 字段 |
|---|---|
| `[7:0]` | `a` |
| `[15:8]` | `b` |
| `[31:16]` | `imm16` |
| `[34:32]` | `slot3` |
| `[36:35]` | `scope2` |
| `[38:37]` | `order2` |
| `[44:39]` | `x6` |

`a/b` 是 opcode 指定类别的寄存器槽。屏障槽 `slot3` 可表达 0..7。唯一使用它的指令是 `BAR.SYNC.CTA`，其中 `slot3` 是显式 `barrier_id`；非屏障同步指令必须把 `slot3` 置零。scope/order 只在相应 opcode 明确定义时有效，否则必须为零。

屏障的规范编码只有一条：

| family | `(class,format,opcode)` | canonical 汇编 | payload 非零字段 | 示例机器字 |
|---|---|---|---|---|
| `bar-sync` | `(5,0,3)` | `BAR.SYNC.CTA id` | `slot3=id` | `BAR.SYNC.CTA 3` → `0x0018000000000185` |

`a/b/imm16/scope2/order2/x6` 都必须为零。屏障不写寄存器，也不读寄存器源，所以 SYNC payload 里没有屏障寄存器槽；`(5,0,4)` 和 `(5,0,5)` 在 1.0 Draft 中未分配，译码为 `ILLEGAL_INSTRUCTION`。

`BarrierWaitRecord {warp_id,owner_snapshot,resume_pc}`、warp blocked record、槽内 waiter 映射和 CTA 的 `live_owner_set` 都是执行状态，不占 SYNC payload。`resume_pc` 固定为该动态屏障指令的 `old_PC+8`，不能由汇编显式提供。

### 6.5.7 COLL

| P 位 | 字段 |
|---|---|
| `[7:0]` | `vd` |
| `[15:8]` | `va` |
| `[23:16]` | `vb` |
| `[31:24]` | `smask` |
| `[39:32]` | `imm8` |
| `[44:40]` | `x5` |

`smask` 是保存 lane mask、标量源或标量目标的 SGPR 号，不是 8 位 lane mask 本身。COLL 格式只承载 `warp_collective` 和 `scalar` form，它们都要求 header guard 为 PT。COLL 没有 scalar-source selector：需要把统一值送进各 lane 的场合用 `V1` 的混合源 `V_MOV`，不用跨 lane 格式。

`S_READFIRST.B64` 的 64 位两端都必须显式写完整、偶数对齐且不越界的寄存器对：

```text
S_READFIRST.B64 s6:s7, v8:v9
```

它的 `smask` 编码 SGPR 目标对基址，`va` 编码 VGPR 源对基址，`vd/vb/imm8/x5` 必须为零；它是 `execution_domain: scalar`、`guard_policy: required_pt`、`required_state: scalar_ready`。它必须从同一个最低编号 active lane 原子快照两个 32 位半部，禁止两个半部分别选择 lane。

反方向的 SGPR64 到各 lane VGPR64 搬运由 `V1` 格式的 `V_MOV.B64 vE:v(E+1), sA:s(A+1)`（`ssrc=1`）完成，见 6.5.3。

`V_SHUFFLE.DOWN.B32` 有两个保留相同 width 编码的 form：

```text
V_SHUFFLE.DOWN.B32 vd, vs, vdelta, width   # (CROSSLANE,0,11)
V_SHUFFLE.DOWN.B32 vd, vs, delta,  width   # (CROSSLANE,0,13)
```

寄存器 form 的 `vb` 是 VGPR delta 编号；立即数 form 的 `vb` 直接编码
`0..31` 的无符号 delta。两者的 `imm8` 都按字面值编码
`width ∈ {2,4,8,16,32}`，`smask/x5` 必须为零。form 只能由 opcode 区分，
不得根据 `vb` 的数值或操作数恰好为零猜测。

### 6.5.8 MMA

| P 位 | 字段 |
|---|---|
| `[7:0]` | `vd_base` |
| `[15:8]` | `va_base` |
| `[23:16]` | `vb_base` |
| `[31:24]` | `vc_base` |
| `[39:32]` | `attr8` |
| `[44:40]` | `x5` |

MATRIX class 只定义一个完整 form：

```text
family: MMA
form: m16n8k16_f16_f16_f32
mnemonic: MMA.M16N8K16.F16.F16.F32
class: MATRIX
format: MMA
opcode: 0
execution_domain: warp_matrix
guard_policy: required_pt
required_state: none
```

四个寄存器号都是该 form 固定映射的片段组基址；形状、A/B/C/D 元素类型、累加顺序、组长度、对齐和别名规则全部由这一个 form 完整规定。`attr8` 和 `x5` 必须全零。其他 MMA opcode、形状、类型、饱和模式或 modifier 均保留，不得从 `attr8/x5` 猜测扩展。MMA 是 warp 集合操作，header guard 必须为 PT。

## 6.6 寄存器编码与资源边界

SGPR 和 VGPR 使用彼此独立的 8 位编号空间：

```text
s0..s255   SGPR
v0..v255   VGPR
vp0..vp15  向量谓词
SCC         每 warp 一份的隐式 1 位标量条件码
```

8 位字段能表达 0..255，不等于每个内核都能使用 256 个寄存器。模块资源描述必须分别给出 `sgpr_count` 和 `vgpr_count`；合法引用必须满足：

```text
0 <= sN < sgpr_count
0 <= vN < vgpr_count
```

若实现或模块还声明 `vp_count`，则必须满足 `0 <= vpN < vp_count <= 16`；否则架构可见的默认上限为 16。

寄存器组由其低编号基址编码。组的每个成员都必须低于相应资源计数，并满足该操作数声明的对齐。例如两个 VGPR 的组要求偶数基址时，`v7` 非法，`v254:v255` 只有在 `vgpr_count=256` 时才合法。MMA 片段组使用其 opcode 明确规定的长度和对齐，不能套用普通双寄存器规则。

任何 64 位操作数在汇编文本中都必须显式写成寄存器对：

```text
s2:s3
v6:v7
```

该规则适用于 64 位源、目标和地址 base。机器字段仍只编码低编号基址，但汇编器禁止接受单独的 `s2` 或 `v6` 作为 64 位操作数，反汇编器也禁止省略第二个寄存器。

字段类别是静态的。要求 SGPR 的槽不能写 `vN`，要求 VGPR 的槽不能写 `sN`。编码中不存在通用寄存器号，也不存在通过最高位区分 SGPR/VGPR 的规则。

某 opcode 未使用的寄存器槽编码为零时，零只是 canonical 填充值，不表示读取 `s0` 或 `v0`。

`SCC` 没有寄存器编号，也不占任何 8 位寄存器槽。`SCMP` 隐式写 SCC。本编码没有 SCC 源字段，也不定义泛化的“SCC 条件执行”；`CTRL.cond6` 只能编码 `PT/!PT/vpN/!vpN`，不能编码 SCC。

## 6.7 立即数

### 6.7.1 类型

每个立即数操作数必须声明以下一种解释：

- `uimmN`：范围 `0 .. 2^N-1`，执行时零扩展。
- `simmN`：范围 `-2^(N-1) .. 2^(N-1)-1`，低 N 位使用二补数，执行时符号扩展。
- `bitsN`：恰好 N 位原始位型，不赋予有符号数值含义。
- 枚举：只接受清单列出的名称和值。

汇编器禁止静默截断、取模或饱和超范围字面量。移位执行可能只读取源的低若干位，不代表立即数字面量可以超出其声明范围。

### 6.7.2 小于容器宽度的立即数

当语义立即数宽度 N 小于格式容器宽度 M 时，只使用容器低 N 位，容器高 `M-N` 位必须为零。该规则对有符号立即数同样适用。

例如 `simm12(-1)` 放入 `imm24` 时：

```text
合法：imm24 = 0x000fff
非法：imm24 = 0xffffff
```

执行时先取低 12 位，再从 12 位符号扩展。这样同一个立即数只有一种机器编码。

`SIMM/VIMM` 的容器宽度是 24 位；`SYS` 的容器宽度是 16 位；`SMEMX` 和向量 memory 的位移容器是 16 位；`SATOM/VATOM` 的位移容器是 8 位；`VATOMX` 没有立即数容器。某 opcode 可以使用更窄的立即数，但不能使用比所在容器更宽的立即数。

一条指令最多有一个普通立即数容器。若某个运算需要不能直接编码的常量，汇编器必须报错或由显式宏展开为多条指令；不得悄悄改变 opcode、交换非交换操作数或借用未定义 payload 位。

浮点立即数只有在 opcode 明确声明 `bitsN` 格式时才可直接编码。不能装入 24 位容器的 binary32 常量必须通过常量物化序列或内存加载获得。

## 6.8 PC-relative 控制目标

所有直接控制目标都使用 `CTRL.disp30`，并相对于当前指令的 `next_pc` 计算：

```text
D         = sign_extend_30(disp30)
target_pc = next_pc + (D << 3)
next_pc   = pc + 8
```

位移单位是 8 字节指令字，不是字节。汇编或链接标签 `target` 时：

```text
delta = target - (pc + 8)
require delta % 8 == 0
D = delta / 8
require -2^29 <= D <= 2^29 - 1
disp30 = D & (2^30 - 1)
```

目标必须由 `disp30` 相对 `next_pc` 得出，结果必须 8 字节对齐，并指向当前内核文本中的一条完整指令。任何其他目标字段或基准都不是合法编码。

例如：

```text
pc=0x40, target=0x80:
next_pc=0x48, D=(0x80-0x48)/8=7, disp30=0x00000007

pc=0x80, target=0x40:
next_pc=0x88, D=(0x40-0x88)/8=-9, disp30=0x3ffffff7
```

链接器只能对 `disp30` 应用 PC-relative 重定位。重定位溢出必须报错，不得截断，也不得自动插入跳板，除非调用者明确启用了会改变代码布局的链接器变换。

## 6.9 must-zero 与唯一编码

每个 form 必须把自己的 45 个 payload 位逐位归入且只归入以下一种：

1. 操数字段；
2. 有定义的枚举或修饰字段；
3. must-zero。

任何没有当前 opcode 语义的位都是 must-zero，包括：

- 基本格式中的未使用寄存器槽；
- opcode 未定义的 `x` 位；
- 小立即数容器中未使用的高位；
- `SCMP.zero8`；
- VMEM uniform-base form 的 `vaddr`、lane-base form 的 `sbase`，以及全部 LOCAL form 的 `sbase`；
- `VATOMX.x`；
- 非 CAS 原子的 `data1`；
- 不使用 scope/order 的格式中的对应字段；
- `VCMP.zero4`；
- 不带目标的控制指令中的 `disp30`。

任一 must-zero 位为 1 都产生 `ILLEGAL_INSTRUCTION`。实现禁止忽略垃圾位后继续执行。

唯一编码要求是：

- 一个合法机器字只能匹配一个 form；
- 一个 canonical 汇编指令只能生成一个机器字；
- 不得根据寄存器号是否为零、立即数值大小或保留位模式选择另一种解释；
- 语法别名必须在编码前归一化；
- canonical 反汇编后重新汇编必须逐位得到原机器字。

```text
assemble(disassemble(W, canonical=true)) == W
```

“唯一编码”指抽象指令及其显式操作数、类型、guard 和修饰符具有唯一表示，不表示所有可观察效果相同的指令必须共用机器字。例如不同 opcode 的 `@!PT` 指令都不提交 lane 效果，但仍是不同的抽象指令和不同编码。

## 6.10 规范汇编文本

canonical 文本遵守以下规则：

- 助记符、类型、地址空间、scope 和 order 使用大写；
- SGPR 写作 `sN`，VGPR 写作 `vN`，向量谓词写作 `vpN`；
- `@PT` 省略，其他合法 guard 显式写出；
- 数值立即数默认十进制，原始位型默认十六进制；
- 负数必须带 `-`，不得依赖超宽十六进制字面量猜测符号；
- 直接控制目标优先显示符号；无符号时显示相对 `next_pc` 的有符号位移，不显示其他目标表示；
- 64 位操作数必须显示完整寄存器对，其他寄存器组必须显示完整范围或使用其专用片段语法；
- 原子助记符必须严格按 `<op>.<type>.<space>.<order>.<scope>` 排列，交换操作只写 `XCHG`；
- 屏障只显示为 `BAR.SYNC.CTA id`，id 必须显式出现；
- 混合源操作数按实际寄存器文件显示为 `sN`/`sE:s(E+1)` 或 `vN`/`vE:v(E+1)`，反汇编不得把 SGPR 源印成 VGPR 号；
- 不允许根据助记符拼写、寄存器前缀或字面量大小模糊选择多个候选形式。

汇编器可以接受大小写、显式 `@PT`、零偏移省略等无损语法别名，但必须先归一化到唯一形式。`BARRIER`、`BARRIER_ARRIVE`、`BARRIER_WAIT`、`V_BCAST`、`MEMBAR` 都不是任何指令的兼容名称或 canonical 别名，必须按未知助记符拒绝。内存排序指令的唯一助记符是 `FENCE.CTA/DEVICE/SYSTEM`。需要多条机器指令的伪操作属于宏，不是编码别名；listing 和调试信息必须显示实际展开。

若源文本不能唯一确定 `(class, format, opcode)`、数据类型、寄存器类别或立即数解释，汇编器必须报错并列出冲突候选，不得按声明顺序或“最接近”原则选择。

非法机器字的反汇编必须输出：

```text
.word 0x................
```

并附带首个静态拒绝原因，不得发明可执行助记符。

## 6.11 译码顺序与错误分类

实现可以并行完成检查，但架构结果必须等价于：

```text
W = load_u64_le(text, pc)

require aligned_and_complete(pc)                         else ILLEGAL_INSTRUCTION
require class_is_assigned(W[3:0])                        else ILLEGAL_INSTRUCTION
require format_is_assigned(W[3:0], W[6:4])               else ILLEGAL_INSTRUCTION
require opcode_is_assigned(W[3:0], W[6:4], W[12:7])      else ILLEGAL_INSTRUCTION
form = lookup_exact_form(class, format, opcode)

require guard_code_is_defined(W[18:13])                  else ILLEGAL_INSTRUCTION
require guard_allowed_for_form(form, W[18:13])           else ILLEGAL_INSTRUCTION
require all_must_zero_bits_are_zero(form, W[63:19])      else ILLEGAL_INSTRUCTION
require all_encoded_enums_are_defined(form, W[63:19])    else ILLEGAL_INSTRUCTION

require register_banks_match(form)                       else ILLEGAL_OPERAND
require register_ids_within_resource_counts(form)        else ILLEGAL_OPERAND
require register_groups_aligned_and_in_range(form)       else ILLEGAL_OPERAND
require static_operand_combinations_are_legal(form)      else ILLEGAL_OPERAND
require direct_target_is_legal_if_checked_now(form)      else ILLEGAL_OPERAND

if form.required_state == scalar_ready:
    require scalar_ready(warp_state)                     else DIVERGENCE_FAULT
```

错误分类固定如下：

### `ILLEGAL_INSTRUCTION`

表示机器字结构本身不是已定义的 canonical 编码，包括：

- 未分配 class、format 或 opcode；
- guard 编码保留，或 form 的 `guard_policy` 不接受该 header guard；
- must-zero 位非零；
- 保留枚举值；
- 原子 `scope=3`；
- opcode 专用字段出现未分配组合；
- 取指未对齐、不完整或越界。

### `ILLEGAL_OPERAND`

表示已经选出唯一合法形式，但静态操作数不满足该形式约束，包括：

- SGPR/VGPR 类别错误；
- 寄存器号超出模块资源计数；
- 寄存器组越界、基址未对齐或禁止的部分重叠；
- 已定义字段值之间形成禁止组合，包括已知 `order/scope` 值不满足 LOAD/STORE/RMW 或地址空间合法矩阵；
- PC-relative 目标不对齐、越出当前内核文本或不满足控制流约束。

### 汇编/链接错误

源级超范围立即数、未知助记符、歧义形式、错误寄存器前缀和重定位溢出必须在生成机器码前报告。工具不得故意生成非法机器字，再把问题推迟到运行时。

动态地址越界、实际访存未对齐、除零、屏障协议不一致和集合参与者不一致不属于静态编码错误；它们由相应执行语义产生运行时故障。

`DIVERGENCE_FAULT` 是已成功静态译码后对当前 warp 动态状态的检查结果。它适用于所有 `required_state: scalar_ready` 的 form，包括全部 scalar form，以及 `CALL` direct、`CALL.IND`、`JUMP.IND` 和 `RET`；它不适用于 `BRA/BRA.P`，也不属于非法机器编码。

静态错误检查对整个 warp 只做一次，先于 guard 和 scalar-ready 求值。发生静态错误时不得提交 SGPR、VGPR、VP、SCC、内存、PC、同步或重汇聚状态。

## 6.12 机器可读清单要求

清单把**物理布局**和**操作数绑定**分开保存，各有唯一归属：

- 根 `format_registry` 拥有物理布局。每个编码格式在这里给出一次自己的 class、payload 位范围和完整字段表（字段名、`lsb`、`width`、`kind`、描述）。
- 每个 form 拥有操作数绑定。它声明自己属于哪个 `encoding_format`、自己的 `opcode`，以及每个操作数绑定到哪个字段。

因此单个 form 至少必须给出：

```yaml
family: v-add                  # 语义分组，语义化 slug
form: u32                      # family 内唯一
mnemonic: V_ADD.U32
syntax: V_ADD.U32 v0, v1, v2
encoding_format: V2
opcode: 0x00
execution_domain: vector
required_state: none
guard_policy: optional
operands: [...]                # 每项含 name/type/access/field
semantics: ...
constraints: [...]
faults: [...]
example: {assembly: ..., machine_word: ...}
```

form 里**不得**重复 `class`、`format` 或 `fields`：它们由 `encoding_format` 加 `format_registry` 唯一决定，工具必须现场推导。任何绑定不到操作数的 payload 字段自动成为 must-zero 洞，不需要另写 `must_zero` 列表。

只有两类信息无法从 registry 推导，因此允许逐 form 覆盖：

- `field_values`：把某个字段固定成一个常量。例如 `FENCE` 三个 form 用它把 `scope2/order2` 钉死成各自的组合。
- `field_notes`：给某个字段一个 form 专属的描述，用于同一个物理槽在不同 form 中承载不同含义的情况，例如 `V_SHUFFLE.DOWN.B32` 的立即数 delta form。

family ID 是语义化 slug（`^[a-z0-9]+(-[a-z0-9]+)*$`，如 `v-add`、`bar-sync`），不是不透明编号。

生成器必须拒绝：

- 两个 form 重复声明同一 `(encoding_format, opcode)` 或同一译码三元组；
- form 直接书写 `class`、`format` 或 `fields`；
- form 引用 `format_registry` 中不存在的 `encoding_format`，或绑定到该格式没有的字段名；
- `format_registry` 中位段重叠、越出 64 位或遗漏 payload 位；
- 同一机器字匹配多个形式；
- 未定义的 `x` 位；
- opcode 的字段类别与操作数类别不一致；
- `vsrc32`/`vsrc64` 操作数出现在非 `V1/V2/V3/VCMP` 格式或非 `vector` 执行域的 form 上；
- 含 `vsrc*` 操作数的 form 把 selector 字段当成 must-zero 洞，或不含 `vsrc*` 的 form 让 selector 变成可变字段；
- `execution_domain` 不属于本章规定的七值集合；
- `guard_policy`、`required_state` 或 form 级 guard 矩阵与本章规则不一致；
- 原子 `order/scope` 被错误拆成额外 opcode/form，或合法矩阵不一致；
- 原子 canonical 名称未按 `<op>.<type>.<space>.<order>.<scope>` 排列，或使用 `EXCH/EXCHANGE` 而不是 `XCHG`；
- MATRIX class 出现第二个 MMA form，或唯一 MMA form 的 `attr8/x5` 非零；
- 立即数宽度大于其格式容器；
- 示例不能 canonical 汇编，或 round-trip 改变机器字。

执行域、machine class、编码格式、family 和 form 必须作为不同概念保存。family 只做语义分组，form 才是唯一译码叶子；每个 form 的 `(class, format, opcode)` 三元组必须全局唯一。生成器不得从 `V2` 自动推导 `v-add` family，不得从 `MEMORY` class 推导 execution domain，也不得从 family 名反推其 payload 布局。完整指令表、汇编器、反汇编器、验证器和 RTL/CModel 解码表必须由同一份清单生成。

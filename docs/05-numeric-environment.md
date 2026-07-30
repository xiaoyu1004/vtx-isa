# VTX-1 ISA 1.0 Draft：数值环境

本章规定浮点结果必须得到什么位型。除明确带 `.APPROX` 的指令外，结果必须逐位一致，不能把“宿主 CPU 大概算得一样”当作实现。

## 1. 先记住六条直观规则

1. **S.F32 和 V.F32 算的是同一种 binary32。** 区别只在寄存器和执行次数：S 形式读写每 warp 一份的 SGPR，算一次；V 形式读写每 lane 一份的 VGPR，对每个参与 lane 独立计算。
2. **F16 是向量数值格式。** V.F16 使用 VGPR 低 16 位；普通标量浮点算术没有 S.F16 形式。MMA 的 A/B 片段可以保存 F16。
3. **默认只有一种舍入。** 浮点结果固定使用“最近值，正中间取偶数”（RNE）。没有动态舍入寄存器，也没有每 lane 不同的舍入模式。
4. **小数不会偷偷冲成零。** normal 以下仍保留 subnormal；输入也不做 DAZ，结果也不做 FTZ。
5. **NaN 结果有统一答案。** 只要一个数值运算应得到 NaN，就返回目标格式的正号 canonical qNaN；load/store/MOV 这种纯位搬运则原样保留 payload。
6. **所有 S 浮点和 S 转换都要求 scalar-ready。** 算术、比较、FMIN/FMAX、FABS/FNEG、近似函数和整数/浮点转换没有例外。失败固定报告 `DIVERGENCE_FAULT`，不读 SGPR、不写 SGPR 或 `SCC`。

VTX-1 不提供浮点异常标志、trap enable、动态舍入状态、DAZ 或 FTZ。invalid、除零、上溢、下溢和 inexact 都不产生设备故障。

本章只关心 32 位和 16 位数值位型。寄存器上没有任何隐藏影子状态，浮点算术、数值转换和 MMA 输出都只写位型，不需要额外说明标签如何传播。

## 2. S 与 V 的执行规则

### 2.1 S.F32

S.F32 指令使用 SGPR。一次动态 S.F32 指令对整个 warp 只计算一次并写一份 SGPR 结果。它不能按参与 lane 重复计算，也不能因为某些 VGPR 中碰巧有相同值而冒充 V.F32。

本章所有 S.F32 指令和所有 S 数值转换都必须在读取任何动态源之前检查执行模型定义的 scalar-ready。warp 必须至少还有一个活 lane，全部活 lane 必须在同一路径上，而且重汇聚栈中不能有未完成的 `FIRST` 或 `SECOND` 帧。

只要还有一条分歧路径没走完，任何 S 浮点或 S 转换都固定报告 `DIVERGENCE_FAULT`。不存在“比较可以跑”“只读 SGPR 可以跑”“结果碰巧相同可以跑”或“这一条不会写结果所以可以跑”的例外。

失败指令不读 SGPR，不做 NaN 分类或舍入，不写 SGPR 或 `SCC`，也不留下部分结果。

### 2.2 V.F32 和 V.F16

V 指令对

```text
E = 当前路径上的候选 lane
无 vp 条件： P = E
@vpN：        P = E & snapshot(vpN)
@!vpN：       P = E & ~snapshot(vpN)
```

中的每个 lane 独立读取该 lane 的 VGPR、独立计算、独立写回。不同 lane 之间没有隐含归约、进位或 NaN 共享。不在 `P` 中的 lane 不读取源，目标保持不变。

除 warp collective 和 MMA 外，一个 lane 的特殊值不会影响另一个 lane。

V 浮点和 V 转换不套用 scalar-ready；它们只更新 `P` 中 lane 的 VGPR 或 `vp`。

V 浮点 form 的 scalar-source selector 可以把其中一个源改成 SGPR，例如 `V_FMUL.F32 vd, va, sB`。这只改变那个源从哪个寄存器文件读取，不改变数值语义：舍入、NaN 传播和 subnormal 处理与两个源都来自 VGPR 时完全一致。执行类别仍是 V，也不会因此变成 S 指令。一条 V 浮点指令最多只能有一个 SGPR 源。

### 2.3 同一算法，不同寄存器

如果 S.F32 与 V.F32 具有相同操作名和相同输入位型，它们必须应用完全相同的 NaN、无穷、零、subnormal 和舍入规则。S/V 前缀不是“快精度”和“准精度”的区别；它只决定 SGPR/VGPR、执行次数以及是否必须通过 scalar-ready。

## 3. 位型与寄存器表示

### 3.1 F16 / IEEE 754 binary16

```text
bit 15      bit 14..10       bit 9..0
sign        exponent         fraction
  1             5               10
```

指数偏置为 15：

- `e=0, f=0`：`+0` 或 `-0`；
- `e=0, f!=0`：`(-1)^s * 2^-14 * (f / 2^10)`，即 subnormal；
- `1<=e<=30`：`(-1)^s * 2^(e-15) * (1 + f/2^10)`；
- `e=31, f=0`：`+Inf` 或 `-Inf`；
- `e=31, f!=0`：NaN。

最小正 subnormal 是 `2^-24`，最小正 normal 是 `2^-14`，最大有限值是 65504。canonical qNaN 为：

```text
F16 canonical qNaN = 0x7e00
```

V.F16 放在一个 VGPR 的低 16 位。任何产生 V.F16 寄存器结果的数值指令必须把高 16 位写成 0；消费 V.F16 的数值指令只解释低 16 位。纯 `MOV.U32` 仍复制全部 32 位，不检查它是不是合法 F16 数值。

普通 S.F16 算术和 S.F16 转换不是本数值环境的一部分；把 F16 当作 U16/U32 位型做标量搬运不等于支持 S.F16 算术。

### 3.2 F32 / IEEE 754 binary32

```text
bit 31      bit 30..23       bit 22..0
sign        exponent         fraction
  1             8               23
```

指数偏置为 127：

- `e=0, f=0`：`+0` 或 `-0`；
- `e=0, f!=0`：`(-1)^s * 2^-126 * (f / 2^23)`；
- `1<=e<=254`：`(-1)^s * 2^(e-127) * (1 + f/2^23)`；
- `e=255, f=0`：`+Inf` 或 `-Inf`；
- `e=255, f!=0`：NaN。

最小正 subnormal 是 `2^-149`，最小正 normal 是 `2^-126`，最大有限值是 `(2-2^-23)*2^127`。canonical qNaN 为：

```text
F32 canonical qNaN = 0x7fc00000
```

F32 占一个 SGPR 或 VGPR。load/store 只搬运位型，不做数值转换。

## 4. NaN、无穷和有符号零

### 4.1 NaN

NaN 的 fraction 最高位为 1 时是 quiet NaN，为 0 时是 signaling NaN。架构不暴露 signaling 异常。

以下规则对 S.F32、V.F32、V.F16 和 MMA 都适用：

- FADD、FSUB、FMUL、FDIV、FSQRT、FFMA、数值转换、近似函数和 MMA 只要语义要求 NaN，就返回目标格式的 canonical qNaN；
- 输入 NaN 的符号和 payload 不传播；
- sNaN 先按 NaN 处理，但不设置异常标志；
- 两个 NaN 也不选择其中一个 payload；
- `FABS` 只清 sign，`FNEG` 只翻转 sign；二者是纯位操作，保留 payload 和 signaling/quiet 位；
- MOV、load、store 是纯位搬运，不 canonicalize NaN；
- FMIN/FMAX 使用第 8 节的 number 选择规则。

常见无效形式：

```text
(+Inf) + (-Inf)
0 * Inf
Inf / Inf
0 / 0
sqrt(负有限数)
sqrt(-Inf)
FMA 中无穷乘积再加相反符号无穷
```

这些形式均返回目标格式 canonical qNaN。

### 4.2 无穷

除无效形式外，无穷按 IEEE 754 扩展实数规则参与计算，例如：

- 有限非零数除以 `+0/-0` 得到带正确符号的 Inf；
- 有限数加同号 Inf 得同号 Inf；
- `sqrt(+Inf)=+Inf`；
- 有限结果上溢时按 RNE 得到最大有限数或 Inf，取决于精确值落在哪一侧。

### 4.3 有符号零

`+0` 和 `-0` 数值比较相等，但位型不同：

- 相反符号的精确抵消在 RNE 下得到 `+0`，`-0 + -0` 得 `-0`；
- 乘法和除法结果为零时，符号为操作数符号异或；
- `sqrt(-0)=-0`；
- `FABS(-0)=+0`，`FNEG(+0)=-0`；
- FFMA 按精确表达式 `a*b+c` 一次舍入后的 IEEE 零符号规则；
- FMIN(-0,+0) 返回 `-0`，FMAX(-0,+0) 返回 `+0`。

## 5. 舍入和 subnormal

记 `RN16(x)`、`RN32(x)` 为把精确实数 `x` 舍入到 F16、F32。固定舍入模式为 round to nearest, ties to even（RNE）：

1. 选择离精确值最近的可表示值；
2. 如果精确值正好在两个值中点，选择最低有效保留位为 0 的那个；
3. 下溢继续使用同一规则并保留 subnormal；
4. 上溢也使用同一规则，不是简单地“只要超出最大有限数就立刻变 Inf”。

F16 正向上溢的 RNE 分界为 65520：小于分界的相应值可舍入到 65504，达到分界时舍入到 `+Inf`。负值对称处理。F32 的对应正分界为 `2^128 - 2^103`。

所有输入 subnormal 按完整数学值参与计算，所有 subnormal 结果按 RNE 保留。实现内部可以使用更宽精度，但必须在每个架构规定的舍入点写出相同位型。

除浮点转整数明确使用 RTZ 外，指令编码不能改变舍入模式。

## 6. 精确 F32 运算

对有限输入，先在数学上的无限精度中求表达式，再在规定位置舍入：

```text
FADD.F32(a,b)   = RN32(a+b)
FSUB.F32(a,b)   = RN32(a-b)
FMUL.F32(a,b)   = RN32(a*b)
FDIV.F32(a,b)   = RN32(a/b)
FSQRT.F32(a)    = RN32(sqrt(a))
FFMA.F32(a,b,c) = RN32(a*b+c)
```

FADD、FSUB、FMUL、FDIV、FSQRT 各只在最终结果处舍入一次。FFMA 的乘积不单独舍入。

S 和 V 形式都按这些公式执行。V 形式对每个参与 lane 单独应用公式；S 形式先通过 scalar-ready，再对 SGPR 输入应用一次。

实现不得把：

```text
FMUL t,a,b
FADD d,t,c
```

自动改成 FFMA，因为前者有两个舍入点。也不得把 FFMA 拆成乘法和加法。允许改变数值结果的快速数学优化不属于 ISA 语义。

## 7. 比较

S/V `FSETP.{EQ,NE,LT,LE,GT,GE,ORD,UNO}.F32` 使用相同数值规则：

- 任一输入是 NaN：NE 和 UNO 为真；其他比较为假；
- 两个输入都不是 NaN：ORD 为真，UNO 为假，其余按数值顺序；
- `+0 == -0`，二者之间 LT 和 GT 都为假；
- Inf 按扩展实数顺序比较。

比较不修改输入，也不产生浮点异常状态。S 比较必须先通过 scalar-ready，然后把 warp 共享结果写入 `SCC`；V 比较把每个参与 lane 的结果写入 `vp`。S 比较不能在未完成的分歧中执行。

## 8. FMIN 和 FMAX

S/V FMIN/FMAX 采用 `minimumNumber` / `maximumNumber` 风格。S 形式必须先通过 scalar-ready，V 形式逐参与 lane 执行：

1. 只有一个输入是 NaN：返回另一个输入的原始位型；
2. 两个输入都是 NaN：返回 F32 canonical qNaN；
3. 数值不等：返回较小值或较大值的原始位型；
4. 输入为 `-0,+0`：FMIN 返回 `-0`，FMAX 返回 `+0`；
5. 数值相等且位型相同：返回该位型。

因此，单个 NaN 不会盖住一个普通数；但双 NaN 仍得到统一 canonical qNaN。

## 9. 转换

### 9.1 整数与 F32

S/V 整数转 F32：

```text
CVT.F32.S32 = RN32(int32(src))
CVT.F32.U32 = RN32(uint32(src))
```

源先变成精确数学整数，再做 RNE。绝对值不超过 `2^24` 的可表示整数必须精确。

F32 转整数先使用 round toward zero（RTZ）截断，再饱和：

| 源 | `CVT.S32.F32` | `CVT.U32.F32` |
|---|---:|---:|
| NaN | 0 | 0 |
| `+Inf` 或大于上界 | `0x7fffffff` | `0xffffffff` |
| `-Inf` 或小于下界 | `0x80000000` | 0 |
| 范围内有限值 | `trunc(x)` 的二补数 | `trunc(x)` |

`-0` 转为整数 0。转换不产生浮点或整数故障。

这里所有 S 转换都读写 SGPR，并且必须先通过 scalar-ready；失败是 `DIVERGENCE_FAULT`，不是数值饱和，也不会写目标 SGPR。所有 V 转换逐参与 lane 读写 VGPR，不套用 scalar-ready。

### 9.2 V.F16 与 V.F32

F16/F32 数值转换只有 V 形式：

`V_CVT.F32.F16`：

- normal、subnormal、零和 Inf 精确扩展；
- 符号保留；
- 任意 F16 NaN 变为 `0x7fc00000`。

`V_CVT.F16.F32`：

- 有限值结果为 `RN16(x)`；
- `+0/-0`、`+Inf/-Inf` 保留符号；
- 任意 F32 NaN 变为低 16 位 `0x7e00`；
- 目标 VGPR 高 16 位写 0。

V load F16 把两个内存字节零扩展到 VGPR，V store F16 只写源 VGPR 低 16 位；二者不做转换或 NaN canonicalization。

## 10. 近似函数

`.APPROX` 只表示允许一个明确受限的误差，不表示任意答案。近似函数返回 S.F32 或逐 lane V.F32；两种形式使用同一误差合同。S 形式仍必须先通过 scalar-ready，不能因为结果本来就是近似值而放宽执行状态。

对非 NaN F32 位型 `u` 定义保持数值顺序的整数键：

```text
ordered(u) = (~u) & 0xffffffff,  sign(u)=1
             u | 0x80000000,     sign(u)=0

ulp_distance(a,b) =
    abs(ordered(bits(a)) - ordered(bits(b)))
```

参考值 `ref` 是无限精度实函数结果经 `RN32` 后的位型。若 `ref` 有限且非零，实现结果必须同号且与 `ref` 相差不超过 2 ULP。若 `ref` 是零或 Inf，结果必须逐位等于 `ref`。NaN 必须为 F32 canonical qNaN。同一实现对相同输入必须确定。

### 10.1 FRCP.APPROX.F32

参考函数为 `1/x`：

- `+0/-0 -> +Inf/-Inf`；
- `+Inf/-Inf -> +0/-0`；
- NaN -> canonical qNaN；
- 其余输入相对 `RN32(1/x)` 不超过 2 ULP。

### 10.2 FRSQRT.APPROX.F32

参考函数为 `1/sqrt(x)`：

- `+0 -> +Inf`，`-0 -> -Inf`；
- `+Inf -> +0`；
- 负有限数和 `-Inf` -> canonical qNaN；
- NaN -> canonical qNaN；
- 正有限数相对 `RN32(1/sqrt(x))` 不超过 2 ULP，结果不得为负。

### 10.3 FEXP2.APPROX.F32

参考函数为 `2^x`：

- `-Inf -> +0`，`+Inf -> +Inf`；
- NaN -> canonical qNaN；
- 有限输入相对 `RN32(2^x)` 不超过 2 ULP；
- 结果非负；
- 对任意非 NaN F32 输入 `a<b`，结果必须满足 `FEXP2(a)<=FEXP2(b)`。

## 11. FFMA 的单次舍入

S/V `FFMA.F32 a,b,c` 都计算精确表达式 `a*b+c`，只在最终写回执行一次 `RN32`。S 形式必须先通过 scalar-ready；V 形式逐参与 lane 计算：

- 乘积不先舍入；
- subnormal 中间积不提前清零；
- 任一输入 NaN返回 canonical qNaN；
- `0*Inf` 或 `Inf*0` 返回 canonical qNaN；
- 无穷乘积加相反符号无穷返回 canonical qNaN；
- 其他有限、无穷和零按 IEEE 融合操作处理。

所以 FFMA 与 `FMUL; FADD` 不一定得到相同末位。

## 12. `MMA.M16N8K16.F16.F16.F32`

MMA 只定义这一种 `M16N8K16`、F16×F16 加 F32 的 form。它属于 MATRIX 执行域，是整 warp 协作指令，不是普通 V.FP，也不是 S 指令。头部 guard 固定为 `PT`，不使用 `vp` 删减参与者，也不检查 scalar-ready。

### 12.1 参与和寄存器组

全部 32 个 lane 都必须仍存活且 active，也就是执行集合等于 live 集合并且恰有 32 个 lane。缺 lane、分歧中只到一部分 lane、不同动态 PC 或会合失败，都报告 `COLLECTIVE_FAULT`，所有 D 保持原值。

令四个编码基址分别为 `vd`、`va`、`vb`、`vc`。每个 lane 使用：

| 矩阵 | 每 lane VGPR | 基址对齐 |
|---|---:|---:|
| A | `va..va+3`，4 个 | 4 |
| B | `vb..vb+1`，2 个 | 2 |
| C | `vc..vc+3`，4 个 | 4 |
| D | `vd..vd+3`，4 个 | 4 |

完整组必须落在可用 VGPR 范围内。A、B、C 三个源组必须两两不重叠；D 必须与 A、B 不重叠。唯一允许的别名是 D 与 C **完整相同**，即 `vd==vc`；D/C 部分重叠或任何其他组间重叠都为 `ILLEGAL_OPERAND`。

通过检查后，先冻结全部 32 个 lane 的 A、B、C，再开始任何 D 写回。因此 `D=C` 是安全的原地累加。

### 12.2 A/B/C/D 元素映射

令 lane 编号 `l` 在 `0..31`。F16 半字编号 `h=0` 表示 VGPR 位 `[15:0]`，`h=1` 表示位 `[31:16]`；两个半字都按小端 F16 位型解释。

A 片段中，`ra=0..3`：

```text
qA = 8*l + 2*ra + h
m  = qA div 16
k  = qA mod 16
A[m,k] = F16_half(VGPR[va+ra, lane=l], h)
```

B 片段中，`rb=0..1`：

```text
qB = 4*l + 2*rb + h
k  = qB div 8
n  = qB mod 8
B[k,n] = F16_half(VGPR[vb+rb, lane=l], h)
```

C 和 D 每个元素占一个完整 VGPR。对 `r=0..3`：

```text
q  = 4*l + r
m  = q div 8
n  = q mod 8

C[m,n] = F32_bits(VGPR[vc+r, lane=l])
VGPR[vd+r, lane=l] = bits(D[m,n])
```

这些公式把 A 的 256 个 F16、B 的 128 个 F16、C/D 的 128 个 F32 各映射一次且不重复。实现不能换一种 lane 布局。

### 12.3 固定数值步骤

每个 A/B F16 先按 `V_CVT.F32.F16` 扩展。有限 F16 到 F32 是精确的；任意 F16 NaN 变成 F32 canonical qNaN。

每个输出 `(m,n)` 独立令 `acc=C[m,n]`，然后严格按 `k=0,1,...,15` 递增执行：

```text
acc = RN32(F32(A[m,k]) * F32(B[k,n]) + acc)
```

每一步就是一次完整的 F32 `FFMA`：乘积不先舍入，只在该步末执行一次 RNE；下一步读取已经舍入的 F32 `acc`。禁止重排 k、树形归约、跨 k 保留额外精度，或用 FP64 累加后只舍入一次。

特殊值逐步处理：

- 任一乘数或 `acc` 为 NaN时，本步得到 F32 canonical qNaN；sNaN 被安静化且 payload 不传播；
- `0*Inf`、`Inf*0`，或无穷乘积再加相反符号无穷，得到 canonical qNaN；
- 其他 Inf 按 F32 FFMA 规则传播；
- subnormal 输入和中间结果不做 DAZ/FTZ；
- 有符号零按 F32 FFMA 的一次舍入规则决定；
- 一旦某一步得到 canonical qNaN，后续步骤仍为 canonical qNaN。

全部 128 个 D 元素算完并通过检查后，所有 D VGPR 一次性提交。MMA 不产生内存事件，也不隐含内存栅栏。

## 13. 一致性测试要求

符合实现至少必须逐位测试：

- S.F32 与 V.F32 对相同输入得到相同数值位型；
- 每一种 S 浮点、S 比较和 S 转换在非 scalar-ready 状态都报告 `DIVERGENCE_FAULT`，不读动态源、不写 SGPR 或 `SCC`；
- 未完成的 `FIRST` 或 `SECOND` 分歧中不存在任何可执行的 S 浮点或 S 转换例外；
- F16/F32 的正负零、最小/最大 subnormal、最小 normal、最大有限值、正负 Inf、qNaN 和 sNaN；
- RNE 中点取偶，以及 subnormal/normal、最大有限值/Inf 两个边界；
- 无 DAZ、无 FTZ；
- NaN canonicalization 与 MOV/load/store 的 payload 原样搬运；
- FADD/FSUB 完全抵消的零符号，FMUL 的 `0*Inf`；
- FFMA 与拆分乘加不同的测试向量；
- FMIN/FMAX 的单 NaN、双 NaN和 `-0/+0`；
- 整数转换的 NaN、Inf、边界和饱和；
- V.F16 写回高 16 位清零；
- `.APPROX` 特殊值、2 ULP 上界和 FEXP2 单调性；
- MMA 的 32-lane 完整参与、A/B/C/D 映射公式、组对齐、`D=C` 唯一别名、源冻结和整体提交；
- MMA 的 `k=0..15` 递增 FFMA、每步 RNE，以及 NaN、Inf、subnormal 和有符号零。

如果宿主平台会扩展精度、自动合约 FMA、冲掉 subnormal 或传播不同 NaN payload，模拟器必须显式屏蔽这些行为。

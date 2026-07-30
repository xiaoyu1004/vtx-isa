# VTX-1 ISA 1.0 Draft：内存模型

本章规定程序能看到什么值、哪些先后关系必须成立。缓存、访存合并、shared bank 和互连实现都不是架构语义。实现可以在内部重排请求，但最后得到的执行必须满足本章。

文中的“必须”“禁止”“可以”分别表示 MUST、MUST NOT、MAY。

## 1. 先记住七条直观规则

1. **SGPR 是每 warp 一份，VGPR 是每 lane 一份。** scalar 访存读写 SGPR，整条 warp 只做一次；vector 访存读写 VGPR，每个参与 lane 各做一次。
2. **所有 scalar 访存和 scalar 原子都要求头部 guard 固定为 `PT`，并且 warp scalar-ready。** 静态 guard 不是 `PT` 时按非法编码处理；不满足 scalar-ready 时固定报告 `DIVERGENCE_FAULT`。两种失败都不读动态源、不形成地址、不产生事件。
3. **vector 访存按参与 lane 展开。** `vp` 条件筛出几个参与 lane，就有几个彼此独立的事件。即使硬件把它们合成一个总线请求，内存模型里仍是多个事件。
4. **地址可以“统一基址 + 各 lane 索引”。** global、param、const 的向量访问可以用一个 SGPR64 基址，再加每 lane 的 VGPR32 无符号索引。SV-mix 固定先做 `zero_extend`，不能把最高位当符号位。
5. **地址空间由 opcode 决定，不由地址值决定。** 每条访存 form 的助记符里写明 `GLOBAL`、`SHARED`、`LOCAL`、`PARAM` 或 `CONST`；地址寄存器只保存位模式，不携带空间身份，也没有 generic 空间和运行期空间推断。
6. **空间有明确限制。** local 只能向量访问；param 和 const 只读；param、const、global 可以标量读，global 可以标量写；shared 同时支持标量和向量访问。
7. **`BAR.SYNC.CTA` 只管整个 CTA 的 shared。** 它不是任意线程子集屏障，也不会替代 global 的原子通信或主机所有权转移。

含数据竞争、原子与普通访问非法混用、或违反主机所有权的程序是未定义程序。未定义不是一种可捕获的设备故障。地址越界、未对齐和错误空间仍按各自精确故障处理；scalar-ready 失败固定为 `DIVERGENCE_FAULT`。

## 2. 两类访存指令

### 2.1 标量访存

scalar 访存使用 SGPR 操作数。成功的 scalar load 对整个 warp 产生**恰好一个 `R` 事件**，成功的 scalar store 产生**恰好一个 `W` 事件**。该事件的执行代理是 warp，记作 `agent=warp`，没有 lane 编号。成功必有一个，失败才是零个。

每条 scalar load 和 scalar store 都必须在读取动态源之前检查执行模型定义的 `scalar-ready(warp)`。大白话说，warp 必须至少还有一个活 lane，全部活 lane 必须在同一路径上，而且重汇聚栈中不能有未完成的 `FIRST` 或 `SECOND` 帧。只要有一条分歧路径还没走完，就不是 scalar-ready；不存在“这一条 scalar 指令碰巧安全，所以可以执行”的例外。

scalar-ready 检查失败固定报告 `DIVERGENCE_FAULT`，不读取 SGPR 地址或数据源，不形成地址，不产生 `R/W/A_*`、`ppo` 或其他内存关系，也不写任何 SGPR。

scalar load 把一次读取结果写入 SGPR；scalar store 从 SGPR 取得一次写入值。无论 warp 有多少个参与 lane，都不能把一次 scalar store 解释为多次相同写入，也不能把一次 scalar 原子解释为多次 RMW。

允许的空间如下：

| 空间 | 标量 load | 标量 store/atomic |
|---|---|---|
| `param` | 允许 | 禁止 |
| `const` | 允许 | 禁止 |
| `global` | 允许 | 允许 |
| `shared` | 允许 | 允许 |
| `local` | 禁止 | 禁止 |

### 2.2 向量访存

vector 访存使用 VGPR 数据操作数。令

```text
E = 当前路径上的候选 lane
无 vp 条件： P = E
@vpN：        P = E & snapshot(vpN)
@!vpN：       P = E & ~snapshot(vpN)
```

对 `P` 中每个 lane 恰好产生一个事件，事件携带自己的 `lane`。不在 `P` 中的 lane 不形成地址、不访问内存、不写 VGPR，也不因坏地址而故障。

同一条向量指令的不同 lane 即使得到相同地址，也仍是不同事件：

- 多个 lane 读同址是多个读；
- 多个 lane 普通写同址且没有 `hb` 排序，是数据竞争；
- 多个 lane 对同址做同宽原子，会分别占据该位置 `mo` 中的一个位置。

实现可以合并物理请求，但不得合并事件身份、原子次数、故障检查或 `rf/co/mo` 关系。

向量访问可用于 global、shared、local、param 和 const；param、const 仍然只读。local 只能走本类访问，每个 lane 只能访问自己的 local allocation。

### 2.3 一条指令的精确提交

scalar 访存先检查静态操作数和固定 `PT`，再检查 scalar-ready，随后才读取 SGPR 并检查唯一地址；全部成功后恰好提交一个事件。vector 访存先确定 `P`，再检查全部参与地址、范围和对齐；全部成功后对 `P` 中每个 lane 恰好提交一个事件。任一参与地址失败时，整条动态指令不产生任何内存事件，也不部分写回 SGPR/VGPR。非参与 lane 不进入检查。

标量和向量只决定“产生几个架构事件”，不规定硬件发出几个缓存请求。

## 3. 地址空间和地址形成

### 3.1 空间和窗口

全部空间按字节寻址，数据采用小端序。

| 空间 | 可见范围 | 地址宽度和来源 |
|---|---|---|
| `global` | 设备内 CTA；主机须遵守所有权 | SGPR64 或 VGPR64 基址 |
| `shared` | 同一 CTA | CTA 内 U32 字节偏移 |
| `local` | 单 lane | 该 lane 私有窗口内的 U32 字节偏移 |
| `param` | 本次 kernel 启动，只读 | SGPR64 或 VGPR64 基址 |
| `const` | 设备只读 | SGPR64 或 VGPR64 基址 |

一次访问落在哪个空间只由 form 决定：每条访存 form 的操作数类型固定写明空间，助记符里也带同一个空间后缀。架构没有 generic 空间，寄存器里的地址值不带空间身份，实现禁止在运行期根据数值猜测空间。

global、param、const allocation 都有空间身份、基址、长度和生命周期。一次访问必须完整落在同一个活跃 allocation 中，不能跨到相邻 allocation。这项检查在实现内部按 allocation 表完成，与寄存器内容无关。shared 每 CTA 一份，local 每真实 lane 一份；不同 lane 的 local 永不别名。

### 3.2 三种地址合同

地址 form 只能明确选择 `uniform`、`lane`、`SV-mix` 三种合同之一。地址先用无界数学整数计算，再检查窗口、allocation 范围和对齐；任何中间步骤都不能按 32 位或 64 位回绕。

#### uniform

整个 warp 使用同一个 SGPR 地址。global、param、const 使用 SGPR64 base；shared 使用 CTA 内 SGPR32 offset。基本模板是：

```text
EA = unsigned(SGPR_base) + sign_extend(immediate)
```

`SMEMX` 仍属于 uniform，只是多一个 SGPR32 index：

```text
SMEMX:
EA = unsigned(SGPR_base)
     + zero_extend(SGPR32_index)
     + sign_extend(immediate)
```

base 和 index 都只是字节数值。uniform 只说明地址统一，不决定事件数：scalar memory 成功后整个 warp 恰好一个事件；若某个 vector form 使用 uniform 地址，仍对 `P` 中每个 lane 各产生一个事件。

#### lane

每个参与 lane 从自己的 VGPR 形成地址：

```text
EA[lane] = unsigned(VGPR_base[lane])
           + sign_extend(immediate)
```

global、param、const 的 lane base 为 VGPR64；shared 和 local 使用各 lane 的 VGPR32 窗口 offset。local **只能**使用 lane 合同，不能使用 uniform、`SMEMX` 或 `SV-mix`。同一个数值 offset 在不同 lane 的 local 中仍指向不同私有 allocation，因为 local form 的窗口按 lane 选取。

#### SV-mix

SGPR 给出统一 base，VGPR 给出逐 lane 无符号字节 index：

```text
EA[lane] = unsigned(SGPR_base)
           + zero_extend(VGPR32_index[lane]) * scale
           + sign_extend(immediate)
```

`scale` 只能取具体 form 明写的值。所有 SV-mix form 都必须对 VGPR32 index 做 `zero_extend`；使用 `sign_extend` 或二补数负 offset 是错误实现。SV-mix 可用于 global、param、const 和 shared，但不能用于 local。

`VATOMX` 是 global vector atomic 的 SV-mix 固定模板：

```text
VATOMX:
EA[lane] = unsigned(SGPR64_base)
           + zero_extend(VGPR32_index[lane])
```

`VATOMX` 的 `scale` 固定为 1，且没有额外缩放变体。它先按 `vp` 得到 `P`，再为 `P` 中每个 lane 形成地址；成功后每 lane 恰好一个原子事件。多个 lane 算出同一地址时仍是多个事件，并分别进入该位置的 `mo`。

### 3.3 越界与对齐

令 `end=start+size`。出现以下任一情况均为 `MEMORY_BOUNDS`：

- `start < 0`；
- global、param、const 的 `start` 或 `end` 超出 U64 地址范围；
- shared/local 的结果超出对应窗口；
- 访问没有完整落在同一个活跃 allocation。

自然对齐要求如下：

| 类型 | 对齐 |
|---|---:|
| U8 | 1 |
| U16、F16 | 2 |
| U32、F32 | 4 |
| U64 | 8 |
| V2.U32、V4.U32 | 8、16 |
| U32 原子 | 4 |
| U64 原子 | 8 |

未对齐报 `MISALIGNED_ACCESS`。同一参与地址同时未对齐和越界时，按精确故障优先级报告。

### 3.4 64 位地址在两个寄存器文件之间搬运

地址就是 64 位数值，没有影子状态，所以在寄存器之间搬运它只是搬 64 个位。两条真实机器 form 覆盖两个方向，它们都不是伪指令，也不能拆成两条 `.B32` 来替代：

```text
V_MOV.B64 vE:v(E+1), sA:s(A+1)      # ssrc=1，SGPR64 -> 每 lane VGPR64
S_READFIRST.B64 sE:s(E+1), vA:v(A+1)
```

`V_MOV.B64` 是 `V1` 格式的混合源 form。`ssrc=0` 时它是普通的 VGPR64 到 VGPR64 复制；`ssrc=1` 时源在 SGPR 文件中解释，于入口冻结一次那个偶数对齐的 SGPR 对，再对 `P` 中每个 lane 整体写入对应 VGPR 对。这就是把一个统一的 64 位地址送进各 lane 的规范做法。它是 vector form，不要求 scalar-ready；非参与 lane 保持原值。

`S_READFIRST.B64` 的头部 guard 固定为 `PT`，并且先检查 scalar-ready。通过后选择编号最小的 live lane，冻结该 lane 的 VGPR 偶数连续寄存器对，再整体写入 SGPR 偶数连续寄存器对。它不检查其他 lane 是否同值。

两条指令都只复制 64 个数值位。目标地址属于哪个空间由后续访存指令的 opcode 决定，与这次搬运无关；把一个 shared offset 搬进 SGPR64 再交给 `S_LD.GLOBAL` 不是“指针伪造”，而是一个越界或越窗口的地址，按 3.3 节的精确故障处理。

超出窗口、落在已释放 allocation，或没有完整落在同一个活跃 allocation 的地址报 `MEMORY_BOUNDS`；寄存器类别、编号或对齐不符合 form 要求时报 `ILLEGAL_OPERAND`。

## 4. 事件和普通访问粒度

一个候选执行由事件集合 `E` 和本章后续关系组成。每个事件至少记录：

```text
id, kind, agent, warp, lane-or-none, cta, instruction-instance,
space, allocation, byte-range, value, scope, order
```

事件种类为：

- `I`：allocation 或所有权纪元开始时的概念初始写；
- `R`、`W`：普通读写；
- `A_R`、`A_W`、`A_RMW`：原子读、写、读改写；
- `F(scope)`：`FENCE`；
- `B(slot,phase)`：`BAR.SYNC.CTA` 的到达和恢复；
- `H`：allocation、启动、完成和所有权转移。

原子事件的 `order` 和 `scope` 来自该动态指令机器字中的 modifier 位，不由地址、寄存器值或实现猜测。译码出的 modifier 会原样成为事件属性，并参与后面的 `ppo/sw/hb`。

自然对齐 U8、U16/F16、U32/F32 普通访问是一个单拷贝元素。普通 U64 由低地址和高地址两个 U32 单拷贝元素组成；V2/V4.U32 由 2/4 个 U32 元素组成。U64 和多元素向量的不同 U32 元素可以分别取值，不保证整条数据的单拷贝原子性。

普通读的 `rf` 按字节定义，但同一个单拷贝元素不能从两个同宽并发写中各取一部分。已经由 `hb` 排序的窄写可以按小端顺序拼出较宽读。并发混宽冲突通常构成数据竞争，程序不能依赖某种撕裂结果。

## 5. 先用大白话理解顺序

- 同一个地址上的写有先后队列：普通字节用 `co`，原子位置用 `mo`。
- 每个读必须说清楚“读的是哪次写”，这就是 `rf`。
- 一个读选了旧写，那么它自然位于后续写之前，这就是 `fr`。
- 单个 warp/lane 里不是所有程序顺序都强制保留；真正必须保留的部分叫 `ppo`。
- release/acquire、CTA 屏障和运行时所有权可以在不同代理之间搭桥，这些桥叫 `sw`。
- `ppo` 和 `sw` 反复串起来得到 `hb`。如果 `A hb B`，意思是 A 必须先于 B 对程序生效。

下面给出完整关系，不能只凭上面的比喻实现。

## 6. 形式关系

以下关系均为严格关系。`r+` 表示传递闭包，`r^-1` 表示逆关系。

### 6.1 `po` 和 `ppo`

`po` 按动态指令顺序连接同一执行代理的事件：

- 同一 lane 的向量事件按该 lane 经历的动态指令顺序排列；
- warp 标量事件按 warp 的动态指令顺序排列；
- 一个标量事件与同 warp 中更早或更晚的向量事件按对应动态指令顺序排列；
- 同一条向量指令的不同 lane 事件之间没有 `po`。

`ppo` 是必须保留的 `po` 子集，由以下边的并集组成：

1. 同一代理且字节区间重叠的 `po-loc`；
2. SGPR 或 VGPR 的真数据依赖、地址依赖和控制依赖；
3. 混合源 `V_MOV.B32/B64` 和 `S_READFIRST.B32/B64` 建立的寄存器值依赖；
4. 任意事件到其后 `FENCE`，以及 `FENCE` 到其后任意事件；
5. 任意事件到其后 RELEASE/ACQ_REL 原子，以及 ACQUIRE/ACQ_REL 原子到其后任意事件；
6. `BAR.SYNC.CTA` 到达前的 shared 事件到该 owner 的 release 到达事件，以及屏障恢复的 acquire 到之后的 shared 事件；
7. 同一 RMW 的读部到写部；
8. 运行时启动、完成和所有权转移要求的边。

锁步执行本身不自动让两个 lane 的普通访问互相排序。只有上面列出的边会进入 `ppo`。

### 6.2 `rf`：读自哪次写

每个普通读字节必须恰好从同空间、同 allocation、同所有权纪元、同地址的一个 `I` 或 `W` 取值，记作 `rf_b`。值必须相等，并满足第 4 节的单拷贝约束。

每个原子读或 RMW 的读部必须从相同自然对齐起点、相同宽度的原子初值或 `mo` 修改取得完整 U32/U64，记作 `rf_a`，禁止撕裂。

```text
rf = 所有 rf_b 的并集，再并上 rf_a
```

跨 lane、跨 warp 或跨主机/设备代理的 `rf` 边记为 `rfe`。普通读不得从其 `hb` 未来的写取值，也不得越过同代理 `po-loc` 中更晚的覆盖写去读旧值。

### 6.3 `co`：普通字节写序

对每个普通字节位置 `b`，`co_b` 是该所有权纪元中 `I_b` 和全部普通写该字节事件之间的严格全序，且 `I_b` 最小。

```text
co = 所有 co_b 的并集
```

一个多字节普通写分别出现在每个所写字节的 `co_b` 中。属于同一个单拷贝元素的覆盖字节必须保持一致的写顺序；普通 U64 或 V2/V4.U32 的不同 U32 元素不要求耦合。

### 6.4 `mo`：原子修改序

原子位置键为：

```text
x = (allocation-id, start, width)
```

其中 `width` 只能为 4 或 8，且 `start` 自然对齐。对每个 `x`，`mo_x` 是初始原子值、全部 `A_W(x)` 和 `A_RMW(x)` 的唯一严格全序，初始值最小。纯 `A_R` 不占 `mo` 位置。

每个 RMW 从它在 `mo_x` 中的紧邻前驱读取，然后不可分割地插入一个新值。失败 CAS 也把旧值原样写回，并占一个 `mo_x` 位置。

```text
mo = 所有 mo_x 的并集
```

一个位置的 `mo` 不排序另一个位置。`co` 和 `mo` 都必须扩展同址的 `ppo` 与 `hb` 写写顺序。

### 6.5 `fr`：读到后续写

若普通读 `r` 从 `w` 读取，且 `w co_b w'`，则有 `r fr_b w'`。若原子读或 RMW 的读部从 `a` 读取，且 `a mo_x a'`，则有 `r fr_a a'`。

```text
fr = 所有 fr_b 的并集，再并上 fr_a
```

### 6.6 scope、`sw` 和 `hb`

`scope` 只允许三个规范名称 `CTA`、`DEVICE`、`SYSTEM`，覆盖范围逐级增大：

- `CTA`：同一 CTA 的 lane 和 warp；
- `DEVICE`：同一设备上的全部 CTA 和设备代理；
- `SYSTEM`：设备与运行时声明的一致主机域。

其他拼写不是第四种 scope，也不能代替这三个规范名称。

shared 的可见范围固定为 CTA，因此 shared 原子事件只允许 `CTA` scope。`DEVICE` 或 `SYSTEM` 不能把 shared 扩大到 CTA 外；这种 modifier 与 shared 空间的组合非法。

两个带 scope 的事件只有在双方 scope 都覆盖对方代理时才相容。CTA 不能同步另一个 CTA，DEVICE 不能同步主机。

`sw` 由以下情况组成：

1. RELEASE/ACQ_REL 修改原子 `X` 与 ACQUIRE/ACQ_REL 原子读 `Y` scope 相容，且 `Y` 读自 `X` 或以 `X` 开始的 release sequence，则 `X sw Y`。
2. release fence `Fr` 在 `po` 中先于原子修改 `X`，acquire 原子 `Y` 读取 `X` 的 release sequence，且相关 scope 相容，则 `Fr sw Y`。
3. release 原子 `X` 的 release sequence 被原子 `Y` 读取，且 `Y po Fa`、`Fa` 是 acquire fence，相关 scope 相容，则 `X sw Fa`。
4. 同时满足 `Fr po X`、`Y po Fa`，且 `Y` 读取 `X` 的 release sequence 时，在全部相关 scope 相容后有 `Fr sw Fa`。
5. 一次成功的 `BAR.SYNC.CTA`，把每个到达 owner 的 shared release 侧连接到每个恢复 waiter 的 shared acquire 侧。阻塞记录冻结 `owner_snapshot: set<linear_tid>`；恢复时 acquire 正好应用于该快照。`EXIT` 只缩小 `live_owner_set`，不贡献 release 侧，因此不建立这类 `sw` 边。
6. 主机 release 所有权并启动 kernel，启动事件同步到设备入口；设备全部完成后，完成事件同步到重新取得所有权的主机访问。

release sequence 是 `mo_x` 中从一个 release 修改开始、随后只包含连续 RMW 的最大序列；遇到普通原子写就结束。

```text
hb = (ppo 并上 sw) 的传递闭包
```

`hb` 必须无环。

### 6.7 闭合公理

一个候选执行只有同时满足以下条件才允许：

1. **良构。** 每个普通读字节恰有一个合法 `rf_b`；每个原子读部恰有一个同址同宽 `rf_a`；每个普通字节有唯一 `co_b`；每个原子位置有唯一 `mo_x`。
2. **原子性。** 每个 RMW 从 `mo` 紧邻前驱读取并作为一个不可分割修改提交。
3. **HB 一致。** `hb` 无环；读不得从 `hb` 未来取值；`co/mo` 不得逆转同址 `hb` 写写顺序。
4. **每位置一致。** `po-loc`、`rf`、`fr`、`co`、`mo` 的并集必须无环。
5. **全局可见性。** `ppo`、`sw`、`rfe`、`fr`、`co`、`mo` 的并集必须无环。
6. **无凭空值。** 真依赖与 `rfe` 的并集必须无环；任何读值都必须来自实际写或 `I`。
7. **精确提交。** 故障、被 `vp` 条件排除或回滚的指令效果不在 `E` 中；成功 vector 指令的全部 lane 效果在架构指令边界共同提交。

这些公理共同闭合了 `rf/co/mo/fr/ppo/sw/hb`，不是让实现任选其中一部分。

## 7. U32/U64 原子

U32 和 U64 原子只用于自然对齐的 global 或 shared 位置。scalar 原子的头部 guard 固定为 `PT`，并且必须先通过 scalar-ready；成功后每条动态指令恰好一个原子事件。失败时不读 SGPR、不形成地址、不读取旧值，也不占 `mo` 位置。vector 原子按 `P` 中每个 lane 恰好一个事件，不套用 scalar-ready。

两种宽度均支持原子 LOAD、STORE，以及 ADD、MIN、MAX、AND、OR、XOR、XCHG、CAS。MIN/MAX 按无符号 U32/U64 比较；ADD 按 `2^32` 或 `2^64` 取模；CAS 比较完整位型并返回旧值。

它们进入内存关系的方式固定如下：

| 指令类别 | 事件 | `rf_a` | `mo` | `fr_a` |
|---|---|---|---|---|
| atomic LOAD | 一个 `A_R` | 从同址同宽初始值或某个 `mo` 修改读取完整值 | 不新增位置 | 指向其读取修改之后的全部 `mo` 修改 |
| atomic STORE | 一个 `A_W` | 无读部 | 追加一个新值 | 无读部，因此自身不产生 `fr_a` |
| atomic RMW | 一个 `A_RMW` | 读取自己在 `mo` 中的紧邻前驱 | 不可分割地追加一个新值 | 由读部指向后续修改 |
| atomic CAS | 一个 `A_RMW` | 读取自己在 `mo` 中的紧邻前驱并返回旧值 | 成功写 replacement；失败把旧值原样追加，二者都占一个位置 | 由读部指向后续修改 |

一个 `A_RMW` 虽有读部和写部，架构事件数仍是一个。atomic LOAD/STORE 是独立 form，不能用“ADD 0”或“XCHG 后丢结果”替代，否则 `rf_a`、`mo`、order 合法集合都会变掉。

### 7.1 动态 `order/scope` modifier

每条原子机器字动态携带 2 位 `order` 和 2 位 `scope`：

| modifier 位型 | `order` |
|---:|---|
| 0 | `RELAXED` |
| 1 | `ACQUIRE` |
| 2 | `RELEASE` |
| 3 | `ACQ_REL` |

| modifier 位型 | `scope` |
|---:|---|
| 0 | `CTA` |
| 1 | `DEVICE` |
| 2 | `SYSTEM` |
| 3 | 保留；译码分类为 `ILLEGAL_INSTRUCTION` |

`order` 决定本地先后，`scope` 决定能和哪些代理建立 `sw`。它们是每次动态原子事件自己的属性，不是 kernel 全局模式。`scope=3` 是保留编码，固定分类为 `ILLEGAL_INSTRUCTION`；它不是第四种 scope，也不能按 `SYSTEM` 或 `CTA` 处理。scope 为 0/1/2、但已知 modifier 组成不合法的操作/空间组合时，分类为 `ILLEGAL_OPERAND`。两类失败都不产生原子事件或其他架构效果。

canonical 原子语法必须显式带出 space、order 和 scope，字段顺序固定为：

```text
(S_ATOM|V_ATOM).<op>.<type>.<space>.<order>.<scope>
```

也就是 space 紧跟 type，之后才是 order 和 scope。例如：

```text
S_ATOM.LOAD.U32.GLOBAL.ACQUIRE.DEVICE s0, [s2:s3 + 0]
V_ATOM.XCHG.U32.GLOBAL.ACQ_REL.DEVICE v0, [s2:s3 + v4], v5
S_ATOM.STORE.U64.SHARED.RELEASE.CTA [s2 + 0], s4:s5
```

文本中的 order/scope 后缀装入同一 operation form 的 modifier 位，不产生另一套 opcode。省略 space、交换 space/order/scope 顺序，或给 shared 写非 `CTA` scope，都不是 canonical 合法语法。

合法组合只有下表：

| 原子类别 | 合法 `order` | global 合法 `scope` | shared 合法 `scope` |
|---|---|---|---|
| LOAD | `RELAXED`、`ACQUIRE` | `CTA`、`DEVICE`、`SYSTEM` | `CTA` |
| STORE | `RELAXED`、`RELEASE` | `CTA`、`DEVICE`、`SYSTEM` | `CTA` |
| ADD/MIN/MAX/AND/OR/XOR/XCHG | 四种全部 | `CTA`、`DEVICE`、`SYSTEM` | `CTA` |
| CAS | 四种全部 | `CTA`、`DEVICE`、`SYSTEM` | `CTA` |

param、const 和 local 不支持原子。没有 SC order，也没有跨所有原子地址的总序。把 scope 写大不会凭空制造一次通信，更不能让 shared 离开 CTA。

同一个所有权纪元中，一旦某个 4/8 字节区间被原子访问，该精确区间就进入原子纪元：

- 后续重叠访问必须使用相同起点、相同宽度的原子；
- 普通访问与它有任何重叠，程序未定义；
- 不同起点或宽度的重叠原子，程序未定义；
- U32 和 U64 原子永不共享同一个 `mo`。

原子操作只搬运和计算位模式。U32 和 U64 原子都不携带任何影子状态，也不需要区分“保留 tag”和“清除 tag”的情况；一次 U64 原子读写的就是那 8 个字节。

## 8. FENCE 和 CTA 屏障

`FENCE.CTA/DEVICE/SYSTEM` 对执行代理做 acquire-release 排序。它对 warp 的 scalar 事件和相关 vector 事件建立第 6 节规定的 `ppo`；它不是会合点，也不等待其他 warp。只有配合同址原子通信并满足 scope 相容时，它才建立跨代理 `sw`。

`BAR.SYNC.CTA id` 是唯一的屏障指令，只有全 CTA 语义。每 CTA 有 8 个槽 `0..7`；owner 唯一身份为 CTA 内 `linear_tid=warp_id*32+lane_id`。完成条件是槽的 `arrived_set` 等于 CTA 当前的 `live_owner_set`；不存在的尾 lane 从不计入，`EXIT` 会把退出线程移出 `live_owner_set`，也没有 `expected` 或子集参数。

- 每次到达是一次 shared CTA release，覆盖该 warp 当前全部 live lane：屏障要求 scalar-ready，所以 warp 只能整体到达。
- `arrived_set` 追上 `live_owner_set` 后，所有等待者一起恢复并各自取得 shared CTA acquire，槽随即清回 idle。
- `EXIT` 可以通过缩小 `live_owner_set` 让屏障完成，但它自己不是 release，因此不给任何 waiter 建立 `sw` 边。

屏障阻塞记录保存 `{warp_id, owner_snapshot, resume_pc}`。恢复只把该 warp 的 PC 写成 `resume_pc` 并置 ready；active/live 掩码、重汇聚栈、调用栈和 shared 事件历史都不改。槽清空不是内存事件；因为槽里不保留任何跨屏障状态，同一个槽的两次屏障之间也没有需要区分的“代”。

这些边只覆盖 shared 事件。屏障不排序 global、local、param、const，也不涉及 host。用屏障交换 global 数据仍需合法原子发布/获取；需要的顺序仍由 atomic order/scope 和 `FENCE` 建立。

需要 arrive/wait 分离的软件用 shared memory 上的原子计数器加 `FENCE.CTA` 自行实现：release 侧用 RELEASE 原子递增，acquire 侧用 ACQUIRE 原子自旋读。这样得到的顺序由第 6 节的原子 `sw` 规则给出，不依赖任何屏障槽状态。

## 9. 主机所有权

global allocation 的所有权为 `HOST`、`DEVICE` 或运行时明确创建的 `SYSTEM_SHARED`。

通常采用独占转移：

1. 主机在 HOST 状态创建并初始化 allocation。
2. 启动前，运行时以 release 操作转为 DEVICE；设备入口通过 acquire 看到初始化。
3. kernel 执行期间，主机不得访问 DEVICE allocation，设备也不得访问 HOST allocation。
4. kernel 全部设备事件结束后，运行时以 SYSTEM release/acquire 转回 HOST。
5. 主机重新取得所有权后才能读取结果或释放 allocation。

`FENCE.SYSTEM` 只排序事件，不改变所有者，不能让主机在 kernel 执行时轮询一个 DEVICE allocation。

只有实现和运行时都声明支持时，allocation 才可进入 `SYSTEM_SHARED`。此时主机自然对齐、lock-free 的 U32/U64 原子映射到 SYSTEM scope 的同宽 `A_R/A_W/A_RMW`，并与设备共享 `mo`。主机只映射 RELAXED、ACQUIRE、RELEASE、ACQ_REL 四种 order。普通 payload 仍须通过 SYSTEM scope 的发布/获取形成 `hb`。

## 10. 数据竞争和 DRF 保证

两个事件满足以下条件时冲突：

1. 来自不同 lane、warp 或主机/设备代理；
2. 位于同一空间、同一 allocation 和同一所有权纪元；
3. 字节区间重叠；
4. 至少一个是写。

若冲突访问至少一方是普通访问，且双方互不 `hb`，程序存在数据竞争并整体未定义。param/const 的只读、每 lane 私有 local、合法同址同宽原子不构成数据竞争。

**DRF 保证：**程序若对所有输入都无数据竞争、遵守原子纪元和主机所有权，并用本章 `sw` 完成跨代理通信，那么普通访问等价于某个尊重 `hb` 与每位置 `co` 的交错执行；每个原子位置按自己的 `mo` 执行。不同地址仍没有架构总序。

## 11. 最小判例

### 11.1 标量和向量事件数

```text
S_LD.GLOBAL.U32 s0, [s2:s3]       // scalar-ready 时，整个 warp 只有一个 R
V_LD.GLOBAL.U32 v0, [s2:s3 + v4]  // P 中每个 lane 各有一个 R
```

第一条的头部 guard 必须是 `PT`，且必须满足 scalar-ready；成功恰好一个事件，失败零个事件。第二条对 `P` 中每个 lane 恰好一个事件，即使所有 `v4` 都相等，也不能退化成一个 scalar 事件。

### 11.2 64 位地址跨寄存器文件

```text
V_MOV.B64 v2:v3, s4:s5      # ssrc=1
S_READFIRST.B64 s6:s7, v2:v3
```

若 `s4:s5` 保存一个合法 global 地址，则每个参与 lane 的 `v2:v3` 都得到同一个 64 位值；`S_READFIRST.B64` 再从编号最小的 live lane 把这 64 位复制回 `s6:s7`。两步都只搬位，空间仍由随后的访存 opcode 决定。

### 11.3 shared 屏障

```text
warp0: V_ST.SHARED.U32 [x], 1; BAR.SYNC.CTA 0
warp1: BAR.SYNC.CTA 0; v5 = V_LD.SHARED.U32 [x]
```

成功的全 CTA 屏障后，`v5` 必须看到 1。把 `x` 换成 global，仅有屏障不足，未排序的普通冲突是数据竞争。

### 11.4 主机完成边

```text
host:   W x=1; launch
device: R x; W y=2; complete
host:   R y
```

合法所有权转移保证设备读到 `x=1`，完成后主机读到 `y=2`。kernel 执行期间主机访问 DEVICE allocation 属于所有权违规。

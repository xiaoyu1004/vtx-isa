# vtx-isa

这个仓库把两类内容合成一份 **VTX-1 ISA 1.0 Draft** 参考手册：

- `isa/vtx1/isa.yaml`：机器能读的指令定义；
- `docs/*.md`：人读的架构与编程说明。

脚本不依赖 Pandoc 或 Typst，从仓库根目录执行即可。

## SGPR 和 VGPR 到底归谁

规范看到的是“每个 warp 自己的寄存器”：

- SGPR 是这个 warp 共享的标量寄存器。一份值供整个 warp 使用；
- VGPR 是这个 warp 中每个 lane 各自的一份向量寄存器切片；
- `vp0..vp15` 同样是每个 warp 的逐 lane 谓词状态。

芯片实现时，SM/CU 通常不会真的给每个 warp 焊一套独立寄存器。更常见的做法是放
几个很大的物理寄存器文件或 SRAM bank，再给每个驻留 warp 分配一段物理切片。
调度器可以让不同 warp 共用同一个物理存储阵列，但不能让它们看到彼此的数据。
也就是说，“物理上切片共享 SM/CU 资源”和“架构上每个 warp 独占自己的 SGPR、
VGPR、vp 命名空间”并不冲突。换 warp、暂停或恢复时，实现必须保持这个架构归属。

## 安装

需要 Python 3.10 或更新版本。PDF 构建还需要中文字体；Windows 会优先使用
`C:/Windows/Fonts/msyh.ttc`。

```powershell
py -m pip install -r requirements.txt
```

依赖只有 PyYAML、jsonschema 和 ReportLab，`requirements.txt` 不锁虚构版本。

## 验证 ISA

```powershell
py tools\validate_isa.py
```

验证器先真正执行 `isa/vtx1/schema.json`（JSON Schema Draft 2020-12），然后检查：

- 实际 family/form 数量是否等于 YAML 自己声明的 `counts`；
- family ID 是否都是语义化 slug（例如 `v-add`、`bar-sync`）；
- form 是否没有重复书写 `class`、`format`、`fields` 这些可派生字段；
- YAML 中 7 种 `execution_domain` 和 8 种指令 `class` 是否完整、合法；
- 每个 form 的 `(class, format, opcode)` 是否唯一；
- form 的 `encoding_format` 是否在对应 class 的 `format_registry` 中注册；
- MEMORY 的 format 6/`SMEMX`、format 7/`VATOMX` 及其 mixed 字段布局；
- class、format、opcode、guard header 的位置和固定值；
- 每个 64 位编码是否无空洞、无重叠；
- `optional`、`required_pt`、`explicit_condition` guard 规则；
- 所有 scalar form 是否要求 `scalar_ready`；
- CALL、CALL.IND、RET 的隐式 call stack、JUMP.IND 不改栈、SSY 只读当前 call depth，
  以及 descriptor 深度 0..16；
- SGPR、VGPR、`vp` operand 是否引用存在且合适的编码字段；
- SGPR64/VGPR64 语法是否使用偶数起始、相邻的 `s0:s1`/`v0:v1` 寄存器对；
- VP 是否保持 32 位、每个元素 1 位，以及 `fault_priority` 是否完整且不重复；
- atomic 的运行时 `order/scope` modifier 字段、scope 值 3 的 reserved 分类、
  `legal_orders/legal_scopes` 合法集合、具体 example 编码和 CAS 双数据操作数；
- VMEM 的 `uniform_base`、`lane_address`、`sv_mix` 地址合同，其中 SV 索引必须
  `zero_extend(vaddr)`；
- `vsrc32`/`vsrc64` 混合源只出现在 `V1/V2/V3/VCMP` 的 `vector` form 上，selector
  字段在这些 form 中可变、在其他 form 中是 must-zero 洞；
- 混合源 `V_MOV.B64` 的 SGPR64→VGPR64 与 `S_READFIRST.B64` 的 VGPR64→SGPR64
  寄存器对规则；
- 每 CTA 恰好 8 个 barrier 槽，`BAR.SYNC.CTA` 是唯一屏障指令，其三元组和 slot3
  编码正确，且 `barrier_contract` 只描述 owner 身份、`live_owner_set`、等待记录
  和 idle 槽；
- MEMORY `address_template` 的字段引用，以及唯一 MMA 的 32-lane `matrix_contract`；
- 示例、all-form 向量和 selector 向量是否满足 fixed/must-zero/reserved 约束。

成功报告会显示 YAML 中的实际计数。失败时进程返回非零退出码，并打印具体 form
和字段位置。调试另一份 YAML 时可以指定路径；若它没有配套向量，可暂时跳过向量：

```powershell
py tools\validate_isa.py path\to\isa.yaml --no-vectors
```

## 构建参考手册

```powershell
py tools\build_spec.py
```

构建会先执行完整验证，再按文件名合并 `docs/*.md`，最后追加按 family 分组的自动
附录。每个 form 都显示执行域、编码格式、语义组、`(class, format, opcode)`
三元组、guard policy、required state、operands、semantics、faults 和 64 位机器字。
atomic form 还显示合法 order/scope modifier 集合；MEMORY form 显示含 mode/scale 的
`address_template`；MMA form 会完整展开唯一的 `matrix_contract`。含混合源操作数的
form 会显示 `Scalar source selector` 表，逐个列出 selector 码对应的 SGPR 源位置。
附录首页同时列出 descriptor/barrier contract。

每次构建开始时只按精确文件名清理旧产物，不会使用通配符，也不会删除 `dist/` 中
其他无关文件。当前会清理：

- `VTX-1-ISA-Reference-1.0-Draft.*`（旧项目名输出）
- `VTX-1-ISA-Reference-2.0.*`
- `generated-instruction-reference.md`

输出只有新的 Draft 文件：

- `dist/VTX-ISA-Reference-1.0-Draft.md`
- `dist/VTX-ISA-Reference-1.0-Draft.html`
- `dist/VTX-ISA-Reference-1.0-Draft.pdf`

Markdown、HTML 和 PDF 封面都会显示 SGPR + VGPR，以及从 YAML 实际计算出的
family/form 数。HTML 自带 CSS 和目录；中文 PDF 带可见目录及书签。Markdown
表格中的反引号代码即使包含 `|` 也不会被错误拆列。

## 运行测试

```powershell
py -m unittest discover -s tests -v
```

测试覆盖 schema、计数、每个 form 的唯一向量、7 个执行域、8 个 class、编码三元组、
派生字段不得手写、SMEMX/VATOMX mixed 格式、header、64 位覆盖、guard、scalar-ready、
控制状态、混合源 selector 模型与 selector 向量、X_BROADCAST、atomic modifier/CAS、
VMEM 地址模式、寄存器对语法、call descriptor、SSY call stack、scope reserved、
atomic 具体示例、SV zero-extension、B64 跨域搬运、`BAR.SYNC.CTA` 是唯一屏障、
简化后的 `barrier_contract`、故障表去掉 `BARRIER_FAULT` 并改名 `DIVERGENCE_FAULT`、
docs08 静态门禁、fault priority、VP、唯一 MMA 合同、机器字以及完整构建和精确
stale 清理。

# CUDA / VTX-1 对照示例

本目录给出六个较大工作负载的 CUDA kernel，以及逐项对应的 VTX-1
汇编示意。每个 CTA 都至少包含 4 个 32-lane warp；推荐配置实际使用
8 个 warp。

| 示例 | 推荐输入 | block/CTA | 推荐 grid | VTX-1 要点 |
|---|---:|---:|---:|---|
| `vecadd` | `N = 16 * 1024 * 1024` | 256 | 4096 blocks，grid-stride | U32 访存 + `V_FADD.F32` |
| `transpose` | 8192 x 8192 | 32 x 8 | 256 x 256 | 32 x 33 padded shared tile |
| `sgemm` | M=N=K=4096 | 16 x 16 | 256 x 256 | shared tiling + `V_FFMA.F32` |
| `hgemm_mma` | M=N=K=4096 | 256 | 256 x 32 | 两条 `MMA.M16N8K16.F16.F16.F32`/warp |
| `reduce` | `N = 16 * 1024 * 1024` | 256 | 4096 blocks | immediate-delta shuffle + shared + CTA barrier |
| `pipeline` | `N = 16 * 1024 * 1024` | 256 | 4096 blocks | shared 原子计数器 + `FENCE.CTA` 点对点同步 |

## 文件对应关系

```text
cuda/vecadd.cu       <-> vtx1/vecadd.vtx
cuda/transpose.cu    <-> vtx1/transpose.vtx
cuda/sgemm.cu        <-> vtx1/sgemm.vtx
cuda/hgemm_mma.cu    <-> vtx1/hgemm_mma.vtx
cuda/reduce.cu       <-> vtx1/reduce.vtx
cuda/pipeline.cu     <-> vtx1/pipeline.vtx
```

CUDA 文件只包含 kernel 和推荐启动常量，不绑定特定 host 框架。VTX 文件顶部
列出了参数 ABI、资源需求和寄存器分配，标签用于表示规范允许的符号控制目标。

## VTX-1 约定

- warp 固定为 32 lane。
- kernel 参数的前 64 字节由运行时复制到 `s0..s15`。
- 指针参数必须由参数布局记录声明为 `GLOBAL_PTR`，因为访存指令的地址空间由
  opcode 决定，而不是由地址值携带。
- uniform 标量（矩阵维度、元素个数等）留在 SGPR 中，由 `V1`/`V2`/`V3`/`VCMP`
  的 scalar-source selector 直接读取；每条向量指令最多一个 SGPR 源，因此不需要
  先把它们复制进 VGPR。
- ISA 没有 `*.F32` load/store；浮点数据通过 `V_LD.*.U32` 和
  `V_ST.*.U32` 原样搬入 VGPR，再交给 FP32 算术指令解释。
- VTX-1 1.0 Draft 唯一的 MMA 是 F16 x F16 -> F32，不能直接表示真正的
  FP32 SGEMM，所以 `sgemm.vtx` 使用 `V_FFMA.F32`。
- `hgemm_mma` 要求 `M % 128 == 0`、`N % 16 == 0`、`K % 16 == 0`。
  每个 warp 用两条 N8 MMA 组成一个 16 x 16 输出 tile，且所有 32 lane
  必须同时 live、active 并位于同一动态 PC。
- `transpose`、`sgemm` 和 `reduce` 中的 `BAR.SYNC.CTA 0` 必须由 CTA 内
  所有线程一致到达；边界线程通过 predicated memory operation 零填充，
  不在 barrier 前提前退出。
- `pipeline` 展示屏障覆盖不到的那一类同步：生产者 warp 和消费者 warp
  之间的点对点交接。ISA 只提供全 CTA 的 `BAR.SYNC.CTA`，没有 split
  arrive/wait，所以流水线用 shared 上的两个原子计数器加 `FENCE.CTA`
  自行构造。两个方向的写法故意不对称：发布侧只用一条 `RELEASE` 原子，
  因为 `RELEASE` 本身已经排序它之前的全部访问，再加栅栏是重复；等待侧
  用 `RELAXED` 自旋，成功后才付一次 `FENCE.CTA`，因为 `ACQUIRE` 自旋会
  逐次付出排序代价。`BAR.SYNC.CTA 0` 只在开头清零计数器时用一次，那正是
  屏障擅长的一次性会合。
- `pipeline` 的所有 `BRA.P` 都是 warp 统一分支：生产者/消费者划分按
  `warp_id` 取值，两个循环条件比较的也都是 warp 内一致的值。因此没有
  分支消耗重汇聚帧（`reconv_stack_depth=0`），warp 全程保持
  scalar-ready，`SATOM` 标志访问才合法。

## 可执行性

仓库目前定义 ISA、编码与 ABI，但没有 assembler、linker、loader 或 simulator。
因此 `.vtx` 是遵循当前 canonical 指令拼写的架构级示例，不是可以在本仓库内
直接生成二进制并运行的源文件。CUDA 文件可由 CUDA 工具链独立编译检查。

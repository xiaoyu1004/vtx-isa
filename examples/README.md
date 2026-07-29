# CUDA / VTX-1 对照示例

本目录给出五个较大工作负载的 CUDA kernel，以及逐项对应的 VTX-1
汇编示意。每个 CTA 都至少包含 4 个 32-lane warp；推荐配置实际使用
8 个 warp。

| 示例 | 推荐输入 | block/CTA | 推荐 grid | VTX-1 要点 |
|---|---:|---:|---:|---|
| `vecadd` | `N = 16 * 1024 * 1024` | 256 | 4096 blocks，grid-stride | U32 访存 + `V_FADD.F32` |
| `transpose` | 8192 x 8192 | 32 x 8 | 256 x 256 | 32 x 33 padded shared tile |
| `sgemm` | M=N=K=4096 | 16 x 16 | 256 x 256 | shared tiling + `V_FFMA.F32` |
| `hgemm_mma` | M=N=K=4096 | 256 | 256 x 32 | 两条 `MMA.M16N8K16.F16.F16.F32`/warp |
| `reduce` | `N = 16 * 1024 * 1024` | 256 | 4096 blocks | immediate-delta shuffle + shared + CTA barrier |

## 文件对应关系

```text
cuda/vecadd.cu       <-> vtx1/vecadd.vtx
cuda/transpose.cu    <-> vtx1/transpose.vtx
cuda/sgemm.cu        <-> vtx1/sgemm.vtx
cuda/hgemm_mma.cu    <-> vtx1/hgemm_mma.vtx
cuda/reduce.cu       <-> vtx1/reduce.vtx
```

CUDA 文件只包含 kernel 和推荐启动常量，不绑定特定 host 框架。VTX 文件顶部
列出了参数 ABI、资源需求和寄存器分配，标签用于表示规范允许的符号控制目标。

## VTX-1 约定

- warp 固定为 32 lane。
- kernel 参数的前 64 字节由运行时复制到 `s0..s15`。
- 指针参数必须由参数布局记录声明为 `GLOBAL_PTR`，以保留地址空间 provenance。
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

## 可执行性

仓库目前定义 ISA、编码与 ABI，但没有 assembler、linker、loader 或 simulator。
因此 `.vtx` 是遵循当前 canonical 指令拼写的架构级示例，不是可以在本仓库内
直接生成二进制并运行的源文件。CUDA 文件可由 CUDA 工具链独立编译检查。

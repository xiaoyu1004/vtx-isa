#include <cuda_runtime.h>
#include <cuda/atomic>
#include <cstdint>

namespace pipeline_example {

constexpr int kThreads = 256;                    // 8 warps: 1 producer + 7 consumers
constexpr int kConsumerWarps = 7;
constexpr int kTile = kConsumerWarps * 32;       // 224 elements per tile
constexpr int kRecommendedBlocks = 4096;
constexpr std::uint32_t kRecommendedTiles = 16;  // tiles handled by each block

using BlockCounter = cuda::atomic_ref<unsigned, cuda::thread_scope_block>;

// Warp-specialized double-buffered scale. Warp 0 stages tiles of the input
// into shared memory; warps 1..7 scale one 32-element slice each and write it
// back. The two warp groups never meet at a barrier, so the producer can fetch
// tile k+1 while the consumers are still working on tile k.
//
// __syncthreads() appears exactly once, to publish the zeroed counters. Each
// steady-state handoff publishes with a release counter increment and waits by
// spinning on relaxed loads followed by a single block-scope acquire fence,
// which is what the VTX-1 version spells as FENCE.CTA. The spin stays relaxed
// because an acquire load would pay for ordering on every iteration.
__global__ void pipeline(const float* __restrict__ in,
                         float* __restrict__ out,
                         std::uint32_t n_tiles,
                         float alpha) {
  __shared__ float buffer[2][kTile];
  __shared__ unsigned filled;
  __shared__ unsigned drained;

  const unsigned lane = threadIdx.x & 31u;
  const unsigned warp = threadIdx.x >> 5;

  if (threadIdx.x == 0) {
    filled = 0u;
    drained = 0u;
  }
  __syncthreads();

  BlockCounter filled_ref(filled);
  BlockCounter drained_ref(drained);
  std::uint32_t base = blockIdx.x * n_tiles * kTile;

  if (warp == 0) {
    for (std::uint32_t k = 0; k < n_tiles; ++k, base += kTile) {
      // buffer[k & 1] last held tile k-2, so wait for its seven consumers.
      // Phrased as 7k <= drained+7 to keep the comparison unsigned at k < 2.
      while (7u * k > drained_ref.load(cuda::memory_order_relaxed) + 7u) {
      }
      cuda::atomic_thread_fence(cuda::memory_order_acquire,
                                cuda::thread_scope_block);

      float* dst = buffer[k & 1u];
      for (int j = 0; j < kConsumerWarps; ++j) {
        const int slot = j * 32 + static_cast<int>(lane);
        dst[slot] = in[base + slot];
      }

      filled_ref.fetch_add(1u, cuda::memory_order_release);
    }
  } else {
    const int slot = static_cast<int>((warp - 1u) * 32u + lane);
    for (std::uint32_t k = 0; k < n_tiles; ++k, base += kTile) {
      while (k + 1u > filled_ref.load(cuda::memory_order_relaxed)) {
      }
      cuda::atomic_thread_fence(cuda::memory_order_acquire,
                                cuda::thread_scope_block);

      out[base + slot] = buffer[k & 1u][slot] * alpha;

      // Release orders the read above before the buffer is handed back.
      drained_ref.fetch_add(1u, cuda::memory_order_release);
    }
  }
}

// Recommended launch, covering N = 16 * 1024 * 1024 elements:
// pipeline<<<kRecommendedBlocks, kThreads>>>(in, out, kRecommendedTiles,
//                                            alpha);
// The launch must satisfy N == gridDim.x * n_tiles * kTile.

}  // namespace pipeline_example

#include <cuda_runtime.h>
#include <cstdint>

namespace reduce_example {

constexpr int kThreads = 256;          // 8 warps per block
constexpr int kRecommendedBlocks = 4096;
constexpr std::uint32_t kRecommendedElements = 16u * 1024 * 1024;

__device__ __forceinline__ float warp_reduce_sum(float value) {
  constexpr unsigned kFullMask = 0xffffffffu;
#pragma unroll
  for (int delta = warpSize / 2; delta > 0; delta /= 2) {
    value += __shfl_down_sync(kFullMask, value, delta);
  }
  return value;
}

// Produces one partial sum per block. Launch this kernel repeatedly on its
// output, or finish the final few values on the host.
__global__ void reduce(const float* __restrict__ input,
                       float* __restrict__ partials,
                       std::uint32_t n) {
  __shared__ float warp_sums[kThreads / 32];

  const unsigned lane = threadIdx.x & 31u;
  const unsigned warp = threadIdx.x >> 5;
  const std::uint32_t stride = blockDim.x * gridDim.x;

  float sum = 0.0f;
  for (std::uint32_t i = blockIdx.x * blockDim.x + threadIdx.x;
       i < n;
       i += stride) {
    sum += input[i];
  }

  sum = warp_reduce_sum(sum);
  if (lane == 0) {
    warp_sums[warp] = sum;
  }
  __syncthreads();

  sum = (warp == 0 && lane < kThreads / 32) ? warp_sums[lane] : 0.0f;
  if (warp == 0) {
    sum = warp_reduce_sum(sum);
    if (lane == 0) {
      partials[blockIdx.x] = sum;
    }
  }
}

// Recommended first pass:
// reduce<<<kRecommendedBlocks, kThreads>>>(input, partials,
//                                          kRecommendedElements);

}  // namespace reduce_example

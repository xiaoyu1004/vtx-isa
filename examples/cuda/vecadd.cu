#include <cuda_runtime.h>
#include <cstdint>

namespace vecadd_example {

constexpr int kThreads = 256;          // 8 warps per block
constexpr int kRecommendedBlocks = 4096;
constexpr std::uint32_t kRecommendedElements = 16u * 1024 * 1024;

__global__ void vecadd(const float* __restrict__ a,
                       const float* __restrict__ b,
                       float* __restrict__ c,
                       std::uint32_t n) {
  const std::uint32_t stride = blockDim.x * gridDim.x;

  for (std::uint32_t i = blockIdx.x * blockDim.x + threadIdx.x;
       i < n;
       i += stride) {
    c[i] = a[i] + b[i];
  }
}

// Recommended launch:
// vecadd<<<kRecommendedBlocks, kThreads>>>(a, b, c, kRecommendedElements);

}  // namespace vecadd_example

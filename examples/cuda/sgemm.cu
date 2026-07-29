#include <cuda_runtime.h>

namespace sgemm_example {

constexpr int kTile = 16;
constexpr int kRecommendedM = 4096;
constexpr int kRecommendedN = 4096;
constexpr int kRecommendedK = 4096;

// Row-major C[M,N] = A[M,K] * B[K,N].
__global__ void sgemm(const float* __restrict__ a,
                      const float* __restrict__ b,
                      float* __restrict__ c,
                      int m,
                      int n,
                      int k) {
  __shared__ float a_tile[kTile][kTile];
  __shared__ float b_tile[kTile][kTile];

  const int row = blockIdx.y * kTile + threadIdx.y;
  const int col = blockIdx.x * kTile + threadIdx.x;
  float accumulator = 0.0f;

  for (int tile_k = 0; tile_k < k; tile_k += kTile) {
    const int a_col = tile_k + threadIdx.x;
    const int b_row = tile_k + threadIdx.y;

    a_tile[threadIdx.y][threadIdx.x] =
        (row < m && a_col < k) ? a[row * k + a_col] : 0.0f;
    b_tile[threadIdx.y][threadIdx.x] =
        (b_row < k && col < n) ? b[b_row * n + col] : 0.0f;

    __syncthreads();

#pragma unroll
    for (int inner_k = 0; inner_k < kTile; ++inner_k) {
      accumulator =
          fmaf(a_tile[threadIdx.y][inner_k],
               b_tile[inner_k][threadIdx.x],
               accumulator);
    }

    __syncthreads();
  }

  if (row < m && col < n) {
    c[row * n + col] = accumulator;
  }
}

// Recommended launch for M=N=K=4096:
// dim3 block(kTile, kTile);                   // 256 threads = 8 warps
// dim3 grid(256, 256);                        // 65,536 blocks
// sgemm<<<grid, block>>>(a, b, c, 4096, 4096, 4096);

}  // namespace sgemm_example

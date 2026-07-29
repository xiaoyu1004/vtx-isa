#include <cuda_fp16.h>
#include <cuda_runtime.h>
#include <mma.h>

namespace hgemm_mma_example {

namespace wmma = nvcuda::wmma;

constexpr int kWarpSize = 32;
constexpr int kWarpsPerBlock = 8;
constexpr int kThreads = kWarpSize * kWarpsPerBlock;
constexpr int kTileM = 16;
constexpr int kTileN = 16;
constexpr int kTileK = 16;
constexpr int kRecommendedM = 4096;
constexpr int kRecommendedN = 4096;
constexpr int kRecommendedK = 4096;

// Row-major C[M,N] = A[M,K] * B[K,N], with FP16 inputs and FP32 output.
// M and N must be multiples of 128 and 16 respectively; K must be a
// multiple of 16. Each warp computes one 16x16 output tile.
__global__ void hgemm_mma(const half* __restrict__ a,
                          const half* __restrict__ b,
                          float* __restrict__ c,
                          int m,
                          int n,
                          int k) {
  (void)m;  // Launch precondition guarantees that every generated tile is valid.
  const int warp = threadIdx.x / kWarpSize;
  const int tile_row = (blockIdx.y * kWarpsPerBlock + warp) * kTileM;
  const int tile_col = blockIdx.x * kTileN;

  wmma::fragment<wmma::accumulator, kTileM, kTileN, kTileK, float> c_frag;
  wmma::fill_fragment(c_frag, 0.0f);

  for (int tile_k = 0; tile_k < k; tile_k += kTileK) {
    wmma::fragment<wmma::matrix_a, kTileM, kTileN, kTileK,
                   half, wmma::row_major>
        a_frag;
    wmma::fragment<wmma::matrix_b, kTileM, kTileN, kTileK,
                   half, wmma::row_major>
        b_frag;

    wmma::load_matrix_sync(a_frag, a + tile_row * k + tile_k, k);
    wmma::load_matrix_sync(b_frag, b + tile_k * n + tile_col, n);
    wmma::mma_sync(c_frag, a_frag, b_frag, c_frag);
  }

  wmma::store_matrix_sync(c + tile_row * n + tile_col,
                          c_frag,
                          n,
                          wmma::mem_row_major);
}

// Recommended launch for M=N=K=4096:
// dim3 block(kThreads);                       // 256 threads = 8 warps
// dim3 grid(4096 / kTileN,
//           4096 / (kWarpsPerBlock * kTileM));  // 256 x 32 = 8192 blocks
// hgemm_mma<<<grid, block>>>(a, b, c, 4096, 4096, 4096);

}  // namespace hgemm_mma_example

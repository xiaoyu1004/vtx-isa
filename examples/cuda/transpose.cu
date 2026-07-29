#include <cuda_runtime.h>

namespace transpose_example {

constexpr int kTileDim = 32;
constexpr int kBlockRows = 8;
constexpr int kRecommendedWidth = 8192;
constexpr int kRecommendedHeight = 8192;

__global__ void transpose(const float* __restrict__ input,
                          float* __restrict__ output,
                          int width,
                          int height) {
  // The extra column removes the common 32-way shared-memory bank conflict.
  __shared__ float tile[kTileDim][kTileDim + 1];

  const int input_x = blockIdx.x * kTileDim + threadIdx.x;
  const int input_y = blockIdx.y * kTileDim + threadIdx.y;

#pragma unroll
  for (int row = 0; row < kTileDim; row += kBlockRows) {
    if (input_x < width && input_y + row < height) {
      tile[threadIdx.y + row][threadIdx.x] =
          input[(input_y + row) * width + input_x];
    }
  }

  __syncthreads();

  const int output_x = blockIdx.y * kTileDim + threadIdx.x;
  const int output_y = blockIdx.x * kTileDim + threadIdx.y;

#pragma unroll
  for (int row = 0; row < kTileDim; row += kBlockRows) {
    if (output_x < height && output_y + row < width) {
      output[(output_y + row) * height + output_x] =
          tile[threadIdx.x][threadIdx.y + row];
    }
  }
}

// Recommended launch for 8192 x 8192:
// dim3 block(kTileDim, kBlockRows);           // 256 threads = 8 warps
// dim3 grid(256, 256);                        // 65,536 blocks
// transpose<<<grid, block>>>(in, out, 8192, 8192);

}  // namespace transpose_example

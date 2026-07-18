// SPDX-License-Identifier: GPL-2.0-only

#include <cublas_v2.h>
#include <cuda_runtime.h>

#include <cmath>
#include <cstdio>
#include <cstdlib>

#define CUDA_CHECK(call)                                                        \
    do {                                                                        \
        cudaError_t status_ = (call);                                            \
        if (status_ != cudaSuccess) {                                            \
            std::fprintf(stderr, "CUDA error at %s:%d: %s\n", __FILE__,         \
                         __LINE__, cudaGetErrorString(status_));                  \
            return 1;                                                           \
        }                                                                       \
    } while (0)

#define CUBLAS_CHECK(call)                                                      \
    do {                                                                        \
        cublasStatus_t status_ = (call);                                         \
        if (status_ != CUBLAS_STATUS_SUCCESS) {                                  \
            std::fprintf(stderr, "cuBLAS error at %s:%d: %d\n", __FILE__,       \
                         __LINE__, static_cast<int>(status_));                    \
            return 1;                                                           \
        }                                                                       \
    } while (0)

__global__ void fill(float *data, size_t count, float value) {
    size_t index = blockIdx.x * blockDim.x + threadIdx.x;
    if (index < count) {
        data[index] = value;
    }
}

int main(int argc, char **argv) {
    int n = argc > 1 ? std::atoi(argv[1]) : 8192;
    int iterations = argc > 2 ? std::atoi(argv[2]) : 20;
    if (n <= 0 || iterations <= 0) {
        std::fprintf(stderr, "usage: %s [matrix_dimension] [iterations]\n", argv[0]);
        return 2;
    }

    cudaDeviceProp properties{};
    CUDA_CHECK(cudaGetDeviceProperties(&properties, 0));
    const size_t elements = static_cast<size_t>(n) * n;
    const size_t bytes = elements * sizeof(float);
    float *a = nullptr;
    float *b = nullptr;
    float *c = nullptr;
    CUDA_CHECK(cudaMalloc(&a, bytes));
    CUDA_CHECK(cudaMalloc(&b, bytes));
    CUDA_CHECK(cudaMalloc(&c, bytes));

    const float input = 1.0f / std::sqrt(static_cast<float>(n));
    const int threads = 256;
    const int blocks = static_cast<int>((elements + threads - 1) / threads);
    fill<<<blocks, threads>>>(a, elements, input);
    fill<<<blocks, threads>>>(b, elements, input);
    CUDA_CHECK(cudaGetLastError());

    cublasHandle_t handle{};
    CUBLAS_CHECK(cublasCreate(&handle));
    const float alpha = 1.0f;
    const float beta = 0.0f;
    auto gemm = [&]() {
        return cublasGemmEx(handle, CUBLAS_OP_N, CUBLAS_OP_N, n, n, n, &alpha,
                            a, CUDA_R_32F, n, b, CUDA_R_32F, n, &beta,
                            c, CUDA_R_32F, n, CUBLAS_COMPUTE_32F_PEDANTIC,
                            CUBLAS_GEMM_DEFAULT);
    };

    CUBLAS_CHECK(gemm());
    CUDA_CHECK(cudaDeviceSynchronize());
    cudaEvent_t start{};
    cudaEvent_t stop{};
    CUDA_CHECK(cudaEventCreate(&start));
    CUDA_CHECK(cudaEventCreate(&stop));
    CUDA_CHECK(cudaEventRecord(start));
    for (int index = 0; index < iterations; ++index) {
        CUBLAS_CHECK(gemm());
    }
    CUDA_CHECK(cudaEventRecord(stop));
    CUDA_CHECK(cudaEventSynchronize(stop));

    float elapsed_ms = 0.0f;
    CUDA_CHECK(cudaEventElapsedTime(&elapsed_ms, start, stop));
    float sample = 0.0f;
    CUDA_CHECK(cudaMemcpy(&sample, c, sizeof(sample), cudaMemcpyDeviceToHost));
    const double operations = 2.0 * n * static_cast<double>(n) * n * iterations;
    const double tflops = operations / (elapsed_ms * 1.0e9);
    const bool correct = std::fabs(sample - 1.0f) <= 0.01f;

    std::printf("device=%s\n", properties.name);
    std::printf("matrix=%d iterations=%d elapsed_ms=%.3f\n", n, iterations, elapsed_ms);
    std::printf("fp32_tflops=%.3f sample=%.6f correctness=%s\n", tflops, sample,
                correct ? "PASS" : "FAIL");

    cudaEventDestroy(start);
    cudaEventDestroy(stop);
    cublasDestroy(handle);
    cudaFree(a);
    cudaFree(b);
    cudaFree(c);
    return correct ? 0 : 3;
}

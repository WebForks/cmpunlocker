// SPDX-License-Identifier: GPL-2.0-only
// Destructive-capacity validator for the experimental CMP 170HX 610 path.
//
// This is deliberately a correctness test, not a bandwidth benchmark.  Each
// stage allocates the requested number of GiB, fills every uint64_t with a
// bijective address-dependent pattern, and then verifies every value.  If a
// driver aliases virtual ranges onto the same physical HBM, at least one of the
// aliased indices should read back the pattern for the other index.

#include <cuda_runtime.h>

#include <cerrno>
#include <climits>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <initializer_list>
#include <limits>
#include <string>
#include <vector>

namespace {

constexpr unsigned int kThreadsPerBlock = 256;
constexpr unsigned int kMaximumBlocks = 65535;
constexpr std::uint64_t kGiB = 1024ULL * 1024ULL * 1024ULL;
constexpr const char *kRequiredAcknowledgement =
    "I-ACCEPT-UNVERIFIED-610-MEMORY-STRESS-AND-CONFIRM-FORCED-AIRFLOW";

struct Config {
    std::string pci_bdf;
    std::string acknowledgement;
    std::vector<std::uint64_t> stages_gib;
    unsigned int passes = 5;
};

enum class StageResult {
    kPassed,
    kMismatch,
    kCudaError,
};

__device__ __forceinline__ std::uint64_t pattern_for(
    std::uint64_t word_index,
    std::uint64_t seed,
    bool invert_output)
{
    // Every operation is invertible on 64-bit integers.  For one seed, distinct
    // word indices therefore produce distinct values instead of merely relying
    // on the collision probability of a conventional hash.
    std::uint64_t value = word_index ^ seed;
    value ^= value >> 30;
    value *= 0xbf58476d1ce4e5b9ULL;
    value ^= value >> 27;
    value *= 0x94d049bb133111ebULL;
    value ^= value >> 31;
    return invert_output ? ~value : value;
}

__global__ void write_pattern(
    std::uint64_t *memory,
    std::size_t word_count,
    std::uint64_t seed,
    bool invert_output)
{
    const std::size_t start =
        static_cast<std::size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
    const std::size_t stride =
        static_cast<std::size_t>(blockDim.x) * gridDim.x;
    for (std::size_t index = start; index < word_count; index += stride) {
        memory[index] = pattern_for(
            static_cast<std::uint64_t>(index), seed, invert_output);
    }
}

__global__ void verify_pattern(
    const std::uint64_t *memory,
    std::size_t word_count,
    std::uint64_t seed,
    bool invert_output,
    unsigned long long *mismatch_count,
    unsigned long long *sample_index,
    unsigned long long *sample_expected,
    unsigned long long *sample_observed)
{
    __shared__ unsigned long long block_mismatches[kThreadsPerBlock];
    const std::size_t start =
        static_cast<std::size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
    const std::size_t stride =
        static_cast<std::size_t>(blockDim.x) * gridDim.x;
    unsigned long long local_mismatches = 0;
    for (std::size_t index = start; index < word_count; index += stride) {
        const std::uint64_t expected = pattern_for(
            static_cast<std::uint64_t>(index), seed, invert_output);
        const std::uint64_t observed = memory[index];
        if (observed != expected) {
            ++local_mismatches;
            if (atomicCAS(
                    sample_index,
                    static_cast<unsigned long long>(ULLONG_MAX),
                    static_cast<unsigned long long>(index)) == ULLONG_MAX) {
                // This is a representative mismatch selected by the first
                // successful atomic operation, not necessarily the lowest
                // mismatching address.
                *sample_expected = static_cast<unsigned long long>(expected);
                *sample_observed = static_cast<unsigned long long>(observed);
            }
        }
    }
    block_mismatches[threadIdx.x] = local_mismatches;
    __syncthreads();
    for (unsigned int width = blockDim.x / 2; width != 0; width /= 2) {
        if (threadIdx.x < width) {
            block_mismatches[threadIdx.x] += block_mismatches[threadIdx.x + width];
        }
        __syncthreads();
    }
    if (threadIdx.x == 0 && block_mismatches[0] != 0) {
        atomicAdd(mismatch_count, block_mismatches[0]);
    }
}

void usage(const char *program)
{
    std::fprintf(
        stderr,
        "Usage: %s --pci-bdf DDDD:BB:DD.F "
        "--stage-gib N [--stage-gib N ...] [--passes N] "
        "--acknowledge TOKEN\n"
        "\n"
        "Allocates and verifies every 64-bit word in each requested stage.\n"
        "This validates addressability only; it makes no performance claim.\n",
        program);
}

bool parse_unsigned(const char *text, std::uint64_t *value)
{
    if (text == nullptr || *text == '\0' || *text == '-') {
        return false;
    }
    char *end = nullptr;
    errno = 0;
    const unsigned long long parsed = std::strtoull(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0') {
        return false;
    }
    *value = static_cast<std::uint64_t>(parsed);
    return true;
}

std::string normalize_bdf(std::string value)
{
    for (char &character : value) {
        if (character >= 'A' && character <= 'F') {
            character = static_cast<char>(character - 'A' + 'a');
        }
    }
    if (value.size() == 7) {
        value.insert(0, "0000:");
    }
    return value;
}

bool is_lower_hex(char value)
{
    return (value >= '0' && value <= '9') || (value >= 'a' && value <= 'f');
}

bool is_valid_bdf(const std::string &value)
{
    if (value.size() != 12 || value[4] != ':' || value[7] != ':' ||
        value[10] != '.' || value[11] < '0' || value[11] > '7') {
        return false;
    }
    for (const std::size_t index : {0U, 1U, 2U, 3U, 5U, 6U, 8U, 9U}) {
        if (!is_lower_hex(value[index])) {
            return false;
        }
    }
    return true;
}

bool verify_sysfs_target(const std::string &bdf)
{
    const std::string root = "/sys/bus/pci/devices/" + bdf;
    std::ifstream vendor_file(root + "/vendor");
    std::ifstream device_file(root + "/device");
    std::string vendor;
    std::string device;
    if (!(vendor_file >> vendor) || !(device_file >> device)) {
        std::fprintf(
            stderr,
            "TARGET_ERROR cannot read sysfs identity for pci_bdf=%s\n",
            bdf.c_str());
        return false;
    }
    for (char &character : vendor) {
        if (character >= 'A' && character <= 'F') {
            character = static_cast<char>(character - 'A' + 'a');
        }
    }
    for (char &character : device) {
        if (character >= 'A' && character <= 'F') {
            character = static_cast<char>(character - 'A' + 'a');
        }
    }
    if (vendor != "0x10de" || device != "0x20c2") {
        std::fprintf(
            stderr,
            "TARGET_ERROR pci_bdf=%s identity=%s:%s expected=0x10de:0x20c2\n",
            bdf.c_str(),
            vendor.c_str(),
            device.c_str());
        return false;
    }
    return true;
}

bool parse_arguments(int argc, char **argv, Config *config)
{
    for (int index = 1; index < argc; ++index) {
        const std::string argument(argv[index]);
        if (argument == "--help" || argument == "-h") {
            usage(argv[0]);
            std::exit(0);
        }
        if (index + 1 >= argc) {
            std::fprintf(stderr, "ARGUMENT_ERROR missing value for %s\n", argument.c_str());
            return false;
        }
        const char *value = argv[++index];
        if (argument == "--pci-bdf") {
            if (!config->pci_bdf.empty()) {
                std::fprintf(stderr, "ARGUMENT_ERROR select exactly one PCI BDF\n");
                return false;
            }
            config->pci_bdf = normalize_bdf(value);
        } else if (argument == "--stage-gib") {
            std::uint64_t parsed = 0;
            if (!parse_unsigned(value, &parsed) || parsed == 0 || parsed > 1024) {
                std::fprintf(stderr, "ARGUMENT_ERROR stage must be 1..1024 GiB\n");
                return false;
            }
            config->stages_gib.push_back(parsed);
        } else if (argument == "--passes") {
            std::uint64_t parsed = 0;
            if (!parse_unsigned(value, &parsed) || parsed == 0 || parsed > 100) {
                std::fprintf(stderr, "ARGUMENT_ERROR passes must be 1..100\n");
                return false;
            }
            config->passes = static_cast<unsigned int>(parsed);
        } else if (argument == "--acknowledge") {
            if (!config->acknowledgement.empty()) {
                std::fprintf(stderr, "ARGUMENT_ERROR acknowledgement supplied twice\n");
                return false;
            }
            config->acknowledgement = value;
        } else {
            std::fprintf(stderr, "ARGUMENT_ERROR unknown option %s\n", argument.c_str());
            return false;
        }
    }
    if (config->pci_bdf.empty() || !is_valid_bdf(config->pci_bdf)) {
        std::fprintf(stderr, "ARGUMENT_ERROR a valid --pci-bdf is required\n");
        return false;
    }
    if (config->stages_gib.empty()) {
        std::fprintf(stderr, "ARGUMENT_ERROR at least one --stage-gib is required\n");
        return false;
    }
    if (config->acknowledgement != kRequiredAcknowledgement) {
        std::fprintf(
            stderr,
            "ARGUMENT_ERROR destructive testing requires --acknowledge %s\n",
            kRequiredAcknowledgement);
        return false;
    }
    return true;
}

bool report_cuda_error(cudaError_t error, const char *operation)
{
    if (error == cudaSuccess) {
        return false;
    }
    std::fprintf(
        stderr,
        "CUDA_ERROR operation=%s code=%d name=%s detail=%s\n",
        operation,
        static_cast<int>(error),
        cudaGetErrorName(error),
        cudaGetErrorString(error));
    return true;
}

int select_device(const Config &config)
{
    int device_count = 0;
    if (report_cuda_error(cudaGetDeviceCount(&device_count), "cudaGetDeviceCount")) {
        return -1;
    }
    for (int device = 0; device < device_count; ++device) {
        char candidate[32] = {};
        if (report_cuda_error(
                cudaDeviceGetPCIBusId(candidate, sizeof(candidate), device),
                "cudaDeviceGetPCIBusId")) {
            return -1;
        }
        if (normalize_bdf(candidate) == config.pci_bdf) {
            return device;
        }
    }
    std::fprintf(
        stderr,
        "CUDA_ERROR operation=select-device detail=no CUDA device has pci_bdf=%s\n",
        config.pci_bdf.c_str());
    return -1;
}

StageResult run_stage(std::uint64_t stage_gib, unsigned int passes)
{
    if (stage_gib > std::numeric_limits<std::uint64_t>::max() / kGiB) {
        std::fprintf(stderr, "VALIDATION_ERROR requested byte count overflows\n");
        return StageResult::kCudaError;
    }
    const std::uint64_t requested_bytes_u64 = stage_gib * kGiB;
    if (requested_bytes_u64 > std::numeric_limits<std::size_t>::max()) {
        std::fprintf(stderr, "VALIDATION_ERROR requested byte count exceeds size_t\n");
        return StageResult::kCudaError;
    }
    const std::size_t requested_bytes = static_cast<std::size_t>(requested_bytes_u64);
    const std::size_t word_count = requested_bytes / sizeof(std::uint64_t);

    std::size_t free_bytes = 0;
    std::size_t total_bytes = 0;
    if (report_cuda_error(cudaMemGetInfo(&free_bytes, &total_bytes), "cudaMemGetInfo")) {
        return StageResult::kCudaError;
    }
    std::printf(
        "STAGE_START stage_gib=%llu requested_bytes=%llu free_bytes=%llu total_bytes=%llu passes=%u\n",
        static_cast<unsigned long long>(stage_gib),
        static_cast<unsigned long long>(requested_bytes_u64),
        static_cast<unsigned long long>(free_bytes),
        static_cast<unsigned long long>(total_bytes),
        passes);
    std::fflush(stdout);
    if (requested_bytes > free_bytes) {
        std::fprintf(
            stderr,
            "VALIDATION_ERROR stage_gib=%llu requested_bytes=%llu exceeds free_bytes=%llu\n",
            static_cast<unsigned long long>(stage_gib),
            static_cast<unsigned long long>(requested_bytes_u64),
            static_cast<unsigned long long>(free_bytes));
        return StageResult::kCudaError;
    }

    std::uint64_t *memory = nullptr;
    unsigned long long *device_mismatches = nullptr;
    unsigned long long *device_sample_index = nullptr;
    unsigned long long *device_sample_expected = nullptr;
    unsigned long long *device_sample_observed = nullptr;

    auto cleanup = [&]() {
        bool failed = false;
        if (device_sample_observed != nullptr) {
            failed |= report_cuda_error(
                cudaFree(device_sample_observed), "cudaFree(sample-observed)");
        }
        if (device_sample_expected != nullptr) {
            failed |= report_cuda_error(
                cudaFree(device_sample_expected), "cudaFree(sample-expected)");
        }
        if (device_sample_index != nullptr) {
            failed |= report_cuda_error(cudaFree(device_sample_index), "cudaFree(sample-index)");
        }
        if (device_mismatches != nullptr) {
            failed |= report_cuda_error(
                cudaFree(device_mismatches), "cudaFree(mismatch-count)");
        }
        if (memory != nullptr) {
            failed |= report_cuda_error(cudaFree(memory), "cudaFree(stage)");
        }
        return failed;
    };

    if (report_cuda_error(cudaMalloc(&memory, requested_bytes), "cudaMalloc(stage)")) {
        cleanup();
        return StageResult::kCudaError;
    }
    if (report_cuda_error(
            cudaMalloc(&device_mismatches, sizeof(*device_mismatches)),
            "cudaMalloc(mismatch-count)") ||
        report_cuda_error(
            cudaMalloc(&device_sample_index, sizeof(*device_sample_index)),
            "cudaMalloc(sample-index)") ||
        report_cuda_error(
            cudaMalloc(&device_sample_expected, sizeof(*device_sample_expected)),
            "cudaMalloc(sample-expected)") ||
        report_cuda_error(
            cudaMalloc(&device_sample_observed, sizeof(*device_sample_observed)),
            "cudaMalloc(sample-observed)")) {
        cleanup();
        return StageResult::kCudaError;
    }

    const std::uint64_t needed_blocks =
        (static_cast<std::uint64_t>(word_count) + kThreadsPerBlock - 1) /
        kThreadsPerBlock;
    const unsigned int blocks = static_cast<unsigned int>(
        needed_blocks < kMaximumBlocks ? needed_blocks : kMaximumBlocks);

    for (unsigned int pass = 1; pass <= passes; ++pass) {
        const std::uint64_t base_seed =
            0xd1b54a32d192ed03ULL ^
            (static_cast<std::uint64_t>(pass) * 0x9e3779b97f4a7c15ULL) ^
            (stage_gib * 0xa24baed4963ee407ULL);

        // Three full-allocation patterns per pass: an independently mixed seed,
        // an inverted seed with complemented output, and another independent
        // seed. This exercises both polarities while preserving the unique
        // address-to-value mapping used to expose aliased physical ranges.
        const std::uint64_t seeds[] = {
            base_seed,
            ~base_seed,
            base_seed ^ 0x6a09e667f3bcc909ULL,
        };
        const bool inverted[] = {false, true, false};

        for (unsigned int pattern = 0; pattern < 3; ++pattern) {
            const std::uint64_t seed = seeds[pattern];
            std::printf(
                "PATTERN_START stage_gib=%llu pass=%u pattern=%u/3 "
                "seed=0x%016llx inverted=%u words=%llu\n",
                static_cast<unsigned long long>(stage_gib),
                pass,
                pattern + 1,
                static_cast<unsigned long long>(seed),
                inverted[pattern] ? 1U : 0U,
                static_cast<unsigned long long>(word_count));
            std::fflush(stdout);

            write_pattern<<<blocks, kThreadsPerBlock>>>(
                memory, word_count, seed, inverted[pattern]);
            if (report_cuda_error(cudaGetLastError(), "write-pattern-launch") ||
                report_cuda_error(cudaDeviceSynchronize(), "write-pattern-sync")) {
                cleanup();
                return StageResult::kCudaError;
            }

            if (report_cuda_error(
                    cudaMemset(device_mismatches, 0, sizeof(*device_mismatches)),
                    "cudaMemset(mismatch-count)") ||
                report_cuda_error(
                    cudaMemset(device_sample_index, 0xff, sizeof(*device_sample_index)),
                    "cudaMemset(sample-index)")) {
                cleanup();
                return StageResult::kCudaError;
            }

            verify_pattern<<<blocks, kThreadsPerBlock>>>(
                memory,
                word_count,
                seed,
                inverted[pattern],
                device_mismatches,
                device_sample_index,
                device_sample_expected,
                device_sample_observed);
            if (report_cuda_error(cudaGetLastError(), "verify-pattern-launch") ||
                report_cuda_error(cudaDeviceSynchronize(), "verify-pattern-sync")) {
                cleanup();
                return StageResult::kCudaError;
            }

            unsigned long long mismatch_count = 0;
            unsigned long long sample_index = ULLONG_MAX;
            unsigned long long sample_expected = 0;
            unsigned long long sample_observed = 0;
            if (report_cuda_error(
                    cudaMemcpy(
                        &mismatch_count,
                        device_mismatches,
                        sizeof(mismatch_count),
                        cudaMemcpyDeviceToHost),
                    "cudaMemcpy(mismatch-count)") ||
                report_cuda_error(
                    cudaMemcpy(
                        &sample_index,
                        device_sample_index,
                        sizeof(sample_index),
                        cudaMemcpyDeviceToHost),
                    "cudaMemcpy(sample-index)")) {
                cleanup();
                return StageResult::kCudaError;
            }
            if (mismatch_count != 0) {
                if (report_cuda_error(
                        cudaMemcpy(
                            &sample_expected,
                            device_sample_expected,
                            sizeof(sample_expected),
                            cudaMemcpyDeviceToHost),
                        "cudaMemcpy(sample-expected)") ||
                    report_cuda_error(
                        cudaMemcpy(
                            &sample_observed,
                            device_sample_observed,
                            sizeof(sample_observed),
                            cudaMemcpyDeviceToHost),
                        "cudaMemcpy(sample-observed)")) {
                    cleanup();
                    return StageResult::kCudaError;
                }
                std::fprintf(
                    stderr,
                    "MISMATCH stage_gib=%llu pass=%u pattern=%u mismatch_count=%llu "
                    "sample_word=%llu sample_byte=0x%llx expected=0x%016llx observed=0x%016llx\n",
                    static_cast<unsigned long long>(stage_gib),
                    pass,
                    pattern + 1,
                    mismatch_count,
                    sample_index,
                    sample_index * static_cast<unsigned long long>(sizeof(std::uint64_t)),
                    sample_expected,
                    sample_observed);
                cleanup();
                return StageResult::kMismatch;
            }

            std::printf(
                "PATTERN_OK stage_gib=%llu pass=%u pattern=%u/3 mismatch_count=0\n",
                static_cast<unsigned long long>(stage_gib),
                pass,
                pattern + 1);
            std::fflush(stdout);
        }

        std::printf(
            "PASS_OK stage_gib=%llu pass=%u patterns=3 mismatch_count=0\n",
            static_cast<unsigned long long>(stage_gib),
            pass);
        std::fflush(stdout);
    }

    if (cleanup()) {
        return StageResult::kCudaError;
    }
    std::printf(
        "STAGE_OK stage_gib=%llu passes=%u mismatch_count=0\n",
        static_cast<unsigned long long>(stage_gib),
        passes);
    std::fflush(stdout);
    return StageResult::kPassed;
}

}  // namespace

int main(int argc, char **argv)
{
    Config config;
    if (!parse_arguments(argc, argv, &config)) {
        usage(argv[0]);
        return 64;
    }
    if (!verify_sysfs_target(config.pci_bdf)) {
        return 1;
    }

    const int selected_device = select_device(config);
    if (selected_device < 0) {
        return 1;
    }
    if (report_cuda_error(cudaSetDevice(selected_device), "cudaSetDevice")) {
        return 1;
    }

    cudaDeviceProp properties{};
    if (report_cuda_error(
            cudaGetDeviceProperties(&properties, selected_device),
            "cudaGetDeviceProperties")) {
        return 1;
    }
    char selected_bdf[32] = {};
    if (report_cuda_error(
            cudaDeviceGetPCIBusId(selected_bdf, sizeof(selected_bdf), selected_device),
            "cudaDeviceGetPCIBusId(selected)")) {
        return 1;
    }

    std::printf(
        "VRAM_VERIFY_START device=%d pci_bdf=%s name=%s stages=%llu passes=%u\n",
        selected_device,
        selected_bdf,
        properties.name,
        static_cast<unsigned long long>(config.stages_gib.size()),
        config.passes);
    std::fflush(stdout);

    for (const std::uint64_t stage_gib : config.stages_gib) {
        const StageResult result = run_stage(stage_gib, config.passes);
        if (result == StageResult::kMismatch) {
            std::fprintf(
                stderr,
                "VRAM_VERIFY_RESULT status=FAIL reason=mismatch mismatch_count_nonzero=1\n");
            return 2;
        }
        if (result == StageResult::kCudaError) {
            std::fprintf(
                stderr,
                "VRAM_VERIFY_RESULT status=FAIL reason=cuda_or_allocation_error\n");
            return 1;
        }
    }

    std::printf("VRAM_VERIFY_RESULT status=PASS mismatch_count=0\n");
    return 0;
}

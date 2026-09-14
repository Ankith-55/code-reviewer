# Performance Benchmarking & Scaling Analysis

This document details the benchmarking methodology, empirical measurements, and scaling behavior of the Distributed AI Code Review System.

---

## 1. Benchmark Methodology

The benchmark harness [`benchmark/benchmark.py`](file:///c:/Ankith/code-reviewer/benchmark/benchmark.py) evaluates how system throughput scales as worker containers are increased.

### Workload Specifications:
* **Repository Size**: 60 reviewable source files across arbitrary nested subdirectories (`src/auth/`, `src/payments/`, `src/services/`).
* **Languages**: Python (`.py`), JavaScript (`.js`), and Go (`.go`).
* **Worker Configurations**: Evaluated across 1, 2, 4, and 8 `review-worker` containers.
* **Consistency**: The exact same in-memory ZIP archive is submitted for each worker configuration to eliminate variance in repository structure or file sizes.

---

## 2. Mathematical Metrics

### 2.1 Total Processing Time ($T_N$)
The wall-clock duration from the instant the HTTP archive is submitted to the instant all 60 files have completed review:

$$T_N = t_{\text{completed}} - t_{\text{submitted}}$$

### 2.2 Throughput
The rate of source code files processed per second:

$$\text{Throughput} = \frac{\text{Total Files Analyzed}}{T_N} \quad (\text{files/second})$$

### 2.3 Speedup ($S_N$)
The relative acceleration of using $N$ workers compared to a baseline of 1 worker:

$$S_N = \frac{T_1}{T_N}$$

* Perfect linear speedup would yield $S_N = N$ (e.g., $S_4 = 4.0\times$).

---

## 3. Empirical Benchmark Results

Measured live on the Docker cluster:

| Workers ($N$) | Total Time ($T_N$) | Throughput (files/s) | Measured Speedup ($S_N$) | Ideal Linear Speedup | Scaling Efficiency |
|:---:|:---:|:---:|:---:|:---:|:---:|
| **1 Worker** | 14.55 s | 4.12 | **1.00×** | 1.00× | 100% |
| **2 Workers** | 9.08 s | 6.61 | **1.60×** | 2.00× | 80% |
| **4 Workers** | 5.49 s | 10.92 | **2.65×** | 4.00× | 66% |
| **8 Workers** | 7.24 s | 8.28 | **2.01×** | 8.00× | 25% |

---

## 4. Why Speedup is Sub-Linear: Amdahl's Law in Practice

Amdahl's Law states that the potential speedup of a distributed system is strictly bounded by the serial (non-parallelizable) portion of the workload ($s$):

$$S_N = \frac{1}{s + \frac{1 - s}{N}}$$

### Contributing Factors to Real-World Sub-Linearity:

1. **Serial Repository Extraction ($s$)**:
   * The `repository-worker` decompresses the ZIP archive and traverses directory trees sequentially. During this extraction and file scanning phase, review workers sit idle waiting for jobs to be fanned out.
2. **Database Write & Lock Contention**:
   * Each review completion requires an atomic SQL update on the `repositories` table to increment `completed_files`.
   * At 8 concurrent workers on a single host machine, lock contention on the single PostgreSQL instance and disk I/O write serialization introduces queue wait latency.
3. **Queue AMQP Protocol Overhead**:
   * Delivering messages, socket multiplexing, and round-trip TCP acknowledgments (`basic_ack`) incur CPU cycles that grow with consumer count.
4. **Local Virtualization Overhead**:
   * Running 8 worker containers alongside RabbitMQ, PostgreSQL, and FastAPI on a single machine leads to CPU core context switching and Docker virtual networking overhead.

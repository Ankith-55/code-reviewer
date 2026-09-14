import io
import time
import zipfile
import subprocess
from typing import List, Dict
import httpx


def generate_benchmark_repository(num_files: int = 60) -> bytes:
    """Generates a reproducible ZIP archive containing realistic code files."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(1, num_files + 1):
            if i % 3 == 0:
                code = f"""
# Service Module {i} - Authentication
import hashlib

def login_user_{i}(username, password):
    # Simulated security issue
    password_hash = "{'a' * 32}"
    if username == "admin":
        eval(f"print('Logging in admin user {i}')")
    return username
"""
                zf.writestr(f"src/auth/login_{i:03d}.py", code)
            elif i % 3 == 1:
                code = f"""
// Payment Gateway {i}
function processPayment{i}(accountNumber, amount) {{
    const query = "SELECT * FROM accounts WHERE id = " + accountNumber;
    if (amount <= 0) {{
        throw new Error("Invalid amount");
    }}
    return true;
}}
module.exports = {{ processPayment{i} }};
"""
                zf.writestr(f"src/payments/gateway_{i:03d}.js", code)
            else:
                code = f"""
package services

import "fmt"

type Service{i} struct {{
    ID int
}}

func (s *Service{i}) HandleRequest(id int) string {{
    // TODO: implement caching
    return fmt.Sprintf("Service {i} processed %d", id)
}}
"""
                zf.writestr(f"src/services/handler_{i:03d}.go", code)

    buf.seek(0)
    return buf.getvalue()


def scale_workers(worker_count: int):
    """Dynamically scales review-worker containers using Docker Compose."""
    print(f"\n[+] Scaling cluster to {worker_count} review worker(s)...")
    cmd = ["docker", "compose", "up", "-d", "--scale", f"review-worker={worker_count}"]
    subprocess.run(cmd, check=True, capture_output=True)
    # Wait for containers to initialize and register with RabbitMQ
    time.sleep(3)


def run_benchmark_run(base_url: str, repo_bytes: bytes, expected_files: int) -> float:
    """Submits the workload and measures wall-clock processing time."""
    start_time = time.perf_counter()

    # 1. Submit review job
    res = httpx.post(
        f"{base_url}/review",
        files={"file": ("bench_repo.zip", repo_bytes, "application/zip")},
        timeout=15.0,
    )
    if res.status_code != 202:
        raise RuntimeError(f"Failed to submit review: {res.text}")

    job_id = res.json()["job_id"]

    # 2. Poll until completed
    while True:
        status_res = httpx.get(f"{base_url}/review/{job_id}/status", timeout=5.0).json()
        status = status_res.get("status")
        completed = status_res.get("completed_files", 0)
        failed = status_res.get("failed_files", 0)
        total = status_res.get("total_files", 0)

        if status == "completed" or (total >= expected_files and (completed + failed) >= total):
            break
        time.sleep(0.3)

    end_time = time.perf_counter()
    return end_time - start_time


def main():
    base_url = "http://localhost:8000"
    num_files = 60
    worker_configs = [1, 2, 4, 8]

    print("=" * 65)
    print("      DISTRIBUTED CODE REVIEW SYSTEM BENCHMARK HARNESS")
    print(f"      Workload: Fixed {num_files}-file multi-language repository")
    print("=" * 65)

    # Pre-generate the exact same ZIP bytes for every run
    repo_bytes = generate_benchmark_repository(num_files=num_files)

    results: List[Dict] = []
    base_time_1_worker = None

    for workers in worker_configs:
        scale_workers(workers)

        print(f"[+] Running benchmark with {workers} worker(s)...")
        elapsed_seconds = run_benchmark_run(base_url, repo_bytes, expected_files=num_files)
        throughput = num_files / elapsed_seconds

        if base_time_1_worker is None:
            base_time_1_worker = elapsed_seconds
            speedup = 1.00
        else:
            speedup = base_time_1_worker / elapsed_seconds

        results.append({
            "workers": workers,
            "time_s": elapsed_seconds,
            "throughput": throughput,
            "speedup": speedup,
        })
        print(f"    Completed in {elapsed_seconds:.2f}s | Throughput: {throughput:.2f} files/s | Speedup: {speedup:.2f}x")

    # Restore default scale to 4
    scale_workers(4)

    # Output Clean Results Table
    print("\n" + "=" * 65)
    print("                    BENCHMARK RESULTS SUMMARY")
    print("=" * 65)
    print(f"{'Workers':<10} {'Time(s)':<12} {'Throughput (files/s)':<22} {'Speedup':<10}")
    print("-" * 65)
    for r in results:
        print(f"{r['workers']:<10} {r['time_s']:<12.2f} {r['throughput']:<22.2f} {r['speedup']:.2f}x")
    print("=" * 65)

    print("\nAnalysis of Non-Linear Speedup (Amdahl's Law):")
    print("1. Serial Overhead: The repository worker extracts the ZIP and scans directories sequentially.")
    print("2. Broker Overhead: RabbitMQ message serialization, ACK transmission, and TCP context switching.")
    print("3. Database Contention: Concurrent workers write status updates and findings to PostgreSQL.")
    print("4. Network Latency: Round trips between FastAPI, RabbitMQ, PostgreSQL, and LLM services.")


if __name__ == "__main__":
    main()

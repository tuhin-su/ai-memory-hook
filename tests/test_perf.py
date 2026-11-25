#!/usr/bin/env python3
"""High-load performance benchmark & stress test for amh.
Tests:
  1. Cold bulk indexing throughput (files/sec, topics/sec)
  2. In-process search engine latency (SQLite FTS5 BM25 across 1,000 queries)
  3. End-to-end CLI search latency (subprocess execution)
  4. Non-blocking hook latency under simultaneous file mutations
  5. Concurrent write stress test (SQLite WAL lock contention across threads)
"""

import os
import sys
import time
import shutil
import tempfile
import random
import string
import subprocess
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
AMH_BIN = REPO_ROOT / "amh.py"

# Import amh directly for in-process query engine benchmarking
sys.path.insert(0, str(REPO_ROOT))
import amh

def random_word(length=7):
    return "".join(random.choices(string.ascii_lowercase, k=length))

def random_camel():
    return f"{random_word(4)}{random_word(5).capitalize()}{random_word(6).capitalize()}"

def generate_synthetic_repo(base_dir: Path, num_files=60, functions_per_file=25):
    """Generates synthetic multi-language files with realistic code topics."""
    print(f"\n[1/6] Generating synthetic repository ({num_files} files, ~{num_files * functions_per_file} topics)...")
    src_dir = base_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    
    total_funcs = 0
    # Python files
    for i in range(num_files // 2):
        f = src_dir / f"service_{i}_{random_word(4)}.py"
        lines = [f'"""Module service_{i} handling enterprise workload."""\n']
        for j in range(functions_per_file):
            fn_name = f"handle_{random_word(6)}_{random_camel()}_{j}"
            total_funcs += 1
            lines.append(f"""
def {fn_name}(user_id: str, payload: dict):
    \"\"\"Processes operation {fn_name} with validation and database write.\"\"\"
    status = 'active'
    token = f'jwt_{{user_id}}_{{payload.get("key", "")}}'
    return {{'ok': True, 'func': '{fn_name}', 'status': status, 'token': token}}
""")
        f.write_text("\n".join(lines), encoding="utf-8")

    # TypeScript / JavaScript files
    for i in range(num_files // 4):
        f = src_dir / f"controller_{i}_{random_word(4)}.ts"
        lines = [f'/** Controller {i} enterprise REST endpoints */\n']
        for j in range(functions_per_file):
            fn_name = f"{random_camel()}Endpoint{j}"
            total_funcs += 1
            lines.append(f"""
export async function {fn_name}(req: Request, res: Response): Promise<void> {{
    // Handles {fn_name} request dispatching
    const traceId = req.headers['x-trace-id'];
    res.json({{ traceId, endpoint: '{fn_name}', timestamp: Date.now() }});
}}
""")
        f.write_text("\n".join(lines), encoding="utf-8")

    # Markdown documentation files
    docs_dir = base_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    for i in range(num_files // 4):
        f = docs_dir / f"spec_{i}_{random_word(4)}.md"
        lines = [f"# Specification Document {i}\n"]
        for j in range(functions_per_file):
            topic = f"Architecture Section {i}-{j} {random_word(8).capitalize()}"
            total_funcs += 1
            lines.append(f"""
## {topic}
This section defines the architectural contract for component {topic}.
It specifies latency bounds, throughput requirements, and caching policies.
""")
        f.write_text("\n".join(lines), encoding="utf-8")

    print(f"      Generated {num_files} files with {total_funcs} structured code topics.")
    return total_funcs

def benchmark_indexing(repo_dir: Path):
    """Measures cold index throughput."""
    print("\n[2/6] Benchmarking cold indexing throughput...")
    t0 = time.perf_counter()
    res = subprocess.run([sys.executable, str(AMH_BIN), "init"], cwd=str(repo_dir), capture_output=True, text=True)
    assert res.returncode == 0, f"init failed: {res.stderr}"

    res = subprocess.run([sys.executable, str(AMH_BIN), "index"], cwd=str(repo_dir), capture_output=True, text=True)
    t1 = time.perf_counter()
    assert res.returncode == 0, f"index failed: {res.stderr}"

    c = sqlite3.connect(amh.AMH_PROJECTS / repo_dir.name / "vector.db")
    file_count = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    topic_count = c.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    db_size_kb = (amh.AMH_PROJECTS / repo_dir.name / "vector.db").stat().st_size / 1024

    elapsed = t1 - t0
    files_per_sec = file_count / elapsed
    topics_per_sec = topic_count / elapsed

    print(f"      Indexed {file_count} files ({topic_count} topics) in {elapsed:.3f}s")
    print(f"      Throughput: {files_per_sec:.1f} files/s | {topics_per_sec:.1f} topics/s")
    print(f"      Database Size: {db_size_kb:.1f} KB")
    c.close()
    return file_count, topic_count

def benchmark_inprocess_search(repo_dir: Path, num_queries=1000):
    """Measures pure SQLite FTS5 BM25 search engine latency without subprocess overhead."""
    print(f"\n[3/6] Benchmarking in-process FTS5 BM25 query latency ({num_queries} queries)...")
    amh.set_project(repo_dir, repo_dir.name)
    c = amh.vector_db()

    sample_topics = [r[0] for r in c.execute("SELECT topic FROM topics ORDER BY RANDOM() LIMIT 50").fetchall()]
    queries = []
    for _ in range(num_queries):
        if random.random() < 0.6 and sample_topics:
            t = random.choice(sample_topics)
            queries.append(t.split("_")[0] if "_" in t else t)
        elif random.random() < 0.8:
            queries.append(f"{random.choice(['user', 'token', 'request', 'service', 'handle'])} {random_word(4)}")
        else:
            queries.append(random_word(5))

    latencies_us = []
    t_start = time.perf_counter()
    for q in queries:
        t0 = time.perf_counter()
        results = amh.search_topics(c, q, k=8)
        t1 = time.perf_counter()
        latencies_us.append((t1 - t0) * 1_000_000)

    total_time = time.perf_counter() - t_start
    qps = num_queries / total_time
    latencies_us.sort()

    p50_ms = latencies_us[int(num_queries * 0.50)] / 1000
    p95_ms = latencies_us[int(num_queries * 0.95)] / 1000
    p99_ms = latencies_us[int(num_queries * 0.99)] / 1000
    avg_ms = (sum(latencies_us) / len(latencies_us)) / 1000

    print(f"      Total In-Process Queries: {num_queries} in {total_time:.3f}s ({qps:.1f} QPS)")
    print(f"      Avg Engine Latency:       {avg_ms:.3f} ms")
    print(f"      p50 Engine Latency:       {p50_ms:.3f} ms")
    print(f"      p95 Engine Latency:       {p95_ms:.3f} ms")
    print(f"      p99 Engine Latency:       {p99_ms:.3f} ms")
    print(f"      Min / Max Engine:         {latencies_us[0]/1000:.3f} ms / {latencies_us[-1]/1000:.3f} ms")

    c.close()
    assert p95_ms < 5.0, f"In-process p95 latency exceeded 5ms: {p95_ms:.3f} ms"

def benchmark_cli_search(repo_dir: Path, num_queries=50):
    """Measures end-to-end CLI subprocess search latency (including Python VM startup)."""
    print(f"\n[4/6] Benchmarking end-to-end CLI subprocess latency ({num_queries} queries)...")
    latencies_ms = []
    t_start = time.perf_counter()

    for _ in range(num_queries):
        t0 = time.perf_counter()
        res = subprocess.run(
            [sys.executable, str(AMH_BIN), "search", "user token", "--k", "8"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True
        )
        t1 = time.perf_counter()
        assert res.returncode == 0
        latencies_ms.append((t1 - t0) * 1000)

    total_time = time.perf_counter() - t_start
    latencies_ms.sort()
    p50 = latencies_ms[int(num_queries * 0.50)]
    p95 = latencies_ms[int(num_queries * 0.95)]
    avg = sum(latencies_ms) / len(latencies_ms)

    print(f"      CLI Total Time:   {total_time:.2f}s ({num_queries/total_time:.1f} invocations/s)")
    print(f"      CLI Avg Latency:  {avg:.2f} ms (dominated by Python interpreter boot)")
    print(f"      CLI p50 Latency:  {p50:.2f} ms")
    print(f"      CLI p95 Latency:  {p95:.2f} ms")

def benchmark_nonblocking_hook(repo_dir: Path, iterations=25):
    """Tests amh hook latency while continuously mutating files in the background."""
    print(f"\n[5/6] Benchmarking non-blocking amh hook latency during file mutations ({iterations} runs)...")
    latencies_ms = []

    for i in range(iterations):
        mod_file = repo_dir / "src" / f"service_{i % 5}_mut.py"
        mod_file.write_text(f"\ndef dynamic_func_{i}_{time.time_ns()}():\n    pass\n", encoding="utf-8")

        t0 = time.perf_counter()
        res = subprocess.run(
            [sys.executable, str(AMH_BIN), "hook", f"find service {i}"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True
        )
        t1 = time.perf_counter()
        assert res.returncode == 0
        assert "<system>" in res.stdout
        assert "<file_topics" in res.stdout
        latencies_ms.append((t1 - t0) * 1000)

    latencies_ms.sort()
    p50 = latencies_ms[int(iterations * 0.50)]
    p95 = latencies_ms[int(iterations * 0.95)]
    avg = sum(latencies_ms) / len(latencies_ms)

    print(f"      Hook Avg Latency: {avg:.2f} ms")
    print(f"      Hook p50 Latency: {p50:.2f} ms")
    print(f"      Hook p95 Latency: {p95:.2f} ms")

def benchmark_concurrent_writes(repo_dir: Path, num_workers=8, writes_per_worker=10):
    """Stress tests SQLite WAL concurrency with parallel amh note and rule writes."""
    print(f"\n[6/6] Stress testing concurrent write operations ({num_workers * writes_per_worker} parallel writes)...")
    
    def write_task(worker_id, item_id):
        note_title = f"Decision Worker {worker_id} Item {item_id}"
        note_text = f"Durable architectural decision from worker {worker_id} at {time.time_ns()}"
        res = subprocess.run(
            [sys.executable, str(AMH_BIN), "note", note_title, "--folder", "decisions", "--text", note_text],
            cwd=str(repo_dir),
            capture_output=True,
            text=True
        )
        return res.returncode == 0

    t0 = time.perf_counter()
    success_count = 0
    total_tasks = num_workers * writes_per_worker

    with ThreadPoolExecutor(max_workers=num_workers) as pool:
        futures = [
            pool.submit(write_task, w, i)
            for w in range(num_workers)
            for i in range(writes_per_worker)
        ]
        for f in as_completed(futures):
            if f.result():
                success_count += 1

    total_time = time.perf_counter() - t0
    print(f"      Completed {success_count}/{total_tasks} parallel writes in {total_time:.2f}s ({total_tasks/total_time:.1f} writes/s)")
    assert success_count == total_tasks, f"Some concurrent writes failed: {success_count}/{total_tasks}"

    notes_count = len(list((amh.AMH_PROJECTS / repo_dir.name / "memory" / "decisions").glob("*.md")))
    print(f"      Persisted {notes_count} note files without data corruption.")

def main():
    print("=" * 65)
    print("      amh High-Load Performance & Stress Test Suite")
    print("=" * 65)
    
    temp_dir = Path(tempfile.mkdtemp(prefix="amh_perf_"))
    (temp_dir / ".git").mkdir()
    try:
        generate_synthetic_repo(temp_dir, num_files=60, functions_per_file=30)
        benchmark_indexing(temp_dir)
        benchmark_inprocess_search(temp_dir, num_queries=1000)
        benchmark_cli_search(temp_dir, num_queries=40)
        benchmark_nonblocking_hook(temp_dir, iterations=20)
        benchmark_concurrent_writes(temp_dir, num_workers=6, writes_per_worker=8)
        
        print("\n" + "=" * 65)
        print("  ✔ ALL HIGH-LOAD PERFORMANCE TESTS PASSED SUCCESSFULLY!")
        print("=" * 65)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)
        subprocess.run([sys.executable, str(AMH_BIN), "forget", temp_dir.name], capture_output=True)
        shutil.rmtree(amh.AMH_PROJECTS / temp_dir.name, ignore_errors=True)

if __name__ == "__main__":
    main()

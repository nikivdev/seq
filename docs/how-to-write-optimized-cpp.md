# How To Write Optimized C++ (Low Latency Playbook)

This document is the working playbook for making `seq` and related systems as low-latency as possible.

It is based on:
- Recent WebKit C++ commit style and optimization patterns.
- Direct review of current hot paths in:
  - `~/code/stream`
  - `~/code/seq`

## Core Principles

1. Optimize measured bottlenecks, not assumptions.
2. Remove unnecessary work before micro-optimizing instructions.
3. Keep hot paths short, predictable, and allocation-free.
4. Design data structures for cache locality and contention avoidance.
5. Isolate cold/error paths from hot paths.
6. Make performance changes with explicit rollback/verification criteria.

## What Recent WebKit C++ Style Shows

WebKit does not chase branchless code blindly. It prefers:

1. Fast-path early return.
2. Cheap guards before expensive work.
3. Data-flow and register-pressure clarity.
4. Correctness and invariants first, then speed.
5. Avoiding redundant updates/recomputation.

Concrete examples:

- Case-sensitive fast return before slower comparisons:
  - `/tmp/webkit-history-17434/Source/WebCore/cssjit/SelectorCompiler.cpp:3639`
- Cheap pointer and length checks before case-insensitive call:
  - `/tmp/webkit-history-17434/Source/WebCore/cssjit/SelectorCompiler.cpp:3644`
  - `/tmp/webkit-history-17434/Source/WebCore/cssjit/SelectorCompiler.cpp:3656`
- Register usage made explicit in helper calculations:
  - `/tmp/webkit-history-17434/Source/WebCore/cssjit/SelectorCompiler.cpp:1690`
- Hotness/call-count propagation for inlining decisions:
  - `/tmp/webkit-history-17434/Source/JavaScriptCore/wasm/WasmInliningDecision.cpp:149`
- Skip redundant work based on changed bounds:
  - `/tmp/webkit-history-17434/Source/WebKit/WebProcess/GPU/graphics/Model/RemoteMeshProxy.cpp:137`
- Direct safe accessor (`document().settings()`) rather than fragile chain:
  - `/tmp/webkit-history-17434/Source/WebCore/rendering/RenderObjectDocument.h:48`

Takeaway: branch reduction and cache-aware layout matter, but only inside a broader strategy of reducing total work and preserving predictability.

## C++ Optimization Rules We Will Use

1. Hot-path rules:
- No heap allocation in steady-state hot path.
- No string formatting/parsing in hot path.
- No locks in per-item hot path unless proven acceptable under load.
- Use `likely/unlikely` only after profiling confirms branch bias.

2. Data/layout rules:
- Prefer contiguous buffers and ring buffers over node-based containers.
- Use fixed-capacity structures where bounds are known.
- Keep frequently accessed fields adjacent and cache-line conscious.
- Avoid false sharing for write-hot atomics/counters.

3. Control-flow rules:
- Fail fast on invalid inputs.
- Separate fast path from slow/error path into separate functions.
- Use cheap prechecks before expensive calls.

4. Concurrency rules:
- Prefer SPSC/MPSC lock-free queues where ownership pattern is known.
- Batch cross-thread handoff.
- Minimize mutex hold time and lock frequency.

5. Build/toolchain rules:
- Release defaults must be optimized (`-O3`/LTO/PGO where appropriate).
- Keep sanitizer builds for correctness, not production latency numbers.

## Current Hotspots (stream)

1. Full payload copy per frame in queue mode:
- `~/code/stream/src/embedded_sender.cpp:93`
- `~/code/stream/src/embedded_sender.cpp:123`

2. Mutex + condition-variable queue in frame path:
- `~/code/stream/src/embedded_sender.cpp:20`

3. Per-frame payload allocation on receive:
- `~/code/stream/src/net/transport.cpp:278`

4. Large hand-rolled JSON parsing and map/string churn in signaling control path:
- `~/code/stream/apps/stream_server/signaling.cpp:218`

## Current Hotspots (seq)

1. Global mutex acquisition on every async writer push:
- `~/code/seq/cli/cpp/src/clickhouse.cpp:460`

2. Vector front-erase in batching path (data movement):
- `~/code/seq/cli/cpp/src/clickhouse.cpp:597`

3. Many queue threshold checks per flush wait cycle:
- `~/code/seq/cli/cpp/src/clickhouse.cpp:670`

4. Environment reconstruction per spawn in capture path:
- `~/code/seq/cli/cpp/src/process.cpp:144`
- `~/code/seq/cli/cpp/src/process.cpp:195`

5. Control-plane/read paths with avoidable dynamic work:
- `~/code/seq/cli/cpp/src/action_pack_server.cpp:42`

## Massive Optimization Program (Step-by-Step)

## Implemented In This Iteration

1. `seq` async writer drain path moved away from front-erase behavior:
- queue drain now uses per-queue head indices and periodic compaction instead of per-batch `erase(begin, ...)`.
- this removes O(n) front-shift on every flush batch and keeps data movement amortized.
- pending row accounting now uses a single maintained counter instead of summing all queues on each push.
- flush wake logic now uses precomputed readiness flags, and explicit `Flush()` requests no longer wait for timeout.

2. `stream` sender queue moved from `std::deque` to a bounded ring buffer:
- queue storage is now contiguous.
- push/pop are index-based (`head/tail/size`) with fixed capacity.
- this removes node-based container churn and improves cache locality in queue mode.
- queue mode now reuses staging payload buffers across frames instead of constructing a fresh payload vector each callback.

3. Added `seq perf-smoke` command for repeatable perf snapshots:
- command: `seq perf-smoke [samples] [sleep_ms]`
- default: `20` samples at `100ms`.
- returns JSON deltas for writer counters (`push_calls`, `wake_count`, `flush_count`, `total/avg_flush_us`, inserts/errors) plus current tail values.

## Step 0: Baseline and Guardrails

Goal: make every optimization measurable and reversible.

Tasks:
1. Add baseline measurements for:
- `stream`: capture->enqueue/send->socket write.
- `seq`: push->flush wake->insert.
2. Track p50/p95/p99 and max for latency.
3. Record throughput and drop rates.
4. Add a repeatable benchmark command set.

Exit criteria:
1. Stable benchmark scripts and outputs checked into repo.
2. We can compare before/after for each subsequent step.

Current baseline endpoint:
1. `seq perf` now includes `trace_writer` JSON with:
- `push_calls`, `wake_count`, `flush_count`
- `last/avg/max/total_flush_us`
- `last/max_pending_rows`
- `inserted_count`, `error_count`

## Step 1: stream Data-Movement Elimination

Goal: remove unnecessary copies and lock contention in frame pipeline.

Tasks:
1. Make direct sender path default for low-latency mode.
2. Replace `PacketQueue` deque+mutex with bounded SPSC ring buffer.
3. Use buffer ownership transfer (or pooled buffers) instead of payload copy.
4. Pre-allocate frame metadata + payload structures.

Expected impact:
1. Lower tail latency spikes.
2. Lower CPU per frame.
3. Fewer drops under burst.

## Step 2: seq AsyncWriter Re-architecture

Goal: reduce per-event push overhead and flush jitter.

Tasks:
1. Replace per-push global mutex with low-contention queueing design.
2. Replace front-erase batching with index/ring based draining.
3. Reuse batch vectors and pre-reserve capacities.
4. Keep flush thread wake policy simple and branch-light.

Expected impact:
1. Lower per-event overhead.
2. Better p99 flush latency.
3. Higher sustained insert throughput.

## Step 3: Control-Plane and Process Path Cleanup

Goal: remove allocation and parsing overhead where unnecessary.

Tasks:
1. Cache environment baseline for `run_capture` and apply deltas only.
2. Reduce string churn in high-frequency request paths.
3. Keep JSON work out of hot loops or switch to zero-copy parse strategy where safe.

Expected impact:
1. Lower command-dispatch overhead.
2. Better responsiveness under concurrent control traffic.

## Step 4: Build and CPU-Level Tuning

Goal: ensure optimized code generation for production binaries.

Tasks:
1. Validate release profiles for `stream` and `seq`.
2. Enable/verify LTO and consider PGO workflow.
3. Evaluate architecture flags by deployment target (avoid overfitting dev machine).

Expected impact:
1. Additional single-digit to double-digit perf gains depending on workload.

## Step 5: Validation and Regression Safety

Goal: lock in wins and prevent regressions.

Tasks:
1. Add perf regression checks in CI/nightly where possible.
2. Keep benchmark dashboards/artifacts.
3. Document each optimization with measured delta and rollback note.

Exit criteria:
1. Measured p99 improvements preserved across releases.

## Branching, Cache, and Data Structures: Practical Guidance

1. Branching:
- Do not force branchless code if it adds extra instructions or hurts clarity.
- Prefer making common branch outcomes obvious and fast.

2. Cache:
- L1 optimization is usually about contiguous data, reuse, and avoiding pointer chasing.
- The biggest wins usually come from fewer allocations and fewer data copies.

3. Data structures:
- For bounded producer/consumer paths: ring buffer beats deque/list.
- For hot lookup paths: prefer compact structures and stable keys.

## What We Are Doing Next

Execution order:
1. Step 0 baseline (required before large rewrites).
2. Step 1 `stream` pipeline optimizations.
3. Step 2 `seq` async writer optimizations.
4. Step 3 control-plane cleanup.
5. Step 4 toolchain tuning.
6. Step 5 regression locking.

This is now the source of truth for the optimization program.

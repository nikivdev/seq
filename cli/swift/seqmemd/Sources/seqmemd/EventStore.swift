import Foundation
import WaxCore

struct LatencyAgg: Sendable {
    var count: UInt64 = 0
    var sumUs: UInt64 = 0
    var maxUs: UInt64 = 0

    mutating func add(_ us: UInt64) {
        count &+= 1
        sumUs &+= us
        if us > maxUs { maxUs = us }
    }

    var avgUs: UInt64 {
        guard count > 0 else { return 0 }
        return sumUs / count
    }
}

actor EventStore {
    private let waxURL: URL
    private let waxOptions: WaxOptions
    private var wax: Wax? = nil

    private var pending: [Data] = []
    private var lastCommit: ContinuousClock.Instant = .now
    private let clock = ContinuousClock()

    private var counters: [String: UInt64] = [:]
    private var latencies: [String: LatencyAgg] = [:]
    private var start: Date = Date()

    init(waxURL: URL, waxOptions: WaxOptions) {
        self.waxURL = waxURL
        self.waxOptions = waxOptions
    }

    func openIfNeeded() async throws {
        if wax != nil { return }
        let fm = FileManager.default
        if fm.fileExists(atPath: waxURL.path) {
            wax = try await Wax.open(at: waxURL, options: waxOptions)
        } else {
            wax = try await Wax.create(at: waxURL, walSize: 4 * 1024 * 1024, options: waxOptions)
        }
    }

    func ingest(raw: Data) async {
        pending.append(raw)

        // Cheap name/duration extraction for in-memory counters. If parsing fails, just count "unknown".
        let s = String(data: raw, encoding: .utf8) ?? ""
        let name = extractJSONStringValue(in: s, key: "name") ?? "unknown"
        counters[name, default: 0] &+= 1

        if let durStr = extractJSONNumberValue(in: s, key: "dur_us"),
           let dur = UInt64(durStr) {
            var agg = latencies[name] ?? LatencyAgg()
            agg.add(dur)
            latencies[name] = agg
        }

        await maybeFlush()
    }

    func metricsJSON() async -> String {
        let uptime = Date().timeIntervalSince(start)
        var obj: [String: Any] = [:]
        obj["uptime_seconds"] = uptime
        obj["events_total"] = counters.values.reduce(0, &+)
        obj["counts"] = counters

        var lat: [String: Any] = [:]
        for (k, v) in latencies {
            lat[k] = [
                "count": v.count,
                "avg_us": v.avgUs,
                "max_us": v.maxUs,
            ]
        }
        obj["latency_us"] = lat
        return jsonString(obj) ?? "{\"error\":\"encode\"}"
    }

    func tailJSON(maxEvents: Int) async -> String {
        do {
            try await flushAndCommit()
            guard let wax else { return "{\"events\":[]}" }
            let metas = await wax.frameMetas()
            if metas.isEmpty { return "{\"events\":[]}" }
            let n = max(0, min(maxEvents, metas.count))
            let start = metas.count - n
            let ids = (start..<metas.count).map { UInt64($0) }
            let contents = try await wax.frameContents(frameIds: ids)
            let ordered = ids.compactMap { id -> Any? in
                guard let data = contents[id] else { return nil }
                if let obj = try? JSONSerialization.jsonObject(with: data, options: []) {
                    return obj
                }
                // Not valid JSON; wrap as string for debuggability.
                return ["raw": String(data: data, encoding: .utf8) ?? ""]
            }
            return jsonString(["events": ordered]) ?? "{\"events\":[]}"
        } catch {
            return jsonString(["error": String(describing: error)]) ?? "{\"error\":\"tail\"}"
        }
    }

    private func maybeFlush() async {
        // Flush aggressively enough to keep ingestion cheap, but amortize Wax actor and I/O work.
        if pending.count >= 128 {
            await flushBatch()
            return
        }

        let now = clock.now
        if lastCommit.duration(to: now) > Duration.milliseconds(50) {
            await flushBatch()
        }
    }

    private func flushBatch() async {
        guard !pending.isEmpty else { return }
        let batch = pending
        pending.removeAll(keepingCapacity: true)

        do {
            try await openIfNeeded()
            guard let wax else { return }
            let options = Array(repeating: FrameMetaSubset(), count: batch.count)
            _ = try await wax.putBatch(batch, options: options, compression: .plain)
        } catch {
            // Drop on error; this daemon should never block seqd on observability.
        }
    }

    private func flushAndCommit() async throws {
        try await openIfNeeded()
        await flushBatch()
        if let wax {
            try await wax.commit()
        }
        lastCommit = clock.now
    }
}

private func extractJSONStringValue(in s: String, key: String) -> String? {
    // Very small and fast extractor for `"key":"value"` with no escape handling.
    // This is for coarse counters only.
    let needle = "\"\(key)\":\""
    guard let r = s.range(of: needle) else { return nil }
    let start = r.upperBound
    guard let end = s[start...].firstIndex(of: "\"") else { return nil }
    return String(s[start..<end])
}

private func extractJSONNumberValue(in s: String, key: String) -> String? {
    // Extract `"key":1234` or `"key": 1234`
    let needle = "\"\(key)\":"
    guard let r = s.range(of: needle) else { return nil }
    var i = r.upperBound
    while i < s.endIndex, s[i] == " " { i = s.index(after: i) }
    var j = i
    while j < s.endIndex, s[j].isNumber { j = s.index(after: j) }
    guard j > i else { return nil }
    return String(s[i..<j])
}

private func jsonString(_ obj: Any) -> String? {
    guard JSONSerialization.isValidJSONObject(obj) else { return nil }
    guard let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]) else { return nil }
    return String(data: data, encoding: .utf8)
}

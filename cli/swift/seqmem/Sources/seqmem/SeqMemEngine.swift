import Foundation
import os
import WaxCore
import Darwin

private struct LatencyAgg: Sendable {
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

private func hexString(_ bytes: Data) -> String {
    // Hot-ish path: avoid String(format:...) loops.
    let table: [UInt8] = Array("0123456789abcdef".utf8)
    var out = [UInt8]()
    out.reserveCapacity(bytes.count * 2)
    for b in bytes {
        out.append(table[Int(b >> 4)])
        out.append(table[Int(b & 0x0F)])
    }
    return String(decoding: out, as: UTF8.self)
}

private func appendJSONString(_ out: inout Data, _ bytes: UnsafeRawBufferPointer) {
    out.append(0x22) // "
    for b in bytes {
        switch b {
        case 0x22: // "
            out.append(contentsOf: [0x5C, 0x22])
        case 0x5C: // \
            out.append(contentsOf: [0x5C, 0x5C])
        case 0x0A: // \n
            out.append(contentsOf: [0x5C, 0x6E])
        case 0x0D: // \r
            out.append(contentsOf: [0x5C, 0x72])
        case 0x09: // \t
            out.append(contentsOf: [0x5C, 0x74])
        default:
            if b < 0x20 {
                // Encode as \u00XX
                let hex = String(format: "\\u%04x", b)
                out.append(contentsOf: hex.utf8)
            } else {
                out.append(b)
            }
        }
    }
    out.append(0x22) // "
}

private func appendJSONString(_ out: inout Data, _ s: String) {
    out.append(0x22) // "
    for b in s.utf8 {
        switch b {
        case 0x22: // "
            out.append(contentsOf: [0x5C, 0x22])
        case 0x5C: // \
            out.append(contentsOf: [0x5C, 0x5C])
        case 0x0A: // \n
            out.append(contentsOf: [0x5C, 0x6E])
        case 0x0D: // \r
            out.append(contentsOf: [0x5C, 0x72])
        case 0x09: // \t
            out.append(contentsOf: [0x5C, 0x74])
        default:
            if b < 0x20 {
                let hex = String(format: "\\u%04x", b)
                out.append(contentsOf: hex.utf8)
            } else {
                out.append(b)
            }
        }
    }
    out.append(0x22) // "
}

// Bridge to native ClickHouse writer (resolved at runtime via dlsym).
// If the host binary links libseqch, we use the zero-cost native binary protocol.
// Otherwise we fall back to the existing JSON file-append path.
private final class CHBridge: @unchecked Sendable {
    static let shared = CHBridge()

    fileprivate typealias CreateFn = @convention(c) (UnsafePointer<CChar>, UInt16, UnsafePointer<CChar>) -> OpaquePointer?
    fileprivate typealias PushMemFn = @convention(c) (OpaquePointer?, UInt64, UInt64, UInt8,
        UnsafePointer<CChar>, UnsafePointer<CChar>, UnsafePointer<CChar>,
        UnsafePointer<CChar>, UnsafePointer<CChar>?) -> Void
    fileprivate typealias FlushFn = @convention(c) (OpaquePointer?) -> Void
    fileprivate typealias DestroyFn = @convention(c) (OpaquePointer?) -> Void

    fileprivate let createWriter: CreateFn?
    fileprivate let pushMemEvent: PushMemFn?
    fileprivate let flush: FlushFn?
    fileprivate let destroyWriter: DestroyFn?

    private init() {
        let handle = dlopen(nil, RTLD_NOW)
        func lookup<T>(_ name: String) -> T? {
            guard let sym = dlsym(handle, name) else { return nil }
            return unsafeBitCast(sym, to: T.self)
        }
        createWriter = lookup("seq_ch_writer_create")
        pushMemEvent = lookup("seq_ch_push_mem_event")
        flush = lookup("seq_ch_flush")
        destroyWriter = lookup("seq_ch_writer_destroy")
    }

    var available: Bool { createWriter != nil && pushMemEvent != nil }
}

private enum CHMode {
    case native
    case mirror
    case file
    case off

    static func fromEnv(_ raw: String?) -> CHMode {
        guard let raw, !raw.isEmpty else { return .file }
        switch raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "mirror", "dual":
            return .mirror
        case "file", "spool", "local-file":
            return .file
        case "off", "none", "disabled":
            return .off
        case "native", "local", "remote", "remote-only":
            return .native
        default:
            return .file
        }
    }

    var wantsNative: Bool {
        switch self {
        case .native, .mirror:
            return true
        case .file, .off:
            return false
        }
    }

}

private enum SeqMemKinds {
    static let session = "seqmem.session"
    static let event = "seqmem.event"
}

private enum SeqMemMetaKeys {
    static let schemaVersion = "seqmem_schema_v"
    static let sessionId = "seqmem_session_id"
    static let eventId = "seqmem_event_id"
    static let contentHash = "seqmem_content_hash"
    static let name = "seqmem_name"
    static let subject = "seqmem_subject"
}

private struct ClickHouseRowV1 {
    static func encodeJSONEachRow(
        tsMs: UInt64,
        durUs: UInt64,
        ok: UInt8,
        nameBytes: UnsafeRawPointer,
        nameLen: Int,
        subjectBytes: UnsafeRawPointer?,
        subjectLen: Int,
        sessionId: String,
        eventIdHex: String,
        contentHashHex: String
    ) -> Data {
        var out = Data()
        out.reserveCapacity(256 + nameLen + max(0, subjectLen))

        out.append(contentsOf: "{\"ts_ms\":".utf8)
        out.append(contentsOf: String(tsMs).utf8)

        out.append(contentsOf: ",\"dur_us\":".utf8)
        out.append(contentsOf: String(durUs).utf8)

        out.append(contentsOf: ",\"ok\":".utf8)
        out.append(contentsOf: (ok != 0 ? "true" : "false").utf8)

        out.append(contentsOf: ",\"session_id\":".utf8)
        appendJSONString(&out, sessionId)

        out.append(contentsOf: ",\"event_id\":".utf8)
        appendJSONString(&out, eventIdHex)

        out.append(contentsOf: ",\"content_hash\":".utf8)
        appendJSONString(&out, contentHashHex)

        out.append(contentsOf: ",\"name\":".utf8)
        let nameBuf = UnsafeRawBufferPointer(start: nameBytes, count: nameLen)
        appendJSONString(&out, nameBuf)

        if let subjectBytes, subjectLen > 0 {
            out.append(contentsOf: ",\"subject\":".utf8)
            let subjBuf = UnsafeRawBufferPointer(start: subjectBytes, count: subjectLen)
            appendJSONString(&out, subjBuf)
        }

        out.append(contentsOf: "}\n".utf8)
        return out
    }
}

// Binary frame format stored in Wax:
// magic(u32) "SEQM" + version(u16) + reserved(u16)
// ts_ms(u64) + dur_us(u64) + ok(u8)
// name_len(u32) + subject_len(u32)
// name bytes + subject bytes (UTF-8)
private enum RecordV1 {
    static let magic: UInt32 = 0x4D_51_45_53 // "SEQM" little-endian
    static let version: UInt16 = 1

    static func encode(
        tsMs: UInt64,
        durUs: UInt64,
        ok: UInt8,
        nameBytes: UnsafeRawPointer,
        nameLen: Int,
        subjectBytes: UnsafeRawPointer?,
        subjectLen: Int
    ) -> Data {
        let subjLen = max(0, subjectLen)
        var data = Data()
        data.reserveCapacity(4 + 2 + 2 + 8 + 8 + 1 + 4 + 4 + nameLen + subjLen)

        data.appendLE(magic)
        data.appendLE(version)
        data.appendLE(UInt16(0))
        data.appendLE(tsMs)
        data.appendLE(durUs)
        data.appendU8(ok)
        data.appendLE(UInt32(nameLen))
        data.appendLE(UInt32(subjLen))
        data.append(nameBytes.assumingMemoryBound(to: UInt8.self), count: nameLen)
        if let subjectBytes, subjLen > 0 {
            data.append(subjectBytes.assumingMemoryBound(to: UInt8.self), count: subjLen)
        }
        return data
    }

    static func decode(_ data: Data) -> [String: Any]? {
        // Minimal validation, best-effort; never throw in observability path.
        return data.withUnsafeBytes { raw -> [String: Any]? in
            var p = raw.baseAddress
            var n = raw.count
            func readU8() -> UInt8? {
                guard let p, n >= 1 else { return nil }
                let v = p.load(as: UInt8.self)
                selfAdvance(1)
                return v
            }
            func readU16() -> UInt16? {
                guard let p, n >= 2 else { return nil }
                let v = p.load(as: UInt16.self).littleEndian
                selfAdvance(2)
                return v
            }
            func readU32() -> UInt32? {
                guard let p, n >= 4 else { return nil }
                let v = p.load(as: UInt32.self).littleEndian
                selfAdvance(4)
                return v
            }
            func readU64() -> UInt64? {
                guard let p, n >= 8 else { return nil }
                let v = p.load(as: UInt64.self).littleEndian
                selfAdvance(8)
                return v
            }
            func readBytes(_ len: Int) -> UnsafeRawBufferPointer? {
                guard let p, n >= len, len >= 0 else { return nil }
                let out = UnsafeRawBufferPointer(start: p, count: len)
                selfAdvance(len)
                return out
            }
            func selfAdvance(_ k: Int) {
                p = p?.advanced(by: k)
                n -= k
            }

            guard let magic = readU32(), magic == RecordV1.magic else { return nil }
            guard let ver = readU16(), ver == RecordV1.version else { return nil }
            _ = readU16() // reserved
            guard let tsMs = readU64() else { return nil }
            guard let durUs = readU64() else { return nil }
            guard let ok = readU8() else { return nil }
            guard let nameLen32 = readU32() else { return nil }
            guard let subjLen32 = readU32() else { return nil }
            let nameLen = Int(nameLen32)
            let subjLen = Int(subjLen32)
            guard let nameBuf = readBytes(nameLen) else { return nil }
            let subjBuf = subjLen > 0 ? readBytes(subjLen) : nil

            let name = String(decoding: nameBuf, as: UTF8.self)
            let subject = subjBuf.map { String(decoding: $0, as: UTF8.self) }

            var obj: [String: Any] = [
                "ts_ms": tsMs,
                "dur_us": durUs,
                "ok": ok != 0,
                "name": name,
            ]
            if let subject, !subject.isEmpty {
                obj["subject"] = subject
            }
            return obj
        }
    }
}

private actor WaxWriter {
    private let clock = ContinuousClock()
    private var url: URL
    private let options: WaxOptions
    private var wax: Wax? = nil
    private let clickhousePath: String?
    private var clickhouseFD: Int32 = -1

    private var lastCommit: ContinuousClock.Instant = .now

    private let sessionId: String
    private var sessionRootFrameId: UInt64? = nil

    private let ttlDays: Int
    private var lastMaintenance: ContinuousClock.Instant = .now

    init(url: URL, clickhousePath: String?, sessionId: String, ttlDays: Int) {
        self.url = url
        self.clickhousePath = clickhousePath
        self.options = WaxOptions(
            walFsyncPolicy: .onCommit,
            ioQueueLabel: "com.seq.mem.io",
            ioQueueQos: .utility
        )
        self.sessionId = sessionId
        self.ttlDays = max(0, ttlDays)
    }

    func configure(url: URL) {
        // Only allow reconfigure before the file is opened.
        if wax == nil {
            self.url = url
        }
    }

    struct PendingFrame {
        var tsMs: Int64
        var content: Data
        var meta: FrameMetaSubset
        var chRow: Data?
    }

    func append(batch: [PendingFrame]) async {
        guard !batch.isEmpty else { return }
        do {
            try await openIfNeeded()
            try await ensureSessionRootIfNeeded()
            guard let wax else { return }
            var metas: [FrameMetaSubset] = []
            metas.reserveCapacity(batch.count)
            var contents: [Data] = []
            contents.reserveCapacity(batch.count)
            var timestamps: [Int64] = []
            timestamps.reserveCapacity(batch.count)

            // Attach parent links at write time, after the session root exists.
            for var item in batch {
                if item.meta.parentId == nil {
                    item.meta.parentId = sessionRootFrameId
                }
                metas.append(item.meta)
                contents.append(item.content)
                timestamps.append(item.tsMs)
            }

            _ = try await wax.putBatch(contents, options: metas, compression: .plain, timestampsMs: timestamps)
            appendClickHouse(rows: batch.compactMap(\.chRow))

            // Amortize commit costs. Observability doesn't need per-event durability.
            let now = clock.now
            let elapsed = lastCommit.duration(to: now)
            if elapsed > .seconds(1) {
                try await wax.commit()
                lastCommit = now
            }

            await maintenanceTick()
        } catch {
            // Best-effort only.
        }
    }

    private func openIfNeeded() async throws {
        if wax != nil { return }
        let fm = FileManager.default
        if fm.fileExists(atPath: url.path) {
            wax = try await Wax.open(at: url, options: options)
        } else {
            _ = fm.createFile(atPath: url.path, contents: nil)
            wax = try await Wax.create(at: url, walSize: 4 * 1024 * 1024, options: options)
        }
    }

    private func ensureSessionRootIfNeeded() async throws {
        guard sessionRootFrameId == nil else { return }
        guard let wax else { return }

        // Keep payload tiny; metadata carries the useful info.
        let startedMs = Int64(Date().timeIntervalSince1970 * 1000)
        let payload = Data("{\"session_id\":\"\(sessionId)\",\"started_ts_ms\":\(startedMs)}".utf8)
        var meta = Metadata()
        meta.entries[SeqMemMetaKeys.schemaVersion] = "1"
        meta.entries[SeqMemMetaKeys.sessionId] = sessionId

        let subset = FrameMetaSubset(
            uri: "seqmem://session/\(sessionId)",
            title: nil,
            kind: SeqMemKinds.session,
            track: "seqmem",
            tags: [],
            labels: [],
            contentDates: [],
            role: .system,
            parentId: nil,
            chunkIndex: nil,
            chunkCount: nil,
            chunkManifest: nil,
            status: .active,
            supersedes: nil,
            supersededBy: nil,
            searchText: nil,
            metadata: meta
        )
        let id = try await wax.put(payload, options: subset, compression: .plain, timestampMs: startedMs)
        sessionRootFrameId = id
        try await wax.commit()
        lastCommit = clock.now
    }

    private func maintenanceTick() async {
        guard ttlDays > 0 else { return }
        guard let wax else { return }

        // Best-effort, bounded frequency.
        let now = clock.now
        if lastMaintenance.duration(to: now) < .seconds(600) { return }
        lastMaintenance = now

        let nowMs = Int64(Date().timeIntervalSince1970 * 1000)
        let cutoffMs = nowMs - Int64(ttlDays) * 86_400_000
        if cutoffMs <= 0 { return }

        do {
            // Commit first so we don't keep an ever-growing pending tail.
            try await wax.commit()
        } catch {
            // Ignore.
        }

        do {
            let frames = await wax.frameMetas()
            var deleteIds: [UInt64] = []
            deleteIds.reserveCapacity(256)
            for f in frames {
                guard f.status == .active else { continue }
                guard f.timestamp < cutoffMs else { continue }
                if f.kind == SeqMemKinds.event || f.kind == SeqMemKinds.session {
                    deleteIds.append(f.id)
                }
            }
            guard !deleteIds.isEmpty else { return }
            for id in deleteIds.prefix(4096) {
                try await wax.delete(frameId: id)
            }
            try await wax.commit()
        } catch {
            // Ignore.
        }
    }

    private func openClickHouseIfNeeded() {
        guard clickhouseFD < 0 else { return }
        guard let clickhousePath, !clickhousePath.isEmpty else { return }

        // Best-effort. Never throw; never crash.
        let dir = (clickhousePath as NSString).deletingLastPathComponent
        if !dir.isEmpty {
            try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        }

        let fd = clickhousePath.withCString { p in
            open(p, O_WRONLY | O_CREAT | O_APPEND, 0o644)
        }
        if fd >= 0 {
            clickhouseFD = fd
        }
    }

    private func appendClickHouse(rows: [Data]) {
        openClickHouseIfNeeded()
        guard clickhouseFD >= 0 else { return }
        guard !rows.isEmpty else { return }

        // Encode a bounded batch to keep worst-case work predictable.
        var out = Data()
        out.reserveCapacity(rows.count * 240)
        for row in rows {
            out.append(row)
        }
        guard !out.isEmpty else { return }

        out.withUnsafeBytes { raw in
            writeAll(fd: clickhouseFD, ptr: raw.baseAddress, len: raw.count)
        }
    }

    private func writeAll(fd: Int32, ptr: UnsafeRawPointer?, len: Int) {
        guard let ptr, len > 0 else { return }
        var off = 0
        while off < len {
            let n = Darwin.write(fd, ptr.advanced(by: off), len - off)
            if n > 0 {
                off += n
                continue
            }
            if n < 0, errno == EINTR { continue }
            break
        }
    }
}

final class SeqMemEngine: @unchecked Sendable {
    private static let ringCapacity = 2048

    private let dedupWindowMs: UInt64
    private let dedupCapacity: Int
    private let emitClickHouseRows: Bool
    private let sessionId: String
    private let chWriter: OpaquePointer?

    private struct State {
        var start = Date()
        var counters: [String: UInt64] = [:]
        var latencies: [String: LatencyAgg] = [:]

        var pending: [WaxWriter.PendingFrame] = []

        var ring: [Data] = Array(repeating: Data(), count: SeqMemEngine.ringCapacity)
        var ringWrite = 0
        var ringCount = 0

        struct DedupSlot {
            var key: Data = Data()
            var tsMs: UInt64 = 0
        }
        var dedupMap: [Data: UInt64] = [:]
        var dedupSlots: [DedupSlot] = []
        var dedupWrite = 0
    }

    private let state: OSAllocatedUnfairLock<State>
    private let writer: WaxWriter
    private let flushTimer: DispatchSourceTimer

    init(waxURL: URL) {
        let chPath = SeqMemGlobal.defaultClickHousePath()
        let env = ProcessInfo.processInfo.environment
        let chMode = CHMode.fromEnv(env["SEQ_CH_MODE"])

        // Native protocol via C bridge when enabled by mode/env.
        let bridge = CHBridge.shared
        if chMode.wantsNative, bridge.available {
            let host = env["SEQ_CH_HOST"] ?? "127.0.0.1"
            let port = UInt16(env["SEQ_CH_PORT"] ?? "") ?? 9000
            let db = env["SEQ_CH_DATABASE"] ?? "seq"
            self.chWriter = host.withCString { h in
                db.withCString { d in
                    bridge.createWriter?(h, port, d)
                }
            }
        } else {
            self.chWriter = nil
        }

        // mirror: always append local JSONEachRow when path exists.
        // native: prefer bridge, but keep file fallback if bridge unavailable.
        // file: JSONEachRow only.
        // off: disable both.
        let shouldEmitFileRows: Bool
        switch chMode {
        case .mirror, .file:
            shouldEmitFileRows = true
        case .native:
            shouldEmitFileRows = (chWriter == nil)
        case .off:
            shouldEmitFileRows = false
        }
        self.emitClickHouseRows = shouldEmitFileRows && (chPath != nil)
        if let s = env["SEQ_MEM_SESSION_ID"], !s.isEmpty {
            self.sessionId = s
        } else {
            self.sessionId = UUID().uuidString
        }

        let ttlDays: Int
        if let s = env["SEQ_MEM_TTL_DAYS"], let v = Int(s) {
            ttlDays = v
        } else {
            ttlDays = 14
        }

        self.writer = WaxWriter(url: waxURL, clickhousePath: chPath, sessionId: sessionId, ttlDays: ttlDays)

        if let s = env["SEQ_MEM_DEDUP_WINDOW_MS"], let v = UInt64(s) {
            self.dedupWindowMs = v
        } else {
            self.dedupWindowMs = 250
        }
        if let s = env["SEQ_MEM_DEDUP_CAP"], let v = Int(s) {
            self.dedupCapacity = max(0, v)
        } else {
            self.dedupCapacity = 4096
        }

        var initial = State()
        if dedupWindowMs > 0, dedupCapacity > 0 {
            initial.dedupSlots = Array(repeating: State.DedupSlot(), count: dedupCapacity)
            initial.dedupMap.reserveCapacity(dedupCapacity)
        }
        self.state = OSAllocatedUnfairLock(initialState: initial)

        let q = DispatchQueue(label: "com.seq.mem.flush", qos: .utility)
        let timer = DispatchSource.makeTimerSource(queue: q)
        timer.schedule(deadline: .now() + .milliseconds(50), repeating: .milliseconds(50))
        self.flushTimer = timer
        timer.setEventHandler { [weak self] in
            self?.flushTick()
        }
        timer.resume()
    }

    func configure(waxURL: URL) {
        Task { [writer] in
            await writer.configure(url: waxURL)
        }
    }

    func recordRequest(
        tsMs: UInt64,
        durUs: UInt64,
        ok: Bool,
        nameBytes: UnsafeRawPointer,
        nameLen: Int,
        subjectBytes: UnsafeRawPointer?,
        subjectLen: Int
    ) {
        let name = String(decoding: UnsafeRawBufferPointer(start: nameBytes, count: nameLen), as: UTF8.self)
        let subject: String? = if let subjectBytes, subjectLen > 0 {
            String(decoding: UnsafeRawBufferPointer(start: subjectBytes, count: subjectLen), as: UTF8.self)
        } else {
            nil
        }
        let sessionId = self.sessionId

        // Copy input bytes synchronously while the pointers are valid.
        let record = RecordV1.encode(
            tsMs: tsMs,
            durUs: durUs,
            ok: ok ? 1 : 0,
            nameBytes: nameBytes,
            nameLen: nameLen,
            subjectBytes: subjectBytes,
            subjectLen: subjectLen
        )

        // Content-hash without timestamp so we can dedup storage while keeping counters faithful.
        var contentBuf = Data()
        contentBuf.reserveCapacity(nameLen + max(0, subjectLen) + 8)
        contentBuf.append(nameBytes.assumingMemoryBound(to: UInt8.self), count: nameLen)
        contentBuf.append(0)
        if let subjectBytes, subjectLen > 0 {
            contentBuf.append(subjectBytes.assumingMemoryBound(to: UInt8.self), count: subjectLen)
        }
        contentBuf.append(0)
        contentBuf.append(contentsOf: [ok ? 1 : 0])
        let contentHash = SHA256Checksum.digest(contentBuf)
        let contentHashHex = hexString(contentHash)

        // Unique event id (includes timestamp).
        var eventBuf = Data()
        eventBuf.reserveCapacity(32 + 8 + 8 + 1)
        eventBuf.append(contentHash)
        var tsLE = tsMs.littleEndian
        var durLE = durUs.littleEndian
        withUnsafeBytes(of: &tsLE) { eventBuf.append(contentsOf: $0) }
        withUnsafeBytes(of: &durLE) { eventBuf.append(contentsOf: $0) }
        eventBuf.append(contentsOf: [ok ? 1 : 0])
        let eventId = SHA256Checksum.digest(eventBuf)
        let eventIdHex = hexString(eventId)

        // Local JSONEachRow spool for mirror/file (or native fallback).
        let chRow: Data?
        if emitClickHouseRows {
            chRow = ClickHouseRowV1.encodeJSONEachRow(
                tsMs: tsMs,
                durUs: durUs,
                ok: ok ? 1 : 0,
                nameBytes: nameBytes,
                nameLen: nameLen,
                subjectBytes: subjectBytes,
                subjectLen: subjectLen,
                sessionId: sessionId,
                eventIdHex: eventIdHex,
                contentHashHex: contentHashHex
            )
        } else {
            chRow = nil
        }

        // Update in-memory aggregates and queues under lock.
        state.withLock { st in
            st.counters[name, default: 0] &+= 1
            var agg = st.latencies[name] ?? LatencyAgg()
            agg.add(durUs)
            st.latencies[name] = agg

            var shouldPersist = true
            if dedupWindowMs > 0, dedupCapacity > 0, !st.dedupSlots.isEmpty {
                if let last = st.dedupMap[contentHash], tsMs &- last <= dedupWindowMs {
                    shouldPersist = false
                }
                st.dedupMap[contentHash] = tsMs
                let idx = st.dedupWrite
                let slot = st.dedupSlots[idx]
                if !slot.key.isEmpty, st.dedupMap[slot.key] == slot.tsMs {
                    st.dedupMap.removeValue(forKey: slot.key)
                }
                st.dedupSlots[idx] = State.DedupSlot(key: contentHash, tsMs: tsMs)
                st.dedupWrite = (idx + 1) % st.dedupSlots.count
            }

            if shouldPersist {
                // Push to native ClickHouse writer when mode allows it.
                if let w = chWriter, let push = CHBridge.shared.pushMemEvent {
                    sessionId.withCString { sid in
                        eventIdHex.withCString { eid in
                            contentHashHex.withCString { chash in
                                name.withCString { n in
                                    if let subject {
                                        subject.withCString { s in
                                            push(w, tsMs, durUs, ok ? 1 : 0, sid, eid, chash, n, s)
                                        }
                                    } else {
                                        push(w, tsMs, durUs, ok ? 1 : 0, sid, eid, chash, n, nil)
                                    }
                                }
                            }
                        }
                    }
                }

                var meta = Metadata()
                meta.entries[SeqMemMetaKeys.schemaVersion] = "1"
                meta.entries[SeqMemMetaKeys.sessionId] = sessionId
                meta.entries[SeqMemMetaKeys.eventId] = eventIdHex
                meta.entries[SeqMemMetaKeys.contentHash] = contentHashHex
                meta.entries[SeqMemMetaKeys.name] = name
                if let subject, !subject.isEmpty { meta.entries[SeqMemMetaKeys.subject] = subject }
                let subset = FrameMetaSubset(
                    uri: "seqmem://event/\(eventIdHex)",
                    title: nil,
                    kind: SeqMemKinds.event,
                    track: "seqmem",
                    tags: [],
                    labels: [],
                    contentDates: [],
                    role: .blob,
                    parentId: nil, // attached at write time
                    chunkIndex: nil,
                    chunkCount: nil,
                    chunkManifest: nil,
                    status: .active,
                    supersedes: nil,
                    supersededBy: nil,
                    searchText: name,
                    metadata: meta
                )

                st.pending.append(
                    WaxWriter.PendingFrame(
                        tsMs: Int64(tsMs),
                        content: record,
                        meta: subset,
                        chRow: chRow
                    )
                )
            }

            st.ring[st.ringWrite] = record
            st.ringWrite = (st.ringWrite + 1) % SeqMemEngine.ringCapacity
            if st.ringCount < SeqMemEngine.ringCapacity {
                st.ringCount += 1
            }
        }
    }

    func metricsJSON() -> String {
        let snapshot = state.withLock { st -> (Date, [String: UInt64], [String: LatencyAgg]) in
            (st.start, st.counters, st.latencies)
        }
        let uptime = Date().timeIntervalSince(snapshot.0)

        var obj: [String: Any] = [:]
        obj["uptime_seconds"] = uptime
        obj["events_total"] = snapshot.1.values.reduce(0, &+)
        obj["counts"] = snapshot.1

        var lat: [String: Any] = [:]
        for (k, v) in snapshot.2 {
            lat[k] = [
                "count": v.count,
                "avg_us": v.avgUs,
                "max_us": v.maxUs,
            ]
        }
        obj["latency_us"] = lat
        return jsonString(obj) ?? "{\"error\":\"encode\"}"
    }

    func tailJSON(maxEvents: Int) -> String {
        let records: [Data] = state.withLock { st in
            let m = max(0, min(maxEvents, st.ringCount))
            guard m > 0 else { return [] }
            let cap = SeqMemEngine.ringCapacity
            let start = (st.ringWrite - m + cap) % cap
            var out: [Data] = []
            out.reserveCapacity(m)
            for i in 0..<m {
                out.append(st.ring[(start + i) % cap])
            }
            return out
        }

        let ordered: [Any] = records.map { rec in
            RecordV1.decode(rec) ?? ["raw_len": rec.count]
        }
        return jsonString(["events": ordered]) ?? "{\"events\":[]}"
    }

    private func flushTick() {
        let batch: [WaxWriter.PendingFrame] = state.withLock { st in
            guard !st.pending.isEmpty else { return [] }
            // Bound each flush to keep GC/ARC work predictable.
            let take = min(st.pending.count, 512)
            let batch = Array(st.pending.prefix(take))
            st.pending.removeFirst(take)
            return batch
        }
        guard !batch.isEmpty else { return }
        Task { [writer] in
            await writer.append(batch: batch)
        }
    }
}

private func jsonString(_ obj: Any) -> String? {
    guard JSONSerialization.isValidJSONObject(obj) else { return nil }
    guard let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]) else { return nil }
    return String(data: data, encoding: .utf8)
}

final class SeqMemGlobal: @unchecked Sendable {
    static let shared = SeqMemGlobal()

    private let engine: SeqMemEngine

    private init() {
        self.engine = SeqMemEngine(waxURL: SeqMemGlobal.defaultWaxURL())
    }

    static func defaultWaxURL() -> URL {
        let fm = FileManager.default
        let base = (try? fm.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true))
            ?? fm.temporaryDirectory
        let dir = base.appendingPathComponent("seq", isDirectory: true)
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("seqmem.mv2s")
    }

    static func defaultClickHousePath() -> String? {
        let env = ProcessInfo.processInfo.environment
        if let p = env["SEQ_CH_MEM_PATH"] {
            return p.isEmpty ? nil : p
        }
        let home = (env["HOME"]?.isEmpty == false) ? env["HOME"]! : FileManager.default.homeDirectoryForCurrentUser.path
        if home.isEmpty { return nil }
        return home + "/repos/ClickHouse/ClickHouse/user_files/seq_mem.jsonl"
    }

    func configure(path: String) {
        engine.configure(waxURL: URL(fileURLWithPath: path))
    }

    func recordRequest(
        namePtr: UnsafeRawPointer,
        nameLen: Int,
        tsMs: UInt64,
        durUs: UInt64,
        ok: UInt8,
        subjectPtr: UnsafeRawPointer?,
        subjectLen: Int
    ) {
        engine.recordRequest(
            tsMs: tsMs,
            durUs: durUs,
            ok: ok != 0,
            nameBytes: namePtr,
            nameLen: nameLen,
            subjectBytes: subjectPtr,
            subjectLen: subjectLen
        )
    }

    func metricsJSONSync() -> String {
        engine.metricsJSON()
    }

    func tailJSONSync(maxEvents: Int) -> String {
        engine.tailJSON(maxEvents: maxEvents)
    }
}

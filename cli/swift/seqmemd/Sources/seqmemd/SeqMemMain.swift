import Dispatch
import Foundation
import WaxCore

private struct Config {
    var ingestSocket: String = "/tmp/seqmemd.sock"
    var querySocket: String = "/tmp/seqmemq.sock"
    var waxPath: String? = nil
}

private func parseArgs() -> Config {
    var cfg = Config()
    var i = 1
    let args = CommandLine.arguments
    while i < args.count {
        switch args[i] {
        case "--ingest-socket":
            if i + 1 < args.count { cfg.ingestSocket = args[i + 1]; i += 2 } else { i += 1 }
        case "--query-socket":
            if i + 1 < args.count { cfg.querySocket = args[i + 1]; i += 2 } else { i += 1 }
        case "--wax":
            if i + 1 < args.count { cfg.waxPath = args[i + 1]; i += 2 } else { i += 1 }
        default:
            i += 1
        }
    }
    return cfg
}

private func defaultWaxURL() -> URL {
    let fm = FileManager.default
    let base = (try? fm.url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true))
        ?? fm.temporaryDirectory
    let dir = base.appendingPathComponent("seq", isDirectory: true)
    try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
    return dir.appendingPathComponent("seqmem.mv2s")
}

private func writeAll(fd: Int32, _ s: String) {
    let bytes = Array((s + "\n").utf8)
    _ = bytes.withUnsafeBytes { raw in
        write(fd, raw.baseAddress, raw.count)
    }
}

private func startIngestLoop(fd: Int32, store: EventStore) {
    DispatchQueue.global(qos: .userInitiated).async {
        var buf = [UInt8](repeating: 0, count: 64 * 1024)
        while true {
            let n = recv(fd, &buf, buf.count, 0)
            if n > 0 {
                let data = Data(buf[0..<n])
                Task { await store.ingest(raw: data) }
                continue
            }
            if n == 0 { continue }
            if errno == EINTR { continue }
            // Keep going on errors.
        }
    }
}

private func startQueryLoop(fd: Int32, store: EventStore) {
    DispatchQueue.global(qos: .userInitiated).async {
        while true {
            let client = accept(fd, nil, nil)
            if client < 0 {
                if errno == EINTR { continue }
                continue
            }

            autoreleasepool {
                let line = readLine(fd: client) ?? ""
                let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
                if trimmed == "PING" {
                    writeAll(fd: client, "PONG")
                    close(client)
                    return
                }

                if trimmed == "METRICS" {
                    Task {
                        let json = await store.metricsJSON()
                        writeAll(fd: client, json)
                        close(client)
                    }
                    return
                }

                if trimmed.hasPrefix("TAIL ") {
                    let nStr = trimmed.dropFirst("TAIL ".count).trimmingCharacters(in: .whitespaces)
                    let n = Int(nStr) ?? 50
                    Task {
                        let json = await store.tailJSON(maxEvents: n)
                        writeAll(fd: client, json)
                        close(client)
                    }
                    return
                }

                writeAll(fd: client, "{\"error\":\"unknown\"}")
                close(client)
            }
        }
    }
}

@main
struct SeqMemMain {
    static func main() {
        let cfg = parseArgs()
        let waxURL: URL
        if let p = cfg.waxPath {
            waxURL = URL(fileURLWithPath: p)
        } else {
            waxURL = defaultWaxURL()
        }

        let options = WaxOptions(
            walFsyncPolicy: .onCommit,
            ioQueueLabel: "com.seq.mem.io",
            ioQueueQos: .utility
        )

        let store = EventStore(waxURL: waxURL, waxOptions: options)
        Task {
            do {
                try await store.openIfNeeded()
            } catch {
                // Keep running even if store can't be opened; we still serve metrics (in-memory only).
            }
        }

        do {
            let ingestFd = try bindUnixDatagramSocket(path: cfg.ingestSocket)
            startIngestLoop(fd: ingestFd, store: store)
        } catch {
            fputs("seqmemd: ingest bind failed: \(error)\n", stderr)
        }

        do {
            let queryFd = try bindUnixStreamSocket(path: cfg.querySocket)
            startQueryLoop(fd: queryFd, store: store)
        } catch {
            fputs("seqmemd: query bind failed: \(error)\n", stderr)
        }

        // Keep the process alive; work happens on background dispatch queues.
        // Note: `dispatchMain()` crashes on macOS 26 when called from a main-queue block.
        while true {
            Thread.sleep(forTimeInterval: 3600)
        }
    }
}

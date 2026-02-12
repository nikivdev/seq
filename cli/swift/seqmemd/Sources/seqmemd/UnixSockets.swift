import Foundation

enum UnixSocketError: Error, CustomStringConvertible {
    case sys(String)
    case invalidPath(String)

    var description: String {
        switch self {
        case .sys(let s): return s
        case .invalidPath(let p): return "invalid socket path: \(p)"
        }
    }
}

func withErrno<T>(_ fn: () -> T) -> (T, Int32) {
    errno = 0
    let r = fn()
    return (r, errno)
}

func sockaddr_un_forPath(_ path: String) throws -> sockaddr_un {
    guard !path.isEmpty else { throw UnixSocketError.invalidPath(path) }
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)

    // sun_path is a fixed-size C array; ensure it fits, including null terminator.
    let maxLen = MemoryLayout.size(ofValue: addr.sun_path)
    let utf8Len = path.utf8CString.count
    guard utf8Len <= maxLen else { throw UnixSocketError.invalidPath(path) }

    path.withCString { cstr in
        withUnsafeMutablePointer(to: &addr.sun_path) { p in
            p.withMemoryRebound(to: CChar.self, capacity: maxLen) { buf in
                // Ensure null-terminated.
                strncpy(buf, cstr, maxLen - 1)
                buf[maxLen - 1] = 0
            }
        }
    }

    return addr
}

func unlinkIfExists(_ path: String) {
    _ = path.withCString { cstr in
        unlink(cstr)
    }
}

func bindUnixStreamSocket(path: String, backlog: Int32 = 16) throws -> Int32 {
    let (fd, err1) = withErrno { socket(AF_UNIX, SOCK_STREAM, 0) }
    if fd < 0 { throw UnixSocketError.sys("socket(stream) failed errno=\(err1)") }

    var yes: Int32 = 1
    _ = setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, socklen_t(MemoryLayout.size(ofValue: yes)))

    unlinkIfExists(path)
    var addr = try sockaddr_un_forPath(path)
    let bindRes: Int32 = withUnsafePointer(to: &addr) { p in
        p.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
            bind(fd, sa, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
    }
    if bindRes != 0 {
        let e = errno
        close(fd)
        throw UnixSocketError.sys("bind(stream) failed errno=\(e) path=\(path)")
    }

    if listen(fd, backlog) != 0 {
        let e = errno
        close(fd)
        throw UnixSocketError.sys("listen failed errno=\(e)")
    }

    return fd
}

func bindUnixDatagramSocket(path: String) throws -> Int32 {
    let (fd, err1) = withErrno { socket(AF_UNIX, SOCK_DGRAM, 0) }
    if fd < 0 { throw UnixSocketError.sys("socket(dgram) failed errno=\(err1)") }

    unlinkIfExists(path)
    var addr = try sockaddr_un_forPath(path)
    let bindRes: Int32 = withUnsafePointer(to: &addr) { p in
        p.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
            bind(fd, sa, socklen_t(MemoryLayout<sockaddr_un>.size))
        }
    }
    if bindRes != 0 {
        let e = errno
        close(fd)
        throw UnixSocketError.sys("bind(dgram) failed errno=\(e) path=\(path)")
    }

    return fd
}

func readLine(fd: Int32, maxBytes: Int = 64 * 1024) -> String? {
    var buf = [UInt8](repeating: 0, count: 1024)
    var out = [UInt8]()
    out.reserveCapacity(1024)

    while out.count < maxBytes {
        let n = read(fd, &buf, buf.count)
        if n > 0 {
            for i in 0..<n {
                let b = buf[i]
                if b == 0x0A { // '\n'
                    return String(bytes: out, encoding: .utf8) ?? ""
                }
                out.append(b)
                if out.count >= maxBytes {
                    break
                }
            }
            continue
        }
        if n == 0 {
            if out.isEmpty { return nil }
            return String(bytes: out, encoding: .utf8) ?? ""
        }
        if errno == EINTR { continue }
        return nil
    }
    return String(bytes: out, encoding: .utf8) ?? ""
}

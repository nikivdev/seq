import Foundation

@_cdecl("seqmem_configure_wax_path")
public func seqmem_configure_wax_path(_ path: UnsafePointer<CChar>?, _ pathLen: Int) {
    guard let path, pathLen > 0 else { return }
    let s = String(decoding: UnsafeRawBufferPointer(start: path, count: pathLen), as: UTF8.self)
    SeqMemGlobal.shared.configure(path: s)
}

@_cdecl("seqmem_record_request")
public func seqmem_record_request(
    _ name: UnsafePointer<UInt8>?,
    _ nameLen: Int,
    _ tsMs: UInt64,
    _ durUs: UInt64,
    _ ok: UInt8,
    _ subject: UnsafePointer<UInt8>?,
    _ subjectLen: Int
) {
    guard let name, nameLen > 0 else { return }
    SeqMemGlobal.shared.recordRequest(
        namePtr: UnsafeRawPointer(name),
        nameLen: nameLen,
        tsMs: tsMs,
        durUs: durUs,
        ok: ok,
        subjectPtr: subject.map { UnsafeRawPointer($0) },
        subjectLen: max(0, subjectLen)
    )
}

@_cdecl("seqmem_metrics_json")
public func seqmem_metrics_json() -> UnsafeMutablePointer<CChar>? {
    let s = SeqMemGlobal.shared.metricsJSONSync()
    return strdup(s)
}

@_cdecl("seqmem_tail_json")
public func seqmem_tail_json(_ maxEvents: Int32) -> UnsafeMutablePointer<CChar>? {
    let s = SeqMemGlobal.shared.tailJSONSync(maxEvents: Int(maxEvents))
    return strdup(s)
}

@_cdecl("seqmem_free")
public func seqmem_free(_ p: UnsafeMutableRawPointer?) {
    guard let p else { return }
    free(p)
}

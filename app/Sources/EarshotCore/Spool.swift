import Foundation

/// One flash page as the v1 upload format carries it (DESIGN.md, Upload contract). The app no longer writes
/// these; they are read to upload spool files written before the v2 format.
public struct SpoolPage: Sendable, Equatable {
  public let session: UInt32
  public let pageIndex: UInt32
  public let timestampMs: UInt64
  public let frames: [Data]

  public init(session: UInt32, pageIndex: UInt32, timestampMs: UInt64, frames: [Data]) {
    self.session = session
    self.pageIndex = pageIndex
    self.timestampMs = timestampMs
    self.frames = frames
  }

  /// Range-checked conversion from the codec's signed ints. A value the v1 record cannot hold
  /// is an error, never a silent clamp: the caller treats it as an append failure and does not ACK.
  public init(session: Int, pageIndex: Int, timestampMs: Int64, frames: [Data]) throws {
    guard let s = UInt32(exactly: session), let p = UInt32(exactly: pageIndex),
      let t = UInt64(exactly: timestampMs), frames.count <= Int(UInt16.max),
      frames.allSatisfy({ $0.count <= Int(UInt16.max) })
    else { throw SpoolError.unrepresentable }
    self.init(session: s, pageIndex: p, timestampMs: t, frames: frames)
  }

  /// `u32 session, u32 page_index, u64 page_ts_ms, u16 frame_count, frame_count x {u16 len, bytes}`, little-endian.
  public func encoded() -> Data {
    var out = Data()
    out.reserveCapacity(18 + frames.reduce(0) { $0 + 2 + $1.count })
    out.appendLE(session)
    out.appendLE(pageIndex)
    out.appendLE(timestampMs)
    out.appendLE(UInt16(frames.count))
    for f in frames {
      out.appendLE(UInt16(f.count))
      out.append(f)
    }
    return out
  }

  /// Parses back-to-back records. Stops at the first incomplete record (a torn tail left by a crash):
  /// `validLength` is the byte length of the complete records before it.
  public static func parse(_ data: Data) -> (pages: [SpoolPage], validLength: Int) {
    let bytes = [UInt8](data)
    var pages: [SpoolPage] = []
    var off = 0
    var valid = 0
    while bytes.count - off >= 18 {
      let session = readLE(bytes, off, 4)
      let index = readLE(bytes, off + 4, 4)
      let ts = readLE(bytes, off + 8, 8)
      let count = Int(readLE(bytes, off + 16, 2))
      var p = off + 18
      var frames: [Data] = []
      var complete = true
      for _ in 0..<count {
        guard bytes.count - p >= 2 else {
          complete = false
          break
        }
        let n = Int(readLE(bytes, p, 2))
        p += 2
        guard bytes.count - p >= n else {
          complete = false
          break
        }
        frames.append(Data(bytes[p..<p + n]))
        p += n
      }
      guard complete else { break }
      pages.append(
        SpoolPage(
          session: UInt32(session), pageIndex: UInt32(index), timestampMs: ts, frames: frames))
      off = p
      valid = p
    }
    return (pages, valid)
  }

  private static func readLE(_ b: [UInt8], _ at: Int, _ n: Int) -> UInt64 {
    var v: UInt64 = 0
    for i in 0..<n { v |= UInt64(b[at + i]) << (8 * UInt64(i)) }
    return v
  }
}

extension Data {
  fileprivate mutating func appendLE<T: FixedWidthInteger>(_ v: T) {
    var le = v.littleEndian
    Swift.withUnsafeBytes(of: &le) { append(contentsOf: $0) }
  }
}

/// The v2 spool and upload record: `u32 length` (little-endian), then `length` bytes of one raw StorageBuffer,
/// exactly as the pendant sent it (DESIGN.md, Upload contract).
public enum RawRecord {
  /// A record holds its length in a u32; a larger buffer is an error, never a truncation.
  public static func encoded(_ storageBuffer: Data) throws -> Data {
    guard let length = UInt32(exactly: storageBuffer.count) else {
      throw SpoolError.unrepresentable
    }
    var out = Data()
    out.reserveCapacity(4 + storageBuffer.count)
    out.appendLE(length)
    out.append(storageBuffer)
    return out
  }

  /// Parses back-to-back records. Stops at the first incomplete record (a torn tail left by a crash):
  /// `validLength` is the byte length of the complete records before it.
  public static func parse(_ data: Data) -> (records: [Data], validLength: Int) {
    let bytes = [UInt8](data)
    var records: [Data] = []
    var off = 0
    while bytes.count - off >= 4 {
      var length = 0
      for i in 0..<4 { length |= Int(bytes[off + i]) << (8 * i) }
      guard bytes.count - off - 4 >= length else { break }
      records.append(Data(bytes[off + 4..<off + 4 + length]))
      off += 4 + length
    }
    return (records, off)
  }
}

public enum SpoolError: Error, Equatable {
  case unrepresentable
  case io(String)
}

/// Where the drain engine puts pages. `sync()` must make every appended page durable before it returns:
/// the engine ACKs (msg 7, which deletes the pendant's copy) only after it does.
public protocol PageSink: AnyObject, Sendable {
  /// Appends one raw StorageBuffer, byte for byte.
  func append(_ storageBuffer: Data) throws
  func sync() throws
}

/// Append-only spool of v2 records (`RawRecord`) under `directory`.
///
/// The active segment is `seg-<n>.pages.v2.open`; closing it renames it to `seg-<n>.pages.v2` (fsync first,
/// then rename, then directory fsync). Only closed segments are uploaded. Segments left open by a crash are
/// closed on the next start; a torn tail record in one was never ACKed, because ACK waits for `sync()`.
/// Segments written in v1 before the update (`.pages.v1`, `.pages.v1.open`) are recovered and listed the same
/// way and keep their format; nothing new is written to them.
public final class Spool: PageSink, @unchecked Sendable {
  public static let closedSuffix = ".pages.v2"
  public static let closedSuffixV1 = ".pages.v1"
  static let openSuffix = ".pages.v2.open"
  static let openSuffixV1 = ".pages.v1.open"

  public let directory: URL
  private let maxSegmentBytes: Int
  private let fullSync: @Sendable (Int32) throws -> Void
  private let lock = NSLock()
  private var fd: Int32 = -1
  private var activeURL: URL?
  private var activeSize = 0
  private var nextSeq = 1

  /// `fullSync` is the file-fsync primitive; tests inject a failing one.
  public init(
    directory: URL, maxSegmentBytes: Int = 1 << 20,
    fullSync: @escaping @Sendable (Int32) throws -> Void = Spool.fullSync
  ) throws {
    self.fullSync = fullSync
    self.directory = directory
    self.maxSegmentBytes = maxSegmentBytes
    // The directory holds recorded audio: private whether created here or found at another mode.
    try FileManager.default.createDirectory(
      at: directory, withIntermediateDirectories: true,
      attributes: [.posixPermissions: 0o700])
    if chmod(directory.path, 0o700) != 0 {
      throw SpoolError.io("chmod \(directory.lastPathComponent): errno \(errno)")
    }
    let names = try FileManager.default.contentsOfDirectory(atPath: directory.path)
    var renamed = false
    for name in names where name.hasSuffix(Self.openSuffix) || name.hasSuffix(Self.openSuffixV1) {
      let from = directory.appendingPathComponent(name)
      let to = directory.appendingPathComponent(String(name.dropLast(".open".count)))
      if rename(from.path, to.path) != 0 { throw SpoolError.io("recover \(name): errno \(errno)") }
      renamed = true
    }
    if renamed { try Self.fsyncDirectory(directory) }
    for name in try FileManager.default.contentsOfDirectory(atPath: directory.path) {
      if let seq = Self.sequence(of: name) { nextSeq = max(nextSeq, seq + 1) }
    }
  }

  deinit { if fd >= 0 { close(fd) } }

  // MARK: PageSink

  public func append(_ storageBuffer: Data) throws {
    let record = try RawRecord.encoded(storageBuffer)
    try lock.withLock {
      try ensureActive()
      let before = activeSize
      if let err = Self.writeAll(fd, record) {
        // A partial record must not stay in the file: later records would be misparsed behind it.
        _ = ftruncate(fd, off_t(before))
        throw SpoolError.io("write: errno \(err)")
      }
      activeSize += record.count
      if activeSize >= maxSegmentBytes { try closeActiveLocked() }
    }
  }

  public func sync() throws {
    try lock.withLock {
      if fd >= 0 { try fullSync(fd) }
    }
  }

  // MARK: Segments

  /// Close the active segment if it holds anything, so the uploader can see it.
  public func rotate() throws {
    try lock.withLock { try closeActiveLocked() }
  }

  /// Closed segments, oldest first.
  public func closedSegments() -> [URL] {
    let names = (try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? []
    return names.filter { name in
      Self.sequence(of: name) != nil
        && (name.hasSuffix(Self.closedSuffix) || name.hasSuffix(Self.closedSuffixV1))
    }
    .sorted()
    .map { directory.appendingPathComponent($0) }
  }

  /// Pages in the spool not yet accepted by earshotd: every closed segment plus the active one.
  public func backlog() -> (segments: Int, pages: Int) {
    var urls = closedSegments()
    let active = lock.withLock { activeURL }
    if let active { urls.append(active) }
    var pages = 0
    for u in urls {
      if let d = try? Data(contentsOf: u) { pages += Self.recordCount(of: u, in: d) }
    }
    return (urls.count, pages)
  }

  /// Delete a segment after earshotd accepted it. The directory is fsynced so the delete is durable.
  public func remove(_ segment: URL) throws {
    try FileManager.default.removeItem(at: segment)
    try Self.fsyncDirectory(directory)
  }

  // MARK: Internals

  private func ensureActive() throws {
    if fd >= 0 { return }
    let url = directory.appendingPathComponent(
      String(format: "seg-%010d", nextSeq) + Self.openSuffix)
    let newFd = open(url.path, O_WRONLY | O_CREAT | O_EXCL | O_APPEND, 0o600)
    guard newFd >= 0 else { throw SpoolError.io("open \(url.lastPathComponent): errno \(errno)") }
    do { try Self.fsyncDirectory(directory) } catch {
      close(newFd)
      throw error
    }
    fd = newFd
    activeURL = url
    activeSize = 0
    nextSeq += 1
  }

  /// Closes the active segment. State is reset only once the data is known durable and renamed: if the fsync
  /// (or rename) fails the segment stays active with its fd, so a later `sync()` retries the same fsync and
  /// can never report success for data whose fsync failed.
  private func closeActiveLocked() throws {
    guard fd >= 0, let url = activeURL else { return }
    if activeSize == 0 {
      resetActiveLocked()
      unlink(url.path)
      return
    }
    try fullSync(fd)
    let closed = directory.appendingPathComponent(
      String(url.lastPathComponent.dropLast(".open".count)))
    guard rename(url.path, closed.path) == 0 else { throw SpoolError.io("rename: errno \(errno)") }
    resetActiveLocked()
    try Self.fsyncDirectory(directory)
  }

  private func resetActiveLocked() {
    close(fd)
    fd = -1
    activeURL = nil
    activeSize = 0
  }

  /// True for a segment written in v1 before the update.
  static func isV1(_ segment: URL) -> Bool {
    let name = segment.lastPathComponent
    return name.hasSuffix(closedSuffixV1) || name.hasSuffix(openSuffixV1)
  }

  /// Complete records in a segment's bytes, read in the format its name says.
  static func recordCount(of segment: URL, in data: Data) -> Int {
    isV1(segment) ? SpoolPage.parse(data).pages.count : RawRecord.parse(data).records.count
  }

  private static func sequence(of name: String) -> Int? {
    guard name.hasPrefix("seg-"),
      [closedSuffix, openSuffix, closedSuffixV1, openSuffixV1].contains(where: {
        name.hasSuffix($0)
      })
    else { return nil }
    return Int(name.dropFirst(4).prefix(while: { $0.isNumber }))
  }

  private static func writeAll(_ fd: Int32, _ data: Data) -> Int32? {
    var offset = 0
    let bytes = [UInt8](data)
    while offset < bytes.count {
      let n = bytes[offset...].withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
      if n < 0 {
        if errno == EINTR { continue }
        return errno
      }
      offset += n
    }
    return nil
  }

  /// F_FULLFSYNC asks the drive to flush its cache too; plain fsync is the fallback where unsupported.
  public static func fullSync(_ fd: Int32) throws {
    if fcntl(fd, F_FULLFSYNC) == 0 { return }
    if fsync(fd) != 0 { throw SpoolError.io("fsync: errno \(errno)") }
  }

  private static func fsyncDirectory(_ dir: URL) throws {
    let d = open(dir.path, O_RDONLY)
    guard d >= 0 else { throw SpoolError.io("open dir: errno \(errno)") }
    defer { close(d) }
    try fullSync(d)
  }
}

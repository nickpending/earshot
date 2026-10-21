import Foundation

@testable import EarshotCore

// Shared test doubles: a scripted pendant behind PendantTransport, a recording page sink, and the wire builders
// they need (mirrors of the protobuf helpers in omi's limitless_golden_test.dart).

final class EventLog: @unchecked Sendable {
  private let lock = NSLock()
  private var items: [String] = []
  func add(_ s: String) { lock.withLock { items.append(s) } }
  var all: [String] { lock.withLock { items } }
}

// MARK: Wire builders

func varint(_ value: Int) -> [UInt8] {
  var v = value
  var out: [UInt8] = []
  while v > 0x7f {
    out.append(UInt8((v & 0x7f) | 0x80))
    v >>= 7
  }
  out.append(UInt8(v & 0x7f))
  return out
}

func intField(_ n: Int, _ v: Int) -> [UInt8] { varint(n << 3) + varint(v) }
func bytesField(_ n: Int, _ b: [UInt8]) -> [UInt8] { varint((n << 3) | 2) + varint(b.count) + b }

func bleWrapper(_ index: Int, _ seq: Int, _ frags: Int, _ payload: [UInt8]) -> Data {
  Data(intField(1, index) + intField(2, seq) + intField(3, frags) + bytesField(4, payload))
}

/// A 40-byte opus-looking frame: valid TOC, filler derived from `seed`.
func opusFrame(_ toc: UInt8 = 0xb8, _ seed: Int) -> [UInt8] {
  [toc] + (0..<39).map { UInt8(truncatingIfNeeded: seed &* 31 &+ $0 &* 7 &+ 1) }
}

func flashPage(ts: Int, frames: [[UInt8]]) -> [UInt8] {
  let blob = frames.flatMap { bytesField(1, $0) }
  let wrapper = [0x08, 0x00] + bytesField(2, blob)
  return intField(1, ts) + intField(2, 1) + bytesField(3, wrapper)
}

func pagePacket(
  msgIndex: Int, index: Int, session: Int = 7, ts: Int = 1_750_000_000_000, frames: [[UInt8]]? = nil
) -> Data {
  let fs = frames ?? (0..<8).map { opusFrame(0xb8, index * 100 + $0) }
  let buffer =
    intField(2, session) + intField(4, index) + intField(5, index)
    + bytesField(6, flashPage(ts: ts + index * 1600, frames: fs))
  return bleWrapper(msgIndex, 0, 1, bytesField(2, buffer))
}

/// The directory of real StorageBuffers captured from the pendant (fixtures/pendant-pages).
let pendantPagesDirectory = URL(fileURLWithPath: #filePath)
  .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
  .deletingLastPathComponent().appendingPathComponent("fixtures/pendant-pages")

/// The bytes of a real StorageBuffer fixture (`<page>.storagebuffer.hex`).
func realStorageBuffer(_ page: Int) throws -> [UInt8] {
  let hex = try String(
    contentsOf: pendantPagesDirectory.appendingPathComponent("\(page).storagebuffer.hex"),
    encoding: .utf8
  ).trimmingCharacters(in: .whitespacesAndNewlines)
  var out: [UInt8] = []
  var i = hex.startIndex
  while i < hex.endIndex {
    let j = hex.index(i, offsetBy: 2)
    out.append(UInt8(hex[i..<j], radix: 16)!)
    i = j
  }
  return out
}

/// A StorageBuffer as the pendant sends it: PendantMessage field 2 inside a one-fragment BLE wrapper.
func storageBufferPacket(msgIndex: Int, _ buffer: [UInt8]) -> Data {
  bleWrapper(msgIndex, 0, 1, bytesField(2, buffer))
}

func statusPacket(
  oldest: Int, newest: Int, session: Int = 3, free: Int = 95_000, total: Int = 95_232
) -> Data {
  let storage =
    intField(1, oldest) + intField(2, newest) + intField(3, session) + intField(4, free)
    + intField(5, total)
  let status = intField(1, 85) + bytesField(5, storage)
  return bleWrapper(15, 0, 1, intField(1, 85) + bytesField(5, status))
}

// MARK: Reading what the engine wrote

enum Command: Equatable, CustomStringConvertible {
  case setTime
  case getStatus
  case setMode(batch: Bool, realTime: Bool)
  case ack(upTo: Int)
  case other(Int)

  var description: String {
    switch self {
    case .setTime: return "msg6"
    case .getStatus: return "msg21"
    case .setMode(let b, let r): return "msg8{\(b ? 1 : 0),\(r ? 1 : 0)}"
    case .ack(let n): return "msg7(\(n))"
    case .other(let n): return "msg\(n)"
    }
  }

  static func decode(_ data: Data) -> Command {
    guard let packet = LimitlessProtocol.parseBlePacket(data) else { return .other(-1) }
    let p = [UInt8](packet.payload)
    let (tag, next) = readVarint(p, 0)
    let msg = tag >> 3
    let (len, bodyStart) = readVarint(p, next)
    let body = Array(p[bodyStart..<min(p.count, bodyStart + len)])
    switch msg {
    case 6: return .setTime
    case 21: return .getStatus
    case 7:
      let (_, n) = readVarint(body, 0)
      return .ack(upTo: readVarint(body, n).0)
    case 8:
      // {1: batch, 2: realTime}, each a single varint byte.
      return .setMode(
        batch: body.count > 1 && body[1] == 1, realTime: body.count > 3 && body[3] == 1)
    default: return .other(msg)
    }
  }
}

func readVarint(_ b: [UInt8], _ start: Int) -> (Int, Int) {
  var result = 0
  var shift = 0
  var pos = start
  while pos < b.count {
    let byte = Int(b[pos])
    pos += 1
    result |= (byte & 0x7f) << shift
    if byte & 0x80 == 0 { break }
    shift += 7
  }
  return (result, pos)
}

// MARK: Doubles

struct InjectedFailure: Error {}

/// A pendant that answers GetDeviceStatus and, on SetMode {1,0}, pushes its pages. Writes and sink calls
/// land in one shared log so a test can assert their order.
final class FakePendant: PendantTransport, @unchecked Sendable {
  let log: EventLog
  private let lock = NSLock()
  private var handler: (@Sendable (Data) -> Void)?
  private var recorded: [Data] = []

  var oldest = 100
  var newest = 159
  /// Page packets pushed after SetMode {1,0}; fewer than the range simulates a stall.
  var pagesToSend: [Data] = []
  var onWrite: (@Sendable (Command) -> Void)?

  init(log: EventLog) { self.log = log }

  func write(_ data: Data) async throws {
    let cmd = Command.decode(data)
    lock.withLock { recorded.append(data) }
    log.add("write:\(cmd)")
    onWrite?(cmd)
    let h = lock.withLock { handler }
    switch cmd {
    case .ack(let n) where ackAware: lock.withLock { ackedThrough = max(ackedThrough, n) }
    case .getStatus:
      let floor = lock.withLock { ackedThrough }
      h?(statusPacket(oldest: ackAware ? max(oldest, floor + 1) : oldest, newest: newest))
    case .setMode(true, false):
      let floor = lock.withLock { ackedThrough }
      for p in pagesToSend where !ackAware || Self.pageIndex(of: p) ?? Int.max > floor { h?(p) }
    default: break
    }
  }

  /// When set, the pendant deletes what an ACK names: status reports the oldest page left and a download sends
  /// only the pages past the last ACK.
  var ackAware = false
  private var ackedThrough = Int.min / 2

  /// The page index inside a one-fragment page packet.
  private static func pageIndex(of packet: Data) -> Int? {
    guard let p = LimitlessProtocol.parseBlePacket(packet),
      let buffer = RawPage.storageBuffers(in: p.payload).first
    else { return nil }
    return RawPage.pageIndex(of: buffer)
  }

  func setNotificationHandler(_ handler: @escaping @Sendable (Data) -> Void) async {
    lock.withLock { self.handler = handler }
  }

  /// A notification the pendant sends on its own, not in answer to a command.
  func push(_ data: Data) {
    let h = lock.withLock { handler }
    h?(data)
  }

  func readBattery() async throws -> Int {
    lock.withLock { batteryReads += 1 }
    return 99
  }

  private var batteryReads = 0
  var batteryReadCount: Int { lock.withLock { batteryReads } }

  var writes: [Data] { lock.withLock { recorded } }
  var commands: [Command] { writes.map(Command.decode) }
  var acks: [Int] {
    commands.compactMap { if case .ack(let n) = $0 { return n } else { return nil } }
  }
}

/// `buffer` with its page index (field 5) replaced: the real StorageBuffers carry the indices they had on the
/// pendant, and a drain needs them consecutive.
func reindexed(_ buffer: [UInt8], to index: Int) throws -> [UInt8] {
  var out: [UInt8] = []
  var pos = 0
  var replaced = false
  while pos < buffer.count {
    let (tag, afterTag) = readVarint(buffer, pos)
    let field = Array(buffer[pos..<afterTag])
    switch tag & 7 {
    case 0:
      let (_, next) = readVarint(buffer, afterTag)
      out += tag >> 3 == 5 ? intField(5, index) : field + Array(buffer[afterTag..<next])
      replaced = replaced || tag >> 3 == 5
      pos = next
    case 2:
      let (length, start) = readVarint(buffer, afterTag)
      out += Array(buffer[pos..<start + length])
      pos = start + length
    default: throw InjectedFailure()
    }
  }
  guard replaced else { throw InjectedFailure() }
  return out
}

/// A sink that logs `append:<index>` (`append:none` for a buffer with no readable index) and `sync`,
/// optionally failing, and keeps the bytes it was given.
final class RecordingSink: PageSink, @unchecked Sendable {
  let log: EventLog
  var failAppendAtIndex: Int?
  var failSync = false
  private let lock = NSLock()
  private var saved: [Data] = []

  init(log: EventLog) { self.log = log }

  var appended: [Data] { lock.withLock { saved } }

  func append(_ storageBuffer: Data) throws {
    let index = RawPage.pageIndex(of: storageBuffer)
    if let index, index == failAppendAtIndex {
      log.add("append-failed:\(index)")
      throw InjectedFailure()
    }
    lock.withLock { saved.append(storageBuffer) }
    log.add("append:\(index.map(String.init) ?? "none")")
  }

  func sync() throws {
    if failSync {
      log.add("sync-failed")
      throw InjectedFailure()
    }
    log.add("sync")
  }
}

func makePages(_ range: ClosedRange<Int>) -> [Data] {
  range.enumerated().map { pagePacket(msgIndex: $0.offset + 10, index: $0.element) }
}

/// For tests whose drain ends with every page in hand: a stall here would be a scheduling pause under
/// load misread as a silent pendant (#18), so its window is long enough never to fire.
let fastTiming = DrainTiming(
  statusTimeout: .seconds(2), stall: .seconds(30), ackEveryPages: 25)

/// For tests whose subject is a stall: the pendant really goes silent, so a short window ends them fast.
let stallTiming = DrainTiming(
  statusTimeout: .seconds(2), stall: .milliseconds(300), ackEveryPages: 25)

func tempDirectory(_ name: String) throws -> URL {
  let dir = FileManager.default.temporaryDirectory
    .appendingPathComponent("earshot-tests-\(name)-\(UUID().uuidString)")
  try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
  return dir
}

/// One session and one drain cycle of `pendant` into a logging sink.
func runDrain(_ pendant: FakePendant, log: EventLog) async throws -> DrainResult {
  let engine = DrainEngine(
    transport: pendant, sink: RecordingSink(log: log), timing: fastTiming)
  try await engine.beginSession()
  return await engine.runCycle()
}

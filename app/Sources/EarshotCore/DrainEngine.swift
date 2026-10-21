import Foundation
import os

/// Unified log, same subsystem as the app: .notice persists.
private let drainLog = Logger(subsystem: "casa.mal.Earshot", category: "drain")

private func logDrainNotice(_ line: String) {
  drainLog.notice("\(line, privacy: .public)")
}

/// Ordered inbox for RX notifications. `push` is synchronous and thread-safe so a Bluetooth callback keeps
/// packet order; `next` waits with a timeout. It holds at most `capacity` packets: one arriving past that is
/// dropped and counted, so a device cannot grow it without limit.
final class PacketQueue: @unchecked Sendable {
  static let capacity = 4096

  private let lock = NSLock()
  private var items: [Data] = []
  private var dropped = 0
  private var waiter: (id: Int, cont: CheckedContinuation<Data?, Never>, timer: Task<Void, Never>)?
  private var counter = 0

  func push(_ data: Data) {
    let w: (id: Int, cont: CheckedContinuation<Data?, Never>, timer: Task<Void, Never>)? =
      lock.withLock {
        if let w = waiter {
          waiter = nil
          return w
        }
        if items.count >= Self.capacity {
          dropped += 1
        } else {
          items.append(data)
        }
        return nil
      }
    if let w {
      w.timer.cancel()
      w.cont.resume(returning: data)
    }
  }

  func clear() {
    lock.withLock {
      items.removeAll()
      dropped = 0
    }
  }

  /// Packets dropped since the last call (or `clear`).
  func takeDropped() -> Int {
    lock.withLock {
      defer { dropped = 0 }
      return dropped
    }
  }

  /// Resume a pending `next` with nil now (used to abort a cycle promptly).
  func wake() {
    let w = lock.withLock {
      () -> (id: Int, cont: CheckedContinuation<Data?, Never>, timer: Task<Void, Never>)? in
      let w = waiter
      waiter = nil
      return w
    }
    if let w {
      w.timer.cancel()
      w.cont.resume(returning: nil)
    }
  }

  func next(timeout: Duration) async -> Data? {
    await withCheckedContinuation { (cont: CheckedContinuation<Data?, Never>) in
      lock.withLock {
        if !items.isEmpty {
          cont.resume(returning: items.removeFirst())
          return
        }
        counter += 1
        let id = counter
        let timer = Task { [weak self] in
          do { try await Task.sleep(for: timeout) } catch { return }
          self?.expire(id)
        }
        waiter = (id, cont, timer)
      }
    }
  }

  private func expire(_ id: Int) {
    let w = lock.withLock {
      () -> (id: Int, cont: CheckedContinuation<Data?, Never>, timer: Task<Void, Never>)? in
      guard let w = waiter, w.id == id else { return nil }
      waiter = nil
      return w
    }
    w?.cont.resume(returning: nil)
  }
}

public struct DrainTiming: Sendable {
  public var statusTimeout: Duration
  /// No new page for this long ends the drain.
  public var stall: Duration
  /// With every page below the newest in hand, this long without the newest ends the drain as caught up: the
  /// pendant is still writing it, and the next cycle takes it.
  public var newestQuiet: Duration
  public var ackEveryPages: Int
  /// A cycle ends after saving this many pages, so what a cycle holds in memory is bounded; the next cycle
  /// starts at once.
  public var pageLimit: Int

  public init(
    statusTimeout: Duration = .seconds(8), stall: Duration = .seconds(30),
    newestQuiet: Duration = .seconds(5), ackEveryPages: Int = 25, pageLimit: Int = 4096
  ) {
    self.pageLimit = pageLimit
    self.statusTimeout = statusTimeout
    self.stall = stall
    self.newestQuiet = newestQuiet
    self.ackEveryPages = ackEveryPages
  }
}

/// The pendant's storage state (reply to GetDeviceStatus).
public struct StorageStatus: Sendable, Equatable {
  public let oldestPage: Int
  public let newestPage: Int
  public let session: Int
  public let freePages: Int
  public let totalPages: Int

  /// Audio on the pendant, in pages.
  public var pagesOnPendant: Int { totalPages - freePages }

  init(_ s: LimitlessProtocol.StorageState) {
    oldestPage = s.oldestFlashPage
    newestPage = s.newestFlashPage
    session = s.currentStorageSession
    freePages = s.freeCapturePages
    totalPages = s.totalCapturePages
  }
}

public enum DrainEnd: String, Sendable, Equatable {
  /// The newest page arrived, or every page below it did and it stayed quiet for `newestQuiet`.
  case caughtUp
  case stall
  case appendFailed
  case syncFailed
  case transportFailed
  case aborted
  /// The pendant holds no pages.
  case nothingToDrain
  /// No storage status arrived in time; nothing was started.
  case noStatus
  /// Another cycle was running, or the session has not begun.
  case busy
  /// The cycle saved `pageLimit` pages; the pendant may hold more, and the next cycle takes them at once.
  case pageLimit
}

public struct DrainResult: Sendable, Equatable {
  public let end: DrainEnd
  public let status: DeviceStatus?
  /// Pages whose raw bytes this cycle appended to the sink.
  public let pagesSaved: Int
  /// Highest page index ACKed this cycle (msg 7), if any.
  public let ackedThrough: Int?
  /// First page index missing from the contiguous run, when pages beyond it arrived: it and everything
  /// after it stay unACKed on the pendant.
  public let blockedAt: Int?

  /// The drain-end log line, built here so a test pins its wording; the app logs it as public.
  public var summary: String {
    let pendant = status.map { "\($0.storage.oldestPage)...\($0.storage.newestPage)" } ?? "unknown"
    return "drain ended \(end.rawValue): saved \(pagesSaved) pages, "
      + "acked through \(ackedThrough.map(String.init) ?? "none"), "
      + "blocked at \(blockedAt.map(String.init) ?? "none"), pendant pages \(pendant)"
  }
}

/// Drain cycle ported from omi's LimitlessFlashDrainEngine: status, SetMode {1,0}, reassemble pages, append
/// each page's raw StorageBuffer to the sink, fsync, ACK, SetMode {0,0}.
///
/// Invariant: msg 7 deletes the pendant's copy. The engine writes it only after `sink.sync()` returned
/// success, and only up to the last page of the contiguous run of pages it received and saved, so a page whose
/// notification was lost, or whose index cannot be read, is never ACKed past. The engine does not interpret
/// what a page holds: earshotd decodes the saved bytes.
public actor DrainEngine {
  private let transport: PendantTransport
  private let sink: PageSink
  private let timing: DrainTiming
  private let now: @Sendable () -> Date
  private let notice: @Sendable (String) -> Void
  private let onStatus: (@Sendable (DeviceStatus) -> Void)?
  private let queue = PacketQueue()
  private let clock = ContinuousClock()

  private var sessionStarted = false
  private var busy = false
  private var aborted = false
  private var messageIndex = 0
  private var requestId: Int64 = 0

  private var fragments = FragmentBuffer()
  private var cycle = Cycle()

  private struct Cycle {
    var endPage = 0
    var start: Int?
    var contiguous = Int.min
    var received = Set<Int>()
    var maxSeen = -1
    var acked = Int.min
    var pagesSinceAck = 0
    var saved = 0
    var lastPageAt = ContinuousClock.now
    var status: DeviceStatus?
    var syncFailed = false
    var ackedAny: Int?
  }

  public init(
    transport: PendantTransport, sink: PageSink, timing: DrainTiming = DrainTiming(),
    now: @escaping @Sendable () -> Date = { Date() },
    notice: (@Sendable (String) -> Void)? = nil,
    onStatus: (@Sendable (DeviceStatus) -> Void)? = nil
  ) {
    self.transport = transport
    self.sink = sink
    self.timing = timing
    self.now = now
    self.notice = notice ?? logDrainNotice
    self.onStatus = onStatus
  }

  // MARK: Session

  /// Call on every connect. SetCurrentTime (msg 6) goes out before any other command; the pendant stops
  /// recording and blinks red without a clock.
  public func beginSession() async throws {
    messageIndex = 0
    requestId = 0
    aborted = false
    queue.clear()
    fragments = FragmentBuffer()
    let q = queue
    let tap = onStatus
    // Every status reply, polled or pushed by the pendant on a mode change, is read the same way and handed
    // on; the packet still goes to the inbox untouched.
    await transport.setNotificationHandler {
      q.push($0)
      if let tap, let status = DeviceStatus.parse($0) { tap(status) }
    }
    try await send {
      LimitlessProtocol.encodeSetCurrentTime(
        messageIndex: $0, requestId: $1, timestampMs: Int64(now().timeIntervalSince1970 * 1000))
    }
    sessionStarted = true
  }

  /// Ends the running cycle promptly (it still tries to ACK what is durable and restore {0,0}).
  public func abort() {
    aborted = true
    queue.wake()
  }

  // MARK: Cycle

  /// The connected session's schedule: wait `firstDelay`, then one cycle every `interval` until cancelled. A
  /// cycle that ended at the page limit leaves pages on the pendant, so the next one starts at once.
  /// The pendant is sent nothing between cycles: no status poll, no battery read.
  public func runLoop(
    firstDelay: Duration, interval: Duration,
    willCycle: @Sendable () async -> Void, didCycle: @Sendable (DrainResult) async -> Void
  ) async {
    try? await Task.sleep(for: firstDelay)
    while !Task.isCancelled {
      await willCycle()
      let result = await runCycle()
      await didCycle(result)
      if result.end == .pageLimit { continue }
      try? await Task.sleep(for: interval)
    }
  }

  public func runCycle() async -> DrainResult {
    guard sessionStarted, !busy else { return result(.busy) }
    busy = true
    aborted = false
    defer { busy = false }
    queue.clear()
    fragments = FragmentBuffer()
    cycle = Cycle()

    do {
      try await send {
        LimitlessProtocol.encodeSetCurrentTime(
          messageIndex: $0, requestId: $1, timestampMs: Int64(now().timeIntervalSince1970 * 1000))
      }
      try await send { LimitlessProtocol.encodeGetDeviceStatus(messageIndex: $0, requestId: $1) }
    } catch {
      return result(.transportFailed)
    }
    guard let status = await awaitStatus() else { return result(aborted ? .aborted : .noStatus) }
    cycle.status = status
    let storage = status.storage
    if storage.newestPage < storage.oldestPage { return result(.nothingToDrain) }

    cycle.endPage = storage.newestPage
    if storage.oldestPage != 0 {
      // Right after a factory reset the pendant reports oldest 0 or absent; otherwise pages
      // run contiguously from oldest, and a hole in that run blocks the ACK.
      cycle.start = storage.oldestPage
      cycle.contiguous = storage.oldestPage - 1
      cycle.acked = cycle.contiguous
    }
    cycle.lastPageAt = clock.now
    do {
      try await send {
        LimitlessProtocol.encodeDownloadFlashPages(
          messageIndex: $0, requestId: $1, batchMode: true, realTime: false)
      }
    } catch {
      return await finish(.transportFailed)
    }

    while true {
      if aborted { return await finish(.aborted) }
      var remaining = timing.stall - (clock.now - cycle.lastPageAt)
      if remaining <= .zero { return await finish(.stall) }
      // Every page below the newest is in hand: the newest is the one the pendant is still writing and
      // may never send, so a short quiet ends the cycle and the next one takes it.
      var onlyNewestMissing = false
      if cycle.start != nil, cycle.contiguous >= cycle.endPage - 1 {
        let quiet = timing.newestQuiet - (clock.now - cycle.lastPageAt)
        if quiet <= .zero { return await finish(.caughtUp) }
        if quiet < remaining {
          remaining = quiet
          onlyNewestMissing = true
        }
      }
      guard let packet = await queue.next(timeout: remaining) else {
        if aborted { return await finish(.aborted) }
        return await finish(onlyNewestMissing ? .caughtUp : .stall)
      }
      for page in reassemble(packet) {
        if let end = await handle(page) { return await finish(end) }
      }
    }
  }

  // MARK: Pages

  /// Saves the raw StorageBuffer first, whatever it holds; only then does its index count toward the run. A
  /// buffer with no readable index is saved but cannot extend the run, so nothing past it is ACKed.
  private func handle(_ page: RawPage) async -> DrainEnd? {
    cycle.lastPageAt = clock.now

    do {
      try sink.append(page.bytes)
      cycle.saved += 1
    } catch {
      return .appendFailed
    }
    guard let index = page.index else {
      notice("received a page without a readable index; saved its raw bytes, ACK held at it")
      return nil
    }
    if cycle.start == nil {
      cycle.start = index
      cycle.contiguous = index - 1
      cycle.acked = cycle.contiguous
    }
    if index > cycle.contiguous {
      cycle.received.insert(index)
      while cycle.received.remove(cycle.contiguous + 1) != nil { cycle.contiguous += 1 }
    }
    cycle.maxSeen = max(cycle.maxSeen, index)
    cycle.pagesSinceAck += 1

    if cycle.pagesSinceAck >= timing.ackEveryPages {
      switch await ackDurable() {
      case .ok: break
      case .syncFailed: return .syncFailed
      case .writeFailed: return .transportFailed
      }
    }
    if cycle.maxSeen >= cycle.endPage { return .caughtUp }
    if cycle.saved >= timing.pageLimit { return .pageLimit }
    return nil
  }

  private enum AckOutcome { case ok, syncFailed, writeFailed }

  /// fsync barrier, then ACK the contiguous run. The ACK write is unreachable unless `sync()` succeeded.
  private func ackDurable() async -> AckOutcome {
    cycle.pagesSinceAck = 0
    guard cycle.start != nil, cycle.contiguous > cycle.acked else { return .ok }
    let upTo = cycle.contiguous
    do { try sink.sync() } catch {
      cycle.syncFailed = true
      return .syncFailed
    }
    do {
      try await send {
        LimitlessProtocol.encodeAcknowledgeProcessedData(
          messageIndex: $0, requestId: $1, upToIndex: upTo)
      }
    } catch {
      return .writeFailed
    }
    cycle.acked = upTo
    cycle.ackedAny = upTo
    return .ok
  }

  private func finish(_ reason: DrainEnd) async -> DrainResult {
    var end = reason
    let clean: (DrainEnd) -> Bool = { $0 == .caughtUp || $0 == .stall || $0 == .pageLimit }
    let dropped = queue.takeDropped()
    if dropped > 0 {
      notice("the inbox was full; dropped \(dropped) packets; their pages stay on the pendant")
    }
    if !cycle.syncFailed {
      switch await ackDurable() {
      case .ok: break
      case .syncFailed: if clean(end) { end = .syncFailed }
      case .writeFailed: if clean(end) { end = .transportFailed }
      }
    }
    // Back to record-to-flash whatever happened above.
    do {
      try await send {
        LimitlessProtocol.encodeDownloadFlashPages(
          messageIndex: $0, requestId: $1, batchMode: false, realTime: false)
      }
    } catch {
      if clean(end) { end = .transportFailed }
    }
    return result(end)
  }

  private func result(_ end: DrainEnd) -> DrainResult {
    DrainResult(
      end: end, status: cycle.status, pagesSaved: cycle.saved, ackedThrough: cycle.ackedAny,
      blockedAt: cycle.received.isEmpty ? nil : cycle.contiguous + 1)
  }

  // MARK: IO

  private func awaitStatus() async -> DeviceStatus? {
    let deadline = clock.now.advanced(by: timing.statusTimeout)
    while !aborted {
      let remaining = deadline - clock.now
      if remaining <= .zero { return nil }
      guard let packet = await queue.next(timeout: remaining) else { return nil }
      if let s = DeviceStatus.parse(Data(packet)) { return s }
    }
    return nil
  }

  /// Reassemble wrapper fragments by index and seq, then pull each StorageBuffer out of the pendant message.
  /// The buffers are returned as they came, in order; nothing here reads what they hold beyond their page
  /// index.
  private func reassemble(_ raw: Data) -> [RawPage] {
    guard let packet = LimitlessProtocol.parseBlePacket(Data(raw)) else { return [] }
    let added = fragments.add(packet)
    for drop in added.drops { notice(drop) }
    guard let whole = added.whole else { return [] }
    return RawPage.storageBuffers(in: whole).map { RawPage(bytes: $0) }
  }

  private func send(_ build: (Int, Int64) -> Data) async throws {
    let index = messageIndex
    messageIndex += 1
    requestId += 1
    try await transport.write(build(index, requestId))
  }
}

/// Wrapper fragments waiting for the rest of their message, bounded in what a device can make it hold: at most
/// `maxIndices` incomplete messages of at most `maxFragments` fragments and `maxMessageBytes` each. A message
/// that breaks a bound is dropped, never completed from what is left, and its page stays on the pendant.
struct FragmentBuffer {
  static let maxFragments = 64
  static let maxMessageBytes = 64 * 1024
  static let maxIndices = 8

  struct Added {
    /// The message, concatenated in seq order, when this fragment completed it.
    var whole: Data?
    /// One notice line per message dropped by this fragment.
    var drops: [String] = []
  }

  private struct Message {
    var numFrags: Int
    var parts: [Int: Data] = [:]
    var bytes = 0
  }

  private var messages: [Int: Message] = [:]
  /// Indices in the order first seen.
  private var order: [Int] = []

  var indexCount: Int { messages.count }
  var totalBytes: Int { messages.values.reduce(0) { $0 + $1.bytes } }
  var largestMessageBytes: Int { messages.values.map(\.bytes).max() ?? 0 }

  mutating func add(_ packet: LimitlessProtocol.BlePacket) -> Added {
    let index = packet.index
    guard (1...Self.maxFragments).contains(packet.numFrags) else {
      return drop(index, "numFrags \(packet.numFrags) is outside 1...\(Self.maxFragments)")
    }
    guard (0..<packet.numFrags).contains(packet.seq) else {
      return drop(index, "seq \(packet.seq) is not below numFrags \(packet.numFrags)")
    }
    var added = Added()
    if let known = messages[index], known.numFrags != packet.numFrags {
      return drop(index, "fragments name numFrags \(known.numFrags) and \(packet.numFrags)")
    }
    if messages[index] == nil {
      if messages.count >= Self.maxIndices, let oldest = order.first {
        remove(oldest)
        added.drops.append(
          "dropped reassembly of index \(oldest): \(Self.maxIndices) incomplete messages held")
      }
      messages[index] = Message(numFrags: packet.numFrags)
      order.append(index)
    }
    var message = messages[index]!
    message.bytes += packet.payload.count - (message.parts[packet.seq]?.count ?? 0)
    message.parts[packet.seq] = packet.payload
    if message.bytes > Self.maxMessageBytes {
      var dropped = drop(index, "message passes \(Self.maxMessageBytes) bytes")
      dropped.drops.insert(contentsOf: added.drops, at: 0)
      return dropped
    }
    messages[index] = message
    guard message.parts.count == message.numFrags else { return added }
    var whole = Data()
    for i in 0..<message.numFrags {
      if let part = message.parts[i] { whole.append(part) }
    }
    remove(index)
    added.whole = whole
    return added
  }

  private mutating func remove(_ index: Int) {
    messages.removeValue(forKey: index)
    order.removeAll { $0 == index }
  }

  private mutating func drop(_ index: Int, _ why: String) -> Added {
    remove(index)
    return Added(drops: [
      "dropped reassembly of index \(index): \(why); its page stays on the pendant"
    ])
  }
}

/// One StorageBuffer exactly as the pendant sent it, with its page index (field 5) when that reads cleanly.
/// The drain saves `bytes` and uses `index` only to order the ACK; it interprets nothing else.
struct RawPage: Equatable {
  let bytes: Data
  let index: Int?

  init(bytes: Data) {
    self.bytes = bytes
    index = Self.pageIndex(of: bytes)
  }

  /// The StorageBuffer payloads (PendantMessage field 2), in order. An empty one holds nothing to save.
  static func storageBuffers(in message: Data) -> [Data] {
    var buffers: [Data] = []
    var pos = 0
    while pos < message.count {
      let (tag, afterTag) = LimitlessProtocol.decodeVarint(message, pos)
      pos = afterTag
      switch tag & 0x07 {
      case 0: pos = LimitlessProtocol.decodeVarint(message, pos).1
      case 1: pos += 8
      case 2:
        let (length, next) = LimitlessProtocol.decodeVarint(message, pos)
        pos = next
        guard length >= 0, length <= Int64(message.count - pos) else { return buffers }
        if tag >> 3 == 2, length > 0 {
          buffers.append(message.subdata(in: pos..<pos + Int(length)))
        }
        pos += Int(length)
      case 5: pos += 4
      default: return buffers
      }
    }
    return buffers
  }

  /// StorageBuffer field 5, or nil when the buffer's top-level fields do not parse cleanly end to end, field 5
  /// is absent, or it is negative: an index that cannot be trusted must not move the ACK.
  static func pageIndex(of buffer: Data) -> Int? {
    var pos = 0
    var index: Int?
    while pos < buffer.count {
      guard let (tag, afterTag) = varint(buffer, pos) else { return nil }
      pos = afterTag
      switch tag & 0x07 {
      case 0:
        guard let (value, next) = varint(buffer, pos) else { return nil }
        pos = next
        if tag >> 3 == 5 { index = Int(exactly: value) }
      case 1: pos += 8
      case 2:
        guard let (length, next) = varint(buffer, pos) else { return nil }
        pos = next
        guard length >= 0, length <= Int64(buffer.count - pos) else { return nil }
        pos += Int(length)
      case 5: pos += 4
      default: return nil
      }
    }
    guard pos == buffer.count, let index, index >= 0 else { return nil }
    return index
  }

  /// A varint at `start`, or nil when the bytes end before its last byte (`decodeVarint` returns the partial
  /// value instead).
  private static func varint(_ data: Data, _ start: Int) -> (Int64, Int)? {
    var pos = start
    while pos < data.count {
      if data[pos] & 0x80 == 0 { return LimitlessProtocol.decodeVarint(data, start) }
      pos += 1
    }
    return nil
  }
}

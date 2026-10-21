import Foundation

/// What the menu needs from one saved StorageBuffer: its battery records, opus frame count, and the
/// button's idle/wake log lines. Display only. The drain never calls it: what is saved and ACKed is decided
/// without reading what a page holds, so a decoder bug can only mislabel the menu.
///
/// Wire shape (docs/protocol.md, Flash page records): StorageBuffer field 5 is the page index and field 6 the
/// flash page; the flash page's field 1 is its timestamp in ms and each field 3 a wrapper whose field 1 is a
/// ms offset from that timestamp. A wrapper's field 2 holds opus frames nested at any depth, field 6 a battery
/// record `{2: mV, 3: mA, 4: degrees C, 5: percent}`, field 10 a firmware log line `{1: text}`.
public struct PageDigest: Sendable, Equatable {
  public struct Battery: Sendable, Equatable {
    public let at: Date
    public let millivolts: Int
    /// Signed: negative while discharging, positive while charging.
    public let milliamps: Int
    public let percent: Int

    public var charging: Bool { milliamps > 0 }
  }

  /// The button toggling the pendant between recording and idle mode.
  public struct ModeEvent: Sendable, Equatable {
    public enum Kind: Sendable, Equatable { case idle, wake }
    public let at: Date
    public let kind: Kind
  }

  static let idleLine = "Switching to idle mode after event BUTTON_LONG_PRESS"
  static let wakeLine = "Waking up from idle mode after event BUTTON_LONG_PRESS"

  public let pageIndex: Int?
  /// The flash page timestamp; nil for a page with no flash page (a CRC-failed, header-only page).
  public let pageTime: Date?
  public let opusFrames: Int
  public let batteries: [Battery]
  public let modeEvents: [ModeEvent]

  init(
    pageIndex: Int? = nil, pageTime: Date? = nil, opusFrames: Int = 0, batteries: [Battery] = [],
    modeEvents: [ModeEvent] = []
  ) {
    self.pageIndex = pageIndex
    self.pageTime = pageTime
    self.opusFrames = opusFrames
    self.batteries = batteries
    self.modeEvents = modeEvents
  }

  /// Best effort and total: a buffer that does not parse yields whatever was read before the break.
  public init(storageBuffer: Data) {
    var index: Int?
    var flashPage: Data?
    for field in Wire.fields(storageBuffer) {
      switch field.value {
      case .varint(let v) where field.number == 5: index = Int(exactly: v)
      case .bytes(let d) where field.number == 6: flashPage = d
      default: break
      }
    }
    pageIndex = index
    guard let flashPage else {
      pageTime = nil
      opusFrames = 0
      batteries = []
      modeEvents = []
      return
    }

    var timestampMs: Int64?
    var wrappers: [Data] = []
    for field in Wire.fields(flashPage) {
      switch field.value {
      case .varint(let v) where field.number == 1: timestampMs = v
      case .bytes(let d) where field.number == 3: wrappers.append(d)
      default: break
      }
    }
    pageTime = timestampMs.map { Date(timeIntervalSince1970: Double($0) / 1000) }

    var frames = 0
    var batteries: [Battery] = []
    var events: [ModeEvent] = []
    for wrapper in wrappers {
      var offsetMs: Int64 = 0
      var audio: [Data] = []
      var battery: Data?
      var logs: [Data] = []
      for field in Wire.fields(wrapper) {
        switch field.value {
        case .varint(let v) where field.number == 1: offsetMs = v
        case .bytes(let d):
          switch field.number {
          case 2: audio.append(d)
          case 6: battery = d
          case 10: logs.append(d)
          default: break
          }
        default: break
        }
      }
      for blob in audio { frames += Self.countOpusFrames(blob) }
      // An unset offset is all ones; a record with no timestamp has no time to show.
      guard let timestampMs, offsetMs >= 0, offsetMs < 86_400_000 else { continue }
      let at = Date(timeIntervalSince1970: Double(timestampMs + offsetMs) / 1000)
      if let battery, let record = Self.battery(battery, at: at) { batteries.append(record) }
      for log in logs {
        guard let text = Self.logText(log) else { continue }
        if text.contains(Self.idleLine) {
          events.append(ModeEvent(at: at, kind: .idle))
        } else if text.contains(Self.wakeLine) {
          events.append(ModeEvent(at: at, kind: .wake))
        }
      }
    }
    opusFrames = frames
    self.batteries = batteries
    modeEvents = events
  }

  private static func battery(_ data: Data, at: Date) -> Battery? {
    var mv: Int?
    var ma: Int?
    var pct: Int?
    for field in Wire.fields(data) {
      guard case .varint(let v) = field.value else { continue }
      switch field.number {
      case 2: mv = Int(exactly: v)
      // milliamps is a negative number sent as a 64-bit varint, which Wire reads back as the signed value.
      case 3: ma = Int(exactly: v)
      case 5: pct = Int(exactly: v)
      default: break
      }
    }
    guard let mv, let pct, (0...100).contains(pct) else { return nil }
    return Battery(at: at, millivolts: mv, milliamps: ma ?? 0, percent: pct)
  }

  private static func logText(_ data: Data) -> String? {
    for field in Wire.fields(data) {
      if field.number == 1, case .bytes(let d) = field.value {
        return String(decoding: d, as: UTF8.self)
      }
    }
    return nil
  }

  /// Opus frames in an audio blob: length-delimited fields of 10...200 bytes opening with an opus TOC byte,
  /// found at any nesting depth (the same shape the codec accepts).
  private static func countOpusFrames(_ data: Data, depth: Int = 0) -> Int {
    guard depth < 8 else { return 0 }
    var count = 0
    for field in Wire.fields(data) {
      guard case .bytes(let d) = field.value, !d.isEmpty else { continue }
      if d.count >= 10, d.count <= 200, LimitlessProtocol.isValidOpusToc(d[d.startIndex]) {
        count += 1
      } else if d.count > 10 {
        count += countOpusFrames(d, depth: depth + 1)
      }
    }
    return count
  }
}

/// A protobuf field walker that stops at the first byte it cannot read.
enum Wire {
  enum Value {
    case varint(Int64)
    case bytes(Data)
    case fixed
  }

  struct Field {
    let number: Int
    let value: Value
  }

  static func fields(_ data: Data) -> [Field] {
    var out: [Field] = []
    var pos = data.startIndex
    while pos < data.endIndex {
      guard let (tag, afterTag) = varint(data, pos), tag > 0 else { break }
      pos = afterTag
      let number = Int(truncatingIfNeeded: tag >> 3)
      switch tag & 7 {
      case 0:
        guard let (v, next) = varint(data, pos) else { return out }
        pos = next
        out.append(Field(number: number, value: .varint(v)))
      case 1, 5:
        let width = (tag & 7) == 1 ? 8 : 4
        guard data.endIndex - pos >= width else { return out }
        pos += width
        out.append(Field(number: number, value: .fixed))
      case 2:
        guard let (length, next) = varint(data, pos), length >= 0,
          length <= Int64(data.endIndex - next)
        else { return out }
        pos = next + Int(length)
        out.append(Field(number: number, value: .bytes(data.subdata(in: next..<pos))))
      default: return out
      }
    }
    return out
  }

  /// A varint at `start`, or nil when the bytes end first or it runs past ten bytes.
  private static func varint(_ data: Data, _ start: Data.Index) -> (Int64, Data.Index)? {
    var result: UInt64 = 0
    var shift: UInt64 = 0
    var pos = start
    while pos < data.endIndex, shift < 70 {
      let byte = data[pos]
      pos += 1
      if shift < 64 { result |= UInt64(byte & 0x7f) << shift }
      if byte & 0x80 == 0 { return (Int64(bitPattern: result), pos) }
      shift += 7
    }
    return nil
  }
}

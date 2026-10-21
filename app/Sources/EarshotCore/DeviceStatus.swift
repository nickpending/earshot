import Foundation

/// The pendant's own account of itself, read from one GetDeviceStatus reply (polled or pushed): storage as
/// omi's `parseDeviceStatus` reads it, plus the device mode and battery that omi's codec leaves alone. Display
/// only: the drain's saves and ACKs never depend on the mode or battery fields.
///
/// Wire shape (docs/protocol.md, Device status reply, beyond storage): BLE wrapper field 4 holds a message
/// whose field 5 is the status; inside it field 5 is storage, field 7 the device mode (empty while the button
/// has the pendant idle, `{1: 1}` or `{1: 2}` while recording) and field 9 the battery
/// `{2: mV, 3: mA (signed, omitted at 0), 4: degrees C, 5: percent}`.
public struct DeviceStatus: Sendable, Equatable {
  public enum Mode: Sendable, Equatable {
    /// Field 7 holds a non-zero mode: recording.
    case on
    /// Field 7 is present and empty (or zero): the button has the pendant idle.
    case off
    /// The reply has no field 7: nothing is claimed about the button.
    case unknown
  }

  public struct Battery: Sendable, Equatable {
    public let millivolts: Int
    /// Signed; nil when the reply omitted it (zero current).
    public let milliamps: Int?
    public let tempC: Int
    public let percent: Int

    public var charging: Bool { (milliamps ?? 0) > 0 }
  }

  public let storage: StorageStatus
  public let mode: Mode
  public let battery: Battery?

  /// The status in one RX notification (BLE wrapper included), or nil when it is not a status reply.
  public static func parse(_ notification: Data) -> DeviceStatus? {
    guard let status = statusFields(notification),
      let state = LimitlessProtocol.parseDeviceStatus(notification)
    else { return nil }
    var mode = Mode.unknown
    var battery: Battery?
    for field in Wire.fields(status) {
      guard case .bytes(let body) = field.value else { continue }
      switch field.number {
      case 7: mode = parseMode(body)
      case 9: battery = parseBattery(body)
      default: break
      }
    }
    return DeviceStatus(storage: StorageStatus(state), mode: mode, battery: battery)
  }

  /// Wrapper field 4 -> field 5, and only when the status holds storage (its field 5): a page or a fragment
  /// does not have that shape.
  private static func statusFields(_ notification: Data) -> Data? {
    for wrapper in Wire.fields(notification) where wrapper.number == 4 {
      guard case .bytes(let message) = wrapper.value else { continue }
      for field in Wire.fields(message) where field.number == 5 {
        guard case .bytes(let status) = field.value else { continue }
        if Wire.fields(status).contains(where: { $0.number == 5 }) { return status }
      }
    }
    return nil
  }

  private static func parseMode(_ body: Data) -> Mode {
    for field in Wire.fields(body) where field.number == 1 {
      if case .varint(let v) = field.value, v != 0 { return .on }
    }
    return .off
  }

  private static func parseBattery(_ body: Data) -> Battery? {
    var mv: Int?
    var ma: Int?
    var temp: Int?
    var pct: Int?
    for field in Wire.fields(body) {
      guard case .varint(let v) = field.value, let n = Int(exactly: v) else { continue }
      switch field.number {
      case 2: mv = n
      case 3: ma = n
      case 4: temp = n
      case 5: pct = n
      default: break
      }
    }
    guard let mv, let temp, let pct else { return nil }
    return Battery(millivolts: mv, milliamps: ma, tempC: temp, percent: pct)
  }
}

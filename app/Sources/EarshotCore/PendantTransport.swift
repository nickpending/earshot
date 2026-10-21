import Foundation

/// The Bluetooth boundary. The CoreBluetooth implementation lives in the app target; tests inject a fake.
///
/// A transport moves bytes and nothing else: command encoding and reply parsing belong to the drain engine
/// and the vendored codec. `write` targets the Limitless TX characteristic (service 632DE001, char 632DE002)
/// and nothing else; no other characteristic is ever written.
public protocol PendantTransport: AnyObject, Sendable {
  /// Write one wrapped command to TX. Returns once the pendant acknowledged the write; throws if it failed.
  func write(_ data: Data) async throws

  /// Register the single receiver of RX (632DE003) notifications, in arrival order.
  func setNotificationHandler(_ handler: @escaping @Sendable (Data) -> Void) async

  /// Battery percent from 180F/2A19.
  func readBattery() async throws -> Int
}

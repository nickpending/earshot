import CoreBluetooth
import EarshotCore
import Foundation
import os

private let log = Logger(subsystem: "casa.mal.Earshot", category: "bluetooth")

enum TransportError: Error {
  case notConnected
  case unreadable
}

enum TransportEvent: Sendable {
  case unavailable(String)
  case scanning
  case ready
  case disconnected
  /// A device advertising the Limitless service while nothing is chosen.
  case seen(identifier: UUID, name: String, rssi: Int)
}

/// CoreBluetooth side of `PendantTransport`. All CoreBluetooth state is touched on the main queue only.
///
/// Discovery asks for exactly two services, the Limitless service and Battery 180F, so the SMP service
/// (firmware update) is never discovered and nothing can write to it. The only `writeValue` call is on
/// the TX characteristic.
///
/// A device is connected only when `PendantChoice.decide` says it is the pendant the operator chose; with
/// nothing chosen the transport only reports what it sees.
final class CoreBluetoothTransport: NSObject, PendantTransport, @unchecked Sendable {
  nonisolated(unsafe) private static let limitless = CBUUID(
    string: "632DE001-604C-446B-A80F-7963E950F3FB")
  nonisolated(unsafe) private static let txUUID = CBUUID(
    string: "632DE002-604C-446B-A80F-7963E950F3FB")
  nonisolated(unsafe) private static let rxUUID = CBUUID(
    string: "632DE003-604C-446B-A80F-7963E950F3FB")
  nonisolated(unsafe) private static let battery = CBUUID(string: "180F")
  nonisolated(unsafe) private static let batteryLevel = CBUUID(string: "2A19")

  var onEvent: (@Sendable (TransportEvent) -> Void)?

  private var central: CBCentralManager?
  private var peripheral: CBPeripheral?
  private var tx: CBCharacteristic?
  private var rx: CBCharacteristic?
  private var batteryChar: CBCharacteristic?
  private var handler: (@Sendable (Data) -> Void)?
  private var pendingWrite: CheckedContinuation<Void, Error>?
  private var pendingBattery: CheckedContinuation<Int, Error>?
  private var wantScan = false
  /// The pendant the operator chose; main queue only.
  private var chosen: UUID?
  private var ignoredLogged = Set<UUID>()

  /// Connect to the chosen pendant from now on, and to nothing else.
  func choose(_ identifier: UUID?) {
    DispatchQueue.main.async {
      self.chosen = identifier
      if self.central != nil, self.peripheral == nil {
        self.central?.stopScan()
        self.scanIfPossible()
      }
    }
  }

  /// Drop the choice: cancel a pending or live connection and list what is nearby again.
  func forget() {
    DispatchQueue.main.async {
      self.chosen = nil
      if let p = self.peripheral { self.central?.cancelPeripheralConnection(p) }
      self.dropConnection()
      self.central?.stopScan()
      self.scanIfPossible()
    }
  }

  /// Begin (or resume) scanning; connects only to the chosen pendant.
  func start() {
    DispatchQueue.main.async {
      self.wantScan = true
      if self.central == nil {
        self.central = CBCentralManager(delegate: self, queue: .main)
      } else {
        self.scanIfPossible()
      }
    }
  }

  // MARK: PendantTransport

  func write(_ data: Data) async throws {
    try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
      DispatchQueue.main.async {
        guard let p = self.peripheral, let tx = self.tx, self.pendingWrite == nil else {
          cont.resume(throwing: TransportError.notConnected)
          return
        }
        self.pendingWrite = cont
        p.writeValue(data, for: tx, type: .withResponse)
      }
    }
  }

  func setNotificationHandler(_ handler: @escaping @Sendable (Data) -> Void) async {
    await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
      DispatchQueue.main.async {
        self.handler = handler
        cont.resume()
      }
    }
  }

  func readBattery() async throws -> Int {
    try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Int, Error>) in
      DispatchQueue.main.async {
        guard let p = self.peripheral, let c = self.batteryChar, self.pendingBattery == nil else {
          cont.resume(throwing: TransportError.notConnected)
          return
        }
        self.pendingBattery = cont
        p.readValue(for: c)
      }
    }
  }

  // MARK: Internals

  private func scanIfPossible() {
    guard wantScan, let central, central.state == .poweredOn, peripheral == nil else { return }
    // Repeated sightings keep the menu's signal and staleness live, only while it is listing.
    central.scanForPeripherals(
      withServices: [Self.limitless],
      options: [CBCentralManagerScanOptionAllowDuplicatesKey: chosen == nil])
    onEvent?(.scanning)
  }

  private func dropConnection() {
    peripheral = nil
    tx = nil
    rx = nil
    batteryChar = nil
    pendingWrite?.resume(throwing: TransportError.notConnected)
    pendingWrite = nil
    pendingBattery?.resume(throwing: TransportError.notConnected)
    pendingBattery = nil
  }
}

extension CoreBluetoothTransport: CBCentralManagerDelegate {
  func centralManagerDidUpdateState(_ central: CBCentralManager) {
    switch central.state {
    case .poweredOn: scanIfPossible()
    case .unauthorized: onEvent?(.unavailable("Bluetooth not allowed for Earshot"))
    case .poweredOff: onEvent?(.unavailable("Bluetooth is off"))
    default: onEvent?(.unavailable("Bluetooth unavailable"))
    }
  }

  func centralManager(
    _ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
    advertisementData: [String: Any], rssi: NSNumber
  ) {
    switch PendantChoice.decide(discovered: peripheral.identifier, chosen: chosen) {
    case .connect:
      central.stopScan()
      self.peripheral = peripheral
      peripheral.delegate = self
      central.connect(peripheral)
    case .list:
      let name =
        advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? peripheral.name
        ?? "Unknown"
      onEvent?(.seen(identifier: peripheral.identifier, name: name, rssi: rssi.intValue))
    case .ignore:
      // Bounded: a device rotating its address makes a new identifier each time.
      if ignoredLogged.count >= 64 { ignoredLogged.removeAll() }
      if ignoredLogged.insert(peripheral.identifier).inserted {
        log.notice("ignored a device that is not the chosen pendant")
      }
    }
  }

  func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
    peripheral.discoverServices([Self.limitless, Self.battery])
  }

  func centralManager(
    _ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?
  ) {
    dropConnection()
    onEvent?(.disconnected)
  }

  func centralManager(
    _ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?
  ) {
    dropConnection()
    onEvent?(.disconnected)
  }
}

extension CoreBluetoothTransport: CBPeripheralDelegate {
  func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
    for service in peripheral.services ?? [] {
      if service.uuid == Self.limitless {
        peripheral.discoverCharacteristics([Self.txUUID, Self.rxUUID], for: service)
      } else if service.uuid == Self.battery {
        peripheral.discoverCharacteristics([Self.batteryLevel], for: service)
      }
    }
  }

  func peripheral(
    _ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?
  ) {
    for c in service.characteristics ?? [] {
      switch c.uuid {
      case Self.txUUID: tx = c
      case Self.rxUUID: rx = c
      case Self.batteryLevel: batteryChar = c
      default: break
      }
    }
    if tx != nil, let rx, batteryChar != nil { peripheral.setNotifyValue(true, for: rx) }
  }

  func peripheral(
    _ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic,
    error: Error?
  ) {
    if characteristic.uuid == Self.rxUUID, characteristic.isNotifying { onEvent?(.ready) }
  }

  func peripheral(
    _ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?
  ) {
    guard let cont = pendingWrite else { return }
    pendingWrite = nil
    if let error { cont.resume(throwing: error) } else { cont.resume() }
  }

  func peripheral(
    _ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?
  ) {
    if characteristic.uuid == Self.rxUUID {
      if let data = characteristic.value { handler?(data) }
    } else if characteristic.uuid == Self.batteryLevel, let cont = pendingBattery {
      pendingBattery = nil
      if let error {
        cont.resume(throwing: error)
      } else if let b = characteristic.value?.first {
        cont.resume(returning: Int(b))
      } else {
        cont.resume(throwing: TransportError.unreadable)
      }
    }
  }
}

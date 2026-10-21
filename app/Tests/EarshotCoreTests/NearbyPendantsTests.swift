import Foundation
import Testing

@testable import EarshotCore

/// C3: the menu offers the devices seen in the last 30 s, strongest signal first, labelled so two pendants
/// that share a name can be told apart.
@Suite struct NearbyPendantsTests {
  static let x = UUID(uuidString: "AAAA0000-0000-4000-8000-000000000001")!
  static let y = UUID(uuidString: "BBBB0000-0000-4000-8000-000000000002")!
  static let z = UUID(uuidString: "CCCC0000-0000-4000-8000-000000000003")!

  @Test func listsFreshDevicesStrongestFirstAndDropsStaleOnes() {
    let now = Date(timeIntervalSince1970: 1_750_000_000)
    var nearby = NearbyPendants()
    nearby.see(identifier: Self.x, name: "Pendant", rssi: -70, at: now)
    nearby.see(identifier: Self.y, name: "Pendant", rssi: -48, at: now)
    nearby.see(identifier: Self.z, name: "Pendant", rssi: -30, at: now.addingTimeInterval(-31))

    let entries = nearby.entries(now: now)
    #expect(entries.map(\.identifier) == [Self.y, Self.x])
    #expect(entries.map(\.label) == ["Pendant  -48 dBm  bbbb", "Pendant  -70 dBm  aaaa"])
  }

  @Test func aDeviceRotatingItsAddressCannotGrowTheListWithoutLimit() {
    let start = Date(timeIntervalSince1970: 1_750_000_000)
    var nearby = NearbyPendants()
    for i in 0..<1000 {
      nearby.see(
        identifier: UUID(), name: "Pendant", rssi: -60,
        at: start.addingTimeInterval(Double(i) / 100))
    }
    #expect(nearby.heldCount <= NearbyPendants.maxHeld)
    // Entries gone stale are pruned when the next one is seen, not only hidden on read.
    nearby.see(identifier: UUID(), name: "Pendant", rssi: -60, at: start.addingTimeInterval(500))
    #expect(nearby.heldCount == 1)
  }

  @Test func aDeviceSeenAgainStaysListedWithItsNewSignal() {
    let now = Date(timeIntervalSince1970: 1_750_000_000)
    var nearby = NearbyPendants()
    nearby.see(identifier: Self.x, name: "Pendant", rssi: -70, at: now.addingTimeInterval(-40))
    nearby.see(identifier: Self.x, name: "Pendant", rssi: -55, at: now)
    #expect(nearby.entries(now: now).map(\.rssi) == [-55])
  }
}

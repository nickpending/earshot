import Foundation

/// The pendant the operator picked, kept as CoreBluetooth's per-Mac peripheral identifier.
public protocol PendantChoiceStore: Sendable {
  func load() -> UUID?
  /// nil forgets the choice.
  func save(_ identifier: UUID?)
}

/// The choice in UserDefaults, so a relaunch reconnects to the same pendant. `suiteName` nil is the app's own
/// defaults.
public struct DefaultsPendantChoiceStore: PendantChoiceStore {
  private let suiteName: String?
  private var defaults: UserDefaults {
    suiteName.flatMap { UserDefaults(suiteName: $0) } ?? .standard
  }
  private static let key = "pendant.chosenIdentifier"

  public init(suiteName: String? = nil) { self.suiteName = suiteName }

  public func load() -> UUID? {
    defaults.string(forKey: Self.key).flatMap { UUID(uuidString: $0) }
  }

  public func save(_ identifier: UUID?) {
    if let identifier {
      defaults.set(identifier.uuidString, forKey: Self.key)
    } else {
      defaults.removeObject(forKey: Self.key)
    }
  }
}

/// What to do with a device seen advertising the Limitless service.
public enum PendantDecision: Sendable, Equatable {
  /// It is the pendant the operator chose.
  case connect
  /// Nothing is chosen: show it in the menu and connect to nothing.
  case list
  /// Something else is chosen, and this is not it.
  case ignore
}

public enum PendantChoice {
  /// The only place that decides whether a discovered device may be connected: only the chosen identifier is.
  public static func decide(discovered: UUID, chosen: UUID?) -> PendantDecision {
    guard let chosen else { return .list }
    return discovered == chosen ? .connect : .ignore
  }

  /// The identifier's first four hex digits, the part the menu shows.
  public static func shortId(_ identifier: UUID) -> String {
    String(identifier.uuidString.replacingOccurrences(of: "-", with: "").prefix(4)).lowercased()
  }
}

/// The devices seen advertising while nothing is chosen, for the menu to offer.
public struct NearbyPendants: Sendable {
  public struct Entry: Sendable, Equatable {
    public let identifier: UUID
    public let name: String
    public let rssi: Int
    public let lastSeen: Date

    /// Name, signal in dBm and the identifier's first four hex digits.
    public var label: String { "\(name)  \(rssi) dBm  \(PendantChoice.shortId(identifier))" }
  }

  /// A device not seen for longer than this is no longer listed.
  public static let staleAfter: TimeInterval = 30
  /// Most devices held at once.
  public static let maxHeld = 32

  private var seen: [UUID: Entry] = [:]

  public init() {}

  public mutating func see(identifier: UUID, name: String, rssi: Int, at: Date) {
    seen[identifier] = Entry(identifier: identifier, name: name, rssi: rssi, lastSeen: at)
    seen = seen.filter { at.timeIntervalSince($0.value.lastSeen) <= Self.staleAfter }
    // A device that rotates its address makes a new identifier each time: keep the most recently seen.
    if seen.count > Self.maxHeld {
      let keep = seen.values.sorted { $0.lastSeen > $1.lastSeen }.prefix(Self.maxHeld)
      seen = Dictionary(uniqueKeysWithValues: keep.map { ($0.identifier, $0) })
    }
  }

  /// Devices held, stale or not (a bound on memory, not what the menu shows).
  var heldCount: Int { seen.count }

  public mutating func clear() { seen.removeAll() }

  /// The devices seen within the cutoff, strongest signal first.
  public func entries(now: Date) -> [Entry] {
    seen.values
      .filter { now.timeIntervalSince($0.lastSeen) <= Self.staleAfter }
      .sorted {
        $0.rssi != $1.rssi ? $0.rssi > $1.rssi : $0.identifier.uuidString < $1.identifier.uuidString
      }
  }
}

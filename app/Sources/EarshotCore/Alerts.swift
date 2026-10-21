import Foundation

/// One thing the wearer can act on, in the words the notification carries.
public struct Alert: Sendable, Equatable {
  public enum Kind: String, Sendable, Equatable { case battery, off, sync }
  public let kind: Kind
  public let title: String
  public let body: String
}

/// What the alerts remember between evaluations.
public struct AlertState: Sendable, Equatable {
  /// The time of the battery reading that fired the low-battery alert; cleared by a later charging reading.
  public var batteryFiredAt: Date?
  public var offFired = false
  public var syncFired = false

  public init(batteryFiredAt: Date? = nil, offFired: Bool = false, syncFired: Bool = false) {
    self.batteryFiredAt = batteryFiredAt
    self.offFired = offFired
    self.syncFired = syncFired
  }
}

/// Where `AlertState` lives so it survives a restart; the app backs it with its own defaults.
public protocol AlertStateStore: Sendable {
  func load() -> AlertState
  func save(_ state: AlertState)
}

/// The three alerts, each edge-triggered: it fires when its condition becomes true and not again until the
/// condition has cleared (battery: until a charging reading is seen; off: until audio is heard; sync: until
/// an upload returns 200). The state is saved to the store on every change and read back at launch, so an
/// alert that fired is not repeated after a restart while its condition still holds. The caller decides when
/// to evaluate and where to send what it returns.
public struct Alerts: Sendable {
  /// At or below this percent, discharging.
  public static let batteryPercent = 15
  /// This long with no opus frames in saved pages.
  public static let offAfter: TimeInterval = 10 * 60
  /// This long with no upload returning 200.
  public static let syncAfter: TimeInterval = 6 * 3600

  private let calendar: Calendar
  private let store: AlertStateStore?
  private var state: AlertState {
    didSet { if state != oldValue { store?.save(state) } }
  }

  public init(calendar: Calendar = .current, store: AlertStateStore? = nil) {
    self.calendar = calendar
    self.store = store
    state = store?.load() ?? AlertState()
  }

  /// Every alert condition that is true now, fired before or not: what the menu shows, so a refused
  /// notification still leaves the condition visible.
  public static func conditions(_ facts: StatusFacts, now: Date, calendar: Calendar = .current)
    -> [Alert]
  {
    [battery(facts), off(facts, now: now, calendar: calendar), sync(facts, now: now)]
      .compactMap { $0 }
  }

  /// Battery and off alerts. Call only when the pendant has just been drained to its newest page: against
  /// older pages "no audio" would only mean the backlog is not through yet.
  public mutating func deviceAlerts(_ facts: StatusFacts, now: Date) -> [Alert] {
    var fired: [Alert] = []

    if let alert = Self.battery(facts), let at = facts.currentBattery?.at {
      let rearmed =
        state.batteryFiredAt.map { (facts.lastChargingAt ?? .distantPast) > $0 } ?? true
      if rearmed {
        state.batteryFiredAt = at
        fired.append(alert)
      }
    }

    if let silence = facts.silence(), now.timeIntervalSince(silence.since) >= Self.offAfter {
      // Charging suppresses the alert without ending the period: unplugged and still quiet fires it once.
      if !state.offFired, let alert = Self.off(facts, now: now, calendar: calendar) {
        state.offFired = true
        fired.append(alert)
      }
    } else if facts.audioHeard(within: Self.offAfter, now: now) {
      // Only heard audio clears it: a restart restarts the silence clock, and that is not a clearing.
      state.offFired = false
    }
    return fired
  }

  public mutating func syncAlert(_ facts: StatusFacts, now: Date) -> Alert? {
    guard let alert = Self.sync(facts, now: now) else {
      // Only an upload that returned 200 clears it: right after a launch the clock restarts at zero.
      if facts.lastSyncAt != nil { state.syncFired = false }
      return nil
    }
    guard !state.syncFired else { return nil }
    state.syncFired = true
    return alert
  }

  // MARK: Conditions

  private static func battery(_ facts: StatusFacts) -> Alert? {
    guard let latest = facts.currentBattery, !latest.charging, latest.percent <= batteryPercent
    else { return nil }
    return Alert(
      kind: .battery, title: "Pendant battery low",
      body: "The pendant is at \(latest.percent)% and not charging. Plug it in.")
  }

  private static func off(_ facts: StatusFacts, now: Date, calendar: Calendar) -> Alert? {
    guard !facts.charging, let silence = facts.silence(),
      now.timeIntervalSince(silence.since) >= offAfter
    else { return nil }
    let since = StatusFacts.clock(silence.since, calendar: calendar)
    if silence.byButton {
      return Alert(
        kind: .off, title: "Pendant is off",
        body: "The button turned recording off at \(since); long-press to resume.")
    }
    return Alert(
      kind: .off, title: "Pendant is not recording",
      body: "No audio is being captured since \(since).")
  }

  private static func sync(_ facts: StatusFacts, now: Date) -> Alert? {
    let last = facts.lastSyncAt ?? facts.startedAt
    guard now.timeIntervalSince(last) >= syncAfter else { return nil }
    if let why = facts.syncFailure {
      return Alert(
        kind: .sync, title: "Audio is not syncing",
        body: "No upload has succeeded for 6 hours: earshotd is unreachable (\(why)).")
    }
    return Alert(
      kind: .sync, title: "Audio is not syncing",
      body: "No upload has succeeded for 6 hours: the pendant is out of range.")
  }
}

/// The alerts' memory in UserDefaults, so a relaunch does not repeat an alert that already fired. `suiteName`
/// nil is the app's own defaults.
public struct DefaultsAlertStore: AlertStateStore {
  private let suiteName: String?
  private var defaults: UserDefaults {
    suiteName.flatMap { UserDefaults(suiteName: $0) } ?? .standard
  }
  private static let batteryKey = "alerts.batteryFiredAt"
  private static let offKey = "alerts.offFired"
  private static let syncKey = "alerts.syncFired"

  public init(suiteName: String? = nil) { self.suiteName = suiteName }

  public func load() -> AlertState {
    AlertState(
      batteryFiredAt: defaults.object(forKey: Self.batteryKey) as? Date,
      offFired: defaults.bool(forKey: Self.offKey), syncFired: defaults.bool(forKey: Self.syncKey))
  }

  public func save(_ state: AlertState) {
    if let at = state.batteryFiredAt {
      defaults.set(at, forKey: Self.batteryKey)
    } else {
      defaults.removeObject(forKey: Self.batteryKey)
    }
    defaults.set(state.offFired, forKey: Self.offKey)
    defaults.set(state.syncFired, forKey: Self.syncKey)
  }
}

public enum NotificationPermission: Sendable, Equatable { case notDetermined, granted, denied }

/// The notification centre behind the dispatcher; the app target implements it over UserNotifications.
public protocol AlertCenter: Sendable {
  func permission() async -> NotificationPermission
  /// Asks the user; true when granted.
  func requestPermission() async -> Bool
  func post(_ alert: Alert) async throws
}

/// Sends alerts to the notification centre, asking for permission the first time one is due. When
/// notifications are refused it posts nothing and says so once at notice; the menu still shows the condition.
public actor AlertDispatcher {
  private let center: AlertCenter
  private let notice: @Sendable (String) -> Void
  private var refusalLogged = false

  public init(center: AlertCenter, notice: @escaping @Sendable (String) -> Void) {
    self.center = center
    self.notice = notice
  }

  public func deliver(_ alerts: [Alert]) async {
    guard !alerts.isEmpty else { return }
    var permission = await center.permission()
    if permission == .notDetermined {
      permission = await center.requestPermission() ? .granted : .denied
    }
    guard permission == .granted else {
      if !refusalLogged {
        refusalLogged = true
        notice("notifications are not allowed; alerts show in the menu only")
      }
      return
    }
    for alert in alerts {
      do { try await center.post(alert) } catch {
        notice("could not post \"\(alert.title)\": \(error)")
      }
    }
  }
}

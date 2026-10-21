import Foundation
import Testing

@testable import EarshotCore

/// C5, C6, C7, C10: three edge-triggered alerts and their delivery.
@Suite struct AlertsTests {
  let utc: Calendar = {
    var c = Calendar(identifier: .gregorian)
    c.timeZone = TimeZone(identifier: "UTC")!
    return c
  }()
  /// 2026-10-06 10:00:00 UTC.
  let t0 = Date(timeIntervalSince1970: 1_791_280_800)

  func at(_ minutes: Double) -> Date { t0.addingTimeInterval(minutes * 60) }
  func facts() -> StatusFacts { StatusFacts(startedAt: at(-10), calendar: utc) }
  func alerts(store: AlertStateStore? = nil) -> Alerts { Alerts(calendar: utc, store: store) }

  func battery(_ minutes: Double, pct: Int, ma: Int = -12) -> PageDigest.Battery {
    .init(at: at(minutes), millivolts: 3700, milliamps: ma, percent: pct)
  }

  func page(
    _ minutes: Double, frames: Int = 0, batteries: [PageDigest.Battery] = [],
    events: [PageDigest.ModeEvent] = []
  ) -> PageDigest {
    PageDigest(
      pageTime: at(minutes), opusFrames: frames, batteries: batteries, modeEvents: events)
  }

  // MARK: Battery (C5)

  @Test func lowBatteryFiresOnceAndStaysQuietUntilACharge() {
    var f = facts()
    var a = alerts()
    f.ingest(page(0, frames: 70, batteries: [battery(0, pct: 16)]))
    #expect(a.deviceAlerts(f, now: at(1)).isEmpty, "16 % is above the threshold")

    f.ingest(page(2, frames: 70, batteries: [battery(2, pct: 15)]))
    let fired = a.deviceAlerts(f, now: at(3))
    #expect(fired.map(\.kind) == [.battery])
    #expect(fired.first?.body.contains("15%") == true)

    f.ingest(page(4, frames: 70, batteries: [battery(4, pct: 14)]))
    #expect(a.deviceAlerts(f, now: at(5)).isEmpty, "once per discharge")
    f.ingest(page(6, frames: 70, batteries: [battery(6, pct: 12)]))
    #expect(a.deviceAlerts(f, now: at(7)).isEmpty)

    // A charging record re-arms it; the next time it is low it fires again.
    f.ingest(page(8, frames: 70, batteries: [battery(8, pct: 12, ma: 35)]))
    #expect(a.deviceAlerts(f, now: at(9)).isEmpty, "charging is not low-and-draining")
    f.ingest(page(10, frames: 70, batteries: [battery(10, pct: 14)]))
    #expect(a.deviceAlerts(f, now: at(11)).map(\.kind) == [.battery])
  }

  @Test func aChargeBetweenTwoEvaluationsStillReArms() {
    var f = facts()
    var a = alerts()
    f.ingest(page(0, frames: 70, batteries: [battery(0, pct: 14)]))
    #expect(a.deviceAlerts(f, now: at(1)).map(\.kind) == [.battery])
    f.ingest(
      page(
        2, frames: 70,
        batteries: [battery(2, pct: 14, ma: 35), battery(3, pct: 14)]))
    #expect(a.deviceAlerts(f, now: at(4)).map(\.kind) == [.battery])
  }

  @Test func lowButChargingDoesNotFire() {
    var f = facts()
    var a = alerts()
    f.ingest(page(0, frames: 70, batteries: [battery(0, pct: 5, ma: 35)]))
    #expect(a.deviceAlerts(f, now: at(1)).isEmpty)
  }

  // MARK: Off (C6)

  @Test func offAlertWaitsForTenMinutesThenNamesTheButton() {
    var f = facts()
    var a = alerts()
    f.ingest(page(-20, frames: 70))
    f.ingest(deviceStatus(.off), at: at(-15))
    #expect(a.deviceAlerts(f, now: at(-6)).isEmpty, "9 minutes since the first reply that saw off")
    let fired = a.deviceAlerts(f, now: at(-5))
    #expect(fired.map(\.kind) == [.off])
    #expect(fired.first?.body.contains("long-press to resume") == true)
    #expect(a.deviceAlerts(f, now: at(0)).isEmpty, "once per off-period")
  }

  @Test func modeOnOrUnknownWithNoAudioKeepsTheNoAudioRule() {
    for mode in [DeviceStatus.Mode.on, .unknown] {
      var f = facts()
      var a = alerts()
      f.ingest(page(-30, frames: 70))
      f.ingest(deviceStatus(mode), at: at(-1))
      let fired = a.deviceAlerts(f, now: at(0))
      #expect(fired.map(\.kind) == [.off])
      #expect(fired.first?.body.contains("No audio is being captured") == true)
    }
  }

  @Test func chargingInTheReplySuppressesTheButtonAlert() {
    var f = facts()
    var a = alerts()
    f.ingest(deviceStatus(.off, milliamps: 150), at: at(-15))
    #expect(a.deviceAlerts(f, now: at(0)).isEmpty)
  }

  @Test func noFramesWithoutAButtonLineSaysNoAudioIsBeingCaptured() {
    var f = facts()
    var a = alerts()
    f.ingest(page(-30, frames: 70))
    f.ingest(page(-5))
    let fired = a.deviceAlerts(f, now: at(0))
    #expect(fired.map(\.kind) == [.off])
    let body = fired.first?.body ?? ""
    #expect(body.contains("No audio is being captured"))
    #expect(!body.contains("long-press"))
  }

  @Test func audioReturningReArmsTheOffAlert() {
    var f = facts()
    var a = alerts()
    f.ingest(page(-30, frames: 70))
    #expect(a.deviceAlerts(f, now: at(0)).map(\.kind) == [.off])
    f.ingest(page(1, frames: 70))
    #expect(a.deviceAlerts(f, now: at(2)).isEmpty)
    #expect(a.deviceAlerts(f, now: at(12)).map(\.kind) == [.off], "a second off-period")
  }

  @Test func chargingSuppressesTheOffAlert() {
    var f = facts()
    var a = alerts()
    f.ingest(page(-30, frames: 70))
    f.ingest(page(-1, batteries: [battery(-1, pct: 50, ma: 35)]))
    #expect(a.deviceAlerts(f, now: at(0)).isEmpty)
  }

  @Test func noPagesYetFiresNothing() {
    var a = alerts()
    #expect(a.deviceAlerts(facts(), now: at(0)).isEmpty)
  }

  // MARK: Restart (C5)

  @Test func anAlertThatFiredIsNotRepeatedAfterARelaunchWhileTheConditionHolds() {
    let store = MemoryStore()
    var f = facts()
    var a = alerts(store: store)
    f.ingest(page(-30, frames: 70, batteries: [battery(-30, pct: 9)]))
    f.ingest(deviceStatus(.off, percent: 9), at: at(-20))
    #expect(Set(a.deviceAlerts(f, now: at(0)).map(\.kind)) == [.battery, .off])

    // Relaunch: new facts, new Alerts over the same store. The pendant is still off and low.
    var f2 = facts()
    var a2 = alerts(store: store)
    f2.ingest(deviceStatus(.off, percent: 9), at: at(1))
    #expect(a2.deviceAlerts(f2, now: at(2)).isEmpty)
    #expect(
      a2.deviceAlerts(f2, now: at(12)).isEmpty, "ten minutes after the relaunch is not a new off")
  }

  @Test func aRelaunchedAlertStillReArmsWhenTheConditionClears() {
    let store = MemoryStore()
    var f = facts()
    var a = alerts(store: store)
    f.ingest(page(-30, frames: 70, batteries: [battery(-30, pct: 9)]))
    #expect(a.deviceAlerts(f, now: at(0)).map(\.kind) == [.battery, .off])

    var f2 = facts()
    var a2 = alerts(store: store)
    f2.ingest(deviceStatus(.on, percent: 9, milliamps: 150), at: at(1))
    f2.ingest(page(2, frames: 70))
    #expect(a2.deviceAlerts(f2, now: at(3)).isEmpty, "audio is back, charging; both re-armed")
    f2.ingest(deviceStatus(.on, percent: 9), at: at(4))
    #expect(a2.deviceAlerts(f2, now: at(5)).map(\.kind) == [.battery], "a new low after a charge")
    #expect(a2.deviceAlerts(f2, now: at(20)).map(\.kind) == [.off], "a second off-period")
  }

  @Test func theSyncAlertSurvivesARelaunch() {
    let store = MemoryStore()
    var f = StatusFacts(startedAt: at(-60 * 7), calendar: utc)
    var a = alerts(store: store)
    #expect(a.syncAlert(f, now: at(0))?.kind == .sync)
    f = StatusFacts(startedAt: at(1), calendar: utc)
    a = alerts(store: store)
    #expect(a.syncAlert(f, now: at(2)) == nil)
    #expect(a.syncAlert(f, now: at(60 * 7)) == nil, "still the same outage")
  }

  @Test func theRealDefaultsStoreCarriesAlertsAcrossARelaunchAndClears() {
    let suite = "earshot-tests-\(UUID().uuidString)"
    defer { UserDefaults().removePersistentDomain(forName: suite) }
    var f = facts()
    var a = alerts(store: DefaultsAlertStore(suiteName: suite))
    f.ingest(page(-30, frames: 70, batteries: [battery(-30, pct: 9)]))
    #expect(a.deviceAlerts(f, now: at(0)).map(\.kind) == [.battery, .off])

    // A new store over the same suite is what a relaunch sees.
    let relaunched = DefaultsAlertStore(suiteName: suite).load()
    #expect(relaunched == AlertState(batteryFiredAt: at(-30), offFired: true, syncFired: false))
    var a2 = alerts(store: DefaultsAlertStore(suiteName: suite))
    #expect(a2.deviceAlerts(f, now: at(1)).isEmpty)

    DefaultsAlertStore(suiteName: suite).save(AlertState())
    #expect(DefaultsAlertStore(suiteName: suite).load() == AlertState())
  }

  // MARK: Sync (C7)

  @Test func sixHoursWithoutASyncFiresOncePerOutage() {
    var f = StatusFacts(startedAt: at(-60 * 7), calendar: utc)
    var a = alerts()
    f.recordUpload(lastSuccess: at(-60 * 5), failure: nil)
    #expect(a.syncAlert(f, now: at(0)) == nil, "5 hours")
    let fired = a.syncAlert(f, now: at(60 + 1))
    #expect(fired?.kind == .sync)
    #expect(fired?.body.contains("out of range") == true)
    #expect(a.syncAlert(f, now: at(60 * 3)) == nil, "once per outage")

    f.recordUpload(lastSuccess: at(60 * 4), failure: nil)
    #expect(a.syncAlert(f, now: at(60 * 4 + 1)) == nil)
    #expect(a.syncAlert(f, now: at(60 * 10 + 1))?.kind == .sync, "a new outage")
  }

  @Test func aFailingUploadSaysEarshotdIsUnreachable() {
    var f = StatusFacts(startedAt: at(-60 * 7), calendar: utc)
    var a = alerts()
    f.recordUpload(lastSuccess: nil, failure: "earshotd unreachable: timed out")
    let fired = a.syncAlert(f, now: at(0))
    #expect(fired?.body.contains("earshotd is unreachable") == true)
  }

  @Test func theClockStartsAtLaunchWhenNothingHasSynced() {
    let f = StatusFacts(startedAt: at(-30), calendar: utc)
    var a = alerts()
    #expect(a.syncAlert(f, now: at(0)) == nil)
    #expect(a.syncAlert(f, now: at(60 * 6)) != nil)
  }

  // MARK: The menu still shows the condition (C10)

  @Test func conditionsListWhatIsWrongWhetherOrNotAnyoneWasNotified() {
    var f = facts()
    f.ingest(page(-30, frames: 70, batteries: [battery(-30, pct: 9)]))
    let kinds = Alerts.conditions(f, now: at(0), calendar: utc).map(\.kind)
    #expect(kinds == [.battery, .off])
  }
}

private final class FakeCenter: AlertCenter, @unchecked Sendable {
  private let lock = NSLock()
  private var state: NotificationPermission
  private let answer: Bool
  private(set) var posted: [Alert] = []
  private(set) var requests = 0

  init(_ state: NotificationPermission, answer: Bool = false) {
    self.state = state
    self.answer = answer
  }

  func permission() async -> NotificationPermission { lock.withLock { state } }

  func requestPermission() async -> Bool {
    lock.withLock {
      requests += 1
      state = answer ? .granted : .denied
      return answer
    }
  }

  func post(_ alert: Alert) async throws { lock.withLock { posted.append(alert) } }
}

private final class Notices: @unchecked Sendable {
  private let lock = NSLock()
  private var items: [String] = []
  func add(_ s: String) { lock.withLock { items.append(s) } }
  var all: [String] { lock.withLock { items } }
}

@Suite struct AlertDispatcherTests {
  let alert = Alert(kind: .battery, title: "Battery low", body: "14%")

  @Test func grantedPermissionPostsEveryAlert() async {
    let center = FakeCenter(.granted)
    let notices = Notices()
    let d = AlertDispatcher(center: center, notice: notices.add)
    await d.deliver([alert, alert])
    #expect(center.posted.count == 2)
    #expect(notices.all.isEmpty)
  }

  @Test func deniedPermissionLogsTheRefusalOnceAcrossManyAlerts() async {
    let center = FakeCenter(.denied)
    let notices = Notices()
    let d = AlertDispatcher(center: center, notice: notices.add)
    await d.deliver([alert])
    await d.deliver([alert, alert])
    #expect(center.posted.isEmpty)
    #expect(center.requests == 0, "a denial is not asked again")
    #expect(notices.all.count == 1)
  }

  @Test func undeterminedPermissionAsksOnceAndLogsWhenRefused() async {
    let center = FakeCenter(.notDetermined, answer: false)
    let notices = Notices()
    let d = AlertDispatcher(center: center, notice: notices.add)
    await d.deliver([alert])
    await d.deliver([alert])
    #expect(center.requests == 1)
    #expect(center.posted.isEmpty)
    #expect(notices.all.count == 1)
  }

  @Test func undeterminedPermissionThatIsGrantedPosts() async {
    let center = FakeCenter(.notDetermined, answer: true)
    let notices = Notices()
    let d = AlertDispatcher(center: center, notice: notices.add)
    await d.deliver([alert])
    #expect(center.posted == [alert])
    #expect(notices.all.isEmpty)
  }

  @Test func nothingToDeliverNeverAsksForPermission() async {
    let center = FakeCenter(.notDetermined, answer: true)
    let d = AlertDispatcher(center: center, notice: { _ in })
    await d.deliver([])
    #expect(center.requests == 0)
  }
}

private final class MemoryStore: AlertStateStore, @unchecked Sendable {
  private let lock = NSLock()
  private var state = AlertState()
  func load() -> AlertState { lock.withLock { state } }
  func save(_ new: AlertState) { lock.withLock { state = new } }
}

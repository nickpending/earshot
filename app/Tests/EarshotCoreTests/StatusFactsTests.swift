import Foundation
import Testing

@testable import EarshotCore

/// C2, C3, C4: the rolling status the menu renders.
@Suite struct StatusFactsTests {
  let utc: Calendar = {
    var c = Calendar(identifier: .gregorian)
    c.timeZone = TimeZone(identifier: "UTC")!
    return c
  }()
  /// 2026-10-06 10:00:00 UTC.
  let t0 = Date(timeIntervalSince1970: 1_791_280_800)

  func at(_ minutes: Double) -> Date { t0.addingTimeInterval(minutes * 60) }

  func facts() -> StatusFacts { StatusFacts(startedAt: at(-600), calendar: utc) }

  func battery(_ minutes: Double, pct: Int, ma: Int = -12) -> PageDigest.Battery {
    .init(at: at(minutes), millivolts: 3800, milliamps: ma, percent: pct)
  }

  func page(
    _ minutes: Double, frames: Int = 0, batteries: [PageDigest.Battery] = [],
    events: [PageDigest.ModeEvent] = []
  ) -> PageDigest {
    PageDigest(
      pageTime: at(minutes), opusFrames: frames, batteries: batteries, modeEvents: events)
  }

  // MARK: Battery (C2)

  @Test func noBatteryRecordReadsUnknown() {
    #expect(facts().lines(now: at(0)).battery == "Battery unknown")
  }

  @Test func dischargingWithAnHourOfRecordsEstimatesHoursLeftFromTheSlope() {
    var f = facts()
    f.ingest(page(-60, batteries: [battery(-60, pct: 70)]))
    f.ingest(page(-30, batteries: [battery(-30, pct: 67)]))
    f.ingest(page(0, batteries: [battery(0, pct: 65)]))
    #expect(f.lines(now: at(0)).battery == "Battery 65%, about 13 h left")
  }

  @Test func underFifteenMinutesOfDataGivesNoEstimate() {
    var f = facts()
    f.ingest(page(-10, batteries: [battery(-10, pct: 66)]))
    f.ingest(page(0, batteries: [battery(0, pct: 65)]))
    #expect(f.lines(now: at(0)).battery == "Battery 65%")
  }

  @Test func recordsOlderThanAnHourDoNotCount() {
    var f = facts()
    f.ingest(page(-180, batteries: [battery(-180, pct: 90)]))
    f.ingest(page(-5, batteries: [battery(-5, pct: 65)]))
    f.ingest(page(0, batteries: [battery(0, pct: 65)]))
    #expect(f.lines(now: at(0)).battery == "Battery 65%")
  }

  @Test func aFlatOrRisingPercentGivesNoEstimate() {
    var f = facts()
    f.ingest(page(-50, batteries: [battery(-50, pct: 65)]))
    f.ingest(page(0, batteries: [battery(0, pct: 65)]))
    #expect(f.lines(now: at(0)).battery == "Battery 65%")
  }

  @Test func chargingSaysChargingAndNoEstimate() {
    var f = facts()
    f.ingest(page(-50, batteries: [battery(-50, pct: 70)]))
    f.ingest(page(0, batteries: [battery(0, pct: 72, ma: 35)]))
    #expect(f.lines(now: at(0)).battery == "Battery 72%, charging")
  }

  @Test func recordsBeforeTheLastChargeDoNotFeedTheEstimate() {
    var f = facts()
    f.ingest(page(-55, batteries: [battery(-55, pct: 30)]))
    f.ingest(page(-30, batteries: [battery(-30, pct: 60, ma: 35)]))
    f.ingest(page(-20, batteries: [battery(-20, pct: 60)]))
    f.ingest(page(0, batteries: [battery(0, pct: 59)]))
    // Only 20 minutes since unplugging: the climb before it must not read as a slope.
    #expect(f.lines(now: at(0)).battery == "Battery 59%, about 20 h left")
  }

  // MARK: Audio (C3)

  @Test func hoursTodayCountTwentyMillisecondFramesInTheLocalDay() {
    var f = facts()
    f.ingest(page(-24 * 60 - 5, frames: 90_000))  // yesterday
    f.ingest(page(-30, frames: 90_000))
    f.ingest(page(-1, frames: 90_000))
    #expect(f.lines(now: at(0)).today == "Audio today: 1.0 h")
  }

  @Test func aPageSentAgainIsCountedOnce() {
    var f = facts()
    let again = PageDigest(pageIndex: 7, pageTime: at(-30), opusFrames: 90_000)
    f.ingest(again)
    f.ingest(again)
    f.ingest(PageDigest(pageIndex: 8, pageTime: at(-29), opusFrames: 90_000))
    #expect(f.lines(now: at(0)).today == "Audio today: 1.0 h")
  }

  @Test func recentFramesReadCapturingWithTheLastHeardTime() {
    var f = facts()
    f.ingest(page(-2, frames: 70))
    let lines = f.lines(now: at(0))
    #expect(lines.audio == "Capturing audio")
    #expect(lines.lastHeard == "Audio last heard 09:58")
  }

  @Test func modeOffReadsOffByButtonSinceTheFirstCycleThatSawItWithNoPage() {
    var f = facts()
    f.ingest(page(-30, frames: 70))
    f.ingest(deviceStatus(.off), at: at(-20))
    f.ingest(deviceStatus(.off), at: at(-10))
    let lines = f.lines(now: at(0))
    #expect(lines.audio == "Off (button) since 09:40")
    #expect(lines.lastHeard == "Audio last heard 09:30")
  }

  @Test func theFirstCycleSeeingModeOnReturnsToTheAudioLine() {
    var f = facts()
    f.ingest(page(-2, frames: 70))
    f.ingest(deviceStatus(.off), at: at(-1))
    #expect(f.lines(now: at(-1)).audio == "Off (button) since 09:59")
    f.ingest(deviceStatus(.on), at: at(0))
    #expect(f.lines(now: at(0)).audio == "Capturing audio")
  }

  @Test func aReplyWithNoModeDoesNotClaimTheButton() {
    var f = facts()
    f.ingest(page(-2, frames: 70))
    f.ingest(deviceStatus(.unknown), at: at(0))
    #expect(f.lines(now: at(0)).audio == "Capturing audio")
  }

  @Test func theIdleLineInASavedPageIsHistoryOnly() {
    var f = facts()
    f.ingest(page(-30, frames: 70))
    f.ingest(page(-20, events: [.init(at: at(-19), kind: .idle)]))
    f.ingest(page(-1))
    #expect(f.lines(now: at(0)).audio == "No audio since 09:30")
  }

  @Test func theBatteryLineFollowsTheReplyEveryCycle() {
    var f = facts()
    f.ingest(page(-60, batteries: [battery(-60, pct: 70)]))
    f.ingest(page(-30, batteries: [battery(-30, pct: 67)]))
    f.ingest(deviceStatus(.on, percent: 65), at: at(0))
    #expect(f.lines(now: at(0)).battery == "Battery 65%, about 11 h left")
    f.ingest(deviceStatus(.on, percent: 64), at: at(1))
    #expect(f.lines(now: at(1)).battery.hasPrefix("Battery 64%"))
    f.ingest(deviceStatus(.on, percent: 70, milliamps: 150), at: at(2))
    #expect(f.lines(now: at(2)).battery == "Battery 70%, charging")
  }

  @Test func aReplyWithoutAnyPageStillFillsTheBatteryLine() {
    var f = facts()
    f.ingest(deviceStatus(.on, percent: 14, milliamps: nil), at: at(0))
    #expect(f.lines(now: at(0)).battery == "Battery 14%")
  }

  @Test func framesStoppingWithoutAnIdleLineReadNoAudioSince() {
    var f = facts()
    f.ingest(page(-20, frames: 70))
    f.ingest(page(-5))
    #expect(f.lines(now: at(0)).audio == "No audio since 09:40")
  }

  @Test func pagesWithNoFramesAtAllStillNameWhenTheQuietBegan() {
    var f = facts()
    f.ingest(page(-20))
    f.ingest(page(-1))
    let lines = f.lines(now: at(0))
    #expect(lines.audio == "No audio since 09:40")
    #expect(lines.lastHeard == "No audio heard yet")
  }

  @Test func beforeAnyPageTheAudioStateIsUnknown() {
    let lines = facts().lines(now: at(0))
    #expect(lines.audio == "Audio unknown")
    #expect(lines.today == "Audio today: 0.0 h")
  }

  @Test func outOfRangeDoesNotClaimTheQuietIsReal() {
    var f = facts()
    f.ingest(page(-20, frames: 70))
    #expect(f.lines(now: at(0), connected: false).audio == "Out of range")
  }

  @Test func audioStoppedFollowsTheButtonOrFiveQuietMinutes() {
    var f = facts()
    #expect(!f.audioStopped(now: at(0)), "nothing seen yet")
    f.ingest(page(-2, frames: 70))
    #expect(!f.audioStopped(now: at(0)))
    #expect(f.audioStopped(now: at(4)))
    f.ingest(page(5, frames: 70))
    f.ingest(deviceStatus(.off), at: at(6))
    #expect(f.audioStopped(now: at(6)), "the button counts at once")
  }

  // MARK: Sync (C4)

  @Test func syncedShowsWholeMinutesSinceTheLastSuccess() {
    var f = facts()
    f.recordUpload(lastSuccess: at(-7.5), failure: nil)
    #expect(f.lines(now: at(0)).sync == "Synced 7 min ago")
  }

  @Test func aFailureShowsItsReason() {
    var f = facts()
    f.recordUpload(lastSuccess: at(-7), failure: "earshotd unreachable: timed out")
    #expect(f.lines(now: at(0)).sync == "Sync failed: earshotd unreachable: timed out")
  }

  @Test func neverSyncedSaysSo() {
    #expect(facts().lines(now: at(0)).sync == "Not synced yet")
  }
}

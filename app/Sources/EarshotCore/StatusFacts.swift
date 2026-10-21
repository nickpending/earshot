import Foundation

/// The rolling picture of the pendant behind the menu: device mode and battery from the status replies, battery
/// history, audio last heard, hours captured today and the last upload. Fed the status replies, the digests of
/// pages the drain has already saved and the uploader's results; it reads nothing the drain depends on. The
/// idle-mode log line in saved pages is history only: the button state comes from the reply's mode.
public struct StatusFacts: Sendable {
  /// What the menu shows, one line each.
  public struct Lines: Sendable, Equatable {
    public let battery: String
    public let audio: String
    public let lastHeard: String
    public let today: String
    public let sync: String
  }

  /// The battery as the newest source says it: the status reply's percent and charging state, or a saved
  /// page's record when that is newer.
  public struct BatteryReading: Sendable, Equatable {
    public let at: Date
    public let percent: Int
    public let charging: Bool
  }

  /// How long audio has been missing and whether the button started it.
  public struct Silence: Sendable, Equatable {
    public let since: Date
    public let byButton: Bool
  }

  /// Frames stop mattering to "capturing" after this long without one.
  static let quietAfter: TimeInterval = 5 * 60
  /// Battery records inside this window feed the hours-left slope.
  static let slopeWindow: TimeInterval = 60 * 60
  /// A slope needs at least this much time between its first and last record.
  static let slopeMinimumSpan: TimeInterval = 15 * 60
  static let frameSeconds = 0.02

  public let startedAt: Date
  private let calendar: Calendar

  private var batteries: [PageDigest.Battery] = []
  public private(set) var latestBattery: PageDigest.Battery?
  /// The time of the newest battery record that showed charging.
  public private(set) var lastChargingAt: Date?

  private var framesByDay: [Date: Int] = [:]
  /// Pages already counted: the drain re-sends pages it has not ACKed, and the menu must not count them twice.
  private var countedPages: Set<PageKey> = []

  private struct PageKey: Hashable {
    let index: Int
    let time: Date
  }
  private var firstPageAt: Date?
  private var lastAudioAt: Date?
  private var deviceBattery: BatteryReading?
  /// When the first status reply saw mode off in the current run of them; nil while the mode is on or unknown.
  private var buttonOffSince: Date?

  public private(set) var lastSyncAt: Date?
  public private(set) var syncFailure: String?

  public init(startedAt: Date, calendar: Calendar = .current) {
    self.startedAt = startedAt
    self.calendar = calendar
  }

  // MARK: Feeding

  public mutating func ingest(_ digest: PageDigest) {
    for record in digest.batteries {
      batteries.append(record)
      if record.at >= latestBattery?.at ?? .distantPast { latestBattery = record }
      if record.charging, record.at >= lastChargingAt ?? .distantPast { lastChargingAt = record.at }
    }
    if let newest = latestBattery {
      batteries.removeAll { $0.at < newest.at.addingTimeInterval(-2 * Self.slopeWindow) }
    }

    guard let time = digest.pageTime else { return }
    firstPageAt = min(time, firstPageAt ?? time)
    if digest.opusFrames > 0 {
      if let index = digest.pageIndex,
        !countedPages.insert(PageKey(index: index, time: time)).inserted
      {
        return
      }
      lastAudioAt = max(time, lastAudioAt ?? .distantPast)
      framesByDay[calendar.startOfDay(for: time), default: 0] += digest.opusFrames
      if let newest = lastAudioAt {
        let keep = calendar.startOfDay(for: newest).addingTimeInterval(-3 * 86_400)
        framesByDay = framesByDay.filter { $0.key >= keep }
        countedPages = countedPages.filter { $0.time >= keep }
      }
    }
  }

  /// One status reply, seen at `now`. Mode off starts counting from the first reply that saw it; mode on or
  /// unknown (no field) ends it. The battery is the reply's own percent and charging state.
  public mutating func ingest(_ status: DeviceStatus, at now: Date) {
    switch status.mode {
    case .off: buttonOffSince = buttonOffSince ?? now
    case .on, .unknown: buttonOffSince = nil
    }
    if let battery = status.battery {
      deviceBattery = BatteryReading(at: now, percent: battery.percent, charging: battery.charging)
      if battery.charging { lastChargingAt = max(now, lastChargingAt ?? .distantPast) }
    }
  }

  /// `lastSuccess` is the time of the last upload that returned 200 (nil keeps the one already known);
  /// `failure` is why the latest attempt failed, nil when it did not.
  public mutating func recordUpload(lastSuccess: Date?, failure: String?) {
    if let lastSuccess { lastSyncAt = lastSuccess }
    syncFailure = failure
  }

  // MARK: Reading

  /// The newest battery reading, from a status reply or a saved page.
  public var currentBattery: BatteryReading? {
    let page = latestBattery.map {
      BatteryReading(at: $0.at, percent: $0.percent, charging: $0.charging)
    }
    guard let device = deviceBattery else { return page }
    if let page, page.at > device.at { return page }
    return device
  }

  public var charging: Bool { currentBattery?.charging ?? false }

  /// Opus frames were heard within `window` before `now`.
  public func audioHeard(within window: TimeInterval, now: Date) -> Bool {
    guard let heard = lastAudioAt else { return false }
    return now.timeIntervalSince(heard) < window
  }

  /// When audio last stopped and why, or nil before any page has been seen.
  public func silence() -> Silence? {
    if let off = buttonOffSince { return Silence(since: off, byButton: true) }
    guard let since = lastAudioAt ?? firstPageAt else { return nil }
    return Silence(since: since, byButton: false)
  }

  /// Audio is known to be missing: the button turned recording off, or no frames for 5 minutes.
  public func audioStopped(now: Date) -> Bool {
    guard let silence = silence() else { return false }
    return silence.byButton || now.timeIntervalSince(silence.since) >= Self.quietAfter
  }

  /// Hours of battery left at the percent slope over the last hour of discharging records, or nil with under
  /// 15 minutes of them, a charger, or a percent that is not falling.
  public func hoursLeft(now: Date) -> Double? {
    guard let latest = currentBattery, !latest.charging else { return nil }
    let from = max(
      now.addingTimeInterval(-Self.slopeWindow), lastChargingAt ?? .distantPast)
    let points = batteries.filter { $0.at >= from && $0.at <= now && !$0.charging }
      .sorted { $0.at < $1.at }
    guard let first = points.first, let last = points.last, points.count >= 2,
      last.at.timeIntervalSince(first.at) >= Self.slopeMinimumSpan
    else { return nil }
    let xs = points.map { $0.at.timeIntervalSince(first.at) / 3600 }
    let ys = points.map { Double($0.percent) }
    let n = Double(points.count)
    let meanX = xs.reduce(0, +) / n
    let meanY = ys.reduce(0, +) / n
    let sxx = xs.map { ($0 - meanX) * ($0 - meanX) }.reduce(0, +)
    guard sxx > 0 else { return nil }
    let sxy = zip(xs, ys).map { ($0 - meanX) * ($1 - meanY) }.reduce(0, +)
    let slope = sxy / sxx
    guard slope < -0.01 else { return nil }
    return Double(latest.percent) / -slope
  }

  public func lines(now: Date, connected: Bool = true) -> Lines {
    Lines(
      battery: batteryLine(now: now), audio: audioLine(now: now, connected: connected),
      lastHeard: lastAudioAt.map { "Audio last heard \(clock($0))" } ?? "No audio heard yet",
      today: todayLine(now: now), sync: syncLine(now: now))
  }

  private func batteryLine(now: Date) -> String {
    guard let battery = currentBattery else { return "Battery unknown" }
    if battery.charging { return "Battery \(battery.percent)%, charging" }
    guard let hours = hoursLeft(now: now) else { return "Battery \(battery.percent)%" }
    let left =
      hours >= 1 ? "\(Int(hours.rounded())) h" : "\(max(1, Int((hours * 60).rounded()))) min"
    return "Battery \(battery.percent)%, about \(left) left"
  }

  private func audioLine(now: Date, connected: Bool) -> String {
    guard let silence = silence() else { return "Audio unknown" }
    if silence.byButton { return "Off (button) since \(clock(silence.since))" }
    if !connected { return "Out of range" }
    if let heard = lastAudioAt, now.timeIntervalSince(heard) < Self.quietAfter {
      return "Capturing audio"
    }
    return "No audio since \(clock(silence.since))"
  }

  private func todayLine(now: Date) -> String {
    let frames = framesByDay[calendar.startOfDay(for: now)] ?? 0
    let hours = Double(frames) * Self.frameSeconds / 3600
    return "Audio today: \(String(format: "%.1f", hours)) h"
  }

  private func syncLine(now: Date) -> String {
    if let why = syncFailure { return "Sync failed: \(why)" }
    guard let synced = lastSyncAt else { return "Not synced yet" }
    let minutes = max(0, Int(now.timeIntervalSince(synced) / 60))
    return "Synced \(minutes) min ago"
  }

  private func clock(_ date: Date) -> String { Self.clock(date, calendar: calendar) }

  /// HH:MM in the calendar's time zone.
  static func clock(_ date: Date, calendar: Calendar) -> String {
    let parts = calendar.dateComponents([.hour, .minute], from: date)
    return String(format: "%02d:%02d", parts.hour ?? 0, parts.minute ?? 0)
  }
}

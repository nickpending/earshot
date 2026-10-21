import EarshotCore
import Foundation
import Observation
import os

/// Unified log (`log show --predicate 'subsystem == "casa.mal.Earshot"'`): every unattended decision. Log them at
/// .notice or above: macOS does not persist .info or .debug, so they could not be read after the fact. Never log
/// the token.
private let log = Logger(subsystem: "casa.mal.Earshot", category: "app")

/// What the menu bar shows, and the loops that feed it: connect, drain every 90 s, upload the spool. Between
/// drains the pendant hears nothing from the app. The menu is decoded from the pages the drain saved (never
/// by the drain itself), so it works with earshotd down.
@MainActor
@Observable
final class StatusModel {
  enum Connection { case disconnected, connected }

  private(set) var connection: Connection = .disconnected
  private(set) var downloading = false
  private(set) var lines: StatusFacts.Lines
  private(set) var audioStopped = false
  /// Alert conditions true now, shown whether or not a notification could be posted.
  private(set) var attention: [String] = []
  private(set) var pagesOnPendant: Int?
  private(set) var backlogPages = 0
  private(set) var problem: String?
  /// The pendant the operator chose; nothing is connected until there is one.
  private(set) var chosen: UUID?
  /// Devices seen advertising while nothing is chosen, strongest signal first.
  private(set) var nearby: [NearbyPendants.Entry] = []

  /// Whether the app is a login item, as the system reports it; only the operator's toggle changes it.
  private(set) var launchAtLogin = false
  private(set) var loginNote: String?
  private let loginItem = LaunchAtLogin(service: LoginItem())

  private static let drainInterval: Duration = .seconds(90)
  private static let firstDrainDelay: Duration = .seconds(5)
  private static let refreshInterval: Duration = .seconds(30)
  private static let uploadInterval: Duration = .seconds(60)
  private static let reconnectBackoff = Backoff(base: .seconds(2), cap: .seconds(60))

  private var transport: CoreBluetoothTransport?
  private var engine: DrainEngine?
  private var spool: Spool?
  private var uploader: Uploader?
  private var tee: TeeSink?
  private var facts: StatusFacts
  private var alerts = Alerts(store: DefaultsAlertStore())
  private let choiceStore: PendantChoiceStore = DefaultsPendantChoiceStore()
  private var seenBook = NearbyPendants()
  private let dispatcher = AlertDispatcher(
    center: Notifier(), notice: { log.notice("\($0, privacy: .public)") })
  private var session: Task<Void, Never>?
  private var uploading = false
  private var reconnectFailures = 0
  private var started = false

  init() {
    let facts = StatusFacts(startedAt: Date())
    self.facts = facts
    lines = facts.lines(now: Date(), connected: false)
  }

  var title: String {
    if chosen == nil { return "No pendant chosen" }
    switch connection {
    case .disconnected: return "Disconnected"
    case .connected:
      if downloading { return "Downloading" }
      return audioStopped ? "Connected, not recording" : "Connected"
    }
  }

  var symbol: String {
    switch connection {
    case .disconnected: return "waveform.slash"
    case .connected:
      if downloading { return "arrow.down.circle" }
      return audioStopped ? "waveform.badge.minus" : "waveform"
    }
  }

  func start() {
    guard !started else { return }
    started = true
    refreshLoginItem()

    do {
      let dir = try FileManager.default.url(
        for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true
      )
      .appendingPathComponent("Earshot/spool")
      let spool = try Spool(directory: dir)
      self.spool = spool
      let transport = CoreBluetoothTransport()
      self.transport = transport
      // The tee only copies bytes the spool already holds; the engine never sees a decoder.
      let tee = TeeSink(spool)
      self.tee = tee
      engine = DrainEngine(
        transport: transport, sink: tee,
        onStatus: { [weak self] status in
          let at = Date()
          Task { @MainActor in self?.ingest(status, at: at) }
        })
      transport.onEvent = { [weak self] event in
        Task { @MainActor in self?.handle(event) }
      }
      chosen = choiceStore.load()
      transport.choose(chosen)
      transport.start()
    } catch {
      log.error("spool unavailable: \(String(describing: error), privacy: .public)")
      problem = "Spool unavailable: \(error)"
      return
    }

    Task { await uploadLoop() }
    Task { await refreshLoop() }
    refreshBacklog()
  }

  // MARK: Launch at login

  /// The operator's toggle: register or unregister, then show what the system says.
  func setLaunchAtLogin(_ on: Bool) {
    let failure = loginItem.set(on)
    log.notice("launch at login \(on ? "on" : "off", privacy: .public)")
    refreshLoginItem()
    if let failure { loginNote = failure }
  }

  private func refreshLoginItem() {
    let state = loginItem.state
    launchAtLogin = state.isOn
    loginNote = state.note
  }

  // MARK: The chosen pendant

  /// Pick the pendant to drain: saved, and the transport connects to it from now on.
  func choose(_ identifier: UUID) {
    choiceStore.save(identifier)
    chosen = identifier
    seenBook.clear()
    nearby = []
    transport?.choose(identifier)
    log.notice("chose pendant \(PendantChoice.shortId(identifier), privacy: .public)")
  }

  /// Forget the chosen pendant: the stored choice is cleared and any connection to it is cancelled.
  func forget() {
    let was = chosen.map(PendantChoice.shortId) ?? "none"
    choiceStore.save(nil)
    chosen = nil
    transport?.forget()
    markDisconnected()
    log.notice("forgot pendant \(was, privacy: .public)")
  }

  // MARK: Bluetooth events

  private func handle(_ event: TransportEvent) {
    switch event {
    case .seen(let identifier, let name, let rssi):
      guard chosen == nil else { return }
      seenBook.see(identifier: identifier, name: name, rssi: rssi, at: Date())
      nearby = seenBook.entries(now: Date())
    case .unavailable(let why):
      log.error("bluetooth unavailable: \(why, privacy: .public)")
      problem = why
      markDisconnected()
    case .scanning:
      break
    case .ready:
      log.notice("pendant connected")
      reconnectFailures = 0
      connection = .connected
      if problem?.hasPrefix("Bluetooth") == true { problem = nil }
      session?.cancel()
      session = Task { await runSession() }
    case .disconnected:
      markDisconnected()
      reconnectFailures += 1
      let delay = Self.reconnectBackoff.delay(afterFailures: reconnectFailures)
      log.notice(
        "pendant disconnected; reconnect attempt \(self.reconnectFailures) in \(String(describing: delay), privacy: .public)"
      )
      Task {
        try? await Task.sleep(for: delay)
        transport?.start()
      }
    }
  }

  private func markDisconnected() {
    connection = .disconnected
    downloading = false
    session?.cancel()
    session = nil
    if let engine { Task { await engine.abort() } }
    refreshLines()
  }

  // MARK: Session

  private func runSession() async {
    guard let engine else { return }
    do { try await engine.beginSession() } catch {
      log.error("SetCurrentTime failed: \(String(describing: error), privacy: .public)")
      problem = "Could not set the pendant clock: \(error)"
      return
    }
    refreshLines()
    await engine.runLoop(
      firstDelay: Self.firstDrainDelay, interval: Self.drainInterval,
      willCycle: { await self.setDownloading(true) },
      didCycle: { await self.finishCycle($0) })
  }

  private func setDownloading(_ on: Bool) { downloading = on }

  private func finishCycle(_ result: DrainResult) async {
    downloading = false
    if let s = result.status { pagesOnPendant = s.storage.pagesOnPendant }
    // An idle pendant holds no pages: nothing to drain is as caught up as a drain that reached the newest.
    await showSaved(caughtUp: result.end == .caughtUp || result.end == .nothingToDrain)
    log.notice("\(result.summary, privacy: .public)")
    if result.end == .appendFailed || result.end == .syncFailed {
      problem = "Spool write failed; pendant pages kept"
    } else if problem?.hasPrefix("Spool write") == true {
      problem = nil
    }
    if result.blockedAt != nil { problem = "A page is missing; pendant copy kept" }
    do {
      try spool?.rotate()
      if problem?.hasPrefix("Spool rotate") == true { problem = nil }
    } catch {
      // The segment stays open and is retried next cycle; until it closes it is not uploaded.
      log.error("spool rotate failed: \(String(describing: error), privacy: .public)")
      problem = "Spool rotate failed; segment not uploading: \(error)"
    }
    refreshBacklog()
    Task { await runUpload() }
  }

  // MARK: Menu facts

  /// Decode the pages the cycle saved, off the main actor, and fold them into the menu's facts. Alerts about
  /// the pendant are judged only after a cycle that reached the newest page: against a half-drained backlog
  /// the newest page seen is old and "no audio" would be an artifact.
  private func showSaved(caughtUp: Bool) async {
    let pages = tee?.take() ?? []
    let digests = await Task.detached { pages.map { PageDigest(storageBuffer: $0) } }.value
    for digest in digests { facts.ingest(digest) }
    refreshLines()
    if caughtUp { await post(alerts.deviceAlerts(facts, now: Date())) }
  }

  /// A status reply, polled by the drain or pushed by the pendant on a mode change: the menu follows it now.
  private func ingest(_ status: DeviceStatus, at: Date) {
    facts.ingest(status, at: at)
    refreshLines()
  }

  private func refreshLoop() async {
    while !Task.isCancelled {
      try? await Task.sleep(for: Self.refreshInterval)
      refreshLines()
      refreshLoginItem()
    }
  }

  private func refreshLines() {
    let now = Date()
    lines = facts.lines(now: now, connected: connection == .connected)
    audioStopped = connection == .connected && facts.audioStopped(now: now)
    attention = Alerts.conditions(facts, now: now).map(\.body)
    nearby = chosen == nil ? seenBook.entries(now: now) : []
  }

  private func post(_ fired: [Alert]) async {
    for alert in fired { log.notice("alert: \(alert.title, privacy: .public)") }
    await dispatcher.deliver(fired)
  }

  // MARK: Upload

  private func uploadLoop() async {
    while !Task.isCancelled {
      await runUpload()
      if let alert = alerts.syncAlert(facts, now: Date()) { await post([alert]) }
      let wait = await uploader?.retryDelay ?? .zero
      try? await Task.sleep(for: wait > .zero ? wait : Self.uploadInterval)
    }
  }

  private func runUpload() async {
    guard !uploading, let spool else { return }
    uploading = true
    defer { uploading = false }
    if uploader == nil {
      do {
        uploader = Uploader(spool: spool, config: try EarshotConfig.load())
        if problem?.hasPrefix("Config") == true { problem = nil }
      } catch {
        log.error("config: \(String(describing: error), privacy: .public)")
        problem = "Config: \(error)"
        return
      }
    }
    guard let uploader else { return }
    let outcome = await uploader.uploadAll()
    facts.recordUpload(lastSuccess: await uploader.lastSuccess, failure: outcome.failure)
    refreshLines()
    if outcome.uploadedSegments > 0 || outcome.failure != nil {
      let retry = await uploader.retryDelay
      log.notice(
        """
        upload: \(outcome.uploadedSegments) segments, \(outcome.uploadedPages) pages sent, \
        failure \(outcome.failure ?? "none", privacy: .public), \
        next retry \(String(describing: retry), privacy: .public)
        """)
    }
    if let why = outcome.failure {
      problem = "Upload: \(why)"
    } else if problem?.hasPrefix("Upload") == true {
      problem = nil
    }
    refreshBacklog()
  }

  private func refreshBacklog() {
    guard let spool else { return }
    Task {
      let pages = await Task.detached { spool.backlog().pages }.value
      backlogPages = pages
    }
  }
}

/// Passes pages to the spool and keeps a copy of each one the spool accepted, for the menu to decode after the
/// cycle. It never reads a page; a failed append keeps nothing, and `sync` is the spool's own.
final class TeeSink: PageSink, @unchecked Sendable {
  private let inner: PageSink
  private let lock = NSLock()
  private var saved: [Data] = []

  init(_ inner: PageSink) { self.inner = inner }

  func append(_ storageBuffer: Data) throws {
    try inner.append(storageBuffer)
    lock.withLock { saved.append(storageBuffer) }
  }

  func sync() throws { try inner.sync() }

  /// The pages saved since the last call.
  func take() -> [Data] {
    lock.withLock {
      defer { saved = [] }
      return saved
    }
  }
}

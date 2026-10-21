import Foundation
import Testing

@testable import EarshotCore

final class FakeLoginItem: LoginItemService, @unchecked Sendable {
  struct Refused: Error, LocalizedError {
    var errorDescription: String? { "refused by the system" }
  }

  private let lock = NSLock()
  private var _status: LoginItemStatus
  private var _calls: [String] = []
  let registerThrows: Bool

  init(_ status: LoginItemStatus, registerThrows: Bool = false) {
    _status = status
    self.registerThrows = registerThrows
  }

  var status: LoginItemStatus { lock.withLock { _status } }
  var calls: [String] { lock.withLock { _calls } }

  func register() throws {
    lock.withLock { _calls.append("register") }
    if registerThrows { throw Refused() }
    lock.withLock { _status = .enabled }
  }

  func unregister() throws {
    lock.withLock {
      _calls.append("unregister")
      _status = .notRegistered
    }
  }
}

/// C7: starting the app only reads the login item; the operator's toggle is the only thing that changes it.
@Suite struct LaunchAtLoginTests {
  @Test func readingTheStateNeverChangesTheRegistration() {
    let expected: [(LoginItemStatus, Bool, String?)] = [
      (.notRegistered, false, nil), (.enabled, true, nil),
      (.requiresApproval, true, "Allow Earshot in System Settings > General > Login Items"),
    ]
    for (status, on, note) in expected {
      let fake = FakeLoginItem(status)
      let state = LaunchAtLogin(service: fake).state
      #expect(state.isOn == on)
      #expect(state.note == note)
      #expect(fake.calls.isEmpty, "reading \(status) called \(fake.calls)")
    }
  }

  @Test func turningItOnRegistersOnce() {
    let fake = FakeLoginItem(.notRegistered)
    #expect(LaunchAtLogin(service: fake).set(true) == nil)
    #expect(fake.calls == ["register"])
  }

  @Test func turningItOffUnregistersOnce() {
    let fake = FakeLoginItem(.enabled)
    #expect(LaunchAtLogin(service: fake).set(false) == nil)
    #expect(fake.calls == ["unregister"])
  }

  @Test func aRefusedRegistrationReturnsANoteAndNeverUnregisters() {
    let fake = FakeLoginItem(.notRegistered, registerThrows: true)
    let note = LaunchAtLogin(service: fake).set(true)
    #expect(note?.contains("refused by the system") == true)
    #expect(fake.calls == ["register"])
  }
}

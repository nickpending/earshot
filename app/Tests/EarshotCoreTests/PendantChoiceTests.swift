import Foundation
import Testing

@testable import EarshotCore

/// C1, C2: only the chosen identifier may be connected, and the choice survives a relaunch.
@Suite struct PendantChoiceTests {
  static let a = UUID(uuidString: "A1B2C3D4-0000-4000-8000-000000000001")!
  static let b = UUID(uuidString: "B2C3D4E5-0000-4000-8000-000000000002")!

  @Test func nothingChosenListsAndNeverConnects() {
    #expect(PendantChoice.decide(discovered: Self.a, chosen: nil) == .list)
    #expect(PendantChoice.decide(discovered: Self.b, chosen: nil) == .list)
  }

  @Test func onlyTheChosenIdentifierConnects() {
    #expect(PendantChoice.decide(discovered: Self.a, chosen: Self.a) == .connect)
    #expect(PendantChoice.decide(discovered: Self.a, chosen: Self.b) == .ignore)
    #expect(PendantChoice.decide(discovered: Self.b, chosen: Self.a) == .ignore)
  }

  @Test func shortIdIsTheFirstFourHexDigits() {
    #expect(PendantChoice.shortId(Self.a) == "a1b2")
  }

  @Test func theDefaultsStoreCarriesTheChoiceAcrossARelaunchAndForgets() {
    let suite = "earshot.tests.pendantchoice.\(UUID().uuidString)"
    defer { UserDefaults().removePersistentDomain(forName: suite) }

    DefaultsPendantChoiceStore(suiteName: suite).save(Self.a)
    #expect(DefaultsPendantChoiceStore(suiteName: suite).load() == Self.a)

    DefaultsPendantChoiceStore(suiteName: suite).save(nil)
    #expect(DefaultsPendantChoiceStore(suiteName: suite).load() == nil)

    let unused = "earshot.tests.pendantchoice.unused.\(UUID().uuidString)"
    defer { UserDefaults().removePersistentDomain(forName: unused) }
    #expect(DefaultsPendantChoiceStore(suiteName: unused).load() == nil)
  }
}

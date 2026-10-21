import Foundation
import Testing

@testable import EarshotCore

@Suite struct SpoolTests {
  static let repoFixture = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .deletingLastPathComponent().appendingPathComponent("fixtures/say-test.pages.v1")

  /// A StorageBuffer for page `i`.
  private func buffer(_ i: Int) -> Data {
    Data(intField(2, 7) + intField(5, i) + bytesField(6, opusFrame(0xb8, i)))
  }

  private func indices(_ records: [Data]) -> [Int] {
    records.compactMap(RawPage.pageIndex(of:))
  }

  // MARK: v1 (read to upload files written before the update)

  @Test func v1RecordFormatIsByteIdenticalToTheV1Fixture() throws {
    // fixtures/say-test.pages.v1 is real pendant audio in the v1 upload format earshotd already accepts.
    let original = try Data(contentsOf: Self.repoFixture)
    let parsed = SpoolPage.parse(original)
    #expect(parsed.validLength == original.count)
    #expect(parsed.pages.count > 10)
    #expect(parsed.pages.reduce(Data()) { $0 + $1.encoded() } == original)
  }

  @Test func v1HeaderLayoutIsLittleEndian() {
    let bytes = [UInt8](
      SpoolPage(
        session: UInt32(1), pageIndex: UInt32(2), timestampMs: UInt64(3), frames: [Data([9, 8])]
      ).encoded())
    #expect(bytes == [1, 0, 0, 0, 2, 0, 0, 0, 3, 0, 0, 0, 0, 0, 0, 0, 1, 0, 2, 0, 9, 8])
  }

  @Test func v1UnrepresentableValuesAreRefusedNotClamped() {
    #expect(throws: SpoolError.unrepresentable) {
      try SpoolPage(session: -1, pageIndex: 1, timestampMs: 1, frames: [])
    }
    #expect(throws: SpoolError.unrepresentable) {
      try SpoolPage(session: 1, pageIndex: Int(UInt32.max) + 1, timestampMs: 1, frames: [])
    }
    #expect(throws: SpoolError.unrepresentable) {
      try SpoolPage(session: 1, pageIndex: 1, timestampMs: -5, frames: [])
    }
  }

  @Test func aV1TornTailRecordIsDroppedNotParsed() {
    let one = SpoolPage(
      session: UInt32(7), pageIndex: UInt32(1), timestampMs: UInt64(1),
      frames: [Data(opusFrame(0xb8, 1))])
    let two = SpoolPage(
      session: UInt32(7), pageIndex: UInt32(2), timestampMs: UInt64(2),
      frames: [Data(opusFrame(0xb8, 2))])
    let complete = one.encoded() + two.encoded()
    let parsed = SpoolPage.parse(complete + two.encoded().prefix(25))
    #expect(parsed.pages.map { Int($0.pageIndex) } == [1, 2])
    #expect(parsed.validLength == complete.count)
  }

  // MARK: v2

  @Test func v2RecordIsALittleEndianLengthThenTheRawBytes() throws {
    let bytes = [UInt8](try RawRecord.encoded(Data([9, 8, 7])))
    #expect(bytes == [3, 0, 0, 0, 9, 8, 7])
    let parsed = RawRecord.parse(Data(bytes + bytes))
    #expect(parsed.records == [Data([9, 8, 7]), Data([9, 8, 7])])
    #expect(parsed.validLength == 14)
  }

  @Test func aV2TornTailRecordIsDroppedNotParsed() throws {
    let complete = try RawRecord.encoded(buffer(1)) + RawRecord.encoded(buffer(2))
    let torn = try complete + RawRecord.encoded(buffer(3)).prefix(9)
    let parsed = RawRecord.parse(torn)
    #expect(indices(parsed.records) == [1, 2])
    #expect(parsed.validLength == complete.count)
    // A length prefix cut short is a torn tail too.
    #expect(RawRecord.parse(complete + Data([5, 0])).validLength == complete.count)
  }

  @Test func appendSyncRotateProducesAClosedV2SegmentInOrder() throws {
    let dir = try tempDirectory("spool")
    defer { try? FileManager.default.removeItem(at: dir) }
    let spool = try Spool(directory: dir)
    for i in 1...5 { try spool.append(buffer(i)) }
    try spool.sync()
    #expect(spool.closedSegments().isEmpty, "the active segment is not uploadable until closed")
    #expect(spool.backlog().pages == 5)

    try spool.rotate()
    let segments = spool.closedSegments()
    #expect(segments.count == 1)
    #expect(segments[0].lastPathComponent.hasSuffix(".pages.v2"))
    let parsed = RawRecord.parse(try Data(contentsOf: segments[0]))
    #expect(indices(parsed.records) == [1, 2, 3, 4, 5])
    #expect(parsed.records[2] == buffer(3), "the record holds the bytes it was given")
  }

  @Test func rotatingAnEmptySpoolLeavesNothing() throws {
    let dir = try tempDirectory("spool-empty")
    defer { try? FileManager.default.removeItem(at: dir) }
    let spool = try Spool(directory: dir)
    try spool.rotate()
    #expect(try FileManager.default.contentsOfDirectory(atPath: dir.path).isEmpty)
  }

  @Test func segmentsRotateAtTheSizeLimit() throws {
    let dir = try tempDirectory("spool-size")
    defer { try? FileManager.default.removeItem(at: dir) }
    let spool = try Spool(directory: dir, maxSegmentBytes: 150)
    for i in 1...12 { try spool.append(buffer(i)) }
    try spool.rotate()
    let counts = spool.closedSegments().map {
      RawRecord.parse(try! Data(contentsOf: $0)).records.count
    }
    #expect(counts.count > 1)
    #expect(counts.reduce(0, +) == 12)
  }

  @Test func survivesRestartAndKeepsNumberingForward() throws {
    let dir = try tempDirectory("spool-restart")
    defer { try? FileManager.default.removeItem(at: dir) }
    let first = try Spool(directory: dir)
    try first.append(buffer(1))
    try first.sync()
    // The app dies here: the segment is still open.
    let second = try Spool(directory: dir)
    #expect(
      second.closedSegments().count == 1, "an open segment left by a crash is closed on start")
    try second.append(buffer(2))
    try second.rotate()
    let all = second.closedSegments().flatMap { RawRecord.parse(try! Data(contentsOf: $0)).records }
    #expect(indices(all) == [1, 2])
  }

  private func permissions(_ url: URL) throws -> Int {
    let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
    return ((attrs[.posixPermissions] as? Int) ?? -1) & 0o777
  }

  @Test func aNewSpoolDirectoryIsPrivate() throws {
    let parent = try tempDirectory("spool-new")
    defer { try? FileManager.default.removeItem(at: parent) }
    let dir = parent.appendingPathComponent("a/b/spool")
    _ = try Spool(directory: dir)
    #expect(try permissions(dir) == 0o700)
  }

  @Test func anExistingSpoolDirectoryIsMadePrivateAndItsSegmentsRecovered() throws {
    let dir = try tempDirectory("spool-open-mode")
    defer { try? FileManager.default.removeItem(at: dir) }
    try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: dir.path)
    try RawRecord.encoded(buffer(1)).write(
      to: dir.appendingPathComponent("seg-0000000001.pages.v2"))
    try RawRecord.encoded(buffer(2)).write(
      to: dir.appendingPathComponent("seg-0000000002.pages.v2.open"))

    let spool = try Spool(directory: dir)

    #expect(try permissions(dir) == 0o700)
    #expect(
      spool.closedSegments().map(\.lastPathComponent)
        == ["seg-0000000001.pages.v2", "seg-0000000002.pages.v2"])
  }

  @Test func v1SegmentsFromBeforeTheUpdateAreListedCountedAndKeepTheirNumbering() throws {
    let dir = try tempDirectory("spool-v1")
    defer { try? FileManager.default.removeItem(at: dir) }
    let v1 = SpoolPage(
      session: UInt32(7), pageIndex: UInt32(1), timestampMs: UInt64(1),
      frames: [Data(opusFrame(0xb8, 1))]
    ).encoded()
    try v1.write(to: dir.appendingPathComponent("seg-0000000004.pages.v1"))
    try (v1 + v1).write(to: dir.appendingPathComponent("seg-0000000005.pages.v1.open"))

    let spool = try Spool(directory: dir)
    #expect(
      spool.closedSegments().map(\.lastPathComponent)
        == ["seg-0000000004.pages.v1", "seg-0000000005.pages.v1"], "the open v1 segment is closed")
    #expect(spool.backlog().pages == 3, "v1 records are counted as v1")

    try spool.append(buffer(9))
    try spool.rotate()
    #expect(spool.closedSegments().last?.lastPathComponent == "seg-0000000006.pages.v2")
    #expect(spool.backlog().pages == 4)
  }

  @Test func removingASegmentDeletesIt() throws {
    let dir = try tempDirectory("spool-remove")
    defer { try? FileManager.default.removeItem(at: dir) }
    let spool = try Spool(directory: dir)
    try spool.append(buffer(1))
    try spool.rotate()
    try spool.remove(spool.closedSegments()[0])
    #expect(spool.closedSegments().isEmpty)
    #expect(spool.backlog().pages == 0)
  }

  @Test func aFailedFsyncDuringSizeRotationIsNeverReportedAsDurableLater() throws {
    let dir = try tempDirectory("spool-fsync")
    defer { try? FileManager.default.removeItem(at: dir) }
    let failing = LockedBox(true)
    let spool = try Spool(
      directory: dir, maxSegmentBytes: 150,
      fullSync: { fd in
        if failing.value { throw InjectedFailure() }
        try Spool.fullSync(fd)
      })
    var appendFailed = false
    for i in 1...12 where !appendFailed {
      do { try spool.append(buffer(i)) } catch { appendFailed = true }
    }
    #expect(appendFailed, "the rotation fsync failure must surface from append")
    #expect(spool.closedSegments().isEmpty, "a segment whose fsync failed is not closed")
    #expect(throws: InjectedFailure.self) { try spool.sync() }

    // Once the disk accepts the fsync again, sync succeeds and the segment can be closed.
    failing.mutate { $0 = false }
    try spool.sync()
    try spool.rotate()
    #expect(spool.closedSegments().count == 1)
  }
}

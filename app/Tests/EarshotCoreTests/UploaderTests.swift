import Foundation
import Testing

@testable import EarshotCore

/// C6: a segment is deleted only after a 200 covering all of its pages; otherwise it stays and is retried with
/// backoff.
@Suite struct UploaderTests {
  struct Rig {
    let dir: URL
    let spool: Spool
    let server: FakeEarshotd
    let uploader: Uploader
  }

  private func rig(url: URL? = nil, server s: FakeEarshotd? = nil) async throws -> Rig {
    let dir = try tempDirectory("upload")
    let spool = try Spool(directory: dir)
    let server: FakeEarshotd
    if let s { server = s } else { server = try await FakeEarshotd() }
    let config = EarshotConfig(serverURL: url ?? server.url, token: FakeEarshotd.token)
    let uploader = Uploader(
      spool: spool, config: config, backoff: Backoff(base: .seconds(5), cap: .seconds(60)))
    return Rig(dir: dir, spool: spool, server: server, uploader: uploader)
  }

  private func fill(_ spool: Spool, _ range: ClosedRange<Int>) throws {
    for i in range {
      try spool.append(Data(intField(2, 7) + intField(5, i) + bytesField(6, opusFrame(0xb8, i))))
    }
    try spool.rotate()
  }

  /// A segment written by the app before the update: v1 records in a `.pages.v1` file.
  private func writeV1Segment(_ dir: URL, seq: Int, _ range: ClosedRange<Int>) throws {
    let records = range.map {
      SpoolPage(
        session: 7, pageIndex: UInt32($0), timestampMs: 1_750_000_000_000 + UInt64($0),
        frames: [Data(opusFrame(0xb8, $0))]
      ).encoded()
    }
    let name = String(format: "seg-%010d", seq) + ".pages.v1"
    try records.reduce(Data(), +).write(to: dir.appendingPathComponent(name))
  }

  @Test func aV1FileGoesUpAsV1AndAV2FileAsV2EachDeletedOnlyAfterA200() async throws {
    let dir = try tempDirectory("upload-mixed")
    try writeV1Segment(dir, seq: 1, 1...3)
    let server = try await FakeEarshotd()
    let spool = try Spool(directory: dir)
    defer {
      server.stop()
      try? FileManager.default.removeItem(at: dir)
    }
    try fill(spool, 10...11)  // numbered after the v1 file
    let segments = spool.closedSegments()
    #expect(
      segments.map(\.lastPathComponent).map {
        $0.split(separator: ".").dropFirst().joined(separator: ".")
      } == ["pages.v1", "pages.v2"])
    let onDisk = LockedBox<[Int]>([])
    server.override = { _ in
      onDisk.mutate { $0.append(spool.closedSegments().count) }
      return nil
    }
    let uploader = Uploader(
      spool: spool, config: EarshotConfig(serverURL: server.url, token: FakeEarshotd.token))

    let outcome = await uploader.uploadAll()

    #expect(outcome.failure == nil)
    #expect(outcome.uploadedSegments == 2)
    #expect(outcome.uploadedPages == 5)
    let types = server.requests.map { $0.headers["content-type"] }
    #expect(types == ["application/vnd.earshot.pages.v1", "application/vnd.earshot.pages.v2"])
    #expect(onDisk.value == [2, 1], "each segment is still on disk while its request is in flight")
    #expect(spool.closedSegments().isEmpty)
  }

  @Test func segmentIsDeletedOnlyAfterA200CoveringAllItsPages() async throws {
    let r = try await rig()
    defer {
      r.server.stop()
      try? FileManager.default.removeItem(at: r.dir)
    }
    try fill(r.spool, 1...10)
    let segment = r.spool.closedSegments()[0]
    let bytes = try Data(contentsOf: segment)

    // While the request is in flight the segment must still be on disk.
    let existedDuringRequest = LockedBox(false)
    let spool = r.spool
    r.server.override = { _ in
      existedDuringRequest.mutate { $0 = !spool.closedSegments().isEmpty }
      return nil
    }
    let outcome = await r.uploader.uploadAll()

    #expect(outcome.failure == nil)
    #expect(outcome.uploadedPages == 10)
    #expect(existedDuringRequest.value)
    #expect(r.spool.closedSegments().isEmpty)
    #expect(r.spool.backlog().pages == 0)
    #expect(await r.uploader.lastSuccess != nil)
    let req = try #require(r.server.requests.first)
    #expect(req.path == "/v1/pages")
    #expect(req.body == bytes)
    #expect(req.headers["authorization"] == "Bearer \(FakeEarshotd.token)")
    #expect(req.headers["content-type"] == "application/vnd.earshot.pages.v2")
    #expect(r.server.storedKeys.count == 10)
  }

  @Test func unreachableServerKeepsTheSegmentAndBacksOff() async throws {
    let dead = try await FakeEarshotd()
    let deadURL = dead.url
    dead.stop()
    try await Task.sleep(for: .milliseconds(200))
    let r = try await rig(url: deadURL)
    defer {
      r.server.stop()
      try? FileManager.default.removeItem(at: r.dir)
    }
    try fill(r.spool, 1...4)

    let first = await r.uploader.uploadAll()
    #expect(first.failure != nil)
    #expect(r.spool.closedSegments().count == 1)
    #expect(r.spool.backlog().pages == 4, "the menu bar shows the backlog")
    #expect(await r.uploader.retryDelay == .seconds(5))

    _ = await r.uploader.uploadAll()
    #expect(await r.uploader.retryDelay == .seconds(10))
    _ = await r.uploader.uploadAll()
    #expect(await r.uploader.retryDelay == .seconds(20))
    #expect(r.spool.closedSegments().count == 1)
  }

  @Test(arguments: [500, 502, 400, 401, 403, 404])
  func nonSuccessKeepsTheSegment(status: Int) async throws {
    let r = try await rig()
    defer {
      r.server.stop()
      try? FileManager.default.removeItem(at: r.dir)
    }
    r.server.override = { _ in (status, Data(#"{"error":"x"}"#.utf8)) }
    try fill(r.spool, 1...3)

    let outcome = await r.uploader.uploadAll()

    #expect(outcome.failure != nil)
    #expect(r.spool.closedSegments().count == 1)
    #expect(await r.uploader.consecutiveFailures == 1)
  }

  @Test(arguments: [#"{"stored":2,"duplicates":0}"#, "ok"])
  func a200ThatDoesNotCoverEveryPageKeepsTheSegment(reply: String) async throws {
    let r = try await rig()
    defer {
      r.server.stop()
      try? FileManager.default.removeItem(at: r.dir)
    }
    r.server.override = { _ in (200, Data(reply.utf8)) }
    try fill(r.spool, 1...5)

    let outcome = await r.uploader.uploadAll()

    #expect(outcome.failure != nil, "a 200 that does not account for all 5 pages is a failure")
    #expect(r.spool.closedSegments().count == 1)
  }

  @Test func retrySucceedsDeletesAndResetsBackoff() async throws {
    let r = try await rig()
    defer {
      r.server.stop()
      try? FileManager.default.removeItem(at: r.dir)
    }
    r.server.override = { _ in (503, Data()) }
    try fill(r.spool, 1...3)
    _ = await r.uploader.uploadAll()
    #expect(await r.uploader.retryDelay == .seconds(5))

    r.server.override = nil
    let outcome = await r.uploader.uploadAll()

    #expect(outcome.failure == nil)
    #expect(r.spool.closedSegments().isEmpty)
    #expect(await r.uploader.consecutiveFailures == 0)
    #expect(await r.uploader.retryDelay == .zero)
  }

  @Test func resendingAPageIsADuplicateNotAnError() async throws {
    let r = try await rig()
    defer {
      r.server.stop()
      try? FileManager.default.removeItem(at: r.dir)
    }
    try fill(r.spool, 1...3)
    _ = await r.uploader.uploadAll()
    try fill(r.spool, 2...4)  // pages 2 and 3 redelivered by the pendant

    let outcome = await r.uploader.uploadAll()

    #expect(outcome.failure == nil)
    #expect(r.spool.closedSegments().isEmpty)
    #expect(r.server.storedKeys.count == 4)
  }

  @Test func segmentsGoOldestFirstAndStopAtTheFirstFailure() async throws {
    let r = try await rig()
    defer {
      r.server.stop()
      try? FileManager.default.removeItem(at: r.dir)
    }
    try fill(r.spool, 1...2)
    try fill(r.spool, 3...4)
    let seen = LockedBox<Int>(0)
    r.server.override = { _ in
      seen.mutate { $0 += 1 }
      return seen.value == 1 ? (500, Data()) : nil
    }

    _ = await r.uploader.uploadAll()

    #expect(seen.value == 1, "the second segment must not be tried behind a failed first one")
    #expect(r.spool.closedSegments().count == 2)
    let ok = await r.uploader.uploadAll()
    #expect(ok.uploadedSegments == 2)
    #expect(r.spool.closedSegments().isEmpty)
  }

  @Test func aTornTailIsNotUploaded() async throws {
    let r = try await rig()
    defer {
      r.server.stop()
      try? FileManager.default.removeItem(at: r.dir)
    }
    try fill(r.spool, 1...3)
    let segment = r.spool.closedSegments()[0]
    let handle = try FileHandle(forWritingTo: segment)
    try handle.seekToEnd()
    try handle.write(contentsOf: Data([1, 2, 3, 4, 5]))
    try handle.close()

    let outcome = await r.uploader.uploadAll()

    #expect(outcome.failure == nil)
    #expect(outcome.uploadedPages == 3)
    #expect(
      r.server.requests.first.map { RawRecord.parse($0.body).validLength == $0.body.count } == true)
    #expect(r.spool.closedSegments().isEmpty)
  }

  /// The bearer token and the recorded audio go to the configured server only: a redirect is the response.
  @Test(arguments: [301, 302, 307, 308])
  func aRedirectIsRefusedAndTheTargetIsNeverCalled(status: Int) async throws {
    let a = try await FakeEarshotd()
    let b = try await FakeEarshotd()
    a.override = { _ in (status, Data()) }
    a.responseHeaders = ["Location": "\(b.url.absoluteString)/v1/pages"]
    let r = try await rig(server: a)
    defer {
      a.stop()
      b.stop()
      try? FileManager.default.removeItem(at: r.dir)
    }
    try fill(r.spool, 1...3)

    let outcome = await r.uploader.uploadAll()

    #expect(b.requests.isEmpty, "the redirect target received a request")
    #expect(a.requests.count == 1)
    #expect(a.requests.first?.headers["authorization"] == "Bearer \(FakeEarshotd.token)")
    #expect(r.spool.closedSegments().count == 1)
    let why = try #require(outcome.failure)
    #expect(why.contains("\(status)"))
    #expect(why.contains("redirect refused"))
  }

  @Test func backoffDoublesAndCaps() {
    let b = Backoff(base: .seconds(5), cap: .seconds(60))
    #expect(
      [0, 1, 2, 3, 4, 5, 9].map { b.delay(afterFailures: $0) }
        == [
          .zero, .seconds(5), .seconds(10), .seconds(20), .seconds(40), .seconds(60), .seconds(60),
        ])
  }
}

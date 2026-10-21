import Foundation

/// POSTs one body and returns the status and response body. A seam so tests can run the real uploader against
/// a local fake of earshotd over loopback HTTP, or inject faults.
public protocol HTTPClient: Sendable {
  func post(_ request: URLRequest, body: Data) async throws -> (status: Int, body: Data)
}

/// Answers every redirect with nil, so the 3xx is the response: the body and the bearer token go to the
/// configured server only.
final class RefuseRedirects: NSObject, URLSessionTaskDelegate {
  func urlSession(
    _ session: URLSession, task: URLSessionTask,
    willPerformHTTPRedirection response: HTTPURLResponse, newRequest request: URLRequest,
    completionHandler: @escaping (URLRequest?) -> Void
  ) {
    completionHandler(nil)
  }
}

public struct URLSessionClient: HTTPClient {
  private let session: URLSession

  public init(session: URLSession = URLSession(configuration: .ephemeral)) {
    self.session = session
  }

  public func post(_ request: URLRequest, body: Data) async throws -> (status: Int, body: Data) {
    let (data, response) = try await session.upload(
      for: request, from: body, delegate: RefuseRedirects())
    guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
    return (http.statusCode, data)
  }
}

public struct Backoff: Sendable, Equatable {
  public var base: Duration
  public var cap: Duration

  public init(base: Duration = .seconds(5), cap: Duration = .seconds(900)) {
    self.base = base
    self.cap = cap
  }

  /// base, 2x base, 4x base ... up to cap, for the n-th consecutive failure (n >= 1).
  public func delay(afterFailures n: Int) -> Duration {
    guard n > 0 else { return .zero }
    var d = base
    for _ in 1..<n {
      d = d * 2
      if d >= cap { return cap }
    }
    return min(d, cap)
  }
}

public struct UploadOutcome: Sendable, Equatable {
  public let uploadedSegments: Int
  public let uploadedPages: Int
  /// Set when a segment was kept for retry: why.
  public let failure: String?
}

/// Uploads closed spool segments to earshotd's `POST /v1/pages` and deletes each only after a 200 that
/// covers every page in it. A segment's extension says its format and so its Content-Type: `.pages.v2` goes up
/// as v2, a segment written in v1 before the update as v1. Anything else (unreachable, non-2xx, a 200 that does not account for every page)
/// keeps the segment; the next attempt waits `retryDelay`.
public actor Uploader {
  public static let contentTypeV1 = "application/vnd.earshot.pages.v1"
  public static let contentTypeV2 = "application/vnd.earshot.pages.v2"

  private let spool: Spool
  private let config: EarshotConfig
  private let http: HTTPClient
  private let backoff: Backoff

  public private(set) var consecutiveFailures = 0
  public private(set) var lastSuccess: Date?
  public private(set) var lastError: String?

  public init(
    spool: Spool, config: EarshotConfig, http: HTTPClient = URLSessionClient(),
    backoff: Backoff = Backoff()
  ) {
    self.spool = spool
    self.config = config
    self.http = http
    self.backoff = backoff
  }

  /// How long to wait before the next `uploadAll` after the failures so far; zero after success.
  public var retryDelay: Duration { backoff.delay(afterFailures: consecutiveFailures) }

  /// Upload every closed segment, oldest first. Stops at the first failure so order is kept.
  @discardableResult
  public func uploadAll() async -> UploadOutcome {
    var segments = 0
    var pages = 0
    for url in spool.closedSegments() {
      switch await upload(url) {
      case .sent(let n):
        segments += 1
        pages += n
        consecutiveFailures = 0
        lastError = nil
        lastSuccess = Date()
      case .failed(let why):
        consecutiveFailures += 1
        lastError = why
        return UploadOutcome(uploadedSegments: segments, uploadedPages: pages, failure: why)
      }
    }
    return UploadOutcome(uploadedSegments: segments, uploadedPages: pages, failure: nil)
  }

  private enum Step {
    case sent(Int)
    case failed(String)
  }

  private func upload(_ url: URL) async -> Step {
    let data: Data
    do { data = try Data(contentsOf: url) } catch {
      return .failed("read \(url.lastPathComponent): \(error)")
    }
    let v1 = Spool.isV1(url)
    let count: Int
    let validLength: Int
    if v1 {
      let parsed = SpoolPage.parse(data)
      (count, validLength) = (parsed.pages.count, parsed.validLength)
    } else {
      let parsed = RawRecord.parse(data)
      (count, validLength) = (parsed.records.count, parsed.validLength)
    }
    if count == 0 {
      // No complete record: a crash tore the only record, which was never ACKed. Nothing to keep.
      do { try spool.remove(url) } catch {
        return .failed("remove empty \(url.lastPathComponent): \(error)")
      }
      return .sent(0)
    }
    // A torn tail record was never ACKed (ACK waits for fsync), so only the complete prefix goes up.
    let body = data.prefix(validLength)

    var request = URLRequest(url: config.serverURL.appendingPathComponent("v1/pages"))
    request.httpMethod = "POST"
    request.timeoutInterval = 60
    request.setValue("Bearer \(config.token)", forHTTPHeaderField: "Authorization")
    request.setValue(
      v1 ? Self.contentTypeV1 : Self.contentTypeV2, forHTTPHeaderField: "Content-Type")

    let reply: (status: Int, body: Data)
    do { reply = try await http.post(request, body: Data(body)) } catch {
      return .failed("earshotd unreachable: \(error.localizedDescription)")
    }
    if (300..<400).contains(reply.status) {
      return .failed("earshotd answered \(reply.status): redirect refused")
    }
    guard reply.status == 200 else { return .failed("earshotd answered \(reply.status)") }
    guard let json = try? JSONSerialization.jsonObject(with: reply.body) as? [String: Any],
      let stored = json["stored"] as? Int, let dup = json["duplicates"] as? Int
    else { return .failed("earshotd 200 without stored/duplicates counts") }
    guard stored + dup == count else {
      return .failed("earshotd covered \(stored + dup) of \(count) pages")
    }
    do { try spool.remove(url) } catch {
      return .failed("remove \(url.lastPathComponent): \(error)")
    }
    return .sent(count)
  }
}

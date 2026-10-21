import Foundation
import Network

@testable import EarshotCore

/// A local stand-in for earshotd's `POST /v1/pages`, over real loopback HTTP. By default it behaves like
/// earshotd: it checks the bearer token and content type, parses the body like earshotd/pages.py (a truncated
/// record is a 400 and stores nothing), and keys v1 pages by (session, page_index, page_ts_ms) and v2 records by
/// their bytes, answering `{"stored": n, "duplicates": m}`. Tests override `override` to inject faults.
final class FakeEarshotd: @unchecked Sendable {
  struct Received: Sendable {
    let path: String
    let headers: [String: String]
    let body: Data
  }

  static let token = "test-upload-token"

  private let listener: NWListener
  private let queue = DispatchQueue(label: "fake-earshotd")
  private let lock = NSLock()
  private var received: [Received] = []
  private var stored = Set<String>()
  private var _override: (@Sendable (Received) -> (Int, Data)?)?
  private(set) var port: UInt16 = 0

  private var _headers: [String: String] = [:]

  /// Extra response headers on every reply, e.g. a `Location`.
  var responseHeaders: [String: String] {
    get { lock.withLock { _headers } }
    set { lock.withLock { _headers = newValue } }
  }

  var override: (@Sendable (Received) -> (Int, Data)?)? {
    get { lock.withLock { _override } }
    set { lock.withLock { _override = newValue } }
  }

  var requests: [Received] { lock.withLock { received } }
  var storedKeys: Set<String> { lock.withLock { stored } }
  var url: URL { URL(string: "http://127.0.0.1:\(port)")! }

  init() async throws {
    let params = NWParameters.tcp
    params.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: .any)
    listener = try NWListener(using: params)
    listener.newConnectionHandler = { [unowned self] conn in self.serve(conn) }
    let ready: UInt16 = try await withCheckedThrowingContinuation { cont in
      let done = LockedBox(false)
      listener.stateUpdateHandler = { [unowned self] state in
        switch state {
        case .ready:
          if !done.value {
            done.mutate { $0 = true }
            cont.resume(returning: self.listener.port?.rawValue ?? 0)
          }
        case .failed(let e):
          if !done.value {
            done.mutate { $0 = true }
            cont.resume(throwing: e)
          }
        default: break
        }
      }
      listener.start(queue: queue)
    }
    port = ready
  }

  func stop() { listener.cancel() }

  // MARK: HTTP

  private func serve(_ conn: NWConnection) {
    conn.start(queue: queue)
    read(conn, Data())
  }

  private func read(_ conn: NWConnection, _ buffer: Data) {
    conn.receive(minimumIncompleteLength: 1, maximumLength: 1 << 20) {
      [self] data, _, done, error in
      var buf = buffer
      if let data { buf.append(data) }
      if let request = parse(buf) {
        let (status, body) = respond(request)
        let extra = responseHeaders.map { "\($0.key): \($0.value)\r\n" }.joined()
        let head =
          "HTTP/1.1 \(status) X\r\nContent-Length: \(body.count)\r\n\(extra)Connection: close\r\n\r\n"
        conn.send(
          content: Data(head.utf8) + body, completion: .contentProcessed { _ in conn.cancel() })
      } else if error != nil || done {
        conn.cancel()
      } else {
        read(conn, buf)
      }
    }
  }

  private func parse(_ buf: Data) -> Received? {
    guard let split = buf.range(of: Data("\r\n\r\n".utf8)) else { return nil }
    let head = String(decoding: buf[..<split.lowerBound], as: UTF8.self)
    let lines = head.components(separatedBy: "\r\n")
    var headers: [String: String] = [:]
    for l in lines.dropFirst() {
      if let c = l.firstIndex(of: ":") {
        headers[l[..<c].lowercased()] = l[l.index(after: c)...].trimmingCharacters(in: .whitespaces)
      }
    }
    let length = Int(headers["content-length"] ?? "0") ?? 0
    let body = buf[split.upperBound...]
    guard body.count >= length else { return nil }
    let path = lines[0].split(separator: " ").dropFirst().first.map(String.init) ?? ""
    return Received(path: path, headers: headers, body: Data(body.prefix(length)))
  }

  private func respond(_ r: Received) -> (Int, Data) {
    lock.withLock { received.append(r) }
    if let o = override, let reply = o(r) { return reply }
    guard r.path == "/v1/pages" else { return (404, Data()) }
    guard r.headers["authorization"] == "Bearer \(Self.token)" else { return (401, Data()) }
    let keys: [String]
    switch r.headers["content-type"] {
    case "application/vnd.earshot.pages.v1":
      let parsed = SpoolPage.parse(r.body)
      guard parsed.validLength == r.body.count else {
        return (400, Data(#"{"error":"truncated"}"#.utf8))
      }
      keys = parsed.pages.map { "\($0.session):\($0.pageIndex):\($0.timestampMs)" }
    case "application/vnd.earshot.pages.v2":
      let parsed = RawRecord.parse(r.body)
      guard parsed.validLength == r.body.count else {
        return (400, Data(#"{"error":"truncated"}"#.utf8))
      }
      // Keyed by the raw bytes: this fake does not decode what earshotd decodes.
      keys = parsed.records.map { "v2:" + $0.base64EncodedString() }
    default:
      return (400, Data(#"{"error":"Content-Type"}"#.utf8))
    }
    var newCount = 0
    lock.withLock {
      for key in keys where stored.insert(key).inserted { newCount += 1 }
    }
    let json = #"{"stored":\#(newCount),"duplicates":\#(keys.count - newCount)}"#
    return (200, Data(json.utf8))
  }
}

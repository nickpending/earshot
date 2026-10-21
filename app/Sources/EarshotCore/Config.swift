import Foundation

public enum ConfigError: Error, Equatable, CustomStringConvertible {
  case missingFile(String)
  case missingKey(String)
  case invalidServerURL(String)
  case plaintextRemote(String)
  /// The file's path, its permission bits, and the uid that owns it.
  case exposed(path: String, mode: UInt16, owner: UInt32)

  public var description: String {
    switch self {
    case .missingFile(let p): return "config not found: \(p)"
    case .missingKey(let k): return "config has no \(k)"
    case .invalidServerURL(let v): return "server_url is not an http(s) URL: \(v)"
    case .plaintextRemote(let v):
      return
        "server_url must be https unless the server is on this machine; http would send the token in the clear: \(v)"
    case .exposed(let path, let mode, let owner):
      return
        "config \(path) is mode \(String(format: "%04o", Int(mode))) owned by uid \(owner); it holds the token, so it must be owned by you and readable by no one else: chmod 600 \(path)"
    }
  }
}

/// `~/.config/earshot/config.toml`: `server_url` and `token`. Nothing else in the app supplies either.
public struct EarshotConfig: Sendable, Equatable {
  public let serverURL: URL
  public let token: String

  public static var defaultPath: URL {
    FileManager.default.homeDirectoryForCurrentUser
      .appendingPathComponent(".config/earshot/config.toml")
  }

  public init(serverURL: URL, token: String) {
    self.serverURL = serverURL
    self.token = token
  }

  /// Refuses, before reading a byte of it, a file any group or other permission bit is set on or that a uid
  /// other than `effectiveUID` owns. It never changes the file.
  public static func load(from path: URL = defaultPath, effectiveUID: uid_t = geteuid()) throws
    -> EarshotConfig
  {
    // Open once: the descriptor that is checked is the one that is read, so a swap of the path between the
    // check and the read cannot slip an unchecked file through.
    let fd = open(path.path, O_RDONLY)
    if fd < 0 { throw ConfigError.missingFile(path.path) }
    let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    var info = stat()
    guard fstat(fd, &info) == 0 else { throw ConfigError.missingFile(path.path) }
    let mode = UInt16(info.st_mode & 0o777)
    if mode & 0o077 != 0 || info.st_uid != effectiveUID {
      throw ConfigError.exposed(path: path.path, mode: mode, owner: info.st_uid)
    }
    // readToEnd returns nil, not empty Data, for an empty file.
    guard let data = try? (handle.readToEnd() ?? Data()),
      let text = String(data: data, encoding: .utf8)
    else { throw ConfigError.missingFile(path.path) }
    return try parse(text)
  }

  /// The subset of TOML the config uses: `key = "string"` lines, `#` comments, `[tables]` ignored.
  public static func parse(_ text: String) throws -> EarshotConfig {
    var values: [String: String] = [:]
    for raw in text.split(whereSeparator: \.isNewline) {
      let line = raw.trimmingCharacters(in: .whitespaces)
      if line.isEmpty || line.hasPrefix("#") || line.hasPrefix("[") { continue }
      guard let eq = line.firstIndex(of: "=") else { continue }
      let key = line[..<eq].trimmingCharacters(in: .whitespaces)
      let rest = line[line.index(after: eq)...].trimmingCharacters(in: .whitespaces)
      if let v = quoted(rest) { values[key] = v }
    }
    guard let url = values["server_url"], !url.isEmpty else {
      throw ConfigError.missingKey("server_url")
    }
    guard let token = values["token"], !token.isEmpty else { throw ConfigError.missingKey("token") }
    guard let parsed = URL(string: url), let scheme = parsed.scheme,
      ["http", "https"].contains(scheme),
      parsed.host != nil
    else { throw ConfigError.invalidServerURL(url) }
    if scheme == "http", !["127.0.0.1", "localhost", "::1", "[::1]"].contains(parsed.host ?? "") {
      throw ConfigError.plaintextRemote(url)
    }
    return EarshotConfig(serverURL: parsed, token: token)
  }

  private static func quoted(_ s: String) -> String? {
    guard let q = s.first, q == "\"" || q == "'" else { return nil }
    let body = s.dropFirst()
    guard let end = body.firstIndex(of: q) else { return nil }
    return String(body[..<end])
  }
}

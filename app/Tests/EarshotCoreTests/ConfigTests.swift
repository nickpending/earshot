import Foundation
import Testing

@testable import EarshotCore

@Suite struct ConfigTests {
  static let valid = "server_url = \"http://127.0.0.1:9\"\ntoken = \"t\"\n"

  /// A config file with `text` at `mode`, in a fresh temp directory.
  private func write(_ text: String, mode: Int) throws -> URL {
    let dir = try tempDirectory("config")
    let file = dir.appendingPathComponent("config.toml")
    try text.write(to: file, atomically: true, encoding: .utf8)
    try FileManager.default.setAttributes([.posixPermissions: mode], ofItemAtPath: file.path)
    return file
  }

  private func mode(of file: URL) throws -> Int {
    let attrs = try FileManager.default.attributesOfItem(atPath: file.path)
    return (attrs[.posixPermissions] as? Int) ?? -1
  }

  @Test func parsesServerURLAndToken() throws {
    let c = try EarshotConfig.parse(
      """
      # earshot
      server_url = "https://earshot.example.test"  # no trailing slash
      token = 'abc123'
      """)
    #expect(c.serverURL.absoluteString == "https://earshot.example.test")
    #expect(c.token == "abc123")
  }

  @Test func missingKeysAreNamed() {
    #expect(throws: ConfigError.missingKey("token")) {
      try EarshotConfig.parse(#"server_url = "https://x.test""#)
    }
    #expect(throws: ConfigError.missingKey("server_url")) {
      try EarshotConfig.parse(#"token = "t""#)
    }
  }

  @Test func nonHTTPURLIsRejected() {
    #expect(throws: ConfigError.invalidServerURL("ftp://x.test")) {
      try EarshotConfig.parse("server_url = \"ftp://x.test\"\ntoken = \"t\"")
    }
  }

  @Test func plainHTTPIsRefusedBeyondThisMachine() throws {
    for url in ["http://192.168.1.20:8080", "http://earshot.example.lan", "http://[fe80::1]:8080"] {
      #expect(throws: ConfigError.plaintextRemote(url)) {
        try EarshotConfig.parse("server_url = \"\(url)\"\ntoken = \"t\"")
      }
    }
    for url in [
      "http://127.0.0.1:8080", "http://localhost:8080", "http://[::1]:8080",
      "https://earshot.example.lan",
    ] {
      #expect(
        try EarshotConfig.parse("server_url = \"\(url)\"\ntoken = \"t\"").serverURL.absoluteString
          == url)
    }
  }

  @Test func loadsFromAFileAndReportsAMissingOne() throws {
    let file = try write(Self.valid, mode: 0o600)
    defer { try? FileManager.default.removeItem(at: file.deletingLastPathComponent()) }
    #expect(try EarshotConfig.load(from: file).token == "t")
    let none = file.deletingLastPathComponent().appendingPathComponent("none.toml")
    #expect(throws: ConfigError.missingFile(none.path)) { try EarshotConfig.load(from: none) }
  }

  // MARK: C1 — a file others can read is refused before any key is parsed

  @Test func anyGroupOrOtherBitIsRefusedNamingPathModeOwnerAndTheFix() throws {
    let noToken = "server_url = \"http://127.0.0.1:9\"\n"
    let cases: [(String, Int, String)] = [
      (Self.valid, 0o644, "0644"), (Self.valid, 0o640, "0640"), (Self.valid, 0o604, "0604"),
      (Self.valid, 0o620, "0620"), (Self.valid, 0o602, "0602"), (noToken, 0o644, "0644"),
    ]
    for (text, perms, digits) in cases {
      let file = try write(text, mode: perms)
      defer { try? FileManager.default.removeItem(at: file.deletingLastPathComponent()) }
      do {
        _ = try EarshotConfig.load(from: file)
        Issue.record("mode \(digits) loaded")
      } catch let error as ConfigError {
        guard case .exposed = error else {
          Issue.record("mode \(digits) threw \(error), not exposed")
          continue
        }
        let d = error.description
        #expect(d.contains(file.path))
        #expect(d.contains(digits))
        #expect(d.contains("uid \(geteuid())"))
        #expect(d.contains("chmod 600"))
      }
    }
  }

  @Test func ownerOnlyFilesLoad() throws {
    for perms in [0o600, 0o400] {
      let file = try write(Self.valid, mode: perms)
      defer { try? FileManager.default.removeItem(at: file.deletingLastPathComponent()) }
      let c = try EarshotConfig.load(from: file)
      #expect(c.serverURL.absoluteString == "http://127.0.0.1:9")
      #expect(c.token == "t")
    }
  }

  @Test func anEmptyPrivateFileReportsTheMissingKeyNotAMissingFile() throws {
    let file = try write("", mode: 0o600)
    defer { try? FileManager.default.removeItem(at: file.deletingLastPathComponent()) }
    #expect(throws: ConfigError.missingKey("server_url")) { try EarshotConfig.load(from: file) }
  }

  // MARK: C2 — owned by someone else

  @Test func aFileOwnedByAnotherUserIsRefusedNamingItsOwner() throws {
    let file = try write(Self.valid, mode: 0o600)
    defer { try? FileManager.default.removeItem(at: file.deletingLastPathComponent()) }
    let me = geteuid()
    do {
      _ = try EarshotConfig.load(from: file, effectiveUID: me + 1)
      Issue.record("a file owned by another uid loaded")
    } catch let error as ConfigError {
      guard case .exposed = error else {
        Issue.record("threw \(error), not exposed")
        return
      }
      #expect(error.description.contains("uid \(me)"))
    }
    #expect(try EarshotConfig.load(from: file, effectiveUID: me).token == "t")
  }

  // MARK: C3 — the app never fixes the operator's file

  @Test func refusingAFileLeavesItsModeAndBytesAlone() throws {
    let file = try write(Self.valid, mode: 0o644)
    defer { try? FileManager.default.removeItem(at: file.deletingLastPathComponent()) }
    let before = try Data(contentsOf: file)
    #expect(throws: (any Error).self) { try EarshotConfig.load(from: file) }
    #expect(try mode(of: file) == 0o644)
    #expect(try Data(contentsOf: file) == before)
  }
}

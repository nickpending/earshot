import EarshotCore
import SwiftUI

@main
struct EarshotApp: App {
  @State private var model = StatusModel()

  init() {
    // Bluetooth and the drain loops start with the app, not when the menu first opens.
    let m = StatusModel()
    m.start()
    _model = State(initialValue: m)
  }

  var body: some Scene {
    MenuBarExtra {
      Text(model.title)
      if let chosen = model.chosen {
        Text("Pendant \(PendantChoice.shortId(chosen))")
        Button("Forget Pendant") { model.forget() }
      } else {
        Menu("Choose Pendant") {
          if model.nearby.isEmpty {
            Text("No pendant seen nearby")
          } else {
            ForEach(model.nearby, id: \.identifier) { entry in
              Button(entry.label) { model.choose(entry.identifier) }
            }
          }
        }
      }
      Divider()
      Text(model.lines.battery)
      Text(model.lines.audio)
      Text(model.lines.lastHeard)
      Text(model.lines.today)
      Text(model.lines.sync)
      Text(model.pagesOnPendant.map { "On pendant: \($0) pages" } ?? "On pendant: unknown")
      Text("Backlog: \(model.backlogPages) pages")
      if !model.attention.isEmpty {
        Divider()
        ForEach(model.attention, id: \.self) { Text($0) }
      }
      if let problem = model.problem {
        Divider()
        Text(problem)
      }
      Divider()
      Toggle(
        "Launch at Login",
        isOn: Binding(get: { model.launchAtLogin }, set: { model.setLaunchAtLogin($0) }))
      if let note = model.loginNote { Text(note) }
      Divider()
      Button("Quit Earshot") { NSApplication.shared.terminate(nil) }
        .keyboardShortcut("q")
    } label: {
      Image(systemName: model.symbol)
    }
  }
}

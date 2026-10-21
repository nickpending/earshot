import EarshotCore
import UserNotifications

/// Posts alerts as macOS notifications. The permission prompt comes the first time an alert is due, not at launch.
struct Notifier: AlertCenter {
  func permission() async -> NotificationPermission {
    switch await UNUserNotificationCenter.current().notificationSettings().authorizationStatus {
    case .notDetermined: return .notDetermined
    case .denied: return .denied
    default: return .granted
    }
  }

  func requestPermission() async -> Bool {
    (try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]))
      ?? false
  }

  /// One identifier per kind, so a repeat replaces the earlier banner instead of stacking.
  func post(_ alert: Alert) async throws {
    let content = UNMutableNotificationContent()
    content.title = alert.title
    content.body = alert.body
    content.sound = .default
    let request = UNNotificationRequest(
      identifier: "casa.mal.Earshot.\(alert.kind.rawValue)", content: content, trigger: nil)
    try await UNUserNotificationCenter.current().add(request)
  }
}

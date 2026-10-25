// Tell the person when a run needs them or has finished, even in another tab.
// Uses the page title always, and desktop notifications if they allowed them.

const BASE_TITLE = "CodeCrew";

export function requestNotifications(): void {
  try {
    if (typeof Notification !== "undefined" && Notification.permission === "default") {
      void Notification.requestPermission();
    }
  } catch {
    /* unsupported: the title still changes */
  }
}

export function setTitle(prefix: string | null): void {
  if (typeof document === "undefined") return;
  document.title = prefix ? `${prefix} · ${BASE_TITLE}` : BASE_TITLE;
}

export function notify(title: string, body: string): void {
  try {
    if (
      typeof document !== "undefined" &&
      document.visibilityState === "hidden" &&
      typeof Notification !== "undefined" &&
      Notification.permission === "granted"
    ) {
      new Notification(title, { body, tag: "codecrew" });
    }
  } catch {
    /* unsupported */
  }
}

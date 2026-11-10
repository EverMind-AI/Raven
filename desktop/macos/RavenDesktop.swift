import Cocoa
import WebKit

// Raven Desktop -- a native macOS window around the Raven page.
// No browser tab: the app IS the window. The gateway is a resident process
// started by launch.sh, so closing this window leaves the engine running.

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var busy = false
    private var loadedPage = false

    private let localHosts: Set<String> = ["127.0.0.1", "localhost", "::1"]

    // MARK: - Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        NSApp.activate(ignoringOtherApps: true)
        reconnect()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    // MARK: - UI

    private func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Raven", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Hide Raven", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Quit Raven", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let fileItem = NSMenuItem()
        let fileMenu = NSMenu(title: "File")
        let reconnectItem = NSMenuItem(title: "Reconnect", action: #selector(reconnectAction), keyEquivalent: "R")
        reconnectItem.target = self
        reconnectItem.keyEquivalentModifierMask = [.command, .shift]
        fileMenu.addItem(reconnectItem)
        let reloadItem = NSMenuItem(title: "Reload Page", action: #selector(reloadAction), keyEquivalent: "r")
        reloadItem.target = self
        fileMenu.addItem(reloadItem)
        fileMenu.addItem(NSMenuItem.separator())
        let closeItem = NSMenuItem(title: "Close Window", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fileMenu.addItem(closeItem)
        fileItem.submenu = fileMenu
        main.addItem(fileItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        for (title, action, key) in [
            ("Undo", #selector(UndoManager.undo), "z"),
            ("Redo", #selector(UndoManager.redo), "Z"),
            ("Cut", #selector(NSText.cut(_:)), "x"),
            ("Copy", #selector(NSText.copy(_:)), "c"),
            ("Paste", #selector(NSText.paste(_:)), "v"),
            ("Select All", #selector(NSText.selectAll(_:)), "a")
        ] as [(String, Selector, String)] {
            let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
            editMenu.addItem(item)
        }
        editItem.submenu = editMenu
        main.addItem(editItem)

        NSApp.mainMenu = main
    }

    private func buildWindow() {
        let visible = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let width = min(1280, visible.width * 0.9)
        let height = min(860, visible.height * 0.9)
        let rect = NSRect(x: 0, y: 0, width: width, height: height)

        window = NSWindow(
            contentRect: rect,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Raven"
        window.setFrameAutosaveName("RavenDesktopMainWindow")
        window.minSize = NSSize(width: 880, height: 560)
        window.center()
        window.isReleasedWhenClosed = false

        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        let pagePrefs = WKWebpagePreferences()
        pagePrefs.allowsContentJavaScript = true
        config.defaultWebpagePreferences = pagePrefs

        webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.allowsMagnification = true
        webView.autoresizingMask = [.width, .height]
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)
    }

    // MARK: - Bootstrap

    @objc private func reconnectAction() { reconnect() }

    @objc private func reloadAction() {
        if loadedPage { webView.reload() } else { reconnect() }
    }

    private func reconnect() {
        guard !busy else { return }
        busy = true
        loadedPage = false
        showStatus("Starting Raven...", retry: false)
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let urlString = AppDelegate.bootstrapURL()
            DispatchQueue.main.async {
                guard let self else { return }
                self.busy = false
                if let s = urlString, let u = URL(string: s) {
                    self.webView.load(URLRequest(url: u))
                } else {
                    self.showStatus("The gateway did not start. Press Cmd-Shift-R to retry, or check ~/.raven/logs/.", retry: true)
                }
            }
        }
    }

    private static func bootstrapURL() -> String? {
        let task = Process()
        let launcher = Bundle.main.bundleURL
            .appendingPathComponent("Contents/Resources/scripts/launch.sh")
        task.executableURL = launcher
        let out = Pipe()
        task.standardOutput = out
        task.standardError = Pipe()
        do {
            try task.run()
        } catch {
            return nil
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        let s = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard let s, s.hasPrefix("http://") || s.hasPrefix("https://") else { return nil }
        return s
    }

    private func showStatus(_ message: String, retry: Bool) {
        loadedPage = false
        let button = retry
            ? "<p style=\"margin-top:18px\"><a href=\"raven://retry\" style=\"color:#7aa2f7;font-size:14px\">Reconnect</a></p>"
            : ""
        let html = """
        <!doctype html><html><head><meta charset="utf-8"><style>
        html,body{height:100%;margin:0;display:flex;align-items:center;justify-content:center;
        font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#101216;color:#e6e8ec}
        @media (prefers-color-scheme: light){html,body{background:#f6f7f9;color:#1d1f23}}
        .box{text-align:center;max-width:520px;padding:24px}
        .mark{font-size:40px}.title{font-size:22px;font-weight:600;margin-top:10px}
        .msg{margin-top:12px;opacity:.72;font-size:14px;line-height:1.6}
        a{text-decoration:none}
        </style></head><body><div class="box"><div class="mark">R</div>
        <div class="title">Raven</div><div class="msg">\(message)</div>\(button)</div></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    // MARK: - WKNavigationDelegate

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.allow); return
        }
        if url.scheme == "raven" {
            if url.host == "retry" { reconnect() }
            decisionHandler(.cancel); return
        }
        if isLocal(url) { decisionHandler(.allow); return }
        if url.scheme == "http" || url.scheme == "https" {
            // External links belong to the system browser, not this window.
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel); return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if let t = webView.title, !t.isEmpty, isLocal(webView.url) {
            window.title = t
            loadedPage = true
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        failIfLocal(error)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        failIfLocal(error)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        reconnect()
    }

    private func failIfLocal(_ error: Error) {
        let ns = error as NSError
        if ns.code == NSURLErrorCancelled { return }
        guard let url = ns.userInfo[NSURLErrorFailingURLErrorKey] as? URL, isLocal(url) else { return }
        showStatus("The page could not reach the gateway. Press Cmd-Shift-R to retry.", retry: true)
    }

    private func isLocal(_ url: URL?) -> Bool {
        guard let url, let host = url.host else { return false }
        return localHosts.contains(host)
    }

    // MARK: - WKUIDelegate

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard navigationAction.targetFrame == nil, let url = navigationAction.request.url else { return nil }
        if isLocal(url) {
            webView.load(navigationAction.request)
        } else if url.scheme == "http" || url.scheme == "https" {
            NSWorkspace.shared.open(url)
        }
        return nil
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = "Raven"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.runModal()
        completionHandler()
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = "Raven"
        alert.informativeText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

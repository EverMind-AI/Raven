import Cocoa
import WebKit

// Raven Desktop -- a native macOS window around the Raven page.
// No browser tab: the app IS the window. The gateway is a resident process
// started by launch.sh, so closing this window leaves the engine running.
//
// The page recognises this window by the RavenShell token in its user agent and
// talks to it through the "raven" script message handler: it lifts this window's
// own splash with {type:"ready"}, asks for a fresh sign-in link with
// {type:"reauth"}, and reports {type:"theme"}, {type:"drag"} and {type:"zoom"}.
// The page's side of that bridge lives in ui-web/src/app/connection.ts and
// ui-web/src/state/shellWindow.ts.

private final class ScriptBridge: NSObject, WKScriptMessageHandler {
    weak var app: AppDelegate?

    init(app: AppDelegate) {
        self.app = app
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        app?.receive(message)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var splash: NSView?
    private var busy = false
    private var loadedPage = false
    private var lastReauth = Date.distantPast

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
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered,
            defer: false
        )
        window.title = "Raven"
        window.setFrameAutosaveName("RavenDesktopMainWindow")
        window.minSize = NSSize(width: 880, height: 560)
        // The page's header band is the title bar in this window: it measures
        // the traffic-light row and drags the window (see page.css and
        // state/shellWindow.ts), so the OS title bar must stay out of its way.
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.isMovableByWindowBackground = false
        window.center()
        window.isReleasedWhenClosed = false

        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        let pagePrefs = WKWebpagePreferences()
        pagePrefs.allowsContentJavaScript = true
        config.defaultWebpagePreferences = pagePrefs
        // The page keys every shell-only path off this token in the user agent.
        config.applicationNameForUserAgent = "RavenShell/1.0"
        config.userContentController.add(ScriptBridge(app: self), name: "raven")

        webView = WKWebView(frame: rect, configuration: config)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = true
        webView.allowsMagnification = true
        webView.autoresizingMask = [.width, .height]

        // The splash is a native sibling of the web view rather than a page:
        // the page removes its own page-level splash in the shell, so this is
        // the only launch surface until the page posts {type:"ready"}.
        let content = NSView(frame: rect)
        content.autoresizingMask = [.width, .height]
        webView.frame = content.bounds
        content.addSubview(webView)
        window.contentView = content

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
        showSplash("Starting Raven...")
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result = AppDelegate.bootstrap()
            DispatchQueue.main.async {
                guard let self else { return }
                self.busy = false
                if let url = result.url {
                    self.webView.load(URLRequest(url: url))
                } else {
                    self.showStatus(result.error ?? "The gateway did not start. Check ~/.raven/web.log.", retry: true)
                }
            }
        }
    }

    private static func bootstrap() -> (url: URL?, error: String?) {
        let launcher = Bundle.main.bundleURL
            .appendingPathComponent("Contents/Resources/scripts/launch.sh")
        let task = Process()
        task.executableURL = launcher
        let out = Pipe()
        task.standardOutput = out

        // stderr goes to a file rather than a pipe: the launcher's failure
        // message is the only explanation the page gets, and reading two pipes
        // in sequence can deadlock on a long one.
        let errorLog = FileManager.default.temporaryDirectory
            .appendingPathComponent("raven-desktop-\(UUID().uuidString).log")
        guard FileManager.default.createFile(atPath: errorLog.path, contents: nil),
              let errorHandle = try? FileHandle(forWritingTo: errorLog) else {
            return (nil, "Could not prepare the launcher's error log.")
        }
        task.standardError = errorHandle
        defer {
            try? errorHandle.close()
            try? FileManager.default.removeItem(at: errorLog)
        }

        do {
            try task.run()
        } catch {
            return (nil, "The launcher could not start: \(error.localizedDescription)")
        }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        task.waitUntilExit()
        try? errorHandle.close()

        let text = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let failure = (try? String(contentsOf: errorLog, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard task.terminationStatus == 0,
              let url = URL(string: text),
              url.scheme == "http" || url.scheme == "https" else {
            let reason = failure.isEmpty
                ? "The gateway did not start (exit \(task.terminationStatus)). See ~/.raven/web.log."
                : failure
            return (nil, reason)
        }
        return (url, nil)
    }

    // MARK: - The page's bridge

    func receive(_ message: WKScriptMessage) {
        guard message.name == "raven",
              message.frameInfo.isMainFrame,
              isLocal(message.webView?.url),
              let body = message.body as? [String: Any],
              let type = body["type"] as? String else { return }
        switch type {
        case "ready":
            hideSplash()
        case "reauth":
            reauth()
        case "theme":
            if let value = body["value"] as? String { applyTheme(value) }
        case "drag":
            dragWindow()
        case "zoom":
            window.zoom(nil)
        default:
            break
        }
    }

    private func reauth() {
        // The page stops asking after two tries; the throttle here keeps a
        // gateway that refuses every fresh nonce from spinning through reloads.
        guard Date().timeIntervalSince(lastReauth) > 2 else { return }
        lastReauth = Date()
        reconnect()
    }

    private func applyTheme(_ value: String) {
        switch value {
        case "dark": NSApp.appearance = NSAppearance(named: .darkAqua)
        case "light": NSApp.appearance = NSAppearance(named: .aqua)
        default: NSApp.appearance = nil
        }
    }

    private func dragWindow() {
        guard let event = NSApp.currentEvent,
              event.type == .leftMouseDown || event.type == .leftMouseDragged else { return }
        window.performDrag(with: event)
    }

    // MARK: - Splash

    private func showSplash(_ message: String) {
        guard splash == nil, let content = window.contentView else { return }
        let view = NSVisualEffectView(frame: content.bounds)
        view.material = .windowBackground
        view.blendingMode = .behindWindow
        view.state = .active
        view.autoresizingMask = [.width, .height]

        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.startAnimation(nil)

        let title = NSTextField(labelWithString: "Raven")
        title.font = .systemFont(ofSize: 22, weight: .semibold)
        let detail = NSTextField(labelWithString: message)
        detail.font = .systemFont(ofSize: 13)
        detail.textColor = .secondaryLabelColor

        let stack = NSStackView(views: [spinner, title, detail])
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = 10
        stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: view.centerYAnchor)
        ])
        content.addSubview(view)
        splash = view
    }

    private func hideSplash() {
        splash?.removeFromSuperview()
        splash = nil
    }

    // MARK: - Status page

    private func showStatus(_ message: String, retry: Bool) {
        hideSplash()
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
        .msg{margin-top:12px;opacity:.72;font-size:14px;line-height:1.6;word-break:break-word}
        a{text-decoration:none}
        </style></head><body><div class="box"><div class="mark">R</div>
        <div class="title">Raven</div><div class="msg">\(escaped(message))</div>\(button)</div></body></html>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    private func escaped(_ text: String) -> String {
        String(text.prefix(600))
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
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

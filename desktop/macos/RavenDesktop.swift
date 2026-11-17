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

// What a navigation should do, decided before any WebKit call is made. Kept as
// a value so the matrix can be exercised without a window or a server.
enum NavigationIntent {
    case allow      // the page itself
    case download   // a file the gateway sends as an attachment
    case preview    // a page this app shows in its own child window
    case external   // hand it to the system browser
    case retry      // raven://retry
    case cancel
}

struct NavigationPolicy {
    let localHosts: Set<String>

    func isLocal(_ url: URL?) -> Bool {
        guard let url, let host = url.host else { return false }
        return localHosts.contains(host)
    }

    /// A click or redirect about to happen.
    func action(for url: URL?, targetFrameIsNil: Bool, shouldPerformDownload: Bool) -> NavigationIntent {
        guard let url else { return .allow }
        if url.scheme == "raven" {
            return url.host == "retry" ? .retry : .cancel
        }
        // A download is not a page. Letting it through replaces the window with
        // the file itself, and the page's script context goes with it.
        if shouldPerformDownload {
            return isLocal(url) ? .download : .external
        }
        if targetFrameIsNil {
            // `target=_blank` and `window.open`. Loading this into the main view
            // would navigate the app away from the page that asked for it.
            if isLocal(url) { return .preview }
            if url.scheme == "http" || url.scheme == "https" { return .external }
            return .allow
        }
        if isLocal(url) { return .allow }
        if url.scheme == "http" || url.scheme == "https" { return .external }
        return .allow
    }

    /// The response to a navigation already allowed: WebKit knows the headers
    /// and the MIME type here, so an attachment can still become a download
    /// rather than a page. Subframes are left alone -- a framed viewer is the
    /// page's own business.
    func response(for url: URL?, isForMainFrame: Bool, disposition: String?, canShowMIMEType: Bool) -> NavigationIntent {
        guard isForMainFrame, isLocal(url) else { return .allow }
        if let disposition, disposition.lowercased().contains("attachment") { return .download }
        if !canShowMIMEType { return .download }
        return .allow
    }
}

private final class ScriptBridge: NSObject, WKScriptMessageHandler {
    weak var app: AppDelegate?

    init(app: AppDelegate) {
        self.app = app
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        app?.receive(message)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate,
                         NSWindowDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var splash: NSView?
    private var busy = false
    private var loadedPage = false
    private var lastReauth = Date.distantPast
    private var previewWindows: [NSWindow] = []

    private let policy = NavigationPolicy(localHosts: ["127.0.0.1", "localhost", "::1"])

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
        // A way back from anywhere the page can navigate to, since this window
        // has no browser toolbar.
        let backItem = NSMenuItem(title: "Back", action: #selector(goBack), keyEquivalent: "[")
        backItem.target = self
        fileMenu.addItem(backItem)
        let forwardItem = NSMenuItem(title: "Forward", action: #selector(goForward), keyEquivalent: "]")
        forwardItem.target = self
        fileMenu.addItem(forwardItem)
        fileMenu.addItem(NSMenuItem.separator())
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

    @objc private func goBack() {
        if webView.canGoBack { webView.goBack() }
    }

    @objc private func goForward() {
        if webView.canGoForward { webView.goForward() }
    }

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
        // The bridge belongs to the main page: a preview window showing a
        // delivered file must not be able to drive the app's splash, theme or
        // sign-in handshake.
        guard message.name == "raven",
              message.webView === webView,
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
        let intent = policy.action(
            for: navigationAction.request.url,
            targetFrameIsNil: navigationAction.targetFrame == nil,
            shouldPerformDownload: navigationAction.shouldPerformDownload)
        switch intent {
        case .allow:
            decisionHandler(.allow)
        case .download:
            decisionHandler(.download)
        case .external:
            // External links belong to the system browser, not this window.
            if let url = navigationAction.request.url { NSWorkspace.shared.open(url) }
            decisionHandler(.cancel)
        case .preview:
            // Left to `createWebViewWith`, which WebKit calls next. Cancelling
            // here would drop a request that delegate is about to answer.
            decisionHandler(.allow)
        case .retry:
            reconnect()
            decisionHandler(.cancel)
        case .cancel:
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let http = navigationResponse.response as? HTTPURLResponse
        let intent = policy.response(
            for: navigationResponse.response.url,
            isForMainFrame: navigationResponse.isForMainFrame,
            disposition: http?.value(forHTTPHeaderField: "Content-Disposition"),
            canShowMIMEType: navigationResponse.canShowMIMEType)
        decisionHandler(intent == .download ? .download : .allow)
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    // MARK: - WKDownloadDelegate

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        completionHandler(AppDelegate.saveLocation(for: suggestedFilename))
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        let alert = NSAlert()
        alert.messageText = "The download failed"
        alert.informativeText = error.localizedDescription
        alert.addButton(withTitle: "OK")
        alert.runModal()
    }

    /// Where a download lands: the reader's Downloads folder, under a name that
    /// does not overwrite an earlier file. `WebKit` asks for this once per
    /// download and fails the download if the answer is nil.
    static func saveLocation(for suggested: String, in folder: URL? = nil) -> URL {
        let fm = FileManager.default
        let dir = folder
            ?? fm.urls(for: .downloadsDirectory, in: .userDomainMask).first
            ?? fm.temporaryDirectory
        let cleaned = suggested.isEmpty ? "download" : suggested
        var target = dir.appendingPathComponent(cleaned)
        let stem = target.deletingPathExtension().lastPathComponent
        let ext = target.pathExtension
        var n = 2
        while fm.fileExists(atPath: target.path) {
            target = dir.appendingPathComponent(ext.isEmpty ? "\(stem) (\(n))" : "\(stem) (\(n)).\(ext)")
            n += 1
        }
        return target
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // A child window loading its file must not retitle the main one, and
        // must not count as "the page is up" for the reload shortcut.
        guard webView === self.webView else { return }
        if let t = webView.title, !t.isEmpty, isLocal(webView.url) {
            window.title = t
            loadedPage = true
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        failIfLocal(error, in: webView)
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        failIfLocal(error, in: webView)
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        // Only the main page is worth rebuilding; a child's crash just closes
        // that child rather than restarting the app's gateway handshake.
        if webView === self.webView {
            reconnect()
        } else {
            webViewDidClose(webView)
        }
    }

    private func failIfLocal(_ error: Error, in webView: WKWebView) {
        let ns = error as NSError
        if ns.code == NSURLErrorCancelled { return }
        guard let url = ns.userInfo[NSURLErrorFailingURLErrorKey] as? URL, isLocal(url) else { return }
        // The status page belongs to the main window; a child that cannot load
        // is closed instead of overwriting the app.
        if webView === self.webView {
            showStatus("The page could not reach the gateway. Press Cmd-Shift-R to retry.", retry: true)
        } else {
            webViewDidClose(webView)
        }
    }

    private func isLocal(_ url: URL?) -> Bool {
        policy.isLocal(url)
    }

    // MARK: - WKUIDelegate

    /// A page asked for a window of its own (`target=_blank`, `window.open`).
    /// The page keeps its own view: the child gets a real window beside it, so
    /// "open in a new tab" shows the file and leaves Raven where it was.
    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        guard navigationAction.targetFrame == nil, let url = navigationAction.request.url else { return nil }
        if !isLocal(url) {
            if url.scheme == "http" || url.scheme == "https" { NSWorkspace.shared.open(url) }
            return nil
        }
        let child = WKWebView(frame: NSRect(x: 0, y: 0, width: 960, height: 720), configuration: configuration)
        child.navigationDelegate = self
        child.uiDelegate = self
        child.allowsMagnification = true
        child.autoresizingMask = [.width, .height]

        let panel = NSWindow(
            contentRect: child.frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false)
        panel.title = url.lastPathComponent.isEmpty ? "Raven" : url.lastPathComponent
        panel.contentView = child
        panel.isReleasedWhenClosed = false
        panel.delegate = self
        panel.center()
        previewWindows.append(panel)
        panel.makeKeyAndOrderFront(nil)
        return child
    }

    /// `window.close()` from a page in a child window.
    func webViewDidClose(_ webView: WKWebView) {
        guard webView !== self.webView else { return }
        for panel in previewWindows where panel.contentView === webView {
            panel.close()
        }
    }

    /// A child window the reader closed with its own button.
    func windowWillClose(_ notification: Notification) {
        guard let closed = notification.object as? NSWindow, closed !== window else { return }
        previewWindows.removeAll { $0 === closed }
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

// MARK: - Policy self-test

/// `RAVEN_DESKTOP_SELFTEST=1` runs the navigation matrix and exits without
/// opening a window, so the decisions that keep the page in place can be
/// checked in CI on any machine -- no gateway, no GUI, no click.
private func runSelfTest() -> Int32 {
    let p = NavigationPolicy(localHosts: ["127.0.0.1", "localhost", "::1"])
    let local = URL(string: "http://127.0.0.1:18793/files/download?token=t")!
    let page = URL(string: "http://127.0.0.1:18793/")!
    let remote = URL(string: "https://example.com/file.zip")!

    var failures: [String] = []
    func check(_ label: String, _ got: NavigationIntent, _ want: NavigationIntent) {
        if String(describing: got) != String(describing: want) {
            failures.append("\(label): got \(got), want \(want)")
        }
    }

    // A download click must never be a page navigation; that is the defect this
    // matrix exists for.
    check("local download", p.action(for: local, targetFrameIsNil: false, shouldPerformDownload: true), .download)
    check("external download", p.action(for: remote, targetFrameIsNil: false, shouldPerformDownload: true), .external)
    check("page load", p.action(for: page, targetFrameIsNil: false, shouldPerformDownload: false), .allow)
    check("external link", p.action(for: remote, targetFrameIsNil: false, shouldPerformDownload: false), .external)
    check("new window local", p.action(for: page, targetFrameIsNil: true, shouldPerformDownload: false), .preview)
    check("new window external", p.action(for: remote, targetFrameIsNil: true, shouldPerformDownload: false), .external)
    check("retry", p.action(for: URL(string: "raven://retry")!, targetFrameIsNil: false, shouldPerformDownload: false), .retry)
    check("other raven scheme", p.action(for: URL(string: "raven://elsewhere")!, targetFrameIsNil: false, shouldPerformDownload: false), .cancel)

    // Headers seen at response time: an attachment is still a download even
    // when the click did not say so.
    check("attachment main", p.response(for: local, isForMainFrame: true,
                                        disposition: "attachment; filename=\"a.png\"",
                                        canShowMIMEType: true), .download)
    check("html main", p.response(for: page, isForMainFrame: true, disposition: "inline",
                                  canShowMIMEType: true), .allow)
    check("unshowable main", p.response(for: local, isForMainFrame: true, disposition: nil,
                                        canShowMIMEType: false), .download)
    check("attachment subframe", p.response(for: local, isForMainFrame: false,
                                            disposition: "attachment; filename=\"a.png\"",
                                            canShowMIMEType: true), .allow)

    // A second download of the same file must not overwrite the first.
    let fm = FileManager.default
    let dir = fm.temporaryDirectory.appendingPathComponent("raven-selftest-\(UUID().uuidString)")
    try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
    let first = dir.appendingPathComponent("a.png")
    fm.createFile(atPath: first.path, contents: Data())
    if AppDelegate.saveLocation(for: "a.png", in: dir) == first {
        failures.append("saveLocation reused an existing name")
    }
    if AppDelegate.saveLocation(for: "b.png", in: dir).lastPathComponent != "b.png" {
        failures.append("saveLocation rewrote a fresh name")
    }
    try? fm.removeItem(at: dir)

    if failures.isEmpty {
        print("selftest: ok (12 policy cases, saveLocation)")
        return 0
    }
    for f in failures { print("selftest: FAIL \(f)") }
    return 1
}

if ProcessInfo.processInfo.environment["RAVEN_DESKTOP_SELFTEST"] == "1" {
    exit(runSelfTest())
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

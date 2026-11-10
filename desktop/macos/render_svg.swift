import AppKit
import WebKit

// Render an SVG file to a 1024px transparent PNG without qlmanage / rsvg.
// qlmanage is unavailable in some sandboxed environments; WebKit is always
// present on macOS and renders SVG faithfully.
//
// The SVG is copied into a temporary directory next to the generated HTML so
// WKWebView's file:// sandbox allows the relative reference. The source SVG
// (which lives in ui-web/src/assets/) is never written to.
//
// Usage: render_svg <input.svg> <output.png>

final class Renderer: NSObject, WKNavigationDelegate {
    let out: URL

    init(out: URL) {
        self.out = out
    }

    func webView(_ web: WKWebView, didFinish navigation: WKNavigation!) {
        let cfg = WKSnapshotConfiguration()
        cfg.rect = NSRect(x: 0, y: 0, width: 1024, height: 1024)
        web.takeSnapshot(with: cfg) { image, error in
            guard let image else {
                print("snapshot error: \(String(describing: error))")
                exit(1)
            }
            guard let tiff = image.tiffRepresentation,
                  let rep = NSBitmapImageRep(data: tiff),
                  let png = rep.representation(using: .png, properties: [:]) else {
                print("png conversion error")
                exit(1)
            }
            do {
                try png.write(to: self.out)
                print("rendered \(self.out.path)")
                exit(0)
            } catch {
                print("write error: \(error)")
                exit(1)
            }
        }
    }

    func webView(_ web: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        print("load error: \(error)")
        exit(1)
    }

    func webView(_ web: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
        print("provisional load error: \(error)")
        exit(1)
    }
}

let args = CommandLine.arguments
guard args.count >= 3 else {
    print("usage: render_svg <input.svg> <output.png>")
    exit(2)
}
let svg = URL(fileURLWithPath: args[1])
let out = URL(fileURLWithPath: args[2])

let tmp = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
    .appendingPathComponent("raven-icon-render-\(ProcessInfo.processInfo.globallyUniqueString)", isDirectory: true)
do {
    try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
    try? FileManager.default.removeItem(at: tmp.appendingPathComponent("mark.svg"))
    try FileManager.default.copyItem(at: svg, to: tmp.appendingPathComponent("mark.svg"))
} catch {
    print("cannot stage svg: \(error)")
    exit(1)
}

let htmlURL = tmp.appendingPathComponent("page.html")
let html = """
<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;padding:0;width:1024px;height:1024px;overflow:hidden;background:transparent}
img{position:absolute;left:96px;top:96px;width:832px;height:832px}
</style></head><body><img src="mark.svg"></body></html>
"""
do {
    try html.write(to: htmlURL, atomically: true, encoding: .utf8)
} catch {
    print("html write error: \(error)")
    exit(1)
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1024, height: 1024))
web.setValue(false, forKey: "drawsBackground")
// WKWebView holds its delegate weakly; keep a strong reference for the run.
let renderer = Renderer(out: out)
web.navigationDelegate = renderer
web.loadFileURL(htmlURL, allowingReadAccessTo: tmp)
DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
    print("timeout (delegate=\(String(describing: web.navigationDelegate)))")
    exit(2)
}
app.run()

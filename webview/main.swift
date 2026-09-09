// main.swift — 原生 macOS 窗口，用 WKWebView 加载本地后端页面
// 用法: E2fsBrowser http://127.0.0.1:PORT/
import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!

    func applicationDidFinishLaunching(_ notification: Notification) {
        let urlArg = CommandLine.arguments.count > 1
            ? CommandLine.arguments[1] : "http://127.0.0.1:8765/"

        let config = WKWebViewConfiguration()
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")

        let webView = WKWebView(frame: .zero, configuration: config)

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
            styleMask: [.titled, .closable, .miniaturizable, .resizable,
                        .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "Ext 文件系统浏览器"
        window.titleVisibility = .hidden
        window.titlebarAppearsTransparent = true
        window.minSize = NSSize(width: 860, height: 560)
        window.contentView = webView
        window.center()
        window.setFrameAutosaveName("E2fsBrowserMain")

        var urlString = urlArg
        if !urlString.contains("#") { urlString += "#native" }
        if let url = URL(string: urlString) {
            webView.load(URLRequest(url: url))
        }
        window.makeKeyAndOrderFront(nil)
        NSApplication.shared.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()

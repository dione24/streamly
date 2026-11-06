import AppKit
import SwiftUI

/// Shows the player's layer. SwiftUI may rebuild this view when the layout changes, but the layer
/// stays with the player, so playback goes on without interruption.
public struct VideoSurface: NSViewRepresentable {
    private let layer: MPVMetalLayer

    public init(layer: MPVMetalLayer) {
        self.layer = layer
    }

    public func makeNSView(context: Context) -> VideoHostView {
        VideoHostView(videoLayer: layer)
    }

    public func updateNSView(_ view: VideoHostView, context: Context) {}
}

public final class VideoHostView: NSView {
    private let videoLayer: MPVMetalLayer

    init(videoLayer: MPVMetalLayer) {
        self.videoLayer = videoLayer
        super.init(frame: .zero)
        layer = videoLayer
        wantsLayer = true
        layerContentsRedrawPolicy = .never
    }

    required init?(coder: NSCoder) { nil }

    public override func layout() {
        super.layout()
        syncSize()
    }

    public override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        // Reclaim the layer in case another host view had it during a layout change.
        if layer !== videoLayer { layer = videoLayer }
        syncSize()
    }

    public override func viewDidChangeBackingProperties() {
        super.viewDidChangeBackingProperties()
        syncSize()
    }

    private func syncSize() {
        guard let window else { return }
        let scale = window.backingScaleFactor
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        videoLayer.contentsScale = scale
        videoLayer.frame = bounds
        videoLayer.drawableSize = CGSize(width: bounds.width * scale, height: bounds.height * scale)
        CATransaction.commit()
    }
}

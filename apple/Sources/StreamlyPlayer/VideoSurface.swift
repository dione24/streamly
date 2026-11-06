import AppKit
import SwiftUI

/// Shows the player's layer. SwiftUI may rebuild this view when the layout changes, and the picture
/// can move to another host (full-window player, mini window): the layer stays with the player, so
/// playback goes on without interruption.
public struct VideoSurface: NSViewRepresentable {
    private let layer: MPVMetalLayer

    public init(layer: MPVMetalLayer) {
        self.layer = layer
    }

    public func makeNSView(context: Context) -> VideoHostView {
        VideoHostView(videoLayer: layer)
    }

    public func updateNSView(_ view: VideoHostView, context: Context) {
        view.adoptLayer()
    }
}

/// The video layer is a sublayer, not the view's own layer: when one host replaces another, AppKit
/// tearing down the old view must not detach the layer from its new parent, whatever the order.
public final class VideoHostView: NSView {
    private let videoLayer: MPVMetalLayer

    init(videoLayer: MPVMetalLayer) {
        self.videoLayer = videoLayer
        super.init(frame: .zero)
        wantsLayer = true
        layer?.backgroundColor = CGColor(red: 0, green: 0, blue: 0, alpha: 1)
        layerContentsRedrawPolicy = .never
    }

    required init?(coder: NSCoder) { nil }

    func adoptLayer() {
        guard window != nil, let layer, videoLayer.superlayer !== layer else { return }
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        layer.addSublayer(videoLayer)
        CATransaction.commit()
        syncSize()
    }

    public override func layout() {
        super.layout()
        syncSize()
    }

    public override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        adoptLayer()
    }

    public override func viewDidChangeBackingProperties() {
        super.viewDidChangeBackingProperties()
        syncSize()
    }

    private func syncSize() {
        guard let window, videoLayer.superlayer === layer else { return }
        let scale = window.backingScaleFactor
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        videoLayer.contentsScale = scale
        videoLayer.frame = bounds
        videoLayer.drawableSize = CGSize(width: bounds.width * scale, height: bounds.height * scale)
        CATransaction.commit()
    }
}

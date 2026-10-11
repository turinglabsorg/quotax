import QtQuick
import QtQuick.Shapes

// Stroked provider marks from the Quotax design system. Drawn in a 24px box
// and scaled, so they stay sharp on a fractional display scale.
Item {
  id: root

  property string shape: "gauge"
  property color ink: "white"

  implicitWidth: 16
  implicitHeight: 16

  readonly property real unit: 24
  readonly property real stroke: unit * 0.15
  readonly property string outline: {
    if (root.shape === "claude")
      return "M12 9.79L12 0.96M13.3 10.21L17.06 5.03M14.1 11.32L22.5 8.59M14.1 12.68L20.19 14.66M13.3 13.79L18.49 20.93M12 14.21L12 20.61M10.7 13.79L5.51 20.93M9.9 12.68L3.81 14.66M9.9 11.32L1.5 8.59M10.7 10.21L6.94 5.03"
    if (root.shape === "codex")
      return "M2.4 4.8L11.04 12L2.4 19.2M13.92 19.68L22.08 19.68"
    if (root.shape === "grok")
      return "M20.04 10.58A8.16 8.16 0 1 1 13.42 3.96M1.44 22.56L22.56 1.44"
    if (root.shape === "ollama")
      return "M15.36 9.6A4.8 4.8 0 0 1 20.16 14.4L20.16 17.76A4.8 4.8 0 0 1 15.36 22.56L8.64 22.56A4.8 4.8 0 0 1 3.84 17.76L3.84 14.4A4.8 4.8 0 0 1 8.64 9.6ZM8.88 9.6L6.96 1.92M15.12 9.6L17.04 1.92"
    return "M3.27 18.48A10.08 10.08 0 1 1 20.73 18.48M12 13.44L7.9 8.72"
  }

  Item {
    width: root.unit
    height: root.unit
    scale: root.width > 0 ? root.width / root.unit : 1
    transformOrigin: Item.TopLeft

    Shape {
      anchors.fill: parent
      preferredRendererType: Shape.CurveRenderer
      antialiasing: true

      ShapePath {
        strokeColor: root.ink
        strokeWidth: root.stroke
        fillColor: "transparent"
        capStyle: ShapePath.RoundCap
        joinStyle: ShapePath.RoundJoin
        PathSvg { path: root.outline }
      }

      ShapePath {
        fillColor: root.shape === "ollama" ? root.ink : "transparent"
        strokeColor: "transparent"
        PathSvg {
          path: root.shape === "ollama"
            ? "M9.36 15.36 m -1.8 0 a 1.8 1.8 0 1 0 3.6 0 a 1.8 1.8 0 1 0 -3.6 0 M14.64 15.36 m -1.8 0 a 1.8 1.8 0 1 0 3.6 0 a 1.8 1.8 0 1 0 -3.6 0"
            : "M0 0"
        }
      }
    }
  }
}

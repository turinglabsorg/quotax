import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "turinglabs.quotax"

  property var rows: []
  property var pending: []
  property bool quotaOpen: false
  property bool fresh: true
  property double nowSec: Date.now() / 1000

  readonly property bool opened: quotaOpen
  readonly property string bin: Quickshell.env("HOME") + "/.local/bin/quotax"
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property int glyphSize: Style.bar.iconCanvas
  readonly property int pad: Style.spacing.md
  readonly property color warningInk: "#ffa348"
  readonly property color criticalInk: "#f66151"
  readonly property color warningFill: "#ff7800"
  readonly property color criticalFill: "#e01b24"
  readonly property color healthyFill: "#33d17a"
  readonly property color claudeInk: "#d97757"

  function open() { quotaOpen = true }
  function close() { quotaOpen = false }
  function toggle() { quotaOpen ? close() : open() }

  // Keep the numbers on screen. Clearing them collapses this slot and the
  // whole right side of the bar jumps.
  function refresh() {
    if (fetchProc.running) return
    root.pending = []
    fetchProc.running = true
  }

  function remaining(window) {
    if (!window) return null
    if (window.resetsAt !== null && window.resetsAt !== undefined && window.resetsAt <= root.nowSec) return 100
    var used = Number(window.usedPercent)
    if (!isFinite(used)) return null
    return 100 - Math.floor(used + 0.5)
  }

  function panelWindows(windows) {
    var session = null
    var longer = null
    for (var i = 0; i < windows.length; i++) {
      if (windows[i].kind === "session") session = windows[i]
      else if (!longer && (windows[i].kind === "weekly" || windows[i].kind === "monthly")) longer = windows[i]
    }
    if (session && longer) return [session, longer]
    var best = null
    var bestLeft = 101
    for (var j = 0; j < windows.length; j++) {
      if (windows[j].kind === "weeklyModel" && windows.length > 1) continue
      if (windows[j].remaining !== null && windows[j].remaining < bestLeft) {
        best = windows[j]
        bestLeft = windows[j].remaining
      }
    }
    return best ? [best] : []
  }

  function nameOf(provider) {
    if (provider === "claude") return "Claude"
    if (provider === "codex") return "Codex"
    if (provider === "grok") return "Grok"
    if (provider === "ollama") return "Ollama"
    return provider || "Quota"
  }

  function tightest(values) {
    if (!values || !values.length) return 100
    var best = Number(values[0])
    for (var i = 1; i < values.length; i++) {
      var value = Number(values[i])
      if (value < best) best = value
    }
    return best
  }

  function previousRow(id) {
    for (var i = 0; i < root.rows.length; i++) {
      if (root.rows[i].id === id) return root.rows[i]
    }
    return null
  }

  function countdown(to) {
    if (to === null || to === undefined) return ""
    var seconds = Math.trunc(Number(to) - root.nowSec)
    if (seconds <= 0) return "now"
    var days = Math.floor(seconds / 86400)
    var hours = Math.floor((seconds % 86400) / 3600)
    var minutes = Math.floor((seconds % 3600) / 60)
    if (days > 0) return hours > 0 ? days + "d " + hours + "h" : days + "d"
    if (hours > 0) return minutes > 0 ? hours + "h " + minutes + "m" : hours + "h"
    return Math.max(minutes, 1) + "m"
  }

  function levelInk(value) {
    if (value <= 5) return criticalInk
    if (value <= 20) return warningInk
    return foreground
  }

  function levelFill(value) {
    if (value <= 5) return criticalFill
    if (value <= 20) return warningFill
    return healthyFill
  }

  function remember(entry) {
    var next = root.pending.slice()
    var replaced = false
    for (var n = 0; n < next.length; n++) {
      if (next[n].id === entry.id) {
        next[n] = entry
        replaced = true
      }
    }
    if (!replaced) next.push(entry)
    root.pending = next
  }

  function ingest(line) {
    var event
    try { event = JSON.parse(line) } catch (error) { return }
    if (!event) return
    if (event.type === "done") {
      var order = { claude: 0, codex: 1, grok: 2, ollama: 3 }
      var sorted = root.pending.slice()
      sorted.sort(function(a, b) {
        var ao = order[a.provider] === undefined ? 9 : order[a.provider]
        var bo = order[b.provider] === undefined ? 9 : order[b.provider]
        return ao - bo
      })
      if (sorted.length > 0) root.rows = sorted
      root.pending = []
      root.fresh = true
      return
    }
    if (event.type !== "result") return
    var id = String(event.id || "")
    var prev = previousRow(id)
    if (!event.snapshot) {
      var issue = event.issue || {}
      var kept = issue.keepsLastSnapshot !== false
      remember({
        id: id,
        provider: prev ? prev.provider : "gauge",
        name: prev ? prev.name : "Quota",
        account: prev ? prev.account : "",
        plan: prev ? prev.plan : "",
        windows: kept && prev ? prev.windows : [],
        barValues: kept && prev ? prev.barValues : [],
        issue: String(issue.message || "Unavailable")
      })
      root.fresh = kept
      return
    }
    var snapshot = event.snapshot
    var windows = []
    var raw = snapshot.windows || []
    for (var i = 0; i < raw.length; i++) {
      var left = remaining(raw[i])
      if (left === null) continue
      windows.push({
        kind: String(raw[i].kind || ""),
        label: String(raw[i].label || ""),
        remaining: left,
        resetsAt: raw[i].resetsAt
      })
    }
    var shown = panelWindows(windows)
    var values = []
    for (var s = 0; s < shown.length; s++) values.push(shown[s].remaining)
    remember({
      id: id || String(snapshot.provider || ""),
      provider: String(snapshot.provider || (prev ? prev.provider : "")),
      name: nameOf(snapshot.provider || (prev ? prev.provider : "")),
      account: String(snapshot.account || ""),
      plan: String(snapshot.plan || ""),
      windows: windows,
      barValues: values,
      issue: ""
    })
  }

  Process {
    id: fetchProc
    command: [root.bin, "fetch", "--json"]
    stdout: SplitParser {
      onRead: function(line) { root.ingest(String(line || "").trim()) }
    }
    onExited: function(exitCode) {
      if (exitCode !== 0) root.fresh = false
    }
  }

  Timer {
    interval: 180000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  Timer {
    interval: 30000
    running: root.quotaOpen
    repeat: true
    onTriggered: root.nowSec = Date.now() / 1000
  }

  onQuotaOpenChanged: if (quotaOpen) nowSec = Date.now() / 1000

  implicitWidth: cluster.implicitWidth + pad * 2
  implicitHeight: barSize

  Row {
    id: cluster
    x: root.pad
    anchors.verticalCenter: parent.verticalCenter
    spacing: 10
    opacity: root.fresh ? 1 : 0.55

    Repeater {
      model: root.rows
      delegate: Row {
        id: account
        required property var modelData
        readonly property var values: modelData.barValues || []
        readonly property int valueCount: values.length
        spacing: 4
        height: root.barSize

        Glyph {
          anchors.verticalCenter: parent.verticalCenter
          width: root.glyphSize
          height: root.glyphSize
          shape: modelData.provider || "gauge"
          ink: account.valueCount > 0 ? root.levelInk(root.tightest(account.values)) : root.foreground
        }

        Column {
          anchors.verticalCenter: parent.verticalCenter
          spacing: account.valueCount > 1 ? -2 : 0

          Repeater {
            model: account.values
            delegate: Text {
              required property var modelData
              text: String(modelData)
              textFormat: Text.PlainText
              color: root.levelInk(Number(modelData))
              font.family: root.fontFamily
              font.pixelSize: account.valueCount > 1 ? 9 : Style.font.bodySmall
              font.weight: account.valueCount > 1 ? Font.DemiBold : Font.Medium
              font.hintingPreference: Font.PreferVerticalHinting
              renderType: Text.NativeRendering
            }
          }

          Text {
            visible: account.valueCount === 0
            text: "–"
            textFormat: Text.PlainText
            color: root.foreground
            opacity: 0.45
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }
        }
      }
    }

    Glyph {
      visible: root.rows.length === 0
      anchors.verticalCenter: parent.verticalCenter
      width: root.glyphSize
      height: root.glyphSize
      shape: "gauge"
      ink: root.foreground
    }
  }

  MouseArea {
    anchors.fill: parent
    acceptedButtons: Qt.LeftButton | Qt.MiddleButton
    onClicked: function(mouse) {
      if (mouse.button === Qt.MiddleButton) root.refresh()
      else root.toggle()
    }
  }

  PopupCard {
    id: quotaPopup
    anchorItem: root
    bar: root.bar
    owner: root
    contentWidth: quotaPopup.fittedContentWidth(Style.space(320))
    contentHeight: quotaPopup.fittedContentHeight(detailColumn.implicitHeight)
    open: root.quotaOpen

    Column {
      id: detailColumn
      width: parent.width
      spacing: 12

      Repeater {
        model: root.rows
        delegate: Column {
          id: accountBlock
          required property int index
          required property var modelData
          width: detailColumn.width
          spacing: 8

          Rectangle {
            visible: accountBlock.index > 0
            width: parent.width
            height: 1
            color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)
          }

          Row {
            width: parent.width
            spacing: 8

            Glyph {
              width: 18
              height: 18
              anchors.verticalCenter: parent.verticalCenter
              shape: modelData.provider || "gauge"
              ink: modelData.provider === "claude" ? root.claudeInk : root.foreground
            }

            Column {
              width: parent.width - 26
              spacing: 1

              Row {
                width: parent.width
                spacing: 8

                Text {
                  width: Math.max(0, parent.width - planLabel.implicitWidth - parent.spacing)
                  textFormat: Text.PlainText
                  text: modelData.name
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  font.bold: true
                  elide: Text.ElideRight
                }

                Text {
                  id: planLabel
                  visible: modelData.plan !== ""
                  textFormat: Text.PlainText
                  text: modelData.plan
                  color: root.foreground
                  opacity: 0.62
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.weight: Font.DemiBold
                }
              }

              Text {
                width: parent.width
                visible: modelData.account !== ""
                textFormat: Text.PlainText
                text: modelData.account
                color: root.foreground
                opacity: 0.62
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }
          }

          Text {
            visible: modelData.issue !== ""
            width: parent.width
            textFormat: Text.PlainText
            text: modelData.issue
            wrapMode: Text.WordWrap
            color: root.warningInk
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
          }

          Repeater {
            model: modelData.windows
            delegate: Column {
              required property var modelData
              width: accountBlock.width
              spacing: 3

              Item {
                width: parent.width
                height: Math.max(windowLabel.implicitHeight, windowValue.implicitHeight)

                Text {
                  id: windowLabel
                  anchors.left: parent.left
                  anchors.right: windowValue.left
                  anchors.rightMargin: 8
                  anchors.verticalCenter: parent.verticalCenter
                  textFormat: Text.PlainText
                  text: modelData.label
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                }

                Text {
                  id: windowValue
                  anchors.right: parent.right
                  anchors.verticalCenter: parent.verticalCenter
                  textFormat: Text.PlainText
                  text: modelData.remaining + "%"
                  color: root.levelInk(modelData.remaining)
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                }
              }

              Rectangle {
                width: parent.width
                height: 6
                radius: 3
                color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.13)

                Rectangle {
                  height: parent.height
                  radius: 3
                  width: modelData.remaining <= 0 ? 0 : Math.max(height, parent.width * modelData.remaining / 100)
                  color: root.levelFill(modelData.remaining)

                  Behavior on width {
                    NumberAnimation { duration: 160; easing.type: Easing.OutCubic }
                  }
                }
              }

              Text {
                visible: modelData.resetsAt !== null && modelData.resetsAt !== undefined
                text: "Resets in " + root.countdown(modelData.resetsAt)
                color: root.foreground
                opacity: 0.62
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }
        }
      }

      Text {
        visible: root.rows.length === 0
        width: parent.width
        text: "No linked accounts"
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }
    }
  }
}

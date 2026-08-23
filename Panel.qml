import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// WalkingPad popup: today's progress, GitHub-style history graph, goal editor
// and the collector enable/disable switch. All data comes from stats.py; the
// QML stays a dumb renderer over its JSON.
Panel {
  id: root
  moduleName: "msegoviadev.walkingpad"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color accent: Color.accent
  readonly property color muted: Color.muted
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property string homeDir: Quickshell.env("HOME")
  readonly property string backendDir: Qt.resolvedUrl("backend").toString().replace("file://", "")
  readonly property string venvPython: homeDir + "/.local/share/walkingpad/venv/bin/python"
  readonly property int goalSteps: Math.max(0, Number(setting("goalSteps", 0)) || 0)
  readonly property bool imperial: Model.useImperial(setting("units", "Auto"), Qt.locale().name)
  readonly property int gridWeeks: 15
  readonly property int cellSize: 18
  readonly property int cellSpacing: 3
  readonly property int cellPitch: cellSize + cellSpacing
  readonly property int gridPixelWidth: gridWeeks * cellSize + (gridWeeks - 1) * cellSpacing
  readonly property int monthStripHeight: Math.ceil(Style.font.caption * 1.4)

  property var stats: null
  property var cells: []
  property var months: []
  readonly property var weekdays: Model.weekdayLabels()
  property var hovered: null
  property int prevTodaySteps: 0
  property int celebratedSteps: 0
  property double nowMs: Date.now()
  property bool padPickerOpen: false

  readonly property bool enabled: stats ? stats.enabled === true : false
  readonly property bool connected: stats ? stats.connected === true : false
  readonly property bool walking: stats ? stats.walking === true : false
  readonly property int todaySteps: stats ? Number(stats.today.steps) || 0 : 0
  readonly property bool goalMet: goalSteps > 0 && todaySteps >= goalSteps
  readonly property real goalProgress: goalSteps > 0 ? Math.min(1, todaySteps / goalSteps) : 0
  readonly property int sessionSeconds: {
    if (!walking || !stats || !stats.live || !stats.live.session_start) return 0
    return Math.max(0, Math.floor((nowMs - Number(stats.live.session_start) * 1000) / 1000))
  }

  readonly property string barText: {
    var icon = walking ? "󰷺" : "󰜎"
    if (!enabled) return icon
    if (goalSteps > 0) return icon + " " + Model.fmt(todaySteps) + "/" + Model.fmt(goalSteps)
    return icon + " " + Model.fmt(todaySteps)
  }

  readonly property string tooltipText: {
    if (!stats) return "WalkingPad"
    if (!enabled) return "WalkingPad collector is off\nClick: details · Right-click: enable"
    var text = "WalkingPad · " + Model.fmt(todaySteps) + " steps today"
    if (goalSteps > 0) text += " (goal " + Model.fmt(goalSteps) + ")"
    if (walking) text += "\nWalking now"
    return text + "\nClick: details · Right-click: " + (enabled ? "disable" : "enable")
  }

  function alpha(color, opacity) {
    return Qt.rgba(color.r, color.g, color.b, opacity)
  }

  function open() {
    root.controller.show()
    root.refresh()
  }

  function close() {
    root.controller.hide()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.open()
  }

  function refresh() {
    if (!statsProc.running) statsProc.running = true
  }

  function toggleEnabled() {
    toggleProc.command = ["systemctl", "--user", enabled ? "stop" : "start", "walkingpad.service"]
    toggleProc.running = true
  }

  function saveGoal(text) {
    var n = parseInt(text, 10)
    if (isNaN(n) || n < 0) n = 0
    setGoalProc.command = ["omarchy-shell", "shell", "setBarWidget", "msegoviadev.walkingpad", "goalSteps", String(n), "{}"]
    setGoalProc.running = true
  }

  function selectPad(address) {
    selectProc.command = [root.venvPython, root.backendDir + "/select-pad.py", address]
    selectProc.running = true
    padPickerOpen = false
  }

  function padDisplayName() {
    if (!stats) return ""
    if (stats.live && stats.live.device_name) return stats.live.device_name
    var selected = String(stats.selected_address || "")
    var devices = stats.devices || []
    for (var i = 0; i < devices.length; i++)
      if (String(devices[i].address) === selected) return devices[i].name || devices[i].address
    return selected || ""
  }

  function padStateText() {
    if (!enabled) return "collector off"
    if (connected) return "connected"
    return "scanning..."
  }

  onTodayStepsChanged: {
    // Celebrate only a fresh crossing: a previous reading below the goal must
    // exist, so opening the bar with the goal already met stays quiet. Capture
    // the count now so the toast cannot race a later poll and under-report.
    if (goalSteps > 0 && prevTodaySteps > 0 && prevTodaySteps < goalSteps && todaySteps >= goalSteps) {
      root.celebratedSteps = todaySteps
      celebrateProc.running = true
    }
    prevTodaySteps = todaySteps
  }

  Process {
    id: statsProc
    command: [root.venvPython, root.backendDir + "/stats.py", "--goal", String(root.goalSteps)]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var raw = String(text || "").trim()
        if (!raw) return
        try {
          root.stats = JSON.parse(raw)
          root.nowMs = Date.now()
          root.cells = Model.buildCells(root.stats.start, root.stats.days, root.goalSteps, root.gridWeeks)
          root.months = Model.monthLabels(root.stats.start, root.gridWeeks)
        } catch (e) {
          console.warn("walkingpad: stats parse failed: " + e)
        }
      }
    }
  }

  Process { id: toggleProc; onExited: refreshDelay.start() }
  Process { id: selectProc; onExited: refreshDelay.start() }
  Process { id: setGoalProc }
  Process {
    id: celebrateProc
    // Clicking the toast opens the popup (the shell keeps and runs the
    // command, so this survives shell restarts).
    command: ["omarchy-notification-send", "-g", "󰜎",
      "--exec", "omarchy-shell shell summon msegoviadev.walkingpad '{}'",
      "Walking goal reached", Model.fmt(root.celebratedSteps) + " steps today"]
  }

  Timer {
    id: refreshDelay
    interval: 800
    onTriggered: root.refresh()
  }

  Timer {
    interval: root.walking || root.opened ? 3000 : 15000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(column.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      blocked: goalField.activeFocus
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Flickable {
        id: scroll
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: column
          width: scroll.width
          spacing: Style.space(14)
          topPadding: Style.space(6)
          bottomPadding: Style.space(6)

          // ---- Today: big count, goal progress, streak and totals.
          Item {
            width: parent.width
            height: Math.max(todayLeft.height, todayRight.height)

            Row {
              id: todayLeft
              anchors.left: parent.left
              anchors.leftMargin: Style.space(16)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(12)

              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: root.walking ? "󰷺" : "󰜎"
                color: root.goalMet ? root.accent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: 44
              }

              Column {
                anchors.verticalCenter: parent.verticalCenter
                spacing: 2

                Row {
                  spacing: Style.space(6)
                  Text {
                    text: Model.fmt(root.todaySteps)
                    color: root.goalMet ? root.accent : root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: 36
                    font.bold: true
                  }
                  Text {
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 6
                    text: root.goalSteps > 0 ? "/ " + Model.fmt(root.goalSteps) : "steps today"
                    color: root.muted
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }

                Text {
                  visible: root.enabled
                  text: !root.connected ? "Pad off, collector watching"
                    : root.walking ? "Walking · " + Model.fmtSpeed(root.stats.live.speed, root.imperial) + " · " + Model.fmtDist(root.stats.live.dist_m, root.imperial) + " · " + Model.fmtTime(root.sessionSeconds)
                    : "Pad on, belt idle"
                  color: root.walking ? root.accent : root.muted
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
                Text {
                  visible: !root.enabled
                  text: "Collector is off, no data is being recorded"
                  color: root.muted
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }

            Column {
              id: todayRight
              anchors.right: parent.right
              anchors.rightMargin: Style.space(16)
              anchors.verticalCenter: parent.verticalCenter
              spacing: 2

              Text {
                anchors.right: parent.right
                text: root.stats && root.stats.streak > 0 ? "󰈸 " + root.stats.streak + " day streak" : ""
                color: root.accent
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                visible: text !== ""
              }
              Text {
                anchors.right: parent.right
                text: root.stats ? Model.fmt(root.stats.totals.steps) + " steps all time" : ""
                color: root.muted
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
              Text {
                anchors.right: parent.right
                text: root.stats ? Model.fmtDist(root.stats.totals.dist_m, root.imperial) + " · " + Model.fmtTime(root.stats.totals.time_s) : ""
                color: root.muted
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          // ---- Goal progress bar.
          Rectangle {
            visible: root.goalSteps > 0
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: Style.space(16)
            anchors.rightMargin: Style.space(16)
            height: 6
            radius: 3
            color: root.alpha(root.foreground, 0.12)

            Rectangle {
              height: parent.height
              width: parent.width * root.goalProgress
              radius: 3
              color: root.accent
              Behavior on width { NumberAnimation { duration: 250 } }
            }
          }

          // ---- History graph.
          Text {
            anchors.left: parent.left
            anchors.leftMargin: Style.space(16)
            text: "Last " + root.gridWeeks + " weeks"
            color: root.foreground
            font.family: root.fontFamily
            font.pixelSize: Style.font.bodySmall
            font.bold: true
          }

          Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: 6

            // Weekday labels (Mon/Wed/Fri), aligned with the cell rows.
            Column {
              spacing: root.cellSpacing
              topPadding: root.monthStripHeight + root.cellSpacing

              Repeater {
                model: root.weekdays
                delegate: Text {
                  width: 32
                  height: root.cellSize
                  verticalAlignment: Text.AlignVCenter
                  horizontalAlignment: Text.AlignRight
                  text: modelData
                  color: root.muted
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
              }
            }

            Column {
              spacing: root.cellSpacing

              // Month strip: a label at the first column of each new month.
              Item {
                width: root.gridPixelWidth
                height: root.monthStripHeight

                Repeater {
                  model: root.months
                  delegate: Text {
                    x: modelData.col * root.cellPitch
                    anchors.bottom: parent.bottom
                    text: modelData.label
                    color: root.muted
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                }
              }

              Grid {
                columns: root.gridWeeks
                rows: 7
                spacing: root.cellSpacing
                flow: Grid.LeftToRight

                Repeater {
                  model: root.cells
                  delegate: Rectangle {
                    width: root.cellSize
                    height: root.cellSize
                    radius: 3
                    color: modelData.state === 2 ? root.accent
                      : modelData.state === 1 ? root.alpha(root.accent, 0.3)
                      : modelData.state === 3 ? root.alpha(root.foreground, 0.05)
                      : root.alpha(root.foreground, 0.12)
                    border.color: modelData.today ? root.accent : "transparent"
                    border.width: modelData.today ? 1 : 0

                    MouseArea {
                      anchors.fill: parent
                      hoverEnabled: true
                      onEntered: root.hovered = modelData
                      onExited: if (root.hovered === modelData) root.hovered = null
                    }
                  }
                }
              }
            }
          }

          Text {
            anchors.left: parent.left
            anchors.leftMargin: Style.space(16)
            // Fixed two-line height so the metrics line on hover does not
            // resize the popup.
            height: Math.ceil(Style.font.caption * 2.5)
            verticalAlignment: Text.AlignTop
            lineHeight: 1.25
            text: root.hovered ? Model.dayLabel(root.hovered, root.goalSteps, root.imperial)
              : root.goalSteps > 0 ? "Bright cells are days the goal was met"
              : "Set a daily goal below to light up goal days"
            color: root.muted
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
          }

          // ---- Pad picker.
          Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            height: 1
            color: root.alpha(root.foreground, 0.12)
          }

          Column {
            id: padSection
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: Style.space(16)
            anchors.rightMargin: Style.space(16)
            spacing: Style.space(4)

            Rectangle {
              width: padSection.width
              height: 30
              radius: 6
              color: padHeaderMouse.containsMouse ? root.alpha(root.foreground, 0.10) : "transparent"

              Text {
                anchors.left: parent.left
                anchors.leftMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                text: "󰜎 " + (root.padDisplayName() || "Walking pad")
                color: root.connected ? root.accent : root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }
              Text {
                anchors.right: padChevron.left
                anchors.rightMargin: 10
                anchors.verticalCenter: parent.verticalCenter
                text: root.padStateText()
                color: root.muted
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
              Text {
                id: padChevron
                anchors.right: parent.right
                anchors.rightMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                text: root.padPickerOpen ? "▴" : "▾"
                color: root.muted
                font.pixelSize: Style.font.caption
              }
              MouseArea {
                id: padHeaderMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.padPickerOpen = !root.padPickerOpen
              }
            }

            Column {
              id: padList
              visible: root.padPickerOpen
              width: padSection.width
              spacing: 3

              Rectangle {
                width: padList.width
                height: 26
                radius: 6
                color: autoMouse.containsMouse ? root.alpha(root.accent, 0.18) : root.alpha(root.foreground, 0.05)

                Text {
                  anchors.left: parent.left
                  anchors.leftMargin: 8
                  anchors.verticalCenter: parent.verticalCenter
                  text: "Auto (strongest signal)"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
                Text {
                  anchors.right: parent.right
                  anchors.rightMargin: 8
                  anchors.verticalCenter: parent.verticalCenter
                  text: root.stats && String(root.stats.selected_address || "") === "" ? "✓" : ""
                  color: root.accent
                  font.pixelSize: Style.font.bodySmall
                }
                MouseArea {
                  id: autoMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.selectPad("auto")
                }
              }

              Repeater {
                model: root.stats ? root.stats.devices : []
                delegate: Rectangle {
                  width: padList.width
                  height: 26
                  radius: 6
                  property bool selected: String(modelData.address) === String(root.stats.selected_address || "")
                  color: devMouse.containsMouse ? root.alpha(root.accent, 0.18) : root.alpha(root.foreground, 0.05)

                  Text {
                    anchors.left: parent.left
                    anchors.leftMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    text: (modelData.name || modelData.address) + (modelData.protocol ? " · " + modelData.protocol : "")
                    color: root.foreground
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                  Text {
                    anchors.right: devCheck.left
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    text: (modelData.rssi || "") + " dBm · " + Model.fmtAge(modelData.last_seen)
                    color: root.muted
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }
                  Text {
                    id: devCheck
                    anchors.right: parent.right
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    text: selected ? "✓" : ""
                    color: root.accent
                    font.pixelSize: Style.font.bodySmall
                  }
                  MouseArea {
                    id: devMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.selectPad(modelData.address)
                  }
                }
              }
            }
          }

          // ---- Goal editor and collector switch.
          Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            height: 1
            color: root.alpha(root.foreground, 0.12)
          }

          Item {
            width: parent.width
            height: footerRow.height

            Row {
              id: footerRow
              anchors.left: parent.left
              anchors.leftMargin: Style.space(16)
              anchors.right: parent.right
              anchors.rightMargin: Style.space(16)
              spacing: Style.space(10)

              Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "Daily goal"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }

              TextField {
                id: goalField
                width: 90
                anchors.verticalCenter: parent.verticalCenter
                placeholderText: root.goalSteps > 0 ? String(root.goalSteps) : "6000"
                text: ""
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                inputMethodHints: Qt.ImhDigitsOnly
                validator: IntValidator { bottom: 0; top: 100000 }
                onAccepted: {
                  root.saveGoal(text)
                  text = ""
                  focus = false
                }
                background: Rectangle {
                  radius: 6
                  color: root.alpha(root.foreground, goalField.activeFocus ? 0.16 : 0.08)
                  border.color: goalField.activeFocus ? root.accent : "transparent"
                  border.width: 1
                }
              }

              Item { width: 1; height: 1 }

              Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: toggleLabel.implicitWidth + 20
                height: 28
                radius: 6
                color: toggleMouse.containsMouse ? root.alpha(root.accent, 0.25) : root.alpha(root.accent, 0.12)

                Text {
                  id: toggleLabel
                  anchors.centerIn: parent
                  text: root.enabled ? "Disable collector" : "Enable collector"
                  color: root.accent
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }

                MouseArea {
                  id: toggleMouse
                  anchors.fill: parent
                  hoverEnabled: true
                  cursorShape: Qt.PointingHandCursor
                  onClicked: root.toggleEnabled()
                }
              }
            }
          }
        }
      }
    }
  }
}

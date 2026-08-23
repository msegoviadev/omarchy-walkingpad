import QtQuick
import qs.Commons
import qs.Ui

// Bar slot for the WalkingPad widget. The popup lives in Panel.qml, loaded
// alongside so the bar slot stays a thin shell-routing surface, matching the
// stock weather widget's split.
BarWidget {
  id: root
  moduleName: "msegoviadev.walkingpad"

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  function refresh() {
    if (panelLoader.item && panelLoader.item.refresh) panelLoader.item.refresh()
  }

  function togglePanel() {
    if (panelLoader.item && panelLoader.item.toggle) panelLoader.item.toggle()
  }

  function toggleEnabled() {
    if (panelLoader.item && panelLoader.item.toggleEnabled) panelLoader.item.toggleEnabled()
  }

  // Shape contract for shell summon/hide/toggle routing (open/close/opened
  // must live on the bar-widget root).
  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function open() {
    if (panelLoader.item && panelLoader.item.open) panelLoader.item.open()
  }

  function close() {
    if (panelLoader.item && panelLoader.item.close) panelLoader.item.close()
  }

  function closeForPopoutSwitch() {
    if (panelLoader.item && panelLoader.item.closeForPopoutSwitch) panelLoader.item.closeForPopoutSwitch()
  }

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: panelLoader.item ? panelLoader.item.barText : "󰜎"
    fontSize: Style.font.bodySmall
    horizontalMargin: 8.5
    dimmed: panelLoader.item ? !panelLoader.item.enabled : false
    active: panelLoader.item ? panelLoader.item.goalMet : false
    activeColor: Color.accent
    tooltipText: panelLoader.item ? panelLoader.item.tooltipText : "WalkingPad"

    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) root.toggleEnabled()
      else if (buttonCode === Qt.MiddleButton) root.refresh()
      else root.togglePanel()
    }
  }
}

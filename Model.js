// Pure helpers for the WalkingPad widget. No QML dependencies beyond Qt.

var KM_PER_MI = 1.609344
var FT_PER_M = 3.28084

// Explicit setting wins; "Auto" falls back to the system locale, matching
// the stock weather plugin (US, Liberia, Myanmar use imperial).
function useImperial(units, localeName) {
  var u = String(units || "").toLowerCase()
  if (u === "imperial") return true
  if (u === "metric") return false
  var name = String(localeName || "").replace(".", "_")
  return /^en[_-]US($|[_.-])/.test(name) || /^en[_-]LR($|[_.-])/.test(name) || /^my($|[_.-])/.test(name)
}

function fmt(n) {
  n = Math.floor(Number(n) || 0)
  var s = String(n)
  var out = ""
  while (s.length > 3) {
    out = "," + s.slice(-3) + out
    s = s.slice(0, -3)
  }
  return s + out
}

function fmtDist(m, imperial) {
  m = Number(m) || 0
  if (imperial) {
    var mi = m / 1609.344
    return mi >= 0.1 ? mi.toFixed(1) + " mi" : Math.round(m * FT_PER_M) + " ft"
  }
  return m >= 1000 ? (m / 1000).toFixed(1) + " km" : Math.round(m) + " m"
}

function fmtTime(s) {
  s = Math.floor(Number(s) || 0)
  var h = Math.floor(s / 3600)
  var m = Math.round((s % 3600) / 60)
  return h > 0 ? h + "h " + m + "m" : m + " min"
}

function fmtAge(ts) {
  var s = Math.max(0, Math.floor(Date.now() / 1000 - Number(ts)))
  if (s < 60) return s + "s ago"
  var m = Math.floor(s / 60)
  if (m < 60) return m + "m ago"
  var h = Math.floor(m / 60)
  if (h < 24) return h + "h ago"
  return Math.floor(h / 24) + "d ago"
}

function fmtSpeed(kmh, imperial) {
  kmh = Number(kmh) || 0
  if (imperial) return (kmh / KM_PER_MI).toFixed(1) + " mph"
  return kmh.toFixed(1) + " km/h"
}

function pad2(n) {
  return (n < 10 ? "0" : "") + n
}

function isoOf(d) {
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate())
}

function parseIso(iso) {
  var p = String(iso).split("-")
  return new Date(Number(p[0]), Number(p[1]) - 1, Number(p[2]))
}

function todayIso() {
  return isoOf(new Date())
}

// Cell states: 0 = no steps, 1 = below goal (or walked, no goal set),
// 2 = goal met, 3 = future day.
// Returned row-major: index = weekday row (0=Mon) * weeks + week column,
// matching a Grid with LeftToRight flow and `columns: weeks`.
function buildCells(startIso, days, goal, weeks) {
  var start = parseIso(startIso)
  var today = todayIso()
  var cells = []
  for (var row = 0; row < 7; row++) {
    for (var col = 0; col < weeks; col++) {
      var d = new Date(start.getTime())
      d.setDate(d.getDate() + col * 7 + row)
      var iso = isoOf(d)
      var steps = days[iso] ? Number(days[iso].steps) || 0 : 0
      var state
      if (iso > today) state = 3
      else if (steps <= 0) state = 0
      else if (goal > 0 && steps >= goal) state = 2
      else state = 1
      cells.push({
        date: iso,
        steps: steps,
        state: state,
        today: iso === today,
        stats: days[iso] || null
      })
    }
  }
  return cells
}

// Month labels above the grid: one entry per week column where the month
// differs from the previous column's, GitHub style.
function monthLabels(startIso, weeks) {
  var start = parseIso(startIso)
  var labels = []
  var prev = -1
  for (var col = 0; col < weeks; col++) {
    var d = new Date(start.getTime())
    d.setDate(d.getDate() + col * 7)
    if (d.getMonth() !== prev) {
      labels.push({ col: col, label: Qt.formatDateTime(d, "MMM") })
      prev = d.getMonth()
    }
  }
  return labels
}

// Weekday names for the grid rows, localized. GitHub labels Mon/Wed/Fri
// only, so other rows come back as "". Names come from formatting known
// dates (Jan 1 2024 was a Monday), which avoids Locale.dayName index
// convention surprises.
function weekdayLabels() {
  var labels = []
  for (var row = 0; row < 7; row++) {
    labels.push(row < 6 && row % 2 === 0 ? Qt.formatDateTime(new Date(2024, 0, 1 + row), "ddd") : "")
  }
  return labels
}

// Hover readout under the history graph. Line 1 is date and steps (plus the
// goal when set); line 2 lists whatever metrics the day carries, so days
// recovered from pad "final" summaries (no speed data) render cleanly.
function dayLabel(cell, goal, imperial) {
  var line = Qt.formatDateTime(parseIso(cell.date), "ddd, MMM d") + ": " + fmt(cell.steps) + " steps"
  if (goal > 0) line += " / " + fmt(goal)
  var s = cell.stats
  if (!s || cell.steps <= 0) return line
  var parts = []
  if (Number(s.dist_m) > 0) parts.push(fmtDist(s.dist_m, imperial))
  if (Number(s.time_s) > 0) parts.push(fmtTime(s.time_s))
  if (s.avg_speed != null) parts.push("avg " + fmtSpeed(s.avg_speed, imperial))
  if (s.max_speed != null) parts.push("max " + fmtSpeed(s.max_speed, imperial))
  var sessions = Number(s.sessions) || 0
  if (sessions > 0) parts.push(sessions === 1 ? "1 session" : sessions + " sessions")
  return parts.length > 0 ? line + "\n" + parts.join(" · ") : line
}

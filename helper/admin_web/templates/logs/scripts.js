var currentLevel = "{{level}}";
var EMPTY_TEXT = {{ logs_empty_text_json | safe }};
var BUFFER_STATS_TEMPLATE = {{ logs_buffer_stats_template_json | safe }};
var LAST_UPDATED_TEMPLATE = {{ logs_last_updated_template_json | safe }};
var REFRESH_FAILED_TEMPLATE = {{ logs_refresh_failed_template_json | safe }};
function applyFilter() {
  currentLevel = document.getElementById("level-filter").value;
  fetchLogs();
}
function formatTemplate(template, values) {
  var output = template;
  Object.keys(values).forEach(function(key) {
    output = output.replace("{" + key + "}", values[key]);
  });
  return output;
}
function escapeHtml(text) {
  var d = document.createElement("div");
  d.textContent = text;
  return d.innerHTML;
}
function padNumber(value) {
  return value < 10 ? "0" + value : String(value);
}
function formatTimestampMs(timestampMs, fallback) {
  var numeric = Number(timestampMs);
  if (!isFinite(numeric) || numeric <= 0) return fallback || "-";
  var date = new Date(numeric);
  if (isNaN(date.getTime())) return fallback || "-";
  return date.getFullYear()
    + "-" + padNumber(date.getMonth() + 1)
    + "-" + padNumber(date.getDate())
    + " " + padNumber(date.getHours())
    + ":" + padNumber(date.getMinutes())
    + ":" + padNumber(date.getSeconds());
}
function renderTimestampCell(entry) {
  var fallback = entry.timestamp || "-";
  var timestampMs = entry.timestamp_ms;
  var attr = "";
  if (timestampMs !== undefined && timestampMs !== null && timestampMs !== "") {
    attr = ' data-timestamp-ms="' + escapeHtml(String(timestampMs)) + '"';
  }
  return '<code class="pp-log-timestamp"' + attr + '>'
    + escapeHtml(formatTimestampMs(timestampMs, fallback))
    + '</code>';
}
function applyTimestampFormatting(root) {
  var scope = root || document;
  var nodes = scope.querySelectorAll(".pp-log-timestamp[data-timestamp-ms]");
  Array.prototype.forEach.call(nodes, function(node) {
    node.textContent = formatTimestampMs(
      node.getAttribute("data-timestamp-ms"),
      node.textContent || "-"
    );
  });
}
function levelBadgeClass(level) {
  if (level === "ERROR" || level === "CRITICAL") return "pp-log-error";
  if (level === "WARNING") return "pp-log-warning";
  if (level === "DEBUG") return "pp-log-debug";
  return "pp-log-info";
}
function fetchLogs() {
  var url = "/dashboard/logs/api?level=" + encodeURIComponent(currentLevel);
  fetch(url)
    .then(function(resp) { return resp.json(); })
    .then(function(data) {
      var tbody = document.getElementById("log-table-body");
      var stats = document.getElementById("buffer-stats");
      var status = document.getElementById("status-line");
      if (!tbody) return;
      if (!data.entries || data.entries.length === 0) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--pp-text-secondary);padding:2rem">'
          + escapeHtml(EMPTY_TEXT)
          + '</td></tr>';
      } else {
        tbody.innerHTML = data.entries.map(function(entry) {
          return '<tr>'
            + '<td>' + renderTimestampCell(entry) + '</td>'
            + '<td><span class="pp-badge ' + levelBadgeClass(entry.level || '') + '">' + escapeHtml(entry.level || '-') + '</span></td>'
            + '<td><code>' + escapeHtml(entry.client_ip || '-') + '</code></td>'
            + '<td>' + escapeHtml(entry.username || '-') + '</td>'
            + '<td>' + escapeHtml(entry.method || '-') + '</td>'
            + '<td><code style="word-break:break-all">' + escapeHtml(entry.target || '-') + '</code></td>'
            + '<td><code style="word-break:break-all">' + escapeHtml(entry.upstream || '-') + '</code></td>'
            + '<td>' + escapeHtml(entry.result || '-') + '</td>'
            + '</tr>';
        }).join('');
      }
      if (stats) {
        stats.textContent = formatTemplate(BUFFER_STATS_TEMPLATE, {
          count: data.count,
          max: data.max_buffer
        });
      }
      if (status) {
        status.textContent = formatTemplate(LAST_UPDATED_TEMPLATE, {
          time: new Date().toLocaleTimeString()
        });
      }
    })
    .catch(function() {
      var status = document.getElementById("status-line");
      if (status) {
        status.textContent = formatTemplate(REFRESH_FAILED_TEMPLATE, {
          time: new Date().toLocaleTimeString()
        });
      }
    });
}
applyTimestampFormatting(document);
setInterval(fetchLogs, 5000);

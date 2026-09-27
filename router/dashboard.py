"""Web dashboard for spend tracking and quota monitoring.

Serves a local-only web page that reads the sqlite ledger directly.
No external dependencies - uses stdlib only.
"""
from __future__ import annotations

import json
import sqlite3
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .core.ledger import _conn


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler serving the dashboard page and its JSON data endpoints."""

    server_version = "CodingRouterDashboard/0.1"

    def do_GET(self) -> None:
        """Route GET requests to the dashboard page or a /api/* endpoint."""
        path = urlparse(self.path).path
        try:
            if path in ("/", "/dashboard"):
                self._serve_dashboard()
            elif path == "/api/data":
                self._serve_api_data()
            elif path == "/api/quota":
                self._serve_quota_data()
            elif path == "/api/spend":
                self._serve_spend_data()
            elif path == "/favicon.ico":
                self.send_error(204)
            else:
                self._send_json({"error": "not found"}, status=404)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.log_error("dashboard request failed: %s", exc)
            self._send_json({"error": "dashboard data unavailable"}, status=500)

    def _send_headers(self, status: int, content_type: str, content_length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send_headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _serve_dashboard(self):
        """Serve the main dashboard HTML page."""
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Coding Router - Spend Dashboard</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            overflow: hidden;
        }
        .header {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }
        .header h1 {
            font-size: 2.5em;
            margin-bottom: 10px;
        }
        .header p {
            opacity: 0.9;
            font-size: 1.1em;
        }
        .content {
            padding: 30px;
        }
        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .card {
            background: #f8f9fa;
            border-radius: 8px;
            padding: 20px;
            border-left: 4px solid #667eea;
        }
        .card h3 {
            color: #333;
            margin-bottom: 15px;
            font-size: 1.2em;
        }
        .metric {
            font-size: 2em;
            font-weight: bold;
            color: #667eea;
            margin-bottom: 5px;
        }
        .metric-label {
            color: #666;
            font-size: 0.9em;
        }
        .chart {
            margin-top: 30px;
            background: white;
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
        }
        .chart h3 {
            margin-bottom: 20px;
            color: #333;
        }
        .bar-chart {
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .bar-item {
            display: flex;
            align-items: center;
            gap: 15px;
        }
        .bar-label {
            min-width: 120px;
            font-weight: 500;
            color: #555;
        }
        .bar-container {
            flex: 1;
            height: 30px;
            background: #e9ecef;
            border-radius: 4px;
            overflow: hidden;
            position: relative;
        }
        .bar-fill {
            height: 100%;
            background: linear-gradient(90deg, #667eea, #764ba2);
            border-radius: 4px;
            transition: width 0.3s ease;
        }
        .bar-value {
            position: absolute;
            inset: 0;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #333;
            font-size: 0.85em;
            font-weight: 600;
        }
        .quota-bar {
            height: 8px;
            background: #e9ecef;
            border-radius: 4px;
            overflow: hidden;
            margin-top: 10px;
        }
        .quota-fill {
            height: 100%;
            background: linear-gradient(90deg, #28a745, #ffc107);
            transition: width 0.3s ease;
        }
        .status-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 12px;
            font-size: 0.8em;
            font-weight: 500;
        }
        .status-ok { background: #d4edda; color: #155724; }
        .status-depleted { background: #f8d7da; color: #721c24; }
        .status-unknown { background: #d1ecf1; color: #0c5460; }
        .adapter-meta { color: #666; margin: 4px 0 8px 135px; display: block; }
        .loading {
            text-align: center;
            padding: 40px;
            color: #666;
        }
        .error {
            background: #f8d7da;
            color: #721c24;
            padding: 15px;
            border-radius: 4px;
            margin: 20px 0;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
        }
        th, td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #dee2e6;
        }
        th {
            background: #f8f9fa;
            font-weight: 600;
            color: #495057;
        }
        tr:hover {
            background: #f8f9fa;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Coding Router Dashboard</h1>
            <p>Spend tracking and quota monitoring across AI coding adapters</p>
        </div>

        <div class="content">
            <div class="grid">
                <div class="card">
                    <h3>Total Spend (30d)</h3>
                    <div class="metric" id="total-spend">$0.00</div>
                    <div class="metric-label">Actual cost across all adapters</div>
                </div>

                <div class="card">
                    <h3>Active Adapters</h3>
                    <div class="metric" id="active-adapters">0</div>
                    <div class="metric-label">Currently healthy and reachable</div>
                </div>

                <div class="card">
                    <h3>Runs (30d)</h3>
                    <div class="metric" id="total-runs">0</div>
                    <div class="metric-label">Total task executions</div>
                </div>

                <div class="card">
                    <h3>Estimated Savings</h3>
                    <div class="metric" id="avoided-cost">$0.00</div>
                    <div class="metric-label">Positive difference between estimated and actual cost</div>
                </div>
            </div>

            <div class="chart">
                <h3>Adapter Status</h3>
                <div id="adapter-status" class="loading">Loading adapter status...</div>
            </div>

            <div class="chart">
                <h3>Spend by Adapter (30d)</h3>
                <div id="spend-chart" class="loading">Loading spend data...</div>
            </div>

            <div class="chart">
                <h3>Recent Runs</h3>
                <div id="recent-runs" class="loading">Loading recent runs...</div>
            </div>
        </div>
    </div>

    <script>
        function escapeHtml(value) {
            return String(value).replace(/[&<>'"]/g, char => ({
                '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
            })[char]);
        }

        async function fetchJson(path) {
            const response = await fetch(path, {cache: 'no-store'});
            if (!response.ok) throw new Error(`${path} returned ${response.status}`);
            return response.json();
        }

        async function loadDashboard() {
            try {
                // Load main data
                const [data, quotaData, spendData] = await Promise.all([
                    fetchJson('/api/data'), fetchJson('/api/quota'), fetchJson('/api/spend')
                ]);

                // Update metrics
                document.getElementById('total-spend').textContent =
                    `$${data.totalSpend.toFixed(4)}`;
                document.getElementById('active-adapters').textContent =
                    data.activeAdapters;
                document.getElementById('total-runs').textContent =
                    data.totalRuns;
                document.getElementById('avoided-cost').textContent =
                    `$${data.avoidedCost.toFixed(4)}`;

                // Render adapter status
                renderAdapterStatus(quotaData.adapters);

                // Render spend chart
                renderSpendChart(spendData.spend);

                // Render recent runs
                renderRecentRuns(spendData.recentRuns);

            } catch (error) {
                console.error('Dashboard error:', error);
                document.querySelectorAll('.loading').forEach(el => {
                    el.innerHTML = '<div class="error">Failed to load data</div>';
                });
            }
        }

        function renderAdapterStatus(adapters) {
            const container = document.getElementById('adapter-status');
            if (!adapters.length) {
                container.innerHTML = '<div class="error">No adapter data available</div>';
                return;
            }

            let html = '<div class="bar-chart">';
            adapters.forEach(adapter => {
                const statusClass = adapter.state === 'ok' ? 'status-ok' :
                                  adapter.state === 'depleted' ? 'status-depleted' : 'status-unknown';
                const reported = Number.isFinite(adapter.remainingPercent) ?
                    Math.min(Math.max(adapter.remainingPercent, 0), 100) : null;
                const quotaText = reported === null ? 'Not reported' : `${reported}% left`;
                const observed = adapter.observedAt ?
                    `Vendor check: ${new Date(adapter.observedAt).toLocaleString()}` :
                    'Vendor check: never';

                html += `
                    <div class="bar-item">
                        <div class="bar-label">${escapeHtml(adapter.name)}</div>
                        <div class="bar-container" title="Vendor-reported quota only">
                            <div class="bar-fill" style="width: ${reported || 0}%"></div>
                            <span class="bar-value">${escapeHtml(quotaText)}</span>
                        </div>
                        <span class="status-badge ${statusClass}" title="${escapeHtml(adapter.detail || '')}">${escapeHtml(adapter.state)}</span>
                    </div>
                    <small class="adapter-meta">${escapeHtml(observed)}${adapter.resetTime ? ` · Resets: ${escapeHtml(adapter.resetTime)}` : ''}</small>
                `;
            });
            html += '</div>';
            container.innerHTML = html;
        }

        function renderSpendChart(spend) {
            const container = document.getElementById('spend-chart');
            if (!spend.length) {
                container.innerHTML = '<div class="error">No spend data available</div>';
                return;
            }

            const maxSpend = Math.max(...spend.map(s => s.total));
            let html = '<div class="bar-chart">';
            spend.forEach(item => {
                const percent = maxSpend > 0 ? (item.total / maxSpend) * 100 : 0;
                html += `
                    <div class="bar-item">
                        <div class="bar-label">${escapeHtml(item.adapter)}</div>
                        <div class="bar-container">
                            <div class="bar-fill" style="width: ${percent}%">
                                $${item.total.toFixed(4)}
                            </div>
                        </div>
                    </div>
                `;
            });
            html += '</div>';
            container.innerHTML = html;
        }

        function renderRecentRuns(runs) {
            const container = document.getElementById('recent-runs');
            if (!runs.length) {
                container.innerHTML = '<div class="error">No recent runs</div>';
                return;
            }

            let html = '<table>';
            html += '<tr><th>Task</th><th>Adapter</th><th>Model</th><th>Cost</th><th>Time</th></tr>';
            runs.forEach(run => {
                html += `
                    <tr>
                        <td>${escapeHtml(run.task.substring(0, 50))}${run.task.length > 50 ? '...' : ''}</td>
                        <td>${escapeHtml(run.adapter)}</td>
                        <td>${escapeHtml(run.model || 'default')}</td>
                        <td>$${run.cost.toFixed(4)}</td>
                        <td>${new Date(run.ts * 1000).toLocaleString()}</td>
                    </tr>
                `;
            });
            html += '</table>';
            container.innerHTML = html;
        }

        // Load dashboard on page load
        loadDashboard();

        // Refresh every 30 seconds
        setInterval(loadDashboard, 30000);
    </script>
</body>
</html>"""

        body = html.encode("utf-8")
        self._send_headers(200, "text/html; charset=utf-8", len(body))
        self.wfile.write(body)

    def _serve_api_data(self):
        """Serve aggregated dashboard data."""
        with closing(_conn()) as c:
            # Total spend (30d)
            total_spend = c.execute(
                "SELECT COALESCE(SUM(actual_cost_usd), 0) FROM runs"
                " WHERE dry_run=0 AND ts > strftime('%s','now','-30 days')"
            ).fetchone()[0] or 0.0

            # Active adapters (reachable in last health check)
            active_adapters = c.execute(
                """SELECT COUNT(*) FROM quota_observations AS q
                   WHERE q.id IN (
                       SELECT MAX(id) FROM quota_observations GROUP BY adapter
                   ) AND q.state='ok'
                   AND q.observed_at > datetime('now', '-1 hour')"""
            ).fetchone()[0] or 0

            # Total runs (30d)
            total_runs = c.execute(
                "SELECT COUNT(*) FROM runs"
                " WHERE dry_run=0 AND ts > strftime('%s','now','-30 days')"
            ).fetchone()[0] or 0

            # Estimated savings from positive estimate-to-actual differences
            # Negative estimate variance is not reported as savings
            avoided_cost = c.execute(
                """SELECT COALESCE(SUM(
                    CASE
                        WHEN est_cost_usd > COALESCE(actual_cost_usd, 0) THEN
                            est_cost_usd - COALESCE(actual_cost_usd, 0)
                        ELSE 0
                    END), 0)
                   FROM runs
                   WHERE dry_run=0 AND ts > strftime('%s','now','-30 days')"""
            ).fetchone()[0] or 0.0

        data = {
            'totalSpend': total_spend,
            'activeAdapters': active_adapters,
            'totalRuns': total_runs,
            'avoidedCost': avoided_cost
        }

        self._send_json(data)

    def _serve_quota_data(self):
        """Serve quota status data."""
        with closing(_conn()) as c:
            observations = c.execute(
                """SELECT adapter, state, detail, reset_at, observed_at,
                          remaining_percent
                   FROM quota_observations
                   WHERE id IN (
                       SELECT MAX(id) FROM quota_observations
                       GROUP BY adapter
                   )
                   ORDER BY adapter"""
            ).fetchall()

        data = {'adapters': [
            {
                'name': adapter,
                'state': state,
                'detail': detail,
                'resetTime': reset_at,
                'observedAt': observed_at,
                'remainingPercent': remaining_percent
            }
            for adapter, state, detail, reset_at, observed_at, remaining_percent in observations
        ]}

        self._send_json(data)

    def _serve_spend_data(self):
        """Serve spend summary data."""
        with closing(_conn()) as c:
            # Spend by adapter (30d)
            spend = c.execute(
                """SELECT adapter, SUM(actual_cost_usd) as total
                   FROM runs
                   WHERE dry_run=0 AND ts > strftime('%s','now','-30 days')
                   GROUP BY adapter
                   ORDER BY total DESC"""
            ).fetchall()

            # Recent runs (last 10)
            recent_runs = c.execute(
                """SELECT task, adapter, model, actual_cost_usd as cost, ts
                   FROM runs
                   WHERE dry_run=0
                   ORDER BY ts DESC
                   LIMIT 10"""
            ).fetchall()

        spend_data = [{'adapter': a, 'total': t or 0.0} for a, t in spend]
        runs_data = [{'task': t, 'adapter': a, 'model': m, 'cost': c or 0.0, 'ts': ts}
                     for t, a, m, c, ts in recent_runs]

        data = {
            'spend': spend_data,
            'recentRuns': runs_data
        }

        self._send_json(data)


def create_dashboard_server(port: int = 8080) -> ThreadingHTTPServer:
    """Bind a localhost-only dashboard server; also initializes the ledger."""
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    with closing(_conn()):
        pass
    return ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)


def start_dashboard(port: int = 8080, open_browser: bool = True) -> None:
    """Start the dashboard server on localhost."""
    server = create_dashboard_server(port)
    actual_port = server.server_address[1]
    url = f"http://127.0.0.1:{actual_port}"
    print(f"Dashboard starting on {url}")
    print("Press Ctrl+C to stop")

    # Open browser automatically
    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
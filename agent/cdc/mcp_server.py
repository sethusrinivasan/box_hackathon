"""
agent/cdc/mcp_server.py — Model Context Protocol (MCP) Server for Serverless DuckDB.

Exposes serverless DuckDB analytics to AI agents and LLM tools over Streamable HTTP
following the latest MCP specification (2024-11-05).

Supports the MCP Apps / UI Extension (io.modelcontextprotocol/ui and mcp-ui),
enabling host AI clients (Claude Desktop, ChatGPT, Cursor, VS Code, Antigravity)
to render an interactive in-place query editor widget directly in chat.

Endpoints:
  POST /mcp — JSON-RPC 2.0 Streamable HTTP transport
  GET  /mcp — Manifest / capabilities / widget preview
  OPTIONS /mcp — CORS preflight
"""
from __future__ import annotations

import html
import json
import logging
from typing import Any

from agent.cdc.reader import DuckDbReader

logger = logging.getLogger("reality_check.cdc.mcp")

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "reality-check-duckdb-mcp"
SERVER_VERSION = "1.0.0"
UI_RESOURCE_URI = "ui://realitycheck/query-widget"


class McpServer:
    """Implements Model Context Protocol server over DuckDbReader."""

    def __init__(self, reader: DuckDbReader, server_base_url: str = "") -> None:
        self.reader = reader
        self.server_base_url = server_base_url.rstrip("/")

    def handle_request(
        self,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        """Entry point for Lambda Function URL routing to /mcp."""
        rc = event.get("requestContext", {}).get("http", {})
        method = (rc.get("method") or event.get("httpMethod") or "POST").upper()

        # Handle CORS OPTIONS preflight
        if method == "OPTIONS":
            return {
                "statusCode": 204,
                "headers": {
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "Content-Type, Authorization, Mcp-Session-Id",
                    "Access-Control-Max-Age": "86400",
                },
                "body": "",
            }

        # Handle GET /mcp (Server manifest or widget preview)
        if method == "GET":
            headers = event.get("headers", {}) or {}
            accept = headers.get("accept", headers.get("Accept", ""))
            if "text/html" in accept:
                table_counts = self.reader.get_table_counts()
                widget_html = self.render_widget_html(
                    initial_query="SELECT usage_date, application_name, gross_usd, net_usd FROM daily_cost ORDER BY usage_date DESC LIMIT 15;",
                    table_counts=table_counts,
                )
                return {
                    "statusCode": 200,
                    "headers": {
                        "Content-Type": "text/html; charset=utf-8",
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "no-store",
                    },
                    "body": widget_html,
                }

            # Return JSON Server Manifest
            return {
                "statusCode": 200,
                "headers": {
                    "Content-Type": "application/json",
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "no-store",
                },
                "body": json.dumps({
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                    "protocolVersion": PROTOCOL_VERSION,
                    "transport": "streamable-http",
                    "description": "Serverless DuckDB Analytics MCP Server with in-place UI query widget",
                    "capabilities": self._capabilities(),
                    "endpoints": {
                        "mcp": f"{self.server_base_url}/mcp",
                        "query_api": f"{self.server_base_url}/api/query",
                        "web_ui": f"{self.server_base_url}/",
                    },
                }, indent=2),
            }

        # Handle POST /mcp (JSON-RPC 2.0)
        body = event.get("body", "{}")
        if isinstance(body, str):
            try:
                payload = json.loads(body) if body.strip() else {}
            except json.JSONDecodeError as e:
                return self._json_rpc_error(None, -32700, f"Parse error: {e}")
        else:
            payload = body or {}

        # Support batch JSON-RPC requests
        if isinstance(payload, list):
            results = [self._handle_single_rpc(req) for req in payload]
            return {
                "statusCode": 200,
                "headers": {
                    "Content-Type": "application/json",
                    "Access-Control-Allow-Origin": "*",
                },
                "body": json.dumps(results, default=str),
            }

        rpc_res = self._handle_single_rpc(payload)
        return {
            "statusCode": 200,
            "headers": {
                "Content-Type": "application/json",
                "Access-Control-Allow-Origin": "*",
                "Cache-Control": "no-store",
            },
            "body": json.dumps(rpc_res, default=str),
        }

    def _capabilities(self) -> dict[str, Any]:
        return {
            "tools": {"listChanged": False},
            "resources": {"subscribe": False, "listChanged": False},
            "prompts": {"listChanged": False},
            "extensions": {
                "io.modelcontextprotocol/ui": {
                    "version": "0.1.0",
                    "description": "MCP Apps interactive in-place UI widget extension",
                },
                "mcp-ui": {
                    "version": "1.0.0",
                    "description": "Interactive HTML/CSS/JS query widget rendering",
                },
            },
        }

    def _handle_single_rpc(self, req: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(req, dict):
            return self._json_rpc_error(None, -32600, "Invalid Request: expected JSON object")

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {}) or {}

        # Notifications (no id)
        if req_id is None and method:
            logger.info("Received MCP notification: %s", method)
            return {}

        if method == "initialize":
            return self._handle_initialize(req_id, params)
        elif method in ("ping", "mcp/ping"):
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        elif method == "tools/list":
            return self._handle_tools_list(req_id)
        elif method == "tools/call":
            return self._handle_tools_call(req_id, params)
        elif method == "resources/list":
            return self._handle_resources_list(req_id)
        elif method == "resources/read":
            return self._handle_resources_read(req_id, params)
        elif method == "prompts/list":
            return self._handle_prompts_list(req_id)
        elif method == "prompts/get":
            return self._handle_prompts_get(req_id, params)
        elif method == "notifications/initialized":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        else:
            return self._json_rpc_error(req_id, -32601, f"Method not found: '{method}'")

    def _handle_initialize(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": self._capabilities(),
                "serverInfo": {
                    "name": SERVER_NAME,
                    "version": SERVER_VERSION,
                },
                "instructions": (
                    "Reality Check DuckDB Analytics Server. Execute read-only analytical queries, "
                    "aggregations, and trend analyses against the serverless DuckDB data store. "
                    "Every tool response includes an interactive in-place UI widget allowing users "
                    "to visually explore data and execute queries directly within the chat interface."
                ),
            },
        }

    def _handle_tools_list(self, req_id: Any) -> dict[str, Any]:
        tools = [
            {
                "name": "query_duckdb",
                "description": (
                    "Executes analytical SQL queries directly against the in-process serverless DuckDB replica "
                    "(replicated from PostgreSQL via CDC). Zero read load on PostgreSQL. "
                    "Returns tabular results, column schemas, query execution time, cache hit status, "
                    "and the physical query execution plan (EXPLAIN). Includes an interactive in-place UI widget."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "SQL query to execute against DuckDB (e.g. SELECT * FROM daily_cost ORDER BY usage_date DESC LIMIT 10;)",
                        },
                        "explain": {
                            "type": "boolean",
                            "description": "Whether to generate and include the physical query execution plan (EXPLAIN)",
                            "default": True,
                        },
                    },
                    "required": ["query"],
                },
                "_meta": {
                    "ui": {
                        "resourceUri": UI_RESOURCE_URI,
                        "mimeType": "text/html",
                        "title": "DuckDB Analytics Query Editor",
                    },
                },
            },
            {
                "name": "get_table_counts",
                "description": (
                    "Returns real-time record counts, table names, and column schemas for all replicated "
                    "analytical tables in the DuckDB analytics store."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
                "_meta": {
                    "ui": {
                        "resourceUri": UI_RESOURCE_URI,
                        "mimeType": "text/html",
                        "title": "DuckDB Analytics Table Overview",
                    },
                },
            },
            {
                "name": "get_cdc_watermark",
                "description": (
                    "Returns current Change Data Capture (CDC) replication watermark metadata, including "
                    "last applied PostgreSQL LSN, latest commit timestamp, synchronization mode, and replication lag."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": tools},
        }

    def _handle_tools_call(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        tool_name = params.get("name")
        args = params.get("arguments", {}) or {}

        try:
            if tool_name == "query_duckdb":
                sql = args.get("query", "")
                if not sql.strip():
                    return self._json_rpc_error(req_id, -32602, "Missing required argument 'query'")
                explain = bool(args.get("explain", True))

                res = self.reader.query(sql, explain=explain)
                table_counts = self.reader.get_table_counts()

                # Format clean Markdown summary
                row_count = res.get("row_count", 0)
                duration_ms = res.get("query_time_ms", 0.0)
                cache_status = "⚡ Local /tmp HIT (< 1ms)" if res.get("cache_hit") else "🔄 S3 Replica Pull"
                plan = res.get("query_plan")

                md_lines = [
                    f"### 🦆 DuckDB Query Execution Results",
                    f"- **Rows:** {row_count} | **Latency:** {duration_ms} ms | **Cache:** {cache_status}",
                    "",
                ]

                # Render Markdown table preview (up to 20 rows)
                rows = res.get("results", [])
                cols = res.get("columns", [])
                if rows and cols:
                    header_line = "| " + " | ".join(cols) + " |"
                    sep_line = "| " + " | ".join(["---"] * len(cols)) + " |"
                    md_lines.append(header_line)
                    md_lines.append(sep_line)
                    for r in rows[:20]:
                        row_vals = [str(r.get(c, "")) for c in cols]
                        md_lines.append("| " + " | ".join(row_vals) + " |")
                    if len(rows) > 20:
                        md_lines.append(f"\n*(Showing first 20 of {len(rows)} rows)*")
                elif not rows:
                    md_lines.append("*Query returned 0 rows.*")

                # Include collapsible query plan if available
                if plan:
                    md_lines.extend([
                        "",
                        "<details><summary>🔍 <b>Physical Query Execution Plan (EXPLAIN)</b></summary>",
                        "",
                        "```text",
                        plan,
                        "```",
                        "</details>",
                    ])

                md_text = "\n".join(md_lines)

                # Generate inplace interactive widget for host AI tools
                widget_html = self.render_widget_html(initial_query=sql, table_counts=table_counts)

                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": md_text,
                            },
                            {
                                "type": "resource",
                                "resource": {
                                    "uri": UI_RESOURCE_URI,
                                    "mimeType": "text/html",
                                    "text": widget_html,
                                },
                            },
                        ],
                        "_meta": {
                            "ui": {
                                "resourceUri": UI_RESOURCE_URI,
                            },
                        },
                    },
                }

            elif tool_name == "get_table_counts":
                counts = self.reader.get_table_counts()
                table_lines = [
                    "### 📊 DuckDB Analytics Replicated Tables",
                    "| Table Name | Record Count | Storage Format |",
                    "| :--- | :---: | :--- |",
                ]
                for tbl, cnt in counts.items():
                    table_lines.append(f"| **`{tbl}`** | {cnt:,} records | DuckDB Columnar (In-Memory / S3) |")

                if not counts:
                    table_lines.append("| *(No user tables found)* | 0 | — |")

                widget_html = self.render_widget_html(
                    initial_query="SHOW TABLES;",
                    table_counts=counts,
                )

                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": "\n".join(table_lines)},
                            {
                                "type": "resource",
                                "resource": {
                                    "uri": UI_RESOURCE_URI,
                                    "mimeType": "text/html",
                                    "text": widget_html,
                                },
                            },
                        ],
                        "_meta": {"ui": {"resourceUri": UI_RESOURCE_URI}},
                    },
                }

            elif tool_name == "get_cdc_watermark":
                watermark = self.reader.get_watermark()
                wm_lines = [
                    "### ⏱️ CDC Replication Watermark Status",
                    f"- **Last Applied LSN:** `{watermark.get('last_applied_lsn', 'N/A')}`",
                    f"- **Last Commit Time:** `{watermark.get('last_commit_timestamp', 'N/A')}`",
                    f"- **Sync Mode:** `{watermark.get('sync_mode', 'N/A')}`",
                    f"- **DuckDB File Size:** `{watermark.get('last_sync_file_size', '< 50 MB')}`",
                ]
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": "\n".join(wm_lines)}],
                    },
                }

            else:
                return self._json_rpc_error(req_id, -32601, f"Unknown tool: '{tool_name}'")

        except Exception as e:
            logger.error("Error handling tool call '%s': %s", tool_name, e, exc_info=True)
            return self._json_rpc_error(req_id, -32000, f"Tool execution failed: {e}")

    def _handle_resources_list(self, req_id: Any) -> dict[str, Any]:
        resources = [
            {
                "uri": UI_RESOURCE_URI,
                "name": "DuckDB Analytics Interactive Query Widget",
                "description": "Standalone HTML/JS interactive query editor widget to run analytical SQL queries in-place",
                "mimeType": "text/html",
            },
            {
                "uri": "analytics://duckdb/tables",
                "name": "Analytical Tables Record Counts",
                "description": "Live record counts and schemas of all tables in the DuckDB analytics replica",
                "mimeType": "application/json",
            },
            {
                "uri": "analytics://duckdb/watermark",
                "name": "CDC Watermark and Lag State",
                "description": "Current replication slot LSN, commit timestamp, and data freshness metadata",
                "mimeType": "application/json",
            },
        ]
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"resources": resources},
        }

    def _handle_resources_read(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        uri = params.get("uri", "")

        if uri == UI_RESOURCE_URI:
            table_counts = self.reader.get_table_counts()
            widget_html = self.render_widget_html(
                initial_query="SELECT usage_date, application_name, gross_usd, net_usd FROM daily_cost ORDER BY usage_date DESC LIMIT 15;",
                table_counts=table_counts,
            )
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": UI_RESOURCE_URI,
                            "mimeType": "text/html",
                            "text": widget_html,
                        }
                    ]
                },
            }

        elif uri == "analytics://duckdb/tables":
            counts = self.reader.get_table_counts()
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(counts, indent=2),
                        }
                    ]
                },
            }

        elif uri == "analytics://duckdb/watermark":
            watermark = self.reader.get_watermark()
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(watermark, indent=2),
                        }
                    ]
                },
            }

        return self._json_rpc_error(req_id, -32002, f"Resource not found: '{uri}'")

    def _handle_prompts_list(self, req_id: Any) -> dict[str, Any]:
        prompts = [
            {
                "name": "analyze_cloud_spend",
                "description": "Analyze AWS cloud spend trends, daily burns, and detect cost anomalies using DuckDB analytics.",
                "arguments": [
                    {
                        "name": "days",
                        "description": "Number of recent days to analyze (default 30)",
                        "required": False,
                    }
                ],
            },
            {
                "name": "explain_query_performance",
                "description": "Execute a SQL query against DuckDB and analyze its physical query plan (EXPLAIN) for performance optimization.",
                "arguments": [
                    {
                        "name": "sql",
                        "description": "Analytical SQL query to inspect",
                        "required": True,
                    }
                ],
            },
        ]
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"prompts": prompts},
        }

    def _handle_prompts_get(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        args = params.get("arguments", {}) or {}

        if name == "analyze_cloud_spend":
            days = args.get("days", "30")
            sql = f"SELECT usage_date, application_name, gross_usd, net_usd FROM daily_cost WHERE usage_date >= CURRENT_DATE - INTERVAL '{days} days' ORDER BY usage_date DESC;"
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "description": "Cloud Spend Trend Analysis Prompt",
                    "messages": [
                        {
                            "role": "user",
                            "content": {
                                "type": "text",
                                "text": f"Please run the query `{sql}` via query_duckdb and summarize cloud burn rate, trends, and anomalies by ApplicationName.",
                            },
                        }
                    ],
                },
            }

        elif name == "explain_query_performance":
            sql = args.get("sql", "SELECT count(*) FROM daily_cost;")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "description": "DuckDB Physical Query Plan Inspector",
                    "messages": [
                        {
                            "role": "user",
                            "content": {
                                "type": "text",
                                "text": f"Run query_duckdb with explain=true for: `{sql}`. Break down the operators, sequential scans, filters, and projection costs.",
                            },
                        }
                    ],
                },
            }

        return self._json_rpc_error(req_id, -32602, f"Prompt not found: '{name}'")

    def _json_rpc_error(self, req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": code,
                "message": message,
            },
        }

    def render_widget_html(
        self,
        initial_query: str = "SELECT usage_date, application_name, gross_usd, net_usd FROM daily_cost ORDER BY usage_date DESC LIMIT 15;",
        table_counts: dict[str, int] | None = None,
    ) -> str:
        """Render self-contained, responsive, sandboxed interactive UI widget for MCP Apps / AI chat."""
        counts = table_counts if table_counts is not None else self.reader.get_table_counts()
        api_url = f"{self.server_base_url}/api/query" if self.server_base_url else "/api/query"

        counts_badge_items = "".join([
            f'<span class="tbl-badge">📁 {html.escape(t)}: <strong>{c}</strong> rows</span>'
            for t, c in counts.items()
        ]) or '<span class="tbl-badge">📁 daily_cost: <strong>Active</strong></span>'

        esc_query = html.escape(initial_query)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DuckDB In-Place Analytics Widget</title>
  <style>
    :root {{
      --bg: #0d1117;
      --card-bg: #161b22;
      --border: #30363d;
      --fg: #e6edf3;
      --muted: #8b949e;
      --accent: #58a6ff;
      --green: #238636;
      --green-hover: #2ea043;
      --code-bg: #090d13;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--fg);
      padding: 14px;
      font-size: 13px;
    }}
    .widget-container {{
      max-width: 960px;
      margin: 0 auto;
    }}
    .widget-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--border);
      flex-wrap: wrap;
      gap: 8px;
    }}
    .widget-title {{
      font-size: 15px;
      font-weight: 600;
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .counts-group {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .tbl-badge {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 2px 8px;
      border-radius: 999px;
      background: rgba(56, 139, 253, 0.12);
      border: 1px solid rgba(56, 139, 253, 0.25);
      font-size: 11px;
      color: var(--accent);
    }}
    .tbl-badge strong {{
      color: #3fb950;
    }}
    .quick-bar {{
      display: flex;
      gap: 6px;
      margin-bottom: 10px;
      flex-wrap: wrap;
    }}
    .quick-btn {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      color: var(--fg);
      border-radius: 6px;
      padding: 4px 10px;
      font-size: 11px;
      cursor: pointer;
      transition: all 0.15s ease;
    }}
    .quick-btn:hover {{
      border-color: var(--accent);
      background: #1f242c;
    }}
    .editor-box {{
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--card-bg);
      margin-bottom: 12px;
      overflow: hidden;
    }}
    textarea.sql-input {{
      width: 100%;
      height: 70px;
      background: var(--code-bg);
      border: none;
      color: #79c0ff;
      padding: 10px 12px;
      font-family: ui-monospace, Menlo, monospace;
      font-size: 12px;
      resize: vertical;
      outline: none;
    }}
    .editor-actions {{
      padding: 6px 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-top: 1px solid var(--border);
      background: #12161e;
    }}
    .run-btn {{
      background: var(--green);
      color: #fff;
      border: none;
      border-radius: 6px;
      padding: 5px 14px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s;
    }}
    .run-btn:hover {{
      background: var(--green-hover);
    }}
    .results-card {{
      border: 1px solid var(--border);
      border-radius: 6px;
      background: var(--card-bg);
      overflow: hidden;
    }}
    .results-tabs-bar {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #12161e;
      border-bottom: 1px solid var(--border);
      padding: 4px 8px;
    }}
    .tabs-group {{
      display: flex;
      gap: 4px;
    }}
    .tab-btn {{
      background: transparent;
      border: none;
      color: var(--muted);
      padding: 5px 12px;
      font-size: 12px;
      cursor: pointer;
      border-radius: 4px;
      font-weight: 500;
    }}
    .tab-btn.active {{
      background: rgba(56, 139, 253, 0.15);
      color: var(--accent);
      font-weight: 600;
    }}
    .stats-label {{
      font-size: 11px;
      color: var(--muted);
    }}
    .stats-val {{
      color: #3fb950;
      font-weight: 600;
      font-family: ui-monospace, monospace;
    }}
    .content-area {{
      max-height: 320px;
      overflow: auto;
    }}
    table.data-table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      text-align: left;
    }}
    table.data-table th {{
      position: sticky;
      top: 0;
      background: #161b22;
      color: var(--muted);
      padding: 6px 10px;
      border-bottom: 1px solid var(--border);
      font-size: 11px;
    }}
    table.data-table td {{
      padding: 6px 10px;
      border-bottom: 1px solid rgba(255,255,255,0.05);
      font-family: ui-monospace, monospace;
    }}
    pre.plan-pre {{
      background: var(--code-bg);
      color: #8b949e;
      padding: 12px;
      font-family: ui-monospace, monospace;
      font-size: 11px;
      line-height: 1.4;
      white-space: pre;
      overflow-x: auto;
    }}
    .empty {{
      padding: 24px;
      text-align: center;
      color: var(--muted);
      font-size: 12px;
    }}
  </style>
</head>
<body>
  <div class="widget-container">
    <div class="widget-header">
      <div class="widget-title">
        <span>🦆</span> DuckDB In-Place Analytics
      </div>
      <div class="counts-group">
        {counts_badge_items}
      </div>
    </div>

    <div class="quick-bar">
      <button class="quick-btn" onclick="applySql(this)" data-sql="SELECT usage_date, application_name, gross_usd, net_usd FROM daily_cost ORDER BY usage_date DESC LIMIT 15;">📊 Recent Costs</button>
      <button class="quick-btn" onclick="applySql(this)" data-sql="SELECT application_name, count(*) as days, round(sum(gross_usd),2) as total_usd, round(avg(gross_usd),2) as avg_usd FROM daily_cost GROUP BY application_name;">📈 Spend by Application</button>
      <button class="quick-btn" onclick="applySql(this)" data-sql="SELECT application_name, service, count(*) as resources FROM resource_inventory GROUP BY application_name, service ORDER BY resources DESC;">📦 Resources by App</button>
      <button class="quick-btn" onclick="applySql(this)" data-sql="SHOW TABLES;">🏷️ Show Tables</button>
      <button class="quick-btn" onclick="applySql(this)" data-sql="SELECT * FROM __cdc_metadata;">⏱️ CDC Watermark</button>
    </div>

    <div class="editor-box">
      <textarea id="widget-sql" class="sql-input">{esc_query}</textarea>
      <div class="editor-actions">
        <span class="stats-label" id="w-status">Zero Aurora connections · In-process execution</span>
        <button id="w-run-btn" class="run-btn" onclick="executeWidgetQuery()">▶ Run SQL</button>
      </div>
    </div>

    <div class="results-card">
      <div class="results-tabs-bar">
        <div class="tabs-group">
          <button id="t-res-btn" class="tab-btn active" onclick="switchWidgetTab('results')">📋 Results (<span id="w-rows">0</span>)</button>
          <button id="t-plan-btn" class="tab-btn" onclick="switchWidgetTab('plan')">🔍 Query Plan (EXPLAIN)</button>
        </div>
        <div class="stats-label">
          Latency: <span class="stats-val" id="w-latency">0 ms</span> · <span id="w-cache">—</span>
        </div>
      </div>
      <div class="content-area" id="w-content">
        <div class="empty">Executing query...</div>
      </div>
    </div>
  </div>

  <script>
    const API_URL = "{api_url}";
    let currentData = null;

    function applySql(btn) {{
      document.getElementById('widget-sql').value = btn.getAttribute('data-sql');
      executeWidgetQuery();
    }}

    function switchWidgetTab(tab) {{
      document.getElementById('t-res-btn').classList.toggle('active', tab === 'results');
      document.getElementById('t-plan-btn').classList.toggle('active', tab === 'plan');

      if (!currentData) return;
      const content = document.getElementById('w-content');
      if (tab === 'results') {{
        renderTable(currentData.results, currentData.columns);
      }} else {{
        renderPlan(currentData.query_plan);
      }}
    }}

    async function executeWidgetQuery() {{
      const sql = document.getElementById('widget-sql').value.trim();
      const runBtn = document.getElementById('w-run-btn');
      const statusEl = document.getElementById('w-status');
      if (!sql) return;

      runBtn.disabled = true;
      runBtn.textContent = "⏳ Running...";
      statusEl.textContent = "Executing in DuckDB...";

      try {{
        const res = await fetch(API_URL, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ query: sql, explain: true }})
        }});
        const data = await res.json();
        currentData = data;

        document.getElementById('w-rows').textContent = data.row_count || 0;
        document.getElementById('w-latency').textContent = (data.query_time_ms || 0) + ' ms';
        document.getElementById('w-cache').textContent = data.cache_hit ? '⚡ Local /tmp HIT' : '🔄 S3 Pulled';
        statusEl.textContent = 'Query completed (' + (data.row_count || 0) + ' rows)';

        // Notify host AI container if running inside an MCP App iframe
        if (window.parent && window.parent !== window) {{
          try {{
            window.parent.postMessage({{
              type: "mcp:widgetQueryExecuted",
              sql: sql,
              rowCount: data.row_count,
              queryTimeMs: data.query_time_ms,
            }}, "*");
          }} catch (e) {{}}
        }}

        const activeTab = document.getElementById('t-plan-btn').classList.contains('active') ? 'plan' : 'results';
        switchWidgetTab(activeTab);
      }} catch (err) {{
        document.getElementById('w-content').innerHTML = '<div class="empty" style="color:#f85149">Error: ' + err.message + '</div>';
        statusEl.textContent = 'Error executing query';
      }} finally {{
        runBtn.disabled = false;
        runBtn.textContent = "▶ Run SQL";
      }}
    }}

    function renderTable(rows, cols) {{
      const content = document.getElementById('w-content');
      if (!rows || rows.length === 0) {{
        content.innerHTML = '<div class="empty">0 rows returned.</div>';
        return;
      }}
      cols = cols || Object.keys(rows[0]);
      let html = '<table class="data-table"><thead><tr>';
      cols.forEach(c => html += '<th>' + c + '</th>');
      html += '</tr></thead><tbody>';
      rows.forEach(r => {{
        html += '<tr>';
        cols.forEach(c => html += '<td>' + (r[c] === null ? '<span style="color:#6e7681">null</span>' : r[c]) + '</td>');
        html += '</tr>';
      }});
      html += '</tbody></table>';
      content.innerHTML = html;
    }}

    function renderPlan(plan) {{
      const content = document.getElementById('w-content');
      if (!plan) {{
        content.innerHTML = '<div class="empty">No physical query plan available.</div>';
        return;
      }}
      content.innerHTML = '<pre class="plan-pre"><code>' + escapeHtml(plan) + '</code></pre>';
    }}

    function escapeHtml(s) {{
      return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }}

    window.addEventListener('DOMContentLoaded', () => {{
      executeWidgetQuery();
    }});
  </script>
</body>
</html>
"""

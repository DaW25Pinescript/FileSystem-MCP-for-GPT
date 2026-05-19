#!/usr/bin/env python3
"""
MCP Server with Codex-style tools
- Adds 'shell' (exec) and 'apply_patch' similar to Codex CLI behavior
- Keeps your search/fetch/write/create/delete tools
- Correct SSE behavior, CORS preflight, JSON-RPC error id echo
"""

import os
import sys
import json
import time
import argparse
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

PROTOCOL_VERSION = "2024-11-05"
# Reasonable execution limits for 'shell'
DEFAULT_TIMEOUT_SEC = 30
MAX_TIMEOUT_SEC = 300
MAX_STDOUT_CHARS = 200_000
MAX_STDERR_CHARS = 200_000
MAX_FETCH_BYTES = 5 * 1024 * 1024
SERVER_VERSION = "1.4.1"

TEXT_SUFFIXES = {
    ".bat", ".cmd", ".css", ".csv", ".env", ".gitignore", ".html", ".ini",
    ".js", ".json", ".jsx", ".log", ".md", ".ps1", ".py", ".rs", ".sh",
    ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
}
SKIP_SEARCH_DIRS = {
    ".git", ".hg", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv",
    "__pycache__", "build", "dist", "node_modules", "venv",
}
DEFAULT_BLOCKED_COMMANDS = (
    "rm -rf",
    "rmdir /s",
    "del /s",
    "format ",
    "remove-item -recurse",
    "remove-item -r",
    "ri -recurse",
)
# "restrict" is intentionally NOT in the valid set yet — planned for a later phase
# (stdout/stderr path scanning + redaction). Accepting it here without the underlying
# behaviour would advertise containment that does not exist.
VALID_SHELL_MODES = ("allow", "disable")
AUDIT_LOG_NAME = ".mcp_audit.log"
QUARANTINE_DIR_NAME = ".mcp_trash"

class MCPSSEHandler(BaseHTTPRequestHandler):
    def __init__(self, allowed_directory: Path, config: dict | None = None, *args, **kwargs):
        self.allowed_directory = allowed_directory
        self.config = config or {}
        super().__init__(*args, **kwargs)

    # -------- utilities --------
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-MCP-Auth")

    def _send_json(self, status: int, payload: dict):
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def _request_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        allowed_origins = set(self.config.get("allowed_origins") or [])
        if origin and not self._origin_allowed(origin, allowed_origins):
            self._send_json(403, {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32003, "message": f"Forbidden Origin: {origin}"},
            })
            return False

        token = self.config.get("auth_token")
        if token:
            auth = self.headers.get("Authorization", "")
            header_token = self.headers.get("X-MCP-Auth", "")
            if auth != f"Bearer {token}" and header_token != token:
                self._send_json(401, {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32004, "message": "Missing or invalid MCP auth token"},
                })
                return False
        return True

    def _origin_allowed(self, origin: str, allowed_origins: set[str]) -> bool:
        if origin in allowed_origins:
            return True
        parsed_origin = urlparse(origin)
        host = (parsed_origin.hostname or "").lower()
        if host in {"localhost", "127.0.0.1", "::1"}:
            return True
        return False

    # -------- HTTP verbs --------
    def do_OPTIONS(self):
        if not self._request_allowed():
            return
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if not self._request_allowed():
            return
        path = self._request_path()
        if path in ("/sse", "/sse/"):
            self.handle_sse_connection()
        elif path == "/":
            self._send_json(200, {
                "jsonrpc": "2.0",
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "Local Filesystem Server", "version": SERVER_VERSION},
                },
            })
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        if not self._request_allowed():
            return
        path = self._request_path()
        if path in ("/", "/mcp", "/sse", "/sse/"):
            self.handle_mcp_message()
        else:
            self.send_error(404, "Not Found")

    # -------- SSE --------
    def handle_sse_connection(self):
        self.send_response(200)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        self.send_sse_event("endpoint", "/")
        self.send_sse_event("message", {
            "jsonrpc": "2.0",
            "method": "notifications/server/ready",
            "params": {"message": "SSE stream established"},
        })
        try:
            while True:
                time.sleep(30)
                self.send_sse_event("ping", {"type": "ping"})
        except Exception:
            pass

    def send_sse_event(self, event_type: str, data):
        try:
            self.wfile.write(f"event: {event_type}\n".encode("utf-8"))
            payload = data if isinstance(data, str) else json.dumps(data)
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
        except Exception:
            pass

    # -------- JSON-RPC --------
    def _request_path(self) -> str:
        return urlparse(self.path).path

    def handle_mcp_message(self):
        message = None
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length <= 0:
                self._send_json(400, {
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32600, "message": "No content"},
                })
                return
            raw = self.rfile.read(content_length).decode("utf-8")
            message = json.loads(raw)
            response = self.process_mcp_message(message)
            if response is None:
                self.send_response(202)
                self._send_cors_headers()
                self.end_headers()
                return
            self._send_json(200, response)
        except Exception as e:
            msg_id = None
            try:
                msg_id = message.get("id")  # best effort
            except Exception:
                pass
            self._send_json(500, {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": str(e)},
            })

    def process_mcp_message(self, message: dict) -> dict:
        method = message.get("method")
        params = message.get("params", {}) or {}
        msg_id = message.get("id")

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "Local Filesystem Server", "version": SERVER_VERSION},
                },
            }

        elif method and method.startswith("notifications/"):
            return None

        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"tools": self.tool_definitions()}
            }

        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {}) or {}
            if tool_name == "shell" and self._shell_mode() == "disable":
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "shell tool is disabled"}}
            try:
                if tool_name == "status":
                    result = self.handle_status(bool(tool_args.get("verbose", False)))
                elif tool_name == "shell":
                    result = self.handle_shell(tool_args)
                elif tool_name == "apply_patch":
                    result = self.handle_apply_patch(tool_args.get("patch", ""), self._bool_arg(tool_args, "dry_run", "dryRun"))
                elif tool_name == "apply_patch_dry_run":
                    result = self.handle_apply_patch(tool_args.get("patch", ""), True)
                elif tool_name == "search":
                    result = self.handle_search(tool_args.get("query", ""), self._arg(tool_args, "max_results", "maxResults", default=50))
                elif tool_name == "search_limited":
                    result = self.handle_search(tool_args.get("query", ""), tool_args.get("max_results", 50))
                elif tool_name == "fetch":
                    result = self.handle_fetch(tool_args.get("id", ""))
                elif tool_name == "write_file":
                    result = self.handle_write_file(tool_args.get("path", ""), tool_args.get("content", ""))
                elif tool_name == "create_directory":
                    result = self.handle_create_directory(tool_args.get("path", ""))
                elif tool_name == "delete_file":
                    result = self.handle_delete_file(
                        tool_args.get("path", ""),
                        dry_run=self._bool_arg(tool_args, "dry_run", "dryRun"),
                        mode=tool_args.get("mode", "quarantine"),
                        confirm_recursive=self._bool_arg(tool_args, "confirm_recursive", "confirmRecursive"),
                    )
                elif tool_name == "delete_file_dry_run":
                    result = self.handle_delete_file(tool_args.get("path", ""), dry_run=True)
                elif tool_name == "delete_file_permanent":
                    result = self.handle_delete_file(
                        tool_args.get("path", ""),
                        dry_run=False,
                        mode="permanent",
                        confirm_recursive=self._bool_arg(tool_args, "confirm_recursive", "confirmRecursive"),
                    )
                else:
                    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}}

                return self._tool_response(msg_id, result)
            except Exception as e:
                return self._tool_response(msg_id, {"success": False, "error": str(e), "tool": tool_name}, is_error=True)

        else:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}

    def _tool_response(self, msg_id, structured: dict, is_error: bool = False) -> dict:
        result = {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(structured, ensure_ascii=False, indent=2),
                    "mimeType": "application/json",
                }
            ],
            "structuredContent": structured,
        }
        if is_error:
            result["isError"] = True
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    def _arg(self, args: dict, snake_name: str, camel_name: str, default=None):
        if snake_name in args:
            return args[snake_name]
        if camel_name in args:
            return args[camel_name]
        return default

    def _bool_arg(self, args: dict, snake_name: str, camel_name: str, default: bool = False) -> bool:
        return bool(self._arg(args, snake_name, camel_name, default))

    def tool_definitions(self) -> list[dict]:
        ok_schema = {"type": "boolean"}
        text_schema = {"type": "string"}
        path_result_schema = {
            "type": "object",
            "properties": {"success": ok_schema, "message": text_schema, "path": text_schema},
            "required": ["success", "path"],
            "additionalProperties": True,
        }
        definitions = [
            {
                "name": "status",
                "title": "Server Status",
                "description": "Return MCP server diagnostics, limits, workspace root, and runtime availability.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "verbose": {"type": "boolean", "description": "Include extended diagnostics when true", "default": False},
                    },
                    "additionalProperties": False,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "success": ok_schema,
                        "serverVersion": text_schema,
                        "protocolVersion": text_schema,
                        "allowedRoot": text_schema,
                        "availableTools": {"type": "array", "items": text_schema},
                    },
                    "required": ["success", "serverVersion", "allowedRoot", "availableTools"],
                    "additionalProperties": True,
                },
                "annotations": {"title": "Server Status", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            },
            {
                "name": "shell",
                "title": "Run Shell Command",
                "description": "Execute a command from a working directory inside the allowed root. String commands run through the platform shell.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "oneOf": [
                                {"type": "string", "description": "Command string run through the platform shell"},
                                {"type": "array", "items": {"type": "string"}, "description": "Command argv without shell interpolation"},
                            ]
                        },
                        "workdir": {"type": "string", "description": "Working directory inside the allowed root"},
                        "timeout": {"type": "integer", "description": "Timeout seconds", "default": DEFAULT_TIMEOUT_SEC, "minimum": 1, "maximum": MAX_TIMEOUT_SEC},
                    },
                    "required": ["command", "workdir"],
                    "additionalProperties": False,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "ok": ok_schema,
                        "exitCode": {"type": ["integer", "null"]},
                        "timedOut": ok_schema,
                        "stdout": text_schema,
                        "stderr": text_schema,
                    },
                    "required": ["ok", "exitCode", "timedOut", "stdout", "stderr"],
                    "additionalProperties": False,
                },
                "annotations": {"title": "Run Shell Command", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True},
            },
            {
                "name": "apply_patch",
                "title": "Apply Patch",
                "description": "Apply a Codex-style patch. Use dry_run to validate without writing files.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "patch": {"type": "string", "description": "Patch text beginning with *** Begin Patch"},
                        "dry_run": {"type": "boolean", "description": "Validate and preview changes without writing", "default": False},
                    },
                    "required": ["patch"],
                    "additionalProperties": False,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "success": ok_schema,
                        "dryRun": ok_schema,
                        "added": {"type": "array", "items": text_schema},
                        "updated": {"type": "array", "items": text_schema},
                        "deleted": {"type": "array", "items": text_schema},
                        "moved": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["success", "dryRun", "added", "updated", "deleted", "moved"],
                    "additionalProperties": False,
                },
                "annotations": {"title": "Apply Patch", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
            },
            {
                "name": "apply_patch_dry_run",
                "title": "Preview Patch",
                "description": "Validate a Codex-style patch and preview affected files without writing changes. Compatibility alias for clients that hide optional parameters.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"patch": {"type": "string", "description": "Patch text beginning with *** Begin Patch"}},
                    "required": ["patch"],
                    "additionalProperties": False,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "success": ok_schema,
                        "dryRun": ok_schema,
                        "added": {"type": "array", "items": text_schema},
                        "updated": {"type": "array", "items": text_schema},
                        "deleted": {"type": "array", "items": text_schema},
                        "moved": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["success", "dryRun", "added", "updated", "deleted", "moved"],
                    "additionalProperties": False,
                },
                "annotations": {"title": "Preview Patch", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            },
            {
                "name": "search",
                "title": "Search Files",
                "description": "Search for files and directories. Optional max_results defaults to 50 and is capped at 500.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query; empty lists the allowed root"},
                        "max_results": {"type": "integer", "description": "Maximum results to return", "default": 50, "minimum": 1, "maximum": 500},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "results": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"id": text_schema, "title": text_schema, "url": text_schema},
                                "required": ["id", "title", "url"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["results"],
                    "additionalProperties": False,
                },
                "annotations": {"title": "Search Files", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            },
            {
                "name": "search_limited",
                "title": "Search Files Limited",
                "description": "Search files and directories with an explicit result limit. Compatibility alias for clients that hide optional parameters.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query; empty lists the allowed root"},
                        "max_results": {"type": "integer", "description": "Maximum results to return", "default": 50, "minimum": 1, "maximum": 500},
                    },
                    "required": ["query", "max_results"],
                    "additionalProperties": False,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "results": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"id": text_schema, "title": text_schema, "url": text_schema},
                                "required": ["id", "title", "url"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["results"],
                    "additionalProperties": False,
                },
                "annotations": {"title": "Search Files Limited", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            },
            {
                "name": "fetch",
                "title": "Fetch File",
                "description": "Fetch file contents or list a directory inside the allowed root.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "description": "File or directory path inside the allowed root"}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "id": text_schema,
                        "title": text_schema,
                        "text": text_schema,
                        "url": text_schema,
                        "metadata": {"type": "object"},
                    },
                    "required": ["id", "title", "text", "url", "metadata"],
                    "additionalProperties": False,
                },
                "annotations": {"title": "Fetch File", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            },
            {
                "name": "write_file",
                "title": "Write File",
                "description": "Write a UTF-8 text file, creating parent directories when needed.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                    "additionalProperties": False,
                },
                "outputSchema": path_result_schema,
                "annotations": {"title": "Write File", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
            },
            {
                "name": "create_directory",
                "title": "Create Directory",
                "description": "Create a directory and parents inside the allowed root.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "outputSchema": path_result_schema,
                "annotations": {"title": "Create Directory", "readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            },
            {
                "name": "delete_file",
                "title": "Delete Or Quarantine",
                "description": "Delete or quarantine a file or directory. Recursive directory deletion requires confirm_recursive=true unless using quarantine mode.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "dry_run": {"type": "boolean", "default": False},
                        "mode": {"type": "string", "enum": ["quarantine", "permanent"], "default": "quarantine"},
                        "confirm_recursive": {"type": "boolean", "description": "Required for permanent recursive directory delete", "default": False},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "success": ok_schema,
                        "dryRun": ok_schema,
                        "mode": text_schema,
                        "message": text_schema,
                        "path": text_schema,
                        "quarantinePath": text_schema,
                        "type": text_schema,
                    },
                    "required": ["success", "path", "type"],
                    "additionalProperties": True,
                },
                "annotations": {"title": "Delete Or Quarantine", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
            },
            {
                "name": "delete_file_dry_run",
                "title": "Preview Delete",
                "description": "Preview deleting or quarantining a path without changing files. Compatibility alias for clients that hide optional parameters.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "success": ok_schema,
                        "dryRun": ok_schema,
                        "mode": text_schema,
                        "message": text_schema,
                        "path": text_schema,
                        "type": text_schema,
                    },
                    "required": ["success", "path", "type"],
                    "additionalProperties": True,
                },
                "annotations": {"title": "Preview Delete", "readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
            },
            {
                "name": "delete_file_permanent",
                "title": "Delete Permanently",
                "description": "Permanently delete a file or directory. Recursive directory deletion requires confirm_recursive=true.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "confirm_recursive": {"type": "boolean", "description": "Required for permanent recursive directory delete", "default": False},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
                "outputSchema": {
                    "type": "object",
                    "properties": {
                        "success": ok_schema,
                        "mode": text_schema,
                        "message": text_schema,
                        "path": text_schema,
                        "type": text_schema,
                    },
                    "required": ["success", "path", "type"],
                    "additionalProperties": True,
                },
                "annotations": {"title": "Delete Permanently", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
            },
        ]
        if self._shell_mode() == "disable":
            definitions = [t for t in definitions if t["name"] != "shell"]
        return definitions

    # -------- Codex-style tool impls --------
    def handle_status(self, verbose: bool = False) -> dict:
        status = {
            "success": True,
            "serverVersion": SERVER_VERSION,
            "protocolVersion": PROTOCOL_VERSION,
            "allowedRoot": str(self.allowed_directory),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "availableTools": [tool["name"] for tool in self.tool_definitions()],
            "maxFetchBytes": MAX_FETCH_BYTES,
            "shell": {
                "defaultTimeoutSeconds": DEFAULT_TIMEOUT_SEC,
                "maxTimeoutSeconds": MAX_TIMEOUT_SEC,
                "blockedCommands": self._blocked_commands(),
            },
            "transport": {
                "legacySse": "/sse/",
                "streamableHttpPost": "/mcp",
                "rootPost": "/",
                "originValidation": True,
                "authRequired": bool(self.config.get("auth_token")),
            },
            "runtime": {
                "gitAvailable": self._command_available("git"),
                "pythonAvailable": self._command_available("python") or self._command_available("py"),
                "ngrokAvailable": self._command_available("ngrok"),
            },
        }
        if verbose:
            status["toolDefinitions"] = self.tool_definitions()
            status["config"] = {
                "allowedOrigins": self.config.get("allowed_origins") or [],
                "blockedCommands": self._blocked_commands(),
                "auditLog": str(self.allowed_directory / AUDIT_LOG_NAME),
                "quarantineDirectory": str(self.allowed_directory / QUARANTINE_DIR_NAME),
            }
        return status

    def handle_shell(self, args: dict) -> dict:
        """
        Execute a shell command similar to Codex CLI's 'shell' tool:
        - command: str | [str]
        - workdir: required; must resolve inside allowed_directory
        - timeout: optional; default DEFAULT_TIMEOUT_SEC
        """
        if "command" not in args or "workdir" not in args:
            raise ValueError("shell requires 'command' and 'workdir'")

        # Resolve working directory safely
        workdir = self.validate_path(args.get("workdir", ""))
        if not workdir.exists() or not workdir.is_dir():
            raise ValueError(f"Invalid workdir: {workdir}")

        # Build command. String commands intentionally run through the platform
        # shell so Windows paths and quoting survive the MCP/JSON boundary.
        cmd = args["command"]
        if isinstance(cmd, str):
            run_command = cmd
            shell = True
            if not cmd.strip():
                raise ValueError("command cannot be empty")
        elif isinstance(cmd, list):
            if not all(isinstance(x, str) for x in cmd):
                raise ValueError("command array must contain only strings")
            run_command = cmd
            shell = False
            if not cmd:
                raise ValueError("command cannot be empty")
        else:
            raise ValueError("command must be string or array of strings")

        blocked = self._matched_blocked_command(cmd)
        if blocked:
            self._audit("shell_blocked", {"command": cmd, "workdir": str(workdir), "matched": blocked})
            raise PermissionError(f"Blocked shell command pattern: {blocked}")

        timeout = max(1, min(int(args.get("timeout", DEFAULT_TIMEOUT_SEC)), MAX_TIMEOUT_SEC))
        self._audit("shell", {"command": cmd, "workdir": str(workdir), "timeout": timeout})
        try:
            proc = subprocess.run(
                run_command,
                cwd=str(workdir),
                capture_output=True,
                text=True,
                errors="replace",
                shell=shell,
                timeout=timeout,
                env=self._restricted_env(),
            )
        except subprocess.TimeoutExpired as te:
            return {
                "ok": False,
                "exitCode": None,
                "timedOut": True,
                "stdout": (te.stdout or "")[:MAX_STDOUT_CHARS],
                "stderr": (te.stderr or f"Timed out after {timeout}s")[:MAX_STDERR_CHARS],
            }

        # Truncate outputs to keep responses manageable
        out = (proc.stdout or "")[:MAX_STDOUT_CHARS]
        err = (proc.stderr or "")[:MAX_STDERR_CHARS]
        return {"ok": proc.returncode == 0, "exitCode": proc.returncode, "timedOut": False, "stdout": out, "stderr": err}

    def handle_apply_patch(self, patch_text: str, dry_run: bool = False) -> dict:
        """
        Apply the common Codex patch format:
          *** Add File: path
          *** Update File: path
          *** Delete File: path

        Update hunks use leading " ", "+", and "-" lines. For compatibility
        with older prompts, an Update section with no hunk markers is treated as
        a full-file replacement.
        """
        lines = patch_text.splitlines(keepends=True)
        if not lines or not lines[0].strip() == "*** Begin Patch":
            raise ValueError("Patch must start with '*** Begin Patch'")

        changed = {"success": True, "dryRun": dry_run, "added": [], "updated": [], "deleted": [], "moved": [], "warnings": []}
        idx = 1

        while idx < len(lines):
            current = lines[idx].rstrip("\r\n")
            if current == "*** End Patch":
                self._audit("apply_patch", changed)
                return changed

            if current.startswith("*** Add File:"):
                rel_path = current[len("*** Add File:"):].strip()
                idx += 1
                body = []
                while idx < len(lines) and not self._is_patch_header(lines[idx]):
                    if not lines[idx].startswith("+"):
                        raise ValueError(f"Add File lines must start with '+': {rel_path}")
                    body.append(lines[idx][1:])
                    idx += 1
                file_path = self.validate_path(rel_path)
                if file_path.exists():
                    raise FileExistsError(f"File already exists: {rel_path}")
                if not dry_run:
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text("".join(body), encoding="utf-8", newline="")
                changed["added"].append(self._relative_id(file_path))
                continue

            if current.startswith("*** Delete File:"):
                rel_path = current[len("*** Delete File:"):].strip()
                file_path = self.validate_path(rel_path)
                if not file_path.exists() or not file_path.is_file():
                    raise FileNotFoundError(f"File not found: {rel_path}")
                if not dry_run:
                    file_path.unlink()
                changed["deleted"].append(self._relative_id(file_path))
                idx += 1
                continue

            if current.startswith("*** Update File:"):
                rel_path = current[len("*** Update File:"):].strip()
                source_path = self.validate_path(rel_path)
                if not source_path.exists() or not source_path.is_file():
                    raise FileNotFoundError(f"File not found: {rel_path}")
                idx += 1

                move_to = None
                if idx < len(lines) and lines[idx].startswith("*** Move to:"):
                    move_to = lines[idx].rstrip("\r\n")[len("*** Move to:"):].strip()
                    idx += 1

                section = []
                while idx < len(lines) and not self._is_patch_header(lines[idx]):
                    section.append(lines[idx])
                    idx += 1

                with source_path.open("r", encoding="utf-8", errors="replace", newline="") as fh:
                    text = fh.read()
                classification = self._classify_section(section)
                if classification == "hunk":
                    new_text = self._apply_update_hunks(text, section, rel_path)
                else:
                    new_text = "".join(section)
                    changed["warnings"].append(
                        f"{rel_path}: no diff markers found in Update section — applied as full-file replacement"
                    )

                target_path = self.validate_path(move_to) if move_to else source_path
                if not dry_run:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    target_path.write_text(new_text, encoding="utf-8", newline="")
                if move_to and target_path != source_path:
                    if not dry_run:
                        source_path.unlink()
                    changed["moved"].append({"from": self._relative_id(source_path), "to": self._relative_id(target_path)})
                else:
                    changed["updated"].append(self._relative_id(target_path))
                continue

            raise ValueError(f"Unknown patch header: {current}")

        raise ValueError("Unclosed patch block (missing '*** End Patch')")

    def _is_patch_header(self, line: str) -> bool:
        stripped = line.rstrip("\r\n")
        return stripped == "*** End Patch" or stripped.startswith((
            "*** Add File:", "*** Update File:", "*** Delete File:", "*** Move to:",
        ))

    def _looks_like_hunk(self, section: list[str]) -> bool:
        saw_marker = False
        saw_edit = False
        for line in section:
            if not line.strip():
                continue
            if line.startswith("@@"):
                return True
            marker = line[:1]
            if marker in (" ", "+", "-"):
                saw_marker = True
                saw_edit = saw_edit or marker in ("+", "-")
                continue
            return False
        return saw_marker and saw_edit

    def _classify_section(self, section: list[str]) -> str:
        """Classify an Update section as 'hunk' or 'full_replace'.

        Raises ValueError when the section contains diff markers (+/-/@@) but is
        not a valid unified diff hunk — preventing silent full-file overwrites
        from malformed patches.
        """
        if not section:
            return "full_replace"
        if self._looks_like_hunk(section):
            return "hunk"
        has_diff_markers = any(
            (line[:1] in ("+", "-") or line.startswith("@@"))
            for line in section if line.strip()
        )
        if has_diff_markers:
            raise ValueError(
                "Section contains diff markers (+/-/@@) but is not a valid unified "
                "diff hunk. Use '*** Add File:' for new files or supply a valid "
                "unified diff hunk with @@ headers."
            )
        return "full_replace"

    def _apply_update_hunks(self, original_text: str, section: list[str], rel_path: str) -> str:
        content = original_text.splitlines(keepends=True)
        cursor = 0
        idx = 0

        while idx < len(section):
            if section[idx].startswith("@@"):
                idx += 1
                continue

            old = []
            new = []
            while True:
                if idx >= len(section) or section[idx].startswith("@@"):
                    break
                line = section[idx]
                marker = line[:1]
                if marker == " ":
                    old.append(line[1:])
                    new.append(line[1:])
                elif marker == "-":
                    old.append(line[1:])
                elif marker == "+":
                    new.append(line[1:])
                elif line.startswith("\\ No newline at end of file"):
                    pass
                else:
                    raise ValueError(f"Malformed hunk line in {rel_path}: {line.rstrip()}")
                idx += 1

            position = self._find_subsequence(content, old, cursor)
            if position is None:
                raise ValueError(f"Patch context not found in {rel_path}")
            line_ending = self._line_ending_for(content[position:position + len(old)])
            if line_ending:
                new = [self._with_line_ending(line, line_ending) for line in new]
            content[position:position + len(old)] = new
            cursor = position + len(new)

        return "".join(content)

    def _find_subsequence(self, content: list[str], needle: list[str], start: int):
        if not needle:
            return start
        for pos in range(start, len(content) - len(needle) + 1):
            window = content[pos:pos + len(needle)]
            if window == needle:
                return pos
            if [self._line_body(line) for line in window] == [self._line_body(line) for line in needle]:
                return pos
            # Pass 3: trailing-whitespace tolerance (spaces/tabs/newlines stripped on both sides).
            # Leading whitespace remains significant so indentation differences still fail to match.
            if [line.rstrip() for line in window] == [line.rstrip() for line in needle]:
                return pos
        return None

    def _line_body(self, line: str) -> str:
        return line.rstrip("\r\n")

    def _line_ending_for(self, lines: list[str]) -> str:
        for line in lines:
            if line.endswith("\r\n"):
                return "\r\n"
            if line.endswith("\n"):
                return "\n"
            if line.endswith("\r"):
                return "\r"
        return ""

    def _with_line_ending(self, line: str, ending: str) -> str:
        body = self._line_body(line)
        return body + ending if line.endswith(("\r", "\n")) else body

    def _restricted_env(self):
        """Environment stripped down; PATH preserved for common tools."""
        env = os.environ.copy()
        # You can further lock this down—e.g., remove proxies/creds
        for k in list(env.keys()):
            if k.upper().startswith(("AWS_", "GCP_", "AZURE_", "DOCKER_", "KUBECONFIG", "SSH_")):
                env.pop(k, None)
        return env

    # -------- Filesystem tools --------
    def handle_search(self, query: str, max_results: int = 50) -> dict:
        results = []
        q = (query or "").lower().strip()
        limit = max(1, min(int(max_results or 50), 500))
        if not q:
            target = self.allowed_directory
            if target.exists() and target.is_dir():
                for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                    rel = item.relative_to(self.allowed_directory)
                    results.append({"id": str(rel), "title": f"{'[DIR] ' if item.is_dir() else ''}{item.name}", "url": f"file://{item.resolve()}"})
        else:
            for dirpath, dirnames, filenames in os.walk(self.allowed_directory):
                dirnames[:] = sorted(
                    [name for name in dirnames if name not in SKIP_SEARCH_DIRS],
                    key=str.lower,
                )
                entries = [(name, True) for name in dirnames] + [(name, False) for name in sorted(filenames, key=str.lower)]
                for name, is_dir in entries:
                    path = Path(dirpath) / name
                    try:
                        rel = path.relative_to(self.allowed_directory)
                        if q in name.lower() or q in str(rel).lower():
                            results.append({"id": str(rel), "title": f"{'[DIR] ' if is_dir else ''}{name}", "url": f"file://{path.resolve()}"})
                            if len(results) >= limit:
                                return {"results": results}
                    except Exception:
                        continue
        return {"results": results[:limit]}

    def handle_fetch(self, file_id: str) -> dict:
        if not file_id:
            raise ValueError("File ID is required")
        path = self.validate_path(file_id)
        if not path.exists():
            raise ValueError(f"Not found: {file_id}")
        if path.is_dir():
            items = []
            for item in sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
                items.append(f"{'[DIR] ' if item.is_dir() else ''}{item.name}")
            content = f"Directory: {file_id}\n\nContents:\n" + "\n".join(items)
            return {"id": file_id, "title": path.name, "text": content, "url": f"file://{path.resolve()}", "metadata": {"type": "directory"}}
        if path.stat().st_size > MAX_FETCH_BYTES:
            raise ValueError(f"File too large (limit {MAX_FETCH_BYTES // (1024 * 1024)}MB)")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            if path.suffix.lower() in TEXT_SUFFIXES:
                text = path.read_text(encoding="utf-8", errors="replace")
            else:
                text = f"[Binary file: {path.suffix or 'unknown'}]"
        return {"id": file_id, "title": path.name, "text": text, "url": f"file://{path.resolve()}", "metadata": {"type": "file", "size": path.stat().st_size}}

    def handle_write_file(self, dest: str, content: str) -> dict:
        if not dest:
            raise ValueError("File path is required")
        path = self.validate_path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        result = {"success": True, "message": f"Wrote {len(content)} bytes to {dest}", "path": self._relative_id(path)}
        self._audit("write_file", result)
        return result

    def handle_create_directory(self, p: str) -> dict:
        if not p:
            raise ValueError("Directory path is required")
        path = self.validate_path(p)
        path.mkdir(parents=True, exist_ok=True)
        result = {"success": True, "message": f"Created directory: {p}", "path": self._relative_id(path)}
        self._audit("create_directory", result)
        return result

    def handle_delete_file(self, p: str, dry_run: bool = False, mode: str = "quarantine", confirm_recursive: bool = False) -> dict:
        if not p:
            raise ValueError("Path is required")
        if mode not in {"quarantine", "permanent"}:
            raise ValueError("delete_file mode must be 'quarantine' or 'permanent'")
        path = self.validate_path(p)
        if not path.exists():
            raise ValueError(f"Path does not exist: {p}")
        rel = self._relative_id(path)
        path_type = "directory" if path.is_dir() else "file"
        if dry_run:
            return {"success": True, "dryRun": True, "mode": mode, "path": rel, "type": path_type, "message": f"Would delete {path_type}: {rel}"}
        if path.is_dir():
            if mode == "permanent" and not confirm_recursive:
                raise PermissionError("Permanent recursive directory delete requires confirm_recursive=true")
            if mode == "quarantine":
                target = self._quarantine_path(path)
                shutil.move(str(path), str(target))
                result = {"success": True, "mode": mode, "message": f"Moved directory to quarantine: {rel}", "path": rel, "quarantinePath": self._relative_id(target), "type": "directory"}
            else:
                shutil.rmtree(path)
                result = {"success": True, "mode": mode, "message": f"Deleted directory permanently: {rel}", "path": rel, "type": "directory"}
            self._audit("delete_file", result)
            return result
        if mode == "quarantine":
            target = self._quarantine_path(path)
            shutil.move(str(path), str(target))
            result = {"success": True, "mode": mode, "message": f"Moved file to quarantine: {rel}", "path": rel, "quarantinePath": self._relative_id(target), "type": "file"}
        else:
            path.unlink()
            result = {"success": True, "mode": mode, "message": f"Deleted file permanently: {rel}", "path": rel, "type": "file"}
        self._audit("delete_file", result)
        return result

    def _blocked_commands(self) -> list[str]:
        configured = self.config.get("blocked_commands")
        if configured is not None:
            return [item for item in configured if item]
        env_value = os.environ.get("MCP_BLOCKED_COMMANDS")
        if env_value:
            return [item.strip() for item in env_value.split(";") if item.strip()]
        return list(DEFAULT_BLOCKED_COMMANDS)

    def _shell_mode(self) -> str:
        mode = self.config.get("shell_mode")
        if mode is None:
            env_value = os.environ.get("MCP_SHELL_MODE")
            mode = env_value if env_value else "allow"
        mode = str(mode).strip().lower()
        if mode not in VALID_SHELL_MODES:
            return "allow"
        return mode

    def _normalise_command_text(self, command) -> str:
        import re
        text = " ".join(command) if isinstance(command, list) else str(command)
        return re.sub(r"\s+", " ", text).strip().lower()

    def _matched_blocked_command(self, command) -> str | None:
        normalized = self._normalise_command_text(command)
        for pattern in self._blocked_commands():
            if self._normalise_command_text(pattern) in normalized:
                return pattern
        return None

    def _command_available(self, command: str) -> bool:
        return shutil.which(command) is not None

    def _quarantine_path(self, source: Path) -> Path:
        quarantine_root = self.allowed_directory / QUARANTINE_DIR_NAME
        quarantine_root.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        candidate = quarantine_root / f"{stamp}_{source.name}"
        counter = 1
        while candidate.exists():
            candidate = quarantine_root / f"{stamp}_{counter}_{source.name}"
            counter += 1
        return candidate

    def _audit(self, action: str, details: dict):
        event = {
            "time": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "details": details,
        }
        try:
            audit_path = self.allowed_directory / AUDIT_LOG_NAME
            with audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # -------- security --------
    def validate_path(self, user_path: str) -> Path:
        if not isinstance(user_path, str) or not user_path.strip():
            raise ValueError("Path must be a non-empty string")
        abs_path = Path(user_path).resolve() if os.path.isabs(user_path) else (self.allowed_directory / user_path).resolve()
        try:
            abs_path.relative_to(self.allowed_directory)
        except ValueError:
            raise PermissionError("Path outside allowed directory")
        return abs_path

    def _relative_id(self, path: Path) -> str:
        return str(path.resolve().relative_to(self.allowed_directory))


class MCPHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


def create_handler(allowed_directory: Path, config: dict | None = None):
    def handler(*args, **kwargs):
        return MCPSSEHandler(allowed_directory, config, *args, **kwargs)
    return handler


def main():
    parser = argparse.ArgumentParser(description="Local filesystem MCP server for ChatGPT/Codex-style workflows.")
    parser.add_argument("directory", help="Directory the server is allowed to access")
    parser.add_argument("--host", default="localhost", help="Host interface to bind (default: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    parser.add_argument("--auth-token", default=os.environ.get("MCP_AUTH_TOKEN", ""), help="Optional bearer token required for HTTP requests")
    parser.add_argument("--allow-origin", action="append", default=[], help="Allowed Origin header. May be repeated. Localhost origins are always allowed.")
    parser.add_argument("--blocked-command", action="append", default=None, help="Blocked shell command substring. May be repeated. Defaults block common destructive patterns.")
    parser.add_argument(
        "--shell-mode",
        choices=list(VALID_SHELL_MODES),
        default=os.environ.get("MCP_SHELL_MODE", "allow"),
        help="Shell tool exposure: 'allow' (default, current behaviour) or 'disable' (removes the shell tool entirely; recommended for ngrok-exposed deployments).",
    )
    args = parser.parse_args()

    allowed_directory = Path(args.directory).resolve()
    if not allowed_directory.exists() or not allowed_directory.is_dir():
        print(f"Error: {allowed_directory} is not a valid directory")
        sys.exit(1)

    print(f"Starting MCP server restricted to: {allowed_directory}")
    print(f"Server URL: http://{args.host}:{args.port}")
    print(f"SSE URL for ChatGPT: http://{args.host}:{args.port}/sse/")
    print(f"Streamable HTTP-style POST URL: http://{args.host}:{args.port}/mcp")

    config = {
        "auth_token": args.auth_token,
        "allowed_origins": args.allow_origin,
        "blocked_commands": args.blocked_command,
        "shell_mode": args.shell_mode,
    }
    with MCPHTTPServer((args.host, args.port), create_handler(allowed_directory, config)) as server:
        print("Server started. Use Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped")


if __name__ == "__main__":
    main()

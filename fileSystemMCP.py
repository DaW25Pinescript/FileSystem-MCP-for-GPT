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
import subprocess
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
SERVER_VERSION = "1.2.0"

TEXT_SUFFIXES = {
    ".bat", ".cmd", ".css", ".csv", ".env", ".gitignore", ".html", ".ini",
    ".js", ".json", ".jsx", ".log", ".md", ".ps1", ".py", ".rs", ".sh",
    ".toml", ".ts", ".tsx", ".txt", ".xml", ".yaml", ".yml",
}
SKIP_SEARCH_DIRS = {
    ".git", ".hg", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv",
    "__pycache__", "build", "dist", "node_modules", "venv",
}

class MCPSSEHandler(BaseHTTPRequestHandler):
    def __init__(self, allowed_directory: Path, *args, **kwargs):
        self.allowed_directory = allowed_directory
        super().__init__(*args, **kwargs)

    # -------- utilities --------
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status: int, payload: dict):
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    # -------- HTTP verbs --------
    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
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
        path = self._request_path()
        if path in ("/", "/sse", "/sse/"):
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
            # Expose Codex-like tools + your originals
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": [
                        # --- Codex-style tools ---
                        {
                            "name": "shell",
                            "description": "Execute shell commands from a working directory inside the allowed workspace (Codex-style)",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "command": {
                                        "oneOf": [
                                            {"type": "string", "description": "Command string (runs through the platform shell)"},
                                            {"type": "array", "items": {"type": "string"}, "description": "Command argv"}
                                        ]
                                    },
                                    "workdir": {"type": "string", "description": "Working directory (relative or absolute within allowed root)"},
                                    "timeout": {"type": "integer", "description": "Timeout (seconds)"}
                                },
                                "required": ["command", "workdir"]
                            }
                        },
                        {
                            "name": "apply_patch",
                            "description": "Apply a multi-file patch in the common Codex '*** Begin Patch' format",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "patch": {"type": "string", "description": "Patch text (*** Begin Patch / *** Update File: path / *** End Patch)"},
                                },
                                "required": ["patch"]
                            }
                        },

                        # --- Filesystem tools you already had ---
                        {
                            "name": "search",
                            "description": "Search for files and directories",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string", "description": "Search query (empty = list root)"},
                                    "max_results": {"type": "integer", "description": "Maximum results to return (default 50, max 500)"}
                                },
                                "required": ["query"]
                            }
                        },
                        {
                            "name": "fetch",
                            "description": "Fetch file or directory contents",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "description": "File or directory path"}
                                },
                                "required": ["id"]
                            }
                        },
                        {
                            "name": "write_file",
                            "description": "Write a UTF-8 text file (creates parents)",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "content": {"type": "string"}
                                },
                                "required": ["path", "content"]
                            }
                        },
                        {
                            "name": "create_directory",
                            "description": "Create a directory (and parents)",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}},
                                "required": ["path"]
                            }
                        },
                        {
                            "name": "delete_file",
                            "description": "Delete a file or directory (recursive for directories)",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}},
                                "required": ["path"]
                            }
                        }
                    ]
                }
            }

        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {}) or {}
            try:
                if tool_name == "shell":
                    result = self.handle_shell(tool_args)
                elif tool_name == "apply_patch":
                    result = self.handle_apply_patch(tool_args.get("patch", ""))
                elif tool_name == "search":
                    result = self.handle_search(tool_args.get("query", ""), tool_args.get("max_results", 50))
                elif tool_name == "fetch":
                    result = self.handle_fetch(tool_args.get("id", ""))
                elif tool_name == "write_file":
                    result = self.handle_write_file(tool_args.get("path", ""), tool_args.get("content", ""))
                elif tool_name == "create_directory":
                    result = self.handle_create_directory(tool_args.get("path", ""))
                elif tool_name == "delete_file":
                    result = self.handle_delete_file(tool_args.get("path", ""))
                else:
                    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}}

                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [
                            {"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2), "mimeType": "application/json"}
                        ]
                    }
                }
            except Exception as e:
                return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(e)}}

        else:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Unknown method: {method}"}}

    # -------- Codex-style tool impls --------
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

        timeout = max(1, min(int(args.get("timeout", DEFAULT_TIMEOUT_SEC)), MAX_TIMEOUT_SEC))
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

    def handle_apply_patch(self, patch_text: str) -> dict:
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

        changed = {"added": [], "updated": [], "deleted": [], "moved": []}
        idx = 1

        while idx < len(lines):
            current = lines[idx].rstrip("\r\n")
            if current == "*** End Patch":
                return {"success": True, **changed}

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
                file_path.parent.mkdir(parents=True, exist_ok=True)
                file_path.write_text("".join(body), encoding="utf-8", newline="")
                changed["added"].append(self._relative_id(file_path))
                continue

            if current.startswith("*** Delete File:"):
                rel_path = current[len("*** Delete File:"):].strip()
                file_path = self.validate_path(rel_path)
                if not file_path.exists() or not file_path.is_file():
                    raise FileNotFoundError(f"File not found: {rel_path}")
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

                text = source_path.read_text(encoding="utf-8", errors="replace")
                if self._looks_like_hunk(section):
                    new_text = self._apply_update_hunks(text, section, rel_path)
                else:
                    new_text = "".join(section)

                target_path = self.validate_path(move_to) if move_to else source_path
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(new_text, encoding="utf-8", newline="")
                if move_to and target_path != source_path:
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
            content[position:position + len(old)] = new
            cursor = position + len(new)

        return "".join(content)

    def _find_subsequence(self, content: list[str], needle: list[str], start: int):
        if not needle:
            return start
        for pos in range(start, len(content) - len(needle) + 1):
            if content[pos:pos + len(needle)] == needle:
                return pos
        return None

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
        return {"success": True, "message": f"Wrote {len(content)} bytes to {dest}", "path": dest}

    def handle_create_directory(self, p: str) -> dict:
        if not p:
            raise ValueError("Directory path is required")
        path = self.validate_path(p)
        path.mkdir(parents=True, exist_ok=True)
        return {"success": True, "message": f"Created directory: {p}", "path": p}

    def handle_delete_file(self, p: str) -> dict:
        if not p:
            raise ValueError("Path is required")
        path = self.validate_path(p)
        if not path.exists():
            raise ValueError(f"Path does not exist: {p}")
        if path.is_dir():
            import shutil
            shutil.rmtree(path)
            return {"success": True, "message": f"Deleted directory: {p}", "path": p, "type": "directory"}
        path.unlink()
        return {"success": True, "message": f"Deleted file: {p}", "path": p, "type": "file"}

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


def create_handler(allowed_directory: Path):
    def handler(*args, **kwargs):
        return MCPSSEHandler(allowed_directory, *args, **kwargs)
    return handler


def main():
    parser = argparse.ArgumentParser(description="Local filesystem MCP server for ChatGPT/Codex-style workflows.")
    parser.add_argument("directory", help="Directory the server is allowed to access")
    parser.add_argument("--host", default="localhost", help="Host interface to bind (default: localhost)")
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on (default: 8000)")
    args = parser.parse_args()

    allowed_directory = Path(args.directory).resolve()
    if not allowed_directory.exists() or not allowed_directory.is_dir():
        print(f"Error: {allowed_directory} is not a valid directory")
        sys.exit(1)

    print(f"Starting MCP server restricted to: {allowed_directory}")
    print(f"Server URL: http://{args.host}:{args.port}")
    print(f"SSE URL for ChatGPT: http://{args.host}:{args.port}/sse/")

    with MCPHTTPServer((args.host, args.port), create_handler(allowed_directory)) as server:
        print("Server started. Use Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped")


if __name__ == "__main__":
    main()

# MCP Filesystem + Codex-Style Server

This is a small, dependency-free MCP-style HTTP/SSE server for giving ChatGPT controlled access to a local workspace.

It is designed for local repo work: inspect files, write fixes, apply Codex-style patches, and run shell commands with the working directory restricted to a chosen root.

## Features

- `shell`: run commands from a workspace directory. String commands use the platform shell, so Windows paths like `C:\Python314\python.exe` are preserved.
- `apply_patch`: apply Codex-style `*** Begin Patch` patches with add, update, delete, move, and normal `+`/`-` hunks.
- `search`: find files and directories, with common noisy folders such as `.git`, `.venv`, `node_modules`, and caches skipped.
- `fetch`: read files or list directories. Known text files such as Markdown fall back to replacement decoding instead of being misclassified as binary after one bad byte.
- `write_file`, `create_directory`, `delete_file`: basic filesystem mutation tools.
- Threaded HTTP/SSE handling so a long-lived SSE connection does not block tool POSTs.

## Requirements

- Python 3.9+
- No third-party packages

## Run Locally

```powershell
python fileSystemMCP.py D:\GitHub --port 8000
```

For a narrower workspace:

```powershell
python fileSystemMCP.py D:\GitHub\pine_python --port 8000
```

The server prints:

```text
Starting MCP server restricted to: D:\GitHub
Server URL: http://localhost:8000
SSE URL for ChatGPT: http://localhost:8000/sse/
Server started. Use Ctrl+C to stop.
```

Optional flags:

```text
--host HOST    Bind interface, default localhost
--port PORT    Listen port, default 8000
```

## Expose to ChatGPT

Cloud ChatGPT cannot reach `localhost` directly. Use a tunnel such as ngrok:

```powershell
ngrok http 8000
```

Add the HTTPS forwarding URL in ChatGPT's custom MCP server settings, usually with `/sse/` appended:

```text
https://example.ngrok-free.app/sse/
```

Security note: this server exposes local file editing and shell execution. File tools enforce the allowed root, and `shell` requires a working directory inside that root, but your operating system shell is not a complete sandbox. Only run it for directories you are comfortable exposing to the connected assistant, and stop the tunnel when you are done.

## Tool Notes

### `shell`

Arguments:

- `command`: string or argv array
- `workdir`: relative or absolute path inside the allowed root
- `timeout`: optional seconds, clamped to 1-300

String commands run through the platform shell. This is intentional for Windows compatibility:

```json
{
  "command": "cmd /c C:\\Python314\\python.exe -m pytest tests -v",
  "workdir": "D:\\GitHub\\pine_python",
  "timeout": 120
}
```

Use an argv array when you want no shell interpolation.

### `apply_patch`

Supports standard Codex-style patches:

```text
*** Begin Patch
*** Update File: example.txt
@@
 old line
-bad line
+good line
 next line
*** End Patch
```

Also supports `*** Add File:`, `*** Delete File:`, and `*** Move to:` inside update sections.

### `search`

Arguments:

- `query`: search text; empty lists the allowed root
- `max_results`: optional, default 50, max 500

### `fetch`

Files up to 5 MB are returned as text when possible. Directories return a simple sorted listing.

## Test

```powershell
python -m unittest -v
python -m py_compile fileSystemMCP.py test_fileSystemMCP.py
```

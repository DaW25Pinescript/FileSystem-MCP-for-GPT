# MCP Filesystem + Codex-Style Server

This is a small, dependency-free MCP-style HTTP/SSE server for giving ChatGPT controlled access to a local workspace.

It is designed for local repo work: inspect files, write fixes, apply Codex-style patches, and run shell commands with the working directory restricted to a chosen root.

## Features

- `shell`: run commands from a workspace directory. String commands use the platform shell, so Windows paths like `C:\Python314\python.exe` are preserved.
- `apply_patch`: apply Codex-style `*** Begin Patch` patches with add, update, delete, move, normal `+`/`-` hunks, and `dry_run`.
- `search`: find files and directories, with optional `max_results` and common noisy folders such as `.git`, `.venv`, `node_modules`, and caches skipped.
- `fetch`: read files or list directories. Known text files such as Markdown fall back to replacement decoding instead of being misclassified as binary after one bad byte.
- `write_file`, `create_directory`, `delete_file`: basic filesystem mutation tools. Deletes default to quarantine instead of immediate permanent deletion.
- `status`: report server version, root, runtime, limits, transport endpoints, and available tools.
- Compatibility alias tools: `apply_patch_dry_run`, `search_limited`, `delete_file_dry_run`, and `delete_file_permanent` expose important optional behavior as required parameters for clients that hide optional fields.
- Structured tool results: every tool returns `structuredContent` plus serialized JSON text for older clients.
- Tool metadata: tools include `title`, `outputSchema`, and read-only/destructive annotations.
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
--host HOST                 Bind interface, default localhost
--port PORT                 Listen port, default 8000
--auth-token TOKEN          Optional bearer token required for requests
--allow-origin ORIGIN       Allowed Origin header; repeatable
--blocked-command TEXT      Blocked shell command substring; repeatable
```

The legacy `/sse/` endpoint remains available. A modern-style POST endpoint is also available at `/mcp`.

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

For ngrok or any non-local exposure, prefer an auth token:

```powershell
python fileSystemMCP.py D:\GitHub --auth-token "change-me"
```

Requests can authenticate with either:

```text
Authorization: Bearer change-me
X-MCP-Auth: change-me
```

If an `Origin` header is present, localhost origins are allowed automatically. Additional allowed origins can be supplied with `--allow-origin`.

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

Shell invocations are appended to `.mcp_audit.log` under the allowed root. By default, obvious destructive command substrings such as `rm -rf`, `rmdir /s`, `del /s`, and `format ` are blocked. Override the list with one or more `--blocked-command` flags or `MCP_BLOCKED_COMMANDS` separated by semicolons.

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

Use `dry_run: true` to validate and preview the patch without writing.

### `search`

Arguments:

- `query`: search text; empty lists the allowed root
- `max_results`: optional, default 50, max 500

Note: if a client UI only displays `query`, refresh/reconnect the MCP server first. The server advertises `max_results` with a JSON Schema default, but some client wrappers may hide optional parameters in their visible shorthand. Use `search_limited` if the wrapper keeps hiding optional fields.

### `fetch`

Files up to 5 MB are returned as text when possible. Directories return a simple sorted listing.

### `delete_file`

Arguments:

- `path`: file or directory path inside the allowed root
- `dry_run`: optional, default false
- `mode`: `quarantine` or `permanent`, default `quarantine`
- `confirm_recursive`: required for permanent recursive directory deletes

Quarantined files are moved under `.mcp_trash` in the allowed root.

If the client wrapper hides optional delete fields, use:

- `delete_file_dry_run(path)`
- `delete_file_permanent(path, confirm_recursive)`

### `status`

Returns server diagnostics, including version, allowed root, platform, tool names, fetch limits, shell timeout limits, configured blocked commands, endpoint paths, and runtime availability for Git/Python/ngrok.

`status(verbose)` has a small optional input because some clients appear to ignore tools with completely empty input schemas.

## Test

```powershell
python -m unittest -v
python -m py_compile fileSystemMCP.py test_fileSystemMCP.py
```

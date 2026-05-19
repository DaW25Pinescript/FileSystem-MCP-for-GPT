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

The `directory` positional argument is optional — if omitted, the server looks for `allowedRoot` in `mcp_config.json` (see [Config file](#config-file) below).

Optional flags:

```text
--config PATH               Path to mcp_config.json (overrides script-adjacent auto-discovery)
--host HOST                 Bind interface, default localhost
--port PORT                 Listen port, default 8000
--auth-token TOKEN          Optional bearer token required for requests
--allow-origin ORIGIN       Allowed Origin header; repeatable
--blocked-command TEXT      Blocked shell command substring; repeatable
--shell-mode MODE           Shell tool exposure: allow | disable (default: allow)
```

The legacy `/sse/` endpoint remains available. A modern-style POST endpoint is also available at `/mcp`.

## Config file

`mcp_config.json` lets you keep settings out of the launcher batch file. Every key is optional; absence falls back to the same defaults as the CLI flags.

**Auto-discovery:** the server looks for `mcp_config.json` in the directory containing `fileSystemMCP.py` (script-adjacent). Override the location with `--config <path>`.

**Precedence (highest wins):**

1. CLI flags
2. `mcp_config.json` values
3. Environment variables (`MCP_AUTH_TOKEN`, `MCP_SHELL_MODE`, `MCP_BLOCKED_COMMANDS`)
4. Built-in defaults

A missing file is not an error — the server starts on built-in defaults. A malformed file (invalid JSON or non-object top level) prints a clear startup error and exits non-zero.

**Schema example:**

```json
{
  "allowedRoot": "D:\\GitHub",
  "host": "localhost",
  "port": 8000,
  "authToken": "",
  "allowedOrigins": [],
  "blockedCommands": [],
  "shellMode": "allow",
  "auditLog": ".mcp_audit.log",
  "trashDir": ".mcp_trash",
  "backupsDir": ".mcp_backups"
}
```

`auditLog`, `trashDir`, and `backupsDir` are **reserved** — they parse without error but currently have no effect; their consumers are still backed by the built-in constants. They are accepted in the schema now so files written today remain valid when those keys are wired in a later phase.

Unknown keys are dropped silently for forward compatibility.

## Expose to ChatGPT

Cloud ChatGPT cannot reach `localhost` directly. Use a tunnel such as ngrok:

```powershell
ngrok http 8000
```

Add the HTTPS forwarding URL in ChatGPT's custom MCP server settings, usually with `/sse/` appended:

```text
https://example.ngrok-free.app/sse/
```

Security note: this server exposes local file editing and shell execution. File tools enforce the allowed root, and `shell` requires a working directory inside that root, but **the operating system shell is not a sandbox**. A shell command can still read or write paths outside the allowed root regardless of the `workdir` argument. Only run it for directories you are comfortable exposing to the connected assistant, and stop the tunnel when you are done. For ngrok-exposed or otherwise untrusted deployments, set `--shell-mode disable` (see [Shell mode](#shell-mode)).

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

Shell invocations are appended to `.mcp_audit.log` under the allowed root. By default, obvious destructive command substrings such as `rm -rf`, `rmdir /s`, `del /s`, `format `, `Remove-Item -Recurse`, `Remove-Item -r`, and the `ri -Recurse` PowerShell alias are blocked. The match is case-insensitive and tolerant of internal whitespace (e.g. `rm  -rf` and `RM -RF` are both blocked). Override the list with one or more `--blocked-command` flags or `MCP_BLOCKED_COMMANDS` separated by semicolons.

#### Shell mode

The `--shell-mode` flag (also settable via `MCP_SHELL_MODE`) controls how the `shell` tool is exposed:

| Mode | Behaviour | When to use |
|------|-----------|-------------|
| `allow` *(default)* | Shell runs normally. `workdir` is validated against the allowed root and the blocked-command list is enforced. **The command itself can still touch paths outside the allowed root** — there is no OS-level containment. | Trusted local use only. |
| `disable` | The `shell` tool is removed from the published tool list and any `tools/call` for `shell` returns a JSON-RPC `-32601` error. | **Recommended for ngrok-exposed or otherwise untrusted deployments** — the only mode that actually prevents shell execution. |

Unknown or unsupported values fall back to `allow`. All other tools (`apply_patch`, `search`, `fetch`, `write_file`, `delete_file`, `status`, etc.) are unaffected by this setting.

A `restrict` mode (best-effort stdout/stderr path scanning) is planned for a later hardening phase. Until that ships it is **not** a recognised value — passing `--shell-mode restrict` on the CLI is rejected by argparse, and setting `MCP_SHELL_MODE=restrict` falls back to `allow`.

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

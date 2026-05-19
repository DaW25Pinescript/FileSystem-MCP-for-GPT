# TODO

## Resolved in v1.4.1 (Phase 2 — Patch Correctness)

- **I-3** Silent full-file overwrite — Update sections containing diff markers (`+`/`-`/`@@`) that are not valid unified-diff hunks now raise an explicit `ValueError` instead of silently overwriting the file. Legitimate full-file replacements (no diff markers anywhere) still apply, now with a `warnings` entry. The response always includes a `warnings: []` field. See `HARDENING_SPEC.md` Phase 2.
- **I-4** `_find_subsequence` whitespace mismatch — added a third fuzzy pass that strips trailing whitespace from both content and needle. Patch contexts that differ from the file only by trailing spaces/tabs now match. Leading whitespace remains significant. Existing CR/LF tolerance unchanged.

## Resolved in v1.4.0 (Phase 1 — Security Hardening)

- **I-1** Shell sandbox escape — `--shell-mode disable` now removes the `shell` tool entirely and is the recommended setting for ngrok-exposed deployments. (`restrict` was descoped from Phase 1 after review — see `HARDENING_SPEC.md` §4.1 and §8 "Reviewer-driven descope".)
- **I-2** Blocked-command bypass — `_matched_blocked_command` now normalises whitespace and case before matching, and `DEFAULT_BLOCKED_COMMANDS` covers `Remove-Item -Recurse` / `Remove-Item -r` / `ri -Recurse`. Backwards compatible: all prior entries unchanged.

## Next Refinement Pass: Config and Launcher Usability

Current stable baseline: server version `1.4.1`.

The MCP v2 connector now exposes the full tool surface after deleting and recreating the ChatGPT connector registration. Treat future schema/tool-name changes as likely requiring a fresh connector registration, not just `refetch_tools=true`.

### Scope

Focus this pass on daily usability and safer configuration. Do not overbuild into broader agent features yet.

### Priority Tasks

1. Add `mcp_config.json`

- Move core settings out of the batch file and CLI flags where practical.
- Include:
  - `allowedRoot`
  - `host`
  - `port`
  - `authToken`
  - `allowedOrigins`
  - `blockedCommands`
  - `auditLog`
  - `trashDir`
  - `backupsDir`
  - `shellMode`
- CLI args should override config values.
- Keep backwards compatibility with the current command format.

2. Polish the launcher

- Keep starting the MCP server and ngrok as before.
- Query the local ngrok API at:

```text
http://127.0.0.1:4040/api/tunnels
```

- Print the exact ChatGPT connector URL clearly, for example:

```text
https://xxxx.ngrok-free.app/sse/
```

- If ngrok is not ready yet, retry for a short period and print a useful error.

3. Keep auth optional, but make it easy

- Support `authToken` from config.
- Keep no-auth as a valid default for now unless ChatGPT auth-header support is confirmed.
- Document the security tradeoff clearly in the README.

4. Add tests

- Config loading.
- CLI override behavior.
- Invalid config handling.
- Launcher/ngrok URL parsing if practical.
- Existing MCP tests must continue passing.

### Explicit Non-Goals For This Pass

Do not add these yet:

- Git tools.
- Backup/restore tools.
- Write allow rules.

These are good later improvements, but this pass should stay focused on config, launcher reliability, and optional auth.

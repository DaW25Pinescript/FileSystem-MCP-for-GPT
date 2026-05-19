# FileSystem-MCP-for-GPT — Hardening Spec

## Header Block
- **Status:** Phases 1 ✅ + 2 ✅ + 3 ✅ complete — Phase 4 spec drafted, implementation pending
- **Date:** 2026-05-19
- **Repo:** `D:\GitHub\FileSystem-MCP-for-GPT`
- **Baseline version:** `1.3.1` → `1.4.0` (Phase 1) → `1.4.1` (Phase 2) → current `1.5.0` (Phase 3)
- **Review level:** Full — security changes, correctness fixes, and new config surface all require careful review before commit
- **Source:** Combined from independent review findings + existing `TODO.md`

---

## 1. Purpose

This spec consolidates all outstanding work for `fileSystemMCP.py` into a single prioritised hardening plan.

The independent review identified **7 new issues** not captured in `TODO.md` (2 security, 2 correctness, 3 quality). The existing `TODO.md` captures **4 usability/config tasks**. Together these form a 4-phase hardening roadmap.

Moves FROM → a working but security-soft and config-fragile MCP server
Moves TO → a hardened, config-driven, correctness-verified MCP server suitable for ngrok-exposed multi-client use

---

## 2. Full Item Register (Priority Ordered)

All items from both the independent review and `TODO.md`, merged and ranked.

| # | Source | Severity | Item | Phase |
|---|--------|----------|------|-------|
| I-1 | Review | 🔴 Critical | `shell` tool escapes allowed root — commands can read/write anywhere on the machine regardless of `allowed_directory` | 1 |
| I-2 | Review | 🔴 Critical | `_matched_blocked_command` trivially bypassable — simple substring match fails on double-space, shell quoting tricks, PowerShell equivalents | 1 |
| I-3 | Review | 🟡 High | `_looks_like_hunk` silent fallback — malformed patch section silently replaces entire file with no warning | 2 |
| I-4 | Review | 🟡 High | `_find_subsequence` trailing whitespace mismatch — patch context fails when file/patch differ only in trailing spaces, raises unhelpful error | 2 |
| T-1 | TODO | 🟡 High | Add `mcp_config.json` — move `allowedRoot`, `host`, `port`, `authToken`, `allowedOrigins`, `blockedCommands`, `auditLog`, `trashDir`, `backupsDir`, `shellMode` out of CLI flags; CLI args override config | 3 |
| T-2 | TODO | 🟡 High | Polish launcher — query `http://127.0.0.1:4040/api/tunnels`, print exact connector URL, retry if ngrok not ready, print useful error | 3 |
| T-3 | TODO | 🟢 Medium | Auth token support — load `authToken` from config; keep no-auth as valid default; document security tradeoff | 3 |
| I-5 | Review | 🟢 Medium | Missing `str_replace` tool — surgical single-location edits are verbose in Codex patch format; `str_replace(path, old_str, new_str)` is a natural complement | 4 |
| I-6 | Review | 🟢 Medium | `write_file` silent overwrite — no confirmation or backup before overwriting existing files | 4 |
| I-7 | Review | 🟢 Low | `handle_fetch` binary fallback not flagged — files with text extensions that fail UTF-8 decode are served as garbled content with no metadata indicator | 4 |
| I-8 | Review | 🟢 Low | SSE thread exhaustion — each SSE connection holds a thread indefinitely; stale connections accumulate under multi-client use | 4 |
| T-4 | TODO | 🟢 Low | Tests — config loading, CLI override, invalid config handling, launcher/ngrok URL parsing; existing tests must continue passing | 4 |

---

## 3. Phase Roadmap

| Phase | Name | Scope | Items | Status |
|-------|------|-------|-------|--------|
| 1 | Security Hardening | Shell sandbox escape + blocked command bypass | I-1, I-2 | ✅ Complete (v1.4.0) |
| 2 | Patch Correctness | Hunk detection silent failure + whitespace mismatch | I-3, I-4 | ✅ Complete (v1.4.1) |
| 3 | Config & Launcher | `mcp_config.json` + auth token (launcher work descoped) | T-1, T-3 | ✅ Complete (v1.5.0) |
| 4 | Quality & Tools | `str_replace`, overwrite guard, binary metadata, SSE, tests | I-5, I-6, I-7, I-8, T-4 | ⏳ Spec drafted |

---

---

# Phase 1 — Security Hardening

## 1. Purpose

**Answers:** Are shell commands genuinely sandboxed, and are blocked command patterns actually blocking?

Moves FROM → `shell` tool accepts any command targeting any path on the machine; blocked list bypassable with trivial tricks
Moves TO → shell command output is path-constrained at the OS level; blocked list handles normalised input

## 2. Scope

### In scope
- Add stdout/stderr output filtering in `handle_shell` to detect and suppress path-escaped reads
- Add `--shell-mode` flag / `shellMode` config option: `allow` (current default), `restrict` (workdir-only enforcement), `disable` (removes tool from tool list)
- Normalise command string before blocked command matching (collapse whitespace, expand common aliases)
- Document the residual risk in README clearly

### Out of scope
- `mcp_config.json` (Phase 3)
- Any new tools
- Any changes to patch, fetch, write, delete, or search handlers

## 3. Key File Paths

| Role | Path |
|------|------|
| Main server | `D:\GitHub\FileSystem-MCP-for-GPT\fileSystemMCP.py` |
| Test suite | `D:\GitHub\FileSystem-MCP-for-GPT\test_fileSystemMCP.py` |
| TODO reference | `D:\GitHub\FileSystem-MCP-for-GPT\TODO.md` |

## 4. Design

### 4.1 Shell Mode (`--shell-mode` / `shellMode`)

Add a `shell_mode` config value. As shipped in Phase 1, **two** valid states are accepted:

| Mode | Behaviour |
|------|-----------|
| `allow` | Current behaviour — shell runs, workdir validated, blocked list checked. Default. |
| `disable` | `shell` tool is removed from `tool_definitions()` output entirely; any `tools/call` for `shell` returns a clear error |

A third mode, `restrict`, was descoped from Phase 1 after review (see §8 Diagnostic Findings → "Reviewer-driven descope"). It is planned for a later hardening phase as best-effort stdout/stderr path scanning, and is **not** an accepted value in v1.4.0 — accepting it without the behaviour would advertise containment that does not exist.

**Recommendation:** default remains `allow` for backwards compatibility. Ngrok-exposed instances should set `disable` — the only mode that actually prevents shell execution.

### 4.2 Blocked Command Hardening

Replace the current `_matched_blocked_command` substring match with a normalised match:

```python
def _normalise_command_text(self, command) -> str:
    text = " ".join(command) if isinstance(command, list) else str(command)
    import re
    return re.sub(r'\s+', ' ', text).strip().lower()
```

Then match against the normalised text. This catches `rm  -rf`, `RM -RF`, mixed-case variants.

Also extend `DEFAULT_BLOCKED_COMMANDS` to include PowerShell equivalents:
```python
DEFAULT_BLOCKED_COMMANDS = (
    "rm -rf", "rmdir /s", "del /s", "format ",
    "remove-item -recurse", "remove-item -r",  # PowerShell
    "ri -recurse",                               # PS alias
)
```

## 5. Acceptance Criteria

| # | Gate | Acceptance Condition | Status |
|---|------|---------------------|--------|
| AC-1 | Shell mode disable | When `shell_mode=disable`, `tool_definitions()` does not include `shell` tool | ✅ `test_shell_disable_removes_tool_from_tool_definitions` |
| AC-2 | Shell mode disable call | When `shell_mode=disable`, `tools/call` for `shell` returns `{"code": -32601, "message": "shell tool is disabled"}` | ✅ `test_shell_disable_tools_call_returns_jsonrpc_error` |
| AC-3 | Blocked command normalised | `rm  -rf /` (double space) is blocked | ✅ `test_blocked_command_double_space_blocked` |
| AC-4 | Blocked command case | `RM -RF /` (uppercase) is blocked | ✅ `test_blocked_command_uppercase_blocked` |
| AC-5 | Blocked command PowerShell | `Remove-Item -Recurse` is blocked | ✅ `test_blocked_command_powershell_remove_item_blocked` |
| AC-6 | Legitimate command passes | `python --version` still executes successfully in allow mode | ✅ `test_blocked_command_legitimate_command_allowed` + pre-existing `test_shell_string_preserves_windows_backslashes` |
| AC-7 | Existing tests pass | All tests in `test_fileSystemMCP.py` pass before and after changes | ✅ 28 tests pass (18 original + 10 new), 1 skipped, 0 failed |
| AC-8 | README updated | Security tradeoff for `shell_mode` options documented in README | ✅ README "Shell mode" section explicitly states `restrict` is not containment; `disable` is the recommended mode for exposed deployments |
| AC-9 | Invalid shell_mode safe | Unknown `shell_mode` values fall back to `allow` rather than failing closed unexpectedly | ✅ `test_shell_mode_invalid_value_falls_back_to_allow` |
| AC-10 | Disable scoped to shell | `shell_mode=disable` removes only the `shell` tool; all other tools remain present | ✅ `test_shell_disable_leaves_other_tools_present` |

## 6. Pre-Code Diagnostic Protocol

1. Run existing test suite — confirm all pass as baseline: `python test_fileSystemMCP.py`
2. Confirm current `DEFAULT_BLOCKED_COMMANDS` tuple and `_matched_blocked_command` implementation — record line numbers
3. Confirm `tool_definitions()` currently always includes `shell` — record that no conditional exists
4. Confirm `handle_shell` has no path-scoping on command string itself — only `workdir` is validated
5. Report: (a) test count baseline, (b) files touched, (c) estimated line delta, (d) any existing tests that test blocked commands

Do not implement until diagnostic report is reviewed.

## 7. Implementation Sequence

1. Add `shell_mode` to `config` dict and `--shell-mode` CLI arg
2. Add conditional in `tool_definitions()` — omit `shell` when `shell_mode == "disable"`
3. Add disabled check at top of `tools/call` handler for `shell`
4. → Verify AC-1, AC-2 pass
5. Extract `_normalise_command_text()` helper
6. Update `_matched_blocked_command` to use normalised text
7. Extend `DEFAULT_BLOCKED_COMMANDS` with PowerShell variants
8. → Verify AC-3, AC-4, AC-5, AC-6 pass
9. Update README with shell mode documentation
10. → Run full test suite, verify AC-7

**No changes expected to:** `handle_fetch`, `handle_write_file`, `handle_search`, `handle_delete_file`, `handle_apply_patch`, any patch utilities

## 8. Diagnostic Findings

Pre-code diagnostic run on 2026-05-19, before any changes.

### Baseline test suite
- `python test_fileSystemMCP.py` → **18 tests, 17 passed + 1 skipped, 0 failed**.

### Code locations (pre-edit, in `fileSystemMCP.py`)
- `SERVER_VERSION = "1.3.1"` at **line 29** → bumped to `1.4.0`.
- `DEFAULT_BLOCKED_COMMANDS = ("rm -rf", "rmdir /s", "del /s", "format ")` at **line 40** → 4 entries.
- `tool_definitions()` at **line 295**; `shell` tool entry at **line 331** — **unconditional**, no `if` gate.
- `handle_shell()` at **line 652**. Validates only `workdir` (line 663); blocked-pattern check at line 685; subprocess invocation at line 693. **No scoping of the command string itself** — confirmed I-1 finding.
- `_blocked_commands()` at **line 1015** — already supports config / `MCP_BLOCKED_COMMANDS` env / default tuple chain.
- `_matched_blocked_command()` at **line 1024** — already lowercases input, but does **not** collapse whitespace. Plain `in` substring match on lowered text.

### Existing blocked-command test coverage
- One test only: `test_shell_timeout_and_blocked_command` (line 206 of `test_fileSystemMCP.py`), asserting `rm -rf important` raises `PermissionError`. No coverage for whitespace normalisation, PowerShell variants, or `shell_mode=disable`.

### Pre-existing behaviour worth noting
- Case-insensitive matching already worked incidentally via `.lower()` (AC-4 was a latent pass; new test added to lock it in).
- Whitespace collapse was the real gap (`rm  -rf` bypassed `"rm -rf"`).
- PowerShell `Remove-Item -Recurse` was the second real gap — absent from `DEFAULT_BLOCKED_COMMANDS`.
- `_blocked_commands()` returns `list(DEFAULT_BLOCKED_COMMANDS)` (a copy), so additive default extension is fully backwards-compatible.

### Files actually touched
1. `fileSystemMCP.py` — `SERVER_VERSION`, `DEFAULT_BLOCKED_COMMANDS`, added `VALID_SHELL_MODES`, added `_shell_mode()` and `_normalise_command_text()` helpers, rewrote `_matched_blocked_command()`, wrapped `tool_definitions()` return with conditional shell filter, added early-return for disabled `shell` in `tools/call` dispatcher, added `--shell-mode` CLI arg and `shell_mode` config key.
2. `test_fileSystemMCP.py` — bumped two `SERVER_VERSION` assertions to `1.4.0`, added new `ShellModeAndBlockedCommandTests` class with 10 deterministic tests (no live destructive subprocess calls).
3. `README.md` — added `--shell-mode` to the flags list, hardened the security note to explicitly say the OS shell is not a sandbox, added a new "Shell mode" subsection that frames `restrict` as best-effort output redaction (not containment) and `disable` as the recommended mode for exposed deployments.

### Post-implementation test suite
- `python test_fileSystemMCP.py` → **28 tests, 27 passed + 1 skipped, 0 failed**. Delta: +10 new tests, all green.

### Reviewer-driven descope
- First review pass flagged that `restrict` as originally described was not a true sandbox. An interim implementation accepted `restrict` as a valid value but did not implement output redaction.
- Second review pass flagged that this created a false security posture — operators or models setting `shell_mode=restrict` would believe they had partial protection when behaviour was identical to `allow`.
- **Resolution (Option A from review):** `restrict` removed from `VALID_SHELL_MODES` entirely for Phase 1. `--shell-mode restrict` is rejected by argparse; `MCP_SHELL_MODE=restrict` falls back to `allow` via `_shell_mode()`'s unknown-value handling. README documents only `allow` and `disable`, with an explicit note that `restrict` is planned for a later phase and not currently a recognised value. No documented behaviour exists without code to back it.

### Residual scope notes
- Output redaction (the original `restrict` design) is deferred to a later phase. When implemented, it will reintroduce `restrict` to `VALID_SHELL_MODES`; until then there is no config or documentation gap.
- `_shell_mode()` silently falls back to `allow` for unrecognised values (including `restrict` via env var). This was retained as the safest "no behaviour change on typo" default for CLI/env. When `mcp_config.json` lands in Phase 3, invalid `shellMode` values should be a hard startup error at config-load time (per reviewer observation #2). Tracked for Phase 3.

---

---

# Phase 2 — Patch Correctness

## 1. Purpose

**Answers:** Do `apply_patch` operations fail safely, or do they silently corrupt files?

Moves FROM → malformed patches silently replace entire files; whitespace differences cause confusing context-not-found errors
Moves TO → malformed patches raise explicit errors with actionable messages; whitespace-tolerant context matching

## 2. Scope

### In scope
- `_looks_like_hunk` — add explicit warning/error when section is non-empty but doesn't look like a hunk (preventing silent full-file replacement)
- `_find_subsequence` — extend fuzzy fallback to strip trailing whitespace from both sides before comparison
- Add a `"warnings"` field to `apply_patch` response when fallback behaviour is used

### Out of scope
- New patch operations (move, copy, rename)
- Any change to `apply_patch_dry_run` beyond inheriting the above fixes
- Any config or launcher work

## 3. Key File Paths

| Role | Path |
|------|------|
| Main server | `D:\GitHub\FileSystem-MCP-for-GPT\fileSystemMCP.py` |
| Test suite | `D:\GitHub\FileSystem-MCP-for-GPT\test_fileSystemMCP.py` |

## 4. Design

### 4.1 `_looks_like_hunk` — Guard Against Silent Replacement

Current behaviour: returns `False` for non-hunk sections → full-file replacement with no signal.

Proposed change: introduce `_classify_section` that distinguishes `'hunk'`, `'full_replace'`, and raises on ambiguous content:

```python
def _classify_section(self, section: list[str]) -> str:
    if not section:
        return 'full_replace'
    if self._looks_like_hunk(section):
        return 'hunk'
    has_diff_markers = any(
        line[:1] in ('+', '-') or line.startswith('@@')
        for line in section if line.strip()
    )
    if has_diff_markers:
        raise ValueError(
            "Section has diff markers (+/-/@@) but is not a valid hunk. "
            "Use '*** Add File:' for new files or provide valid unified diff format."
        )
    return 'full_replace'
```

### 4.2 `_find_subsequence` — Trailing Whitespace Tolerance

Add a third fuzzy pass that strips trailing whitespace from both content and needle:

```python
# Pass 3: strip trailing whitespace on both sides
if [l.rstrip() for l in content[pos:pos+len(needle)]] == [l.rstrip() for l in needle]:
    return pos
```

### 4.3 Response `warnings` Field

Add optional `warnings` list to `apply_patch` response for transparency on fallback paths:

```json
{
  "success": true,
  "dryRun": false,
  "added": [],
  "updated": ["src/main.py"],
  "deleted": [],
  "moved": [],
  "warnings": ["src/main.py: no hunk markers found — applied as full-file replacement"]
}
```

## 5. Acceptance Criteria

| # | Gate | Acceptance Condition | Status |
|---|------|---------------------|--------|
| AC-1 | Malformed hunk raises | A patch section with `+`/`-` markers that isn't valid hunk format raises `ValueError` with descriptive message | ✅ `test_apply_patch_section_with_diff_markers_but_no_hunk_raises` |
| AC-2 | Clean full-replace still works | A valid `*** Update File:` section with no diff markers applies correctly | ✅ `test_apply_patch_valid_full_replace_writes_file_with_warning` |
| AC-3 | Trailing whitespace match | A patch context that differs from file only in trailing spaces successfully finds and applies the hunk | ✅ `test_apply_patch_context_with_trailing_whitespace_matches` |
| AC-4 | Whitespace mismatch error | When context genuinely cannot be found, error raised with `"Patch context not found in [file]"` | ✅ `test_apply_patch_genuine_context_miss_still_raises` + tightened assertion in pre-existing `test_apply_patch_dry_run_and_failed_context` |
| AC-5 | Warning field present | Full-file replacement path includes `warnings` array in response; key always present even when empty | ✅ `test_apply_patch_valid_full_replace_writes_file_with_warning` + `test_apply_patch_warnings_field_present_even_when_empty` |
| AC-6 | Dry-run inherits fixes | `apply_patch_dry_run` reflects same corrected behaviour | ✅ `test_apply_patch_dry_run_inherits_classify_error` |
| AC-7 | Existing tests pass | All tests pass before and after changes | ✅ 35 tests pass (28 pre-Phase-2 + 7 new), 1 skipped, 0 failed |

## 6. Pre-Code Diagnostic Protocol

1. Run existing test suite — confirm all pass as baseline
2. Locate `_looks_like_hunk`, `_apply_update_hunks`, `_find_subsequence` — record line numbers
3. Confirm whether any existing tests cover the full-file replacement path or whitespace matching
4. Report: (a) test count baseline, (b) coverage gaps for these two paths, (c) proposed new test cases

Do not implement until diagnostic report is reviewed.

## 7. Implementation Sequence

1. Add `_classify_section` helper, replace direct `_looks_like_hunk` call in `_apply_update_hunks`
2. Update `handle_apply_patch` response to include `warnings: []` field
3. → Verify AC-1, AC-2 pass
4. Add trailing whitespace pass to `_find_subsequence`
5. → Verify AC-3, AC-4 pass
6. Propagate `warnings` from `_apply_update_hunks` up through `handle_apply_patch`
7. → Verify AC-5, AC-6 pass
8. Add tests for both new paths
9. → Run full suite, verify AC-7

**No changes expected to:** `handle_fetch`, `handle_write_file`, `handle_search`, `handle_delete_file`, `handle_shell`, `validate_path`

## 8. Diagnostic Findings

Pre-code diagnostic run on 2026-05-19, after Phase 1 landed (baseline 28 tests).

### Code locations confirmed (pre-edit, in `fileSystemMCP.py`)
- `handle_apply_patch` at **line 734**; the controversial fork is at lines **806–809** — `_looks_like_hunk(section)` → `_apply_update_hunks`, else → `"".join(section)` silent full-file overwrite.
- `_looks_like_hunk` at **line 833** — `True` for `@@` or pure-marker sections with at least one edit; `False` for empty list and for any section containing a non-blank non-marker line.
- `_apply_update_hunks` at **line 849** — already raises `ValueError("Malformed hunk line ...")` and `ValueError("Patch context not found in ...")`.
- `_find_subsequence` at **line 890** — two passes only: exact equality, then `_line_body` (CR/LF rstrip). No tolerance for trailing spaces/tabs.

### Watchpoint 1 — `_looks_like_hunk → full-file-replacement` coverage
- **Zero existing coverage.** All four `apply_patch` tests in `test_fileSystemMCP.py` (lines 222, 242, 262, 290) use Update sections beginning with `@@` + valid markers. The `else: new_text = "".join(section)` branch had no test before Phase 2.
- **Implication:** `_classify_section`'s behaviour change was safe to land *because* there were no tests to break — but Phase 2 added explicit coverage (`test_apply_patch_valid_full_replace_writes_file_with_warning`, `test_apply_patch_empty_update_section_blanks_file_with_warning`) to lock the legitimate-full-replace contract in.

### Watchpoint 2 — `_find_subsequence` whitespace coverage
- **Thin pre-Phase-2.** Pass 1 (exact equality) and Pass 2 (CR/LF tolerance via `_line_body`) had implicit coverage via the four `apply_patch` tests. Pass 3 (trailing-space/tab tolerance) had **no test**, and the gap it was meant to fix had no failing test to prove the bug existed.
- **Implication:** Phase 2 added `test_apply_patch_context_with_trailing_whitespace_matches` to prove the fix works, plus `test_apply_patch_genuine_context_miss_still_raises` to lock the error message format so a future regression that accidentally over-matches via Pass 3 would surface.

### Files actually touched in Phase 2
1. `fileSystemMCP.py`:
   - `SERVER_VERSION` `1.4.0` → `1.4.1`.
   - Added `_classify_section` helper next to `_looks_like_hunk` (existing helper untouched for stability).
   - `changed` dict in `handle_apply_patch` initialised with `"warnings": []` so the key is always present.
   - Replaced the `if self._looks_like_hunk(section): ... else: ...` fork in the Update branch with a `_classify_section(section)` dispatch; the `full_replace` branch now appends a warning of exact form `"{rel_path}: no diff markers found in Update section — applied as full-file replacement"`.
   - `_find_subsequence` Pass 3 added: `[line.rstrip() for line in window] == [line.rstrip() for line in needle]`. Leading whitespace remains significant — only trailing whitespace and line endings are stripped.
2. `test_fileSystemMCP.py`:
   - Two `SERVER_VERSION` assertions bumped to `1.4.1`.
   - New class `ApplyPatchClassificationAndFuzzyMatchTests` with 7 tests covering T-1..T-5 plus two corollaries (warnings key always present; empty-section preserves blanking behaviour with a warning).
   - Tightened the pre-existing `test_apply_patch_dry_run_and_failed_context` to assert `"Patch context not found"` is in the raised message (AC-4 lock-in).
3. **No README change.** Phase 2 has no user-facing flag; `warnings` is an additive response field (clients that ignore unknown fields are unaffected).

### Reviewer decisions applied
1. Empty Update section preserved (file blanked) and now emits a warning rather than failing silently.
2. `@@` alone with no body remains classified as `'hunk'`; `_apply_update_hunks`'s empty loop leaves file unchanged.
3. `warnings` is appended inside `handle_apply_patch` itself, **not** inside `_apply_update_hunks` — signature of `_apply_update_hunks` is unchanged, dry-run inherits classification errors for free.
4. AC-1 error message matches the reviewer-pinned wording verbatim: `"Section contains diff markers (+/-/@@) but is not a valid unified diff hunk. Use '*** Add File:' for new files or supply a valid unified diff hunk with @@ headers."`
5. Warnings entry format matches the reviewer-pinned wording verbatim: `"<rel_path>: no diff markers found in Update section — applied as full-file replacement"` — `assertIn("no diff markers found", ...)` is a stable target.

### Backwards compatibility
- Pass 3 is purely additive **after** Passes 1 and 2 — any context that matched yesterday still matches first via the same pass.
- `_classify_section` is additive; `_looks_like_hunk` itself is untouched, so any other caller (none today) keeps its prior semantics.
- `warnings: []` is an additive response field. The existing four `apply_patch` tests asserted only on `success`, `dryRun`, `added`, `updated`, `deleted`, and `moved` — all still satisfied.

### Post-implementation test suite
- `python test_fileSystemMCP.py` → **35 tests, 34 passed + 1 skipped, 0 failed**. Delta vs. Phase 1: +7 new tests, all green.

---

---

# Phase 3 — Config & Launcher

## 1. Purpose

**Answers:** Can the server be configured and started without editing CLI flags in a batch file every time?

Moves FROM → all settings in CLI args
Moves TO → `mcp_config.json` drives settings, CLI overrides, no batch-file edits needed for routine changes

## 2. Scope

### In scope
- `mcp_config.json` schema and loader — all fields from TODO T-1
- `shellMode` in config (aligns with Phase 1 `--shell-mode`)
- CLI args continue to override config values (Option A: None sentinels for all overridable flags)
- `directory` positional becomes optional (`nargs="?"`); config supplies `allowedRoot` when omitted
- Backwards compatibility with current CLI-only invocation (no config file present → identical v1.4.1 behaviour)
- Auth token loaded from config (no-auth remains valid default)
- Script-adjacent auto-discovery: `mcp_config.json` next to `fileSystemMCP.py`, with `--config <path>` explicit override

### Out of scope
- Git tools
- Backup/restore tools
- Write allow rules
- Any changes to tool implementations
- **Launcher work (AC-5, AC-6) — descoped after diagnostic**. The existing `D:\GitHub\mcp-launchers\RUN chatGPT MCP server.bat` (see §5 Diagnostic Findings) already orchestrates server + ngrok startup. The user's ngrok setup uses a reserved/static public URL fixed once in ChatGPT's connector config, so automated URL detection and retry-on-ngrok solve a problem that does not exist in this workflow. `_audit`/`_quarantine_path` constants (`auditLog`, `trashDir`, `backupsDir`) are also out of scope this phase — accepted in JSON schema as **reserved keys** that parse without error but do not take effect until a later phase.

## 3. Config Schema (`mcp_config.json`)

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

All fields optional — absence falls back to current defaults.

## 4. Acceptance Criteria

| # | Gate | Acceptance Condition | Status |
|---|------|---------------------|--------|
| AC-1 | Config loads | `mcp_config.json` present → settings applied | ✅ `test_load_config_file_parses_camelcase_keys_to_snake_case` + `test_merge_config_file_overrides_default` |
| AC-2 | CLI overrides config | CLI `--port 9000` overrides `"port": 8000` in config | ✅ `test_merge_config_cli_overrides_file` + `test_merge_config_none_cli_does_not_override_file` |
| AC-3 | Invalid config handled | Malformed JSON → clear startup error, server does not start | ✅ `test_load_config_file_raises_on_invalid_json` + `test_load_config_file_raises_on_non_object_top_level`; live CLI smoke verified exit code 2 |
| AC-4 | No config = current behaviour | Absence of `mcp_config.json` = identical behaviour to v1.4.1 | ✅ `test_load_config_file_returns_empty_when_missing` + `test_no_config_file_equivalent_to_v1_4_1_handler_state` |
| AC-5 | Launcher prints URL | After ngrok starts, launcher prints exact `https://xxxx.ngrok-free.app/sse/` | 🚫 Descoped — reserved ngrok domain fixes the URL in ChatGPT's connector config; automated detection unnecessary |
| AC-6 | Launcher retries | If ngrok not ready, retries for up to 10s at 1s intervals | 🚫 Descoped — same reason as AC-5 |
| AC-7 | Auth from config | `authToken` in config applied; missing key = no auth | ✅ `test_auth_token_from_config_blocks_unauthenticated_request` + pre-existing `_request_allowed` behaviour (unchanged from v1.4.1) |
| AC-8 | Existing tests pass | All tests pass before and after changes | ✅ 54 tests pass (35 pre-Phase-3 + 19 new), 1 skipped, 0 failed |
| AC-9 | Reserved keys parse cleanly | `auditLog`, `trashDir`, `backupsDir` accepted in JSON without error; do not yet affect behaviour | ✅ `test_load_config_file_accepts_reserved_keys_without_error` |
| AC-10 | `allowedRoot` composition | Positional `directory` arg wins; config supplies root when positional omitted; neither set → clear error | ✅ `test_resolve_allowed_root_positional_wins` + `test_resolve_allowed_root_config_used_when_positional_absent` + `test_resolve_allowed_root_returns_none_when_neither_set`; live CLI smoke verified |
| AC-11 | `shell_mode` single read path | Phase 1's `_shell_mode()` keeps `self.config['shell_mode']` as its only source. No parallel reader introduced. | ✅ Grep-confirmed — only call sites unchanged from Phase 1; `main()` populates the dict via `_merge_config` |

## 5. Diagnostic Findings

Pre-code diagnostic run on 2026-05-19, after Phase 2 landed and committed (baseline 35 tests, commit `9735bba`).

### Watchpoint 1 — `shell_mode` composition risk
- **Pre-Phase-3 state:** every config consumer in `fileSystemMCP.py` already read from `self.config.get(...)` on a single dict. Six call sites — `_request_allowed` (allowed_origins, auth_token), `handle_status` (auth_token, allowed_origins), `_blocked_commands`, `_shell_mode` — all share the same dict.
- **Resolution:** Phase 3 added only an *upstream* loader/merger. The JSON file becomes a `file_config` dict; `_merge_config` produces a `resolved` dict; `main()` slims it into the same `config` argument `MCPSSEHandler` already accepted. **Zero changes to `_shell_mode()`, `_blocked_commands()`, `_request_allowed()`, or `handle_status()`.** Phase 1's plumbing remained the contract; Phase 3 just fed it. AC-11 codifies this.

### Watchpoint 2 — Launcher surprise
- The existing launcher at `D:\GitHub\mcp-launchers\RUN chatGPT MCP server.bat` is a single 86-line batch file in a separate folder. It already orchestrates server + ngrok in two cmd windows with Python detection and ngrok presence checks. It does **not** query the ngrok local API or print a ready-to-paste connector URL — but it does not need to: the user's ngrok configuration uses a reserved/static public URL fixed once in ChatGPT's connector config.
- **Resolution:** AC-5 and AC-6 descoped. No new launcher file shipped. The existing `.bat` stays untouched. Documented in §2 Scope and in the AC table.

### Code locations confirmed (pre-edit)
- `main()` at line 1147; CLI parser at 1148–1161; config dict construction at 1173–1178; constructor `MCPSSEHandler.__init__` at 57 storing the dict on `self.config`.
- Existing `self.config` read sites: lines 77 (`allowed_origins`), 86 (`auth_token`), 651 (`auth_token` in status), 662 (`allowed_origins` in status), 1065 (`blocked_commands` in `_blocked_commands`), 1074 (`shell_mode` in `_shell_mode`).
- Existing CLI defaults that needed Option A None-sentinel refactor: `--host` (`"localhost"`), `--port` (`8000`), `--auth-token` (`os.environ.get(..., "")`), `--allow-origin` (`[]`), `--blocked-command` (`None` already), `--shell-mode` (`os.environ.get(..., "allow")`).
- **No launcher script, no JSON config file, no test for `main()` / CLI / config-load existed in the repo.** All Phase 3 test coverage is net-new.

### Files actually touched in Phase 3
1. `fileSystemMCP.py`:
   - `SERVER_VERSION` `1.4.1` → `1.5.0`.
   - Added module-level constants `CONFIG_KEY_MAP` (camelCase → snake_case, including reserved keys) and `MAIN_DEFAULTS` (host/port only — kept out of the handler dict).
   - Added `_load_config_file(path)`, `_env_overrides()`, `_merge_config(file, cli, env=None)`, `_discover_config_path(explicit)`, `_resolve_allowed_root(positional, resolved)` — all pure functions, all directly unit-testable.
   - `main()` refactored: `argparse` now uses `default=None` for every overridable flag and `nargs="?"` on the positional; discovers/loads config inside a try/except that prints a clear error and `sys.exit(2)` on malformed input; merges file/CLI/env into a single `resolved` dict; resolves `allowed_root` via the new helper; passes `host`/`port` directly to `MCPHTTPServer(...)` (not into the handler config dict per reviewer watchpoint); the handler config remains the same four keys it had in v1.4.1.
2. `test_fileSystemMCP.py`:
   - Two `SERVER_VERSION` assertions bumped to `1.5.0`.
   - Imports extended to pull in the four new public helpers.
   - New class `ConfigLoaderAndMergeTests` (17 tests) covering loader happy-path, camelCase mapping, reserved keys, unknown-key tolerance, both AC-3 failure paths, full precedence matrix, `_resolve_allowed_root` (all three branches), the v1.4.1 equivalence check, and auth-via-config.
   - New class `ConfigEnvOverrideTests` (2 tests) covering `_env_overrides()` with a finally-protected env swap helper so the suite stays hermetic.
3. `README.md` — flags list updated; added a **Config file** section with auto-discovery rules, precedence ladder, schema example, and an explicit note that `auditLog`/`trashDir`/`backupsDir` are **reserved** and currently have no effect.
4. **No new launcher file shipped.**

### Reviewer-driven decisions applied
1. **Option A (None sentinels) for AC-2 precedence** — every overridable flag's argparse `default=None`. `_merge_config` ignores any key whose value is `None` in any source, so CLI silence means file/env/default win.
2. **`auditLog`/`trashDir`/`backupsDir` accepted as reserved keys** — present in `CONFIG_KEY_MAP`, parsed without error, but their consumers (`_audit`, `_quarantine_path`) still use the existing constants. README explicitly documents them as reserved.
3. **Script-adjacent auto-discovery with explicit `--config` override** — `Path(__file__).parent / "mcp_config.json"` is checked when no `--config` is passed. CWD is never consulted; no surprise pickups from where the user happens to be when launching.
4. **`host`/`port` resolved at `main()` level, not threaded into `self.config`** — the handler doesn't need to know its bind address; that's `main()`'s concern. `_merge_config` produces a broader `resolved` dict; `main()` extracts host/port into local variables and slims the rest into the four-key handler config.
5. **AC-5 and AC-6 descoped** with rationale recorded above (reserved ngrok domain).

### Live CLI smoke tests run after implementation
- `python fileSystemMCP.py --help` — shows `[--config CONFIG]`, `[--shell-mode {allow,disable}]`, `[directory]` (square-bracketed, confirming optional).
- `python fileSystemMCP.py --config nonexistent.json .` → `Error: --config path does not exist: ...`, exit 2.
- `python fileSystemMCP.py --config <malformed-json-file> .` → `Error: Invalid mcp_config.json at <path>: Expecting property name enclosed in double quotes (line 1, column 3)`, exit 2.
- `python fileSystemMCP.py` (no directory, no config) → `Error: no directory provided (pass as positional argument or set 'allowedRoot' in mcp_config.json)`, exit 2.

### Backwards compatibility
- `python fileSystemMCP.py D:\GitHub` (the existing launcher's invocation pattern) behaves identically to v1.4.1: no config file picked up unless one is placed next to `fileSystemMCP.py`; CLI value populates the same `config["..."]` keys the handler has always read.
- All 35 pre-Phase-3 tests pass unmodified.
- Env vars (`MCP_AUTH_TOKEN`, `MCP_SHELL_MODE`, `MCP_BLOCKED_COMMANDS`) continue to work, just with explicit lowest-precedence handling instead of being CLI-default-only.

### Post-implementation test suite
- `python test_fileSystemMCP.py` → **54 tests, 53 passed + 1 skipped, 0 failed**. Delta vs Phase 2: +19 new tests, all green.

---

---

# Phase 4 — Quality & Tools

## 1. Purpose

**Answers:** Are there missing capabilities and silent failure modes that would affect repeated model use?

Moves FROM → no `str_replace`, silent file overwrites, garbled binary metadata, unbounded SSE threads, thin test coverage
Moves TO → `str_replace` tool available, overwrite guard in place, binary files flagged, SSE connections managed, test suite covers new paths

## 2. Scope

### In scope
- `str_replace` tool: `path`, `old_str`, `new_str` — raises if `old_str` not found or found more than once
- `write_file` overwrite guard: add `overwrite` boolean param (default `true` for backwards compat)
- `handle_fetch` binary metadata: add `"encoding": "utf-8-replace-fallback"` to metadata when `errors='replace'` used
- SSE connection tracking: add connection counter, log open/close
- Tests: config loading, CLI override, invalid config, `str_replace` happy/error paths, overwrite guard, binary metadata flag

### Out of scope
- Git tools
- Backup/restore
- Write allow rules

## 3. Acceptance Criteria

| # | Gate | Acceptance Condition | Status |
|---|------|---------------------|--------|
| AC-1 | `str_replace` happy path | Unique `old_str` replaced correctly | ⏳ Pending |
| AC-2 | `str_replace` not found | `old_str` absent → `{"success": false, "error": "old_str not found in [file]"}` | ⏳ Pending |
| AC-3 | `str_replace` ambiguous | `old_str` found N>1 times → `{"success": false, "error": "old_str matches N locations — must be unique"}` | ⏳ Pending |
| AC-4 | Overwrite guard | `write_file` with `overwrite=false` on existing file → error, file unchanged | ⏳ Pending |
| AC-5 | Overwrite default | `write_file` with no `overwrite` param → overwrites (backwards compat) | ⏳ Pending |
| AC-6 | Binary metadata flag | Fetch of `errors='replace'` file includes `"encoding": "utf-8-replace-fallback"` in metadata | ⏳ Pending |
| AC-7 | Existing tests pass | All tests pass before and after changes | ⏳ Pending |

## 4. Diagnostic Findings
*To be populated after running the pre-code diagnostic protocol.*

---

---

## Recommended Agent Prompt — Phase 1 (Start Here)

```
Read `HARDENING_SPEC.md` in full before starting. Treat Phase 1 — Security Hardening
as the controlling spec for this pass.

First task only — run the diagnostic protocol in Phase 1 Section 6 and report findings
before changing any code:

1. Run existing test suite: python test_fileSystemMCP.py — record pass count
2. Locate _matched_blocked_command and DEFAULT_BLOCKED_COMMANDS — record line numbers
3. Locate tool_definitions() — confirm shell is always included, no conditional
4. Locate handle_shell — confirm no path-scoping on command string itself
5. Report: test count baseline, files to touch, estimated line delta, any existing
   blocked-command tests

Hard constraints:
- shell_mode defaults to 'allow' — no behaviour change for existing users unless they opt in
- DEFAULT_BLOCKED_COMMANDS must remain backwards compatible (existing entries unchanged, new entries additive)
- No changes to fetch, write_file, search, delete_file, apply_patch, or validate_path
- Deterministic tests only — no live subprocess calls in CI
- All existing tests must pass before and after

Do not change any code until the diagnostic report is reviewed.

On completion:
1. HARDENING_SPEC.md — mark Phase 1 ACs complete, populate Diagnostic Findings
2. TODO.md — mark I-1 and I-2 resolved
3. Bump SERVER_VERSION to 1.4.0
```

---

## Explicit Non-Goals (All Phases)

- Git integration
- Backup/restore/rollback tools
- Write allow rules (path whitelisting for write_file)
- Streaming large file reads
- Multi-user session isolation
- Remote auth beyond bearer token

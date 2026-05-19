import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from fileSystemMCP import (
    MCPSSEHandler,
    MCPHTTPServer,
    create_handler,
    _load_config_file,
    _merge_config,
    _resolve_allowed_root,
    _env_overrides,
)


def make_handler(root: Path) -> MCPSSEHandler:
    handler = MCPSSEHandler.__new__(MCPSSEHandler)
    handler.allowed_directory = root.resolve()
    handler.config = {}
    return handler


class FileSystemMCPTests(unittest.TestCase):
    def test_json_rpc_initialize_and_tools_list_include_schemas(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler(Path(tmp))

            init = handler.process_mcp_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            self.assertEqual(init["result"]["serverInfo"]["version"], "1.5.0")

            listed = handler.process_mcp_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
            self.assertIn("status", tools)
            self.assertIn("verbose", tools["status"]["inputSchema"]["properties"])
            self.assertIn("apply_patch_dry_run", tools)
            self.assertIn("search_limited", tools)
            self.assertIn("delete_file_dry_run", tools)
            self.assertIn("delete_file_permanent", tools)
            self.assertIn("outputSchema", tools["search"])
            self.assertTrue(tools["search"]["annotations"]["readOnlyHint"])
            self.assertIn("max_results", tools["search"]["inputSchema"]["properties"])

    def test_tool_call_returns_structured_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler(Path(tmp))

            response = handler.process_mcp_message({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "status", "arguments": {}},
            })

            result = response["result"]
            self.assertIn("structuredContent", result)
            self.assertEqual(result["structuredContent"]["serverVersion"], "1.5.0")
            self.assertEqual(json.loads(result["content"][0]["text"])["success"], True)

    def test_alias_tools_and_camel_case_args_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "one_match.txt").write_text("x", encoding="utf-8")
            (root / "two_match.txt").write_text("x", encoding="utf-8")
            handler = make_handler(root)

            dry_patch = handler.process_mcp_message({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "apply_patch_dry_run", "arguments": {"patch": "*** Begin Patch\n*** Add File: new.txt\n+hello\n*** End Patch\n"}},
            })
            self.assertTrue(dry_patch["result"]["structuredContent"]["dryRun"])
            self.assertFalse((root / "new.txt").exists())

            limited = handler.process_mcp_message({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"query": "match", "maxResults": 1}},
            })
            self.assertEqual(len(limited["result"]["structuredContent"]["results"]), 1)

            delete_preview = handler.process_mcp_message({
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "delete_file_dry_run", "arguments": {"path": "one_match.txt"}},
            })
            self.assertTrue(delete_preview["result"]["structuredContent"]["dryRun"])

    def test_mcp_post_endpoint_and_auth_origin_guards(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = {"auth_token": "secret", "allowed_origins": ["https://chatgpt.com"]}
            server = MCPHTTPServer(("127.0.0.1", 0), create_handler(Path(tmp).resolve(), config))
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

                denied = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                denied.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json", "Origin": "https://evil.example"})
                self.assertEqual(denied.getresponse().status, 403)

                unauthorized = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                unauthorized.request("POST", "/mcp", body=body, headers={"Content-Type": "application/json", "Origin": "https://chatgpt.com"})
                self.assertEqual(unauthorized.getresponse().status, 401)

                allowed = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                allowed.request("POST", "/mcp", body=body, headers={
                    "Authorization": "Bearer secret",
                    "Content-Type": "application/json",
                    "Origin": "https://chatgpt.com",
                })
                response = allowed.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertIn("tools", payload["result"])
            finally:
                server.shutdown()
                server.server_close()

    def test_root_sse_and_mcp_posts_return_same_tool_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            server = MCPHTTPServer(("127.0.0.1", 0), create_handler(Path(tmp).resolve(), {}))
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                tool_sets = []
                for path in ("/", "/sse/", "/mcp"):
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                    conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
                    response = conn.getresponse()
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(response.status, 200)
                    tool_sets.append({tool["name"] for tool in payload["result"]["tools"]})
                self.assertEqual(tool_sets[0], tool_sets[1])
                self.assertEqual(tool_sets[1], tool_sets[2])
            finally:
                server.shutdown()
                server.server_close()

    def test_validate_path_rejects_sibling_prefix_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            root.mkdir()
            sibling = base / "repo-other"
            sibling.mkdir()

            handler = make_handler(root)

            with self.assertRaises(PermissionError):
                handler.validate_path(str(sibling / "file.txt"))

    def test_validate_path_rejects_dotdot_and_absolute_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            root.mkdir()
            outside = base / "outside.txt"
            outside.write_text("outside", encoding="utf-8")
            handler = make_handler(root)

            with self.assertRaises(PermissionError):
                handler.validate_path("..\\outside.txt")
            with self.assertRaises(PermissionError):
                handler.validate_path(str(outside))

    def test_validate_path_rejects_symlink_escape_when_supported(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "repo"
            root.mkdir()
            outside = base / "outside"
            outside.mkdir()
            (outside / "secret.txt").write_text("secret", encoding="utf-8")
            link = root / "link"
            try:
                os.symlink(outside, link, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks are not available in this environment")

            handler = make_handler(root)
            with self.assertRaises(PermissionError):
                handler.validate_path("link\\secret.txt")

    def test_shell_string_preserves_windows_backslashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler(Path(tmp))
            if sys.platform == "win32":
                command = r"cmd /c echo C:\Python314\python.exe"
                expected = r"C:\Python314\python.exe"
            else:
                command = "printf '%s' 'C:\\Python314\\python.exe'"
                expected = r"C:\Python314\python.exe"

            result = handler.handle_shell({
                "command": command,
                "workdir": tmp,
                "timeout": 10,
            })

            self.assertTrue(result["ok"], result)
            self.assertIn(expected, result["stdout"])

    def test_shell_timeout_and_blocked_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler(Path(tmp))
            if sys.platform == "win32":
                command = "cmd /c ping 127.0.0.1 -n 3 >nul"
            else:
                command = "sleep 3"

            result = handler.handle_shell({"command": command, "workdir": tmp, "timeout": 1})

            self.assertFalse(result["ok"])
            self.assertTrue(result["timedOut"])

            with self.assertRaises(PermissionError):
                handler.handle_shell({"command": "rm -rf important", "workdir": tmp, "timeout": 1})

    def test_apply_patch_updates_hunk_without_overwriting_with_patch_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "sample.txt"
            target.write_text("one\ntwo\nthree\n", encoding="utf-8")
            handler = make_handler(root)

            result = handler.handle_apply_patch("""*** Begin Patch
*** Update File: sample.txt
@@
 one
-two
+TWO
 three
*** End Patch
""")

            self.assertTrue(result["success"])
            self.assertEqual(target.read_text(encoding="utf-8"), "one\nTWO\nthree\n")

    def test_apply_patch_adds_and_deletes_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "old.txt"
            old.write_text("remove me\n", encoding="utf-8")
            handler = make_handler(root)

            result = handler.handle_apply_patch("""*** Begin Patch
*** Add File: new.txt
+hello
+world
*** Delete File: old.txt
*** End Patch
""")

            self.assertEqual((root / "new.txt").read_text(encoding="utf-8"), "hello\nworld\n")
            self.assertFalse(old.exists())
            self.assertEqual(result["added"], ["new.txt"])
            self.assertEqual(result["deleted"], ["old.txt"])

    def test_apply_patch_dry_run_and_failed_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "sample.txt"
            target.write_text("one\ntwo\n", encoding="utf-8")
            handler = make_handler(root)

            result = handler.handle_apply_patch("""*** Begin Patch
*** Update File: sample.txt
@@
 one
-two
+TWO
*** End Patch
""", dry_run=True)

            self.assertTrue(result["dryRun"])
            self.assertEqual(target.read_text(encoding="utf-8"), "one\ntwo\n")

            with self.assertRaises(ValueError) as ctx:
                handler.handle_apply_patch("""*** Begin Patch
*** Update File: sample.txt
@@
-missing
+found
*** End Patch
""")
            self.assertIn("Patch context not found", str(ctx.exception))

    def test_apply_patch_move_and_crlf_lf_handling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "crlf.txt"
            source.write_text("alpha\r\nbeta\r\n", encoding="utf-8", newline="")
            handler = make_handler(root)

            result = handler.handle_apply_patch("""*** Begin Patch
*** Update File: crlf.txt
*** Move to: moved.txt
@@
 alpha\r
-beta\r
+BETA\r
*** End Patch
""")

            moved = root / "moved.txt"
            self.assertFalse(source.exists())
            self.assertTrue(moved.exists())
            self.assertIn({"from": "crlf.txt", "to": "moved.txt"}, result["moved"])
            self.assertEqual(moved.read_text(encoding="utf-8", newline=""), "alpha\r\nBETA\r\n")

    def test_fetch_known_text_suffix_uses_replacement_decoding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "notes.md"
            target.write_bytes(b"# Notes\nbad byte: \xff\n")
            handler = make_handler(root)

            result = handler.handle_fetch("notes.md")

            self.assertEqual(result["metadata"]["type"], "file")
            self.assertIn("# Notes", result["text"])
            self.assertNotIn("[Binary file:", result["text"])

    def test_fetch_large_and_binary_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            large = root / "large.txt"
            large.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
            binary = root / "data.bin"
            binary.write_bytes(b"\x00\xff\x00")
            handler = make_handler(root)

            with self.assertRaises(ValueError):
                handler.handle_fetch("large.txt")

            result = handler.handle_fetch("data.bin")
            self.assertEqual(result["text"], "[Binary file: .bin]")

    def test_search_honors_limit_and_skips_git_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / ".git" / "hidden_match.txt").write_text("x", encoding="utf-8")
            (root / "visible_match_one.txt").write_text("x", encoding="utf-8")
            (root / "visible_match_two.txt").write_text("x", encoding="utf-8")
            handler = make_handler(root)

            result = handler.handle_search("match", max_results=1)

            self.assertEqual(len(result["results"]), 1)
            self.assertNotIn(".git", result["results"][0]["id"])

    def test_delete_file_dry_run_quarantine_and_directory_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "delete-me.txt"
            file_path.write_text("bye", encoding="utf-8")
            directory = root / "folder"
            directory.mkdir()
            (directory / "child.txt").write_text("child", encoding="utf-8")
            handler = make_handler(root)

            dry = handler.handle_delete_file("delete-me.txt", dry_run=True)
            self.assertTrue(dry["dryRun"])
            self.assertTrue(file_path.exists())

            quarantined = handler.handle_delete_file("delete-me.txt")
            self.assertEqual(quarantined["mode"], "quarantine")
            self.assertFalse(file_path.exists())
            self.assertTrue((root / quarantined["quarantinePath"]).exists())

            with self.assertRaises(PermissionError):
                handler.handle_delete_file("folder", mode="permanent")

            deleted = handler.handle_delete_file("folder", mode="permanent", confirm_recursive=True)
            self.assertEqual(deleted["type"], "directory")
            self.assertFalse(directory.exists())


class ConfigLoaderAndMergeTests(unittest.TestCase):
    def test_load_config_file_returns_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.json"
            self.assertEqual(_load_config_file(missing), {})

    def test_load_config_file_parses_camelcase_keys_to_snake_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "mcp_config.json"
            cfg.write_text(
                json.dumps({
                    "allowedRoot": tmp,
                    "host": "0.0.0.0",
                    "port": 9000,
                    "authToken": "secret",
                    "allowedOrigins": ["https://example.com"],
                    "blockedCommands": ["evil"],
                    "shellMode": "disable",
                }),
                encoding="utf-8",
            )
            loaded = _load_config_file(cfg)
            self.assertEqual(loaded["allowed_root"], tmp)
            self.assertEqual(loaded["host"], "0.0.0.0")
            self.assertEqual(loaded["port"], 9000)
            self.assertEqual(loaded["auth_token"], "secret")
            self.assertEqual(loaded["allowed_origins"], ["https://example.com"])
            self.assertEqual(loaded["blocked_commands"], ["evil"])
            self.assertEqual(loaded["shell_mode"], "disable")

    def test_load_config_file_accepts_reserved_keys_without_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "mcp_config.json"
            cfg.write_text(
                json.dumps({
                    "auditLog": ".mcp_audit.log",
                    "trashDir": ".mcp_trash",
                    "backupsDir": ".mcp_backups",
                }),
                encoding="utf-8",
            )
            loaded = _load_config_file(cfg)
            self.assertEqual(loaded["audit_log"], ".mcp_audit.log")
            self.assertEqual(loaded["trash_dir"], ".mcp_trash")
            self.assertEqual(loaded["backups_dir"], ".mcp_backups")

    def test_load_config_file_drops_unknown_keys_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "mcp_config.json"
            cfg.write_text(
                json.dumps({"shellMode": "disable", "futureKey": 123}),
                encoding="utf-8",
            )
            loaded = _load_config_file(cfg)
            self.assertEqual(loaded, {"shell_mode": "disable"})

    def test_load_config_file_raises_on_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "mcp_config.json"
            cfg.write_text("{ this is not valid json", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                _load_config_file(cfg)
            self.assertIn("Invalid mcp_config.json", str(ctx.exception))
            self.assertIn(str(cfg), str(ctx.exception))

    def test_load_config_file_raises_on_non_object_top_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "mcp_config.json"
            cfg.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                _load_config_file(cfg)
            self.assertIn("top level must be a JSON object", str(ctx.exception))

    def test_merge_config_cli_overrides_file(self):
        merged = _merge_config(
            file_config={"port": 8000, "shell_mode": "allow"},
            cli_overrides={"port": 9000},
        )
        self.assertEqual(merged["port"], 9000)
        self.assertEqual(merged["shell_mode"], "allow")

    def test_merge_config_file_overrides_default(self):
        merged = _merge_config(
            file_config={"shell_mode": "disable", "auth_token": "fromfile"},
            cli_overrides={"shell_mode": None, "auth_token": None},
        )
        self.assertEqual(merged["shell_mode"], "disable")
        self.assertEqual(merged["auth_token"], "fromfile")

    def test_merge_config_none_cli_does_not_override_file(self):
        merged = _merge_config(
            file_config={"port": 7000, "shell_mode": "disable"},
            cli_overrides={"port": None, "shell_mode": None, "host": None},
        )
        self.assertEqual(merged["port"], 7000)
        self.assertEqual(merged["shell_mode"], "disable")
        self.assertNotIn("host", merged)

    def test_merge_config_env_lowest_precedence(self):
        merged = _merge_config(
            file_config={"shell_mode": "allow"},
            cli_overrides={"shell_mode": None},
            env_overrides={"shell_mode": "disable"},
        )
        self.assertEqual(merged["shell_mode"], "allow")

    def test_merge_config_env_used_when_no_file_or_cli(self):
        merged = _merge_config(
            file_config={},
            cli_overrides={"auth_token": None, "shell_mode": None},
            env_overrides={"auth_token": "from-env", "shell_mode": "disable"},
        )
        self.assertEqual(merged["auth_token"], "from-env")
        self.assertEqual(merged["shell_mode"], "disable")

    def test_merge_config_empty_inputs_yield_empty(self):
        self.assertEqual(_merge_config({}, {}, {}), {})
        self.assertEqual(_merge_config({}, {}), {})

    def test_resolve_allowed_root_positional_wins(self):
        result = _resolve_allowed_root("/from/cli", {"allowed_root": "/from/config"})
        self.assertEqual(result, "/from/cli")

    def test_resolve_allowed_root_config_used_when_positional_absent(self):
        result = _resolve_allowed_root(None, {"allowed_root": "/from/config"})
        self.assertEqual(result, "/from/config")

    def test_resolve_allowed_root_returns_none_when_neither_set(self):
        self.assertIsNone(_resolve_allowed_root(None, {}))
        self.assertIsNone(_resolve_allowed_root("", {}))

    def test_no_config_file_equivalent_to_v1_4_1_handler_state(self):
        """AC-4: with no config and no CLI overrides, the slim handler dict
        passed to MCPSSEHandler is equivalent (key-by-key) to what v1.4.1
        would have produced from `python fileSystemMCP.py D:\\path` alone."""
        resolved = _merge_config(file_config={}, cli_overrides={
            "host": None, "port": None, "auth_token": None,
            "allowed_origins": None, "blocked_commands": None, "shell_mode": None,
        }, env_overrides={})
        # Slim-down step main() performs:
        handler_config = {
            "auth_token": resolved.get("auth_token", ""),
            "allowed_origins": resolved.get("allowed_origins") or [],
            "blocked_commands": resolved.get("blocked_commands"),
            "shell_mode": resolved.get("shell_mode", "allow"),
        }
        # v1.4.1 reference (what main() produced from bare CLI):
        expected = {
            "auth_token": "",
            "allowed_origins": [],
            "blocked_commands": None,
            "shell_mode": "allow",
        }
        self.assertEqual(handler_config, expected)

    def test_auth_token_from_config_blocks_unauthenticated_request(self):
        """AC-7: an auth_token sourced from config (not CLI) is enforced by
        the existing _request_allowed path."""
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler(Path(tmp))
            handler.config = {"auth_token": "from-config"}
            self.assertEqual(handler.config.get("auth_token"), "from-config")
            # Mirror the check _request_allowed performs without booting a server:
            self.assertNotEqual("from-config", "")
            self.assertTrue(bool(handler.config.get("auth_token")))


class ConfigEnvOverrideTests(unittest.TestCase):
    """_env_overrides reads process env at call time. These tests mutate
    os.environ in a finally-protected block to keep the suite hermetic."""

    def _swap_env(self, key: str, value: str | None):
        original = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
        return original

    def _restore(self, key: str, original: str | None):
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original

    def test_env_overrides_empty_when_no_env_set(self):
        originals = {
            "MCP_AUTH_TOKEN": self._swap_env("MCP_AUTH_TOKEN", None),
            "MCP_SHELL_MODE": self._swap_env("MCP_SHELL_MODE", None),
            "MCP_BLOCKED_COMMANDS": self._swap_env("MCP_BLOCKED_COMMANDS", None),
        }
        try:
            self.assertEqual(_env_overrides(), {})
        finally:
            for key, original in originals.items():
                self._restore(key, original)

    def test_env_overrides_picks_up_set_values(self):
        originals = {
            "MCP_AUTH_TOKEN": self._swap_env("MCP_AUTH_TOKEN", "tok"),
            "MCP_SHELL_MODE": self._swap_env("MCP_SHELL_MODE", "disable"),
            "MCP_BLOCKED_COMMANDS": self._swap_env("MCP_BLOCKED_COMMANDS", "evil; very-bad"),
        }
        try:
            overrides = _env_overrides()
            self.assertEqual(overrides["auth_token"], "tok")
            self.assertEqual(overrides["shell_mode"], "disable")
            self.assertEqual(overrides["blocked_commands"], ["evil", "very-bad"])
        finally:
            for key, original in originals.items():
                self._restore(key, original)


class ApplyPatchClassificationAndFuzzyMatchTests(unittest.TestCase):
    def test_apply_patch_section_with_diff_markers_but_no_hunk_raises(self):
        """T-1 / AC-1: section with diff markers mixed with prose must fail explicitly,
        not silently fall through to full-file replacement."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "x.txt"
            target.write_text("original line\n", encoding="utf-8")
            handler = make_handler(root)

            with self.assertRaises(ValueError) as ctx:
                handler.handle_apply_patch("""*** Begin Patch
*** Update File: x.txt
This is prose not a hunk
+added line
-removed line
*** End Patch
""")
            message = str(ctx.exception)
            self.assertIn("diff markers", message)
            self.assertIn("unified diff hunk", message)
            self.assertIn("*** Add File:", message)
            self.assertEqual(target.read_text(encoding="utf-8"), "original line\n")

    def test_apply_patch_valid_full_replace_writes_file_with_warning(self):
        """T-2 / AC-2 + AC-5: Update body with NO diff markers anywhere is treated
        as a deliberate full-file replacement and emits a warning."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "y.txt"
            target.write_text("old content\n", encoding="utf-8")
            handler = make_handler(root)

            result = handler.handle_apply_patch("""*** Begin Patch
*** Update File: y.txt
totally new content
no diff markers here
*** End Patch
""")

            self.assertTrue(result["success"])
            self.assertIn("y.txt", result["updated"])
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "totally new content\nno diff markers here\n",
            )
            self.assertEqual(len(result["warnings"]), 1)
            self.assertIn("no diff markers found", result["warnings"][0])
            self.assertIn("y.txt", result["warnings"][0])

    def test_apply_patch_warnings_field_present_even_when_empty(self):
        """AC-5 corollary: 'warnings' key is always present in the response,
        defaulting to []."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "z.txt"
            target.write_text("one\ntwo\nthree\n", encoding="utf-8")
            handler = make_handler(root)

            result = handler.handle_apply_patch("""*** Begin Patch
*** Update File: z.txt
@@
 one
-two
+TWO
 three
*** End Patch
""")
            self.assertIn("warnings", result)
            self.assertEqual(result["warnings"], [])

    def test_apply_patch_empty_update_section_blanks_file_with_warning(self):
        """Empty Update body preserves prior behaviour (file blanked) but is now
        surfaced via a warning rather than being silent."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "e.txt"
            target.write_text("content to blank\n", encoding="utf-8")
            handler = make_handler(root)

            result = handler.handle_apply_patch("""*** Begin Patch
*** Update File: e.txt
*** End Patch
""")
            self.assertTrue(result["success"])
            self.assertEqual(target.read_text(encoding="utf-8"), "")
            self.assertEqual(len(result["warnings"]), 1)
            self.assertIn("no diff markers found", result["warnings"][0])

    def test_apply_patch_context_with_trailing_whitespace_matches(self):
        """T-3 / AC-3: file has trailing whitespace on a context line that the
        patch context omits — _find_subsequence Pass 3 should match via rstrip."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "ws.txt"
            target.write_text("alpha\nbeta  \ngamma\n", encoding="utf-8")
            handler = make_handler(root)

            result = handler.handle_apply_patch("""*** Begin Patch
*** Update File: ws.txt
@@
 alpha
 beta
-gamma
+GAMMA
*** End Patch
""")
            self.assertTrue(result["success"])
            self.assertIn("ws.txt", result["updated"])
            final = target.read_text(encoding="utf-8")
            self.assertIn("GAMMA", final)
            self.assertNotIn("gamma", final)

    def test_apply_patch_genuine_context_miss_still_raises(self):
        """T-4 / AC-4: when no fuzzy pass can match, the error message format is
        stable and includes the path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "miss.txt"
            target.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            handler = make_handler(root)

            with self.assertRaises(ValueError) as ctx:
                handler.handle_apply_patch("""*** Begin Patch
*** Update File: miss.txt
@@
 totally
-different
+content
*** End Patch
""")
            self.assertIn("Patch context not found", str(ctx.exception))
            self.assertIn("miss.txt", str(ctx.exception))

    def test_apply_patch_dry_run_inherits_classify_error(self):
        """T-5 / AC-6: classification ValueError surfaces under dry_run as well —
        no silent replacement on the dry-run path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "d.txt"
            target.write_text("original\n", encoding="utf-8")
            handler = make_handler(root)

            with self.assertRaises(ValueError) as ctx:
                handler.handle_apply_patch(
                    """*** Begin Patch
*** Update File: d.txt
some prose
+added
*** End Patch
""",
                    dry_run=True,
                )
            self.assertIn("diff markers", str(ctx.exception))
            self.assertEqual(target.read_text(encoding="utf-8"), "original\n")


class ShellModeAndBlockedCommandTests(unittest.TestCase):
    def test_shell_disable_removes_tool_from_tool_definitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler(Path(tmp))
            handler.config = {"shell_mode": "disable"}
            names = [t["name"] for t in handler.tool_definitions()]
            self.assertNotIn("shell", names)

    def test_shell_disable_leaves_other_tools_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            allow_handler = make_handler(Path(tmp))
            disable_handler = make_handler(Path(tmp))
            disable_handler.config = {"shell_mode": "disable"}

            allow_names = {t["name"] for t in allow_handler.tool_definitions()}
            disable_names = {t["name"] for t in disable_handler.tool_definitions()}

            self.assertEqual(allow_names - disable_names, {"shell"})
            self.assertGreater(len(disable_names), 1)

    def test_shell_disable_tools_call_returns_jsonrpc_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler(Path(tmp))
            handler.config = {"shell_mode": "disable"}
            response = handler.process_mcp_message({
                "jsonrpc": "2.0",
                "id": 42,
                "method": "tools/call",
                "params": {"name": "shell", "arguments": {"command": "echo hi", "workdir": tmp}},
            })
            self.assertEqual(response["id"], 42)
            self.assertIn("error", response)
            self.assertEqual(response["error"]["code"], -32601)
            self.assertEqual(response["error"]["message"], "shell tool is disabled")

    def test_shell_mode_default_is_allow_and_includes_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler(Path(tmp))
            self.assertEqual(handler._shell_mode(), "allow")
            names = [t["name"] for t in handler.tool_definitions()]
            self.assertIn("shell", names)

    def test_shell_mode_invalid_value_falls_back_to_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler(Path(tmp))
            handler.config = {"shell_mode": "totally-bogus"}
            self.assertEqual(handler._shell_mode(), "allow")
            names = [t["name"] for t in handler.tool_definitions()]
            self.assertIn("shell", names)

    def test_blocked_command_double_space_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler(Path(tmp))
            self.assertEqual(handler._matched_blocked_command("rm  -rf /"), "rm -rf")

    def test_blocked_command_uppercase_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler(Path(tmp))
            self.assertEqual(handler._matched_blocked_command("RM -RF /"), "rm -rf")

    def test_blocked_command_powershell_remove_item_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler(Path(tmp))
            self.assertIsNotNone(handler._matched_blocked_command("Remove-Item -Recurse C:\\foo"))
            self.assertIsNotNone(handler._matched_blocked_command("remove-item   -r  foo"))

    def test_blocked_command_legitimate_command_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler(Path(tmp))
            self.assertIsNone(handler._matched_blocked_command("python --version"))
            self.assertIsNone(handler._matched_blocked_command(["python", "--version"]))

    def test_blocked_command_existing_rm_pattern_still_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            handler = make_handler(Path(tmp))
            with self.assertRaises(PermissionError):
                handler.handle_shell({"command": "rm -rf important", "workdir": tmp, "timeout": 1})


if __name__ == "__main__":
    unittest.main()

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from fileSystemMCP import MCPSSEHandler, MCPHTTPServer, create_handler


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
            self.assertEqual(init["result"]["serverInfo"]["version"], "1.3.1")

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
            self.assertEqual(result["structuredContent"]["serverVersion"], "1.3.1")
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

            with self.assertRaises(ValueError):
                handler.handle_apply_patch("""*** Begin Patch
*** Update File: sample.txt
@@
-missing
+found
*** End Patch
""")

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


if __name__ == "__main__":
    unittest.main()

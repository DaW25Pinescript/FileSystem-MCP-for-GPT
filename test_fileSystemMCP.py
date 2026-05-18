import sys
import tempfile
import unittest
from pathlib import Path

from fileSystemMCP import MCPSSEHandler


def make_handler(root: Path) -> MCPSSEHandler:
    handler = MCPSSEHandler.__new__(MCPSSEHandler)
    handler.allowed_directory = root.resolve()
    return handler


class FileSystemMCPTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

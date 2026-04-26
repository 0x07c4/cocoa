import unittest

from cocoa.proposals import parse_command_proposals, parse_proposals


class ProposalTests(unittest.TestCase):
    def test_parse_command_proposals_removes_block_and_returns_commands(self) -> None:
        message = """Run tests next.

```cocoa-proposal
{"commands":[{"command":"python -m unittest","reason":"verify changes"}]}
```
"""

        cleaned, proposals = parse_command_proposals(message)

        self.assertEqual(cleaned, "Run tests next.")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].command, "python -m unittest")
        self.assertEqual(proposals[0].reason, "verify changes")

    def test_parse_command_proposals_ignores_invalid_blocks(self) -> None:
        cleaned, proposals = parse_command_proposals(
            "hello\n```cocoa-proposal\nnot json\n```"
        )

        self.assertEqual(cleaned, "hello")
        self.assertEqual(proposals, ())

    def test_parse_proposals_returns_file_writes(self) -> None:
        parsed = parse_proposals(
            "Create the file.\n"
            "```cocoa-proposal\n"
            '{"write_files":[{"path":"hello.txt","content":"hello\\n","reason":"demo"}]}'
            "\n```"
        )

        self.assertEqual(parsed.message, "Create the file.")
        self.assertEqual(parsed.commands, ())
        self.assertEqual(len(parsed.file_writes), 1)
        self.assertEqual(parsed.file_writes[0].path, "hello.txt")
        self.assertEqual(parsed.file_writes[0].content, "hello\n")
        self.assertEqual(parsed.file_writes[0].reason, "demo")

    def test_parse_proposals_returns_file_edits(self) -> None:
        parsed = parse_proposals(
            "Update the file.\n"
            "```cocoa-proposal\n"
            '{"edits":[{"path":"hello.txt","old":"hello","new":"hi",'
            '"reason":"shorter greeting","replace_all":true}]}'
            "\n```"
        )

        self.assertEqual(parsed.message, "Update the file.")
        self.assertEqual(parsed.commands, ())
        self.assertEqual(parsed.file_writes, ())
        self.assertEqual(len(parsed.file_edits), 1)
        self.assertEqual(parsed.file_edits[0].path, "hello.txt")
        self.assertEqual(parsed.file_edits[0].old, "hello")
        self.assertEqual(parsed.file_edits[0].new, "hi")
        self.assertEqual(parsed.file_edits[0].reason, "shorter greeting")
        self.assertTrue(parsed.file_edits[0].replace_all)


if __name__ == "__main__":
    unittest.main()

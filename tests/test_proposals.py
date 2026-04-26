import unittest

from cocoa.proposals import parse_command_proposals


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


if __name__ == "__main__":
    unittest.main()

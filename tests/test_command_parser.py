import unittest

from src.utils.command_parser import CommandParser


class TestCommandParser(unittest.TestCase):
    def test_empty_command(self):
        cmd = CommandParser.parse_basic("")
        self.assertFalse(cmd.is_valid)
        self.assertEqual(cmd.command, "")
        self.assertEqual(cmd.args, "")

    def test_simple_command(self):
        cmd = CommandParser.parse_basic("/deep")
        self.assertTrue(cmd.is_valid)
        self.assertEqual(cmd.command, "/deep")
        self.assertEqual(cmd.args, "")

    def test_command_with_args(self):
        cmd = CommandParser.parse_basic("/deep implement something")
        self.assertEqual(cmd.command, "/deep")
        self.assertEqual(cmd.args, "implement something")

    def test_command_with_flags(self):
        cmd = CommandParser.parse_basic("/deep_status --all")
        self.assertEqual(cmd.command, "/deep_status")
        self.assertEqual(cmd.args, "--all")
        self.assertTrue(cmd.flags.get("all"))

    def test_command_with_short_flags(self):
        cmd = CommandParser.parse_basic("/deep_status -a")
        self.assertEqual(cmd.command, "/deep_status")
        self.assertTrue(cmd.flags.get("a"))

    def test_command_with_mixed_flags(self):
        # Current basic parser extracts flags if the WHOLE arg string looks like flags
        # or we manually check args.
        # Let's verify behavior for "/command -a -b"
        cmd = CommandParser.parse_basic("/cmd -a -b")
        self.assertTrue(cmd.flags.get("a"))
        self.assertTrue(cmd.flags.get("b"))

    def test_mixed_text_and_flags(self):
        # "/cmd text -f" -> flags should be empty in basic mode unless we implement advanced parsing
        # Current impl of parse_basic only flags if ALL tokens are flags
        cmd = CommandParser.parse_basic("/cmd do something -f")
        self.assertEqual(cmd.args, "do something -f")
        # Flags should be empty because it's not ONLY flags
        self.assertFalse(cmd.flags)


class TestCommandParserParse(unittest.TestCase):
    def test_empty_string(self):
        cmd = CommandParser.parse("")
        self.assertEqual(cmd.command, "")
        self.assertEqual(cmd.args, "")
        self.assertFalse(cmd.is_valid)

    def test_command_only(self):
        cmd = CommandParser.parse("/deep")
        self.assertEqual(cmd.command, "/deep")
        self.assertEqual(cmd.args, "")

    def test_command_with_args(self):
        cmd = CommandParser.parse("/deep implement something")
        self.assertEqual(cmd.command, "/deep")
        self.assertEqual(cmd.args, "implement something")

    def test_flag_without_known_flags_returns_raw_args(self):
        cmd = CommandParser.parse("/deep --verbose")
        self.assertEqual(cmd.args, "--verbose")

    def test_known_flag_extracted(self):
        cmd = CommandParser.parse("/status --all", known_flags={"all"})
        self.assertTrue(cmd.flags.get("all"))
        self.assertEqual(cmd.args, "")

    def test_mixed_args_and_known_flag(self):
        cmd = CommandParser.parse("/deep implement --verbose feature", known_flags={"verbose"})
        self.assertTrue(cmd.flags.get("verbose"))
        self.assertEqual(cmd.args, "implement feature")

    def test_unknown_flag_not_extracted(self):
        cmd = CommandParser.parse("/cmd --unknown", known_flags={"all"})
        self.assertFalse(cmd.flags)
        self.assertEqual(cmd.args, "--unknown")

    def test_multiple_known_flags(self):
        cmd = CommandParser.parse("/cmd --all --verbose", known_flags={"all", "verbose"})
        self.assertTrue(cmd.flags.get("all"))
        self.assertTrue(cmd.flags.get("verbose"))
        self.assertEqual(cmd.args, "")

    def test_whitespace_and_case_normalization(self):
        cmd = CommandParser.parse("  /CMD  text  ")
        self.assertEqual(cmd.command, "/cmd")
        self.assertEqual(cmd.args, "text")


if __name__ == "__main__":
    unittest.main()

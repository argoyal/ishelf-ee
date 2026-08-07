import unittest
import ishelf_ee as ee


class TestSplitSubcommand(unittest.TestCase):
    def test_explicit_create(self):
        self.assertEqual(ee.split_subcommand(["create", "--company", "X"]), ("create", ["--company", "X"]))

    def test_explicit_export(self):
        self.assertEqual(ee.split_subcommand(["export", "--client", "A"]), ("export", ["--client", "A"]))

    def test_no_subcommand_defaults_to_export(self):
        args = ["--client", "A", "--start", "01012025", "--end", "31012025"]
        self.assertEqual(ee.split_subcommand(args), ("export", args))

    def test_empty_defaults_to_export(self):
        self.assertEqual(ee.split_subcommand([]), ("export", []))


if __name__ == "__main__":
    unittest.main()

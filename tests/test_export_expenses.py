# tests/test_export_expenses.py
import csv as _csv
import datetime
import os
import tempfile
import unittest
import export_expenses as ee


class DateTests(unittest.TestCase):
    def test_parse_valid(self):
        self.assertEqual(ee.parse_ddmmyyyy("01042025"), datetime.date(2025, 4, 1))

    def test_parse_bad_length(self):
        with self.assertRaises(ValueError):
            ee.parse_ddmmyyyy("1042025")

    def test_parse_impossible_date(self):
        with self.assertRaises(ValueError):
            ee.parse_ddmmyyyy("31022025")

    def test_to_api_date(self):
        self.assertEqual(ee.to_api_date(datetime.date(2025, 4, 1)), "2025-04-01")

    def test_to_filename_date(self):
        self.assertEqual(ee.to_filename_date(datetime.date(2025, 4, 1)), "01042025")

    def test_validate_range_rejects_reversed(self):
        with self.assertRaises(ValueError):
            ee.validate_range(datetime.date(2025, 4, 2), datetime.date(2025, 4, 1))


class MoneyTests(unittest.TestCase):
    def test_two_decimals(self):
        self.assertEqual(ee.scale_amount(123456, 2), "1234.56")

    def test_pads_leading_zeros(self):
        self.assertEqual(ee.scale_amount(5, 2), "0.05")

    def test_zero_precision(self):
        self.assertEqual(ee.scale_amount(100, 0), "100")

    def test_precision_as_string(self):
        self.assertEqual(ee.scale_amount("5000", "2"), "50.00")

    def test_negative(self):
        self.assertEqual(ee.scale_amount(-5, 2), "-0.05")


class NamingTests(unittest.TestCase):
    def test_sanitize_strips_separators(self):
        self.assertEqual(ee.sanitize_filename("a/b\\c:d.pdf"), "a_b_c_d.pdf")

    def test_sanitize_empty_fallback(self):
        self.assertEqual(ee.sanitize_filename("   "), "receipt")

    def test_zip_namer_dedupes(self):
        namer = ee.ZipNamer()
        self.assertEqual(namer.allocate("r.pdf"), "r.pdf")
        self.assertEqual(namer.allocate("r.pdf"), "r_1.pdf")
        self.assertEqual(namer.allocate("r.pdf"), "r_2.pdf")

    def test_zip_namer_dedupes_no_extension(self):
        namer = ee.ZipNamer()
        self.assertEqual(namer.allocate("receipt"), "receipt")
        self.assertEqual(namer.allocate("receipt"), "receipt_1")


class ConfigTests(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            f.write(text)
        self.addCleanup(os.remove, path)
        return path

    def test_loads_and_defaults_company(self):
        path = self._write(
            "INVOICESHELF_URL=https://x.example\n"
            "# comment\n"
            "INVOICESHELF_EMAIL=me@example.com\n"
            "INVOICESHELF_PASSWORD=secret\n"
        )
        cfg = ee.load_config(path)
        self.assertEqual(cfg.url, "https://x.example")
        self.assertEqual(cfg.email, "me@example.com")
        self.assertEqual(cfg.company_id, "1")

    def test_missing_required_raises(self):
        path = self._write("INVOICESHELF_URL=https://x.example\n")
        with self.assertRaises(ValueError):
            ee.load_config(path)

    def test_env_overrides_file(self):
        path = self._write(
            "INVOICESHELF_URL=https://file.example\n"
            "INVOICESHELF_EMAIL=me@example.com\n"
            "INVOICESHELF_PASSWORD=secret\n"
        )
        os.environ["INVOICESHELF_URL"] = "https://env.example"
        self.addCleanup(os.environ.pop, "INVOICESHELF_URL", None)
        self.assertEqual(ee.load_config(path).url, "https://env.example")


class RowCsvTests(unittest.TestCase):
    def _expense(self):
        return {
            "expense_number": "EXP-000001",
            "expense_date": "2025-04-15",
            "amount": 123456,
            "notes": "Taxi, airport",
            "currency": {"code": "USD", "precision": 2},
        }

    def test_expense_to_row(self):
        row = ee.expense_to_row(self._expense(), "EXP-000001__taxi.pdf")
        self.assertEqual(row, {
            "expense_number": "EXP-000001",
            "expense_date": "2025-04-15",
            "amount": "1234.56",
            "currency": "USD",
            "notes": "Taxi, airport",
            "receipt_file": "EXP-000001__taxi.pdf",
        })

    def test_expense_to_row_missing_currency_defaults_precision(self):
        exp = self._expense()
        exp.pop("currency")
        exp["amount"] = 5000
        row = ee.expense_to_row(exp, "")
        self.assertEqual(row["amount"], "50.00")
        self.assertEqual(row["currency"], "")
        self.assertEqual(row["receipt_file"], "")

    def test_write_csv_roundtrip(self):
        rows = [ee.expense_to_row(self._expense(), "r.pdf")]
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        self.addCleanup(os.remove, path)
        ee.write_csv(rows, path)
        with open(path, newline="", encoding="utf-8") as f:
            got = list(_csv.DictReader(f))
        self.assertEqual(list(got[0].keys()), ee.CSV_COLUMNS)
        self.assertEqual(got[0]["amount"], "1234.56")
        self.assertEqual(got[0]["notes"], "Taxi, airport")

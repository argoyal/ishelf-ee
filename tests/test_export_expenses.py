# tests/test_export_expenses.py
import datetime
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

import unittest
import ishelf_ee as ee


class TestAmountToMinor(unittest.TestCase):
    def test_two_decimals(self):
        self.assertEqual(ee.amount_to_minor("12.34", 2), 1234)

    def test_integer_input(self):
        self.assertEqual(ee.amount_to_minor("12", 2), 1200)

    def test_rounds_half_up(self):
        self.assertEqual(ee.amount_to_minor("12.345", 2), 1235)

    def test_zero_precision(self):
        self.assertEqual(ee.amount_to_minor("12.9", 0), 13)

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            ee.amount_to_minor("abc", 2)


class TestBuildBody(unittest.TestCase):
    def test_required_fields_and_omits_none_customer(self):
        body = ee.build_expense_body(expense_date="2026-08-07", amount_minor=1234,
                                     category_id=3, currency_id=1, notes="Anthropic")
        self.assertEqual(body["amount"], 1234)
        self.assertEqual(body["expense_date"], "2026-08-07")
        self.assertEqual(body["expense_category_id"], 3)
        self.assertEqual(body["currency_id"], 1)
        self.assertNotIn("customer_id", body)
        self.assertNotIn("exchange_rate", body)

    def test_includes_optional_when_given(self):
        body = ee.build_expense_body(expense_date="2026-08-07", amount_minor=100,
                                     category_id=3, currency_id=2, notes="", customer_id=7,
                                     exchange_rate="83.1")
        self.assertEqual(body["customer_id"], 7)
        self.assertEqual(body["exchange_rate"], "83.1")


if __name__ == "__main__":
    unittest.main()

import unittest
import ishelf_ee as ee


class TestMultipart(unittest.TestCase):
    def test_fields_and_file(self):
        body, ctype = ee.encode_multipart(
            {"expense_date": "2026-08-07", "amount": "1234"},
            file_field=("attachment_receipt", "r.png", b"\x89PNG_bytes", "image/png"))
        self.assertTrue(ctype.startswith("multipart/form-data; boundary="))
        boundary = ctype.split("boundary=")[1].encode()
        self.assertIn(b'Content-Disposition: form-data; name="expense_date"', body)
        self.assertIn(b"2026-08-07", body)
        self.assertIn(b'name="attachment_receipt"; filename="r.png"', body)
        self.assertIn(b"Content-Type: image/png", body)
        self.assertIn(b"\x89PNG_bytes", body)
        self.assertTrue(body.rstrip().endswith(b"--" + boundary + b"--"))

    def test_no_file(self):
        body, ctype = ee.encode_multipart({"a": "1"}, None)
        self.assertIn(b'name="a"', body)
        self.assertIn("multipart/form-data; boundary=", ctype)


if __name__ == "__main__":
    unittest.main()

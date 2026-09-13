import io
import sys
import unittest
from pathlib import Path


FRENCH_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRENCH_DIR))

import app as french_app


class AppValidationTests(unittest.TestCase):
    def setUp(self):
        french_app.app.config.update(TESTING=True)
        self.client = french_app.app.test_client()

    def test_home_page_is_french_specific(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"French", response.data)
        self.assertIn(b"character_list", response.data)
        self.assertNotIn(b"SRT file", response.data)

    def test_character_list_is_required(self):
        response = self.client.post(
            "/process",
            data={
                "subtitle_files": (
                    io.BytesIO(b"not used because validation stops first"),
                    "episode.xlsx",
                )
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"required character list", response.data)

    def test_rejects_non_xlsx_subtitle(self):
        response = self.client.post(
            "/process",
            data={
                "subtitle_files": (io.BytesIO(b"subtitle"), "episode.xls"),
                "character_list": (io.BytesIO(b"characters"), "characters.xlsx"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"must be an .xlsx workbook", response.data)


if __name__ == "__main__":
    unittest.main()

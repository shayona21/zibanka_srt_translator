# English to French Subtitle Translator

A Flask application that translates English television subtitle workbooks into
French with Gemini.

## Input

- One to ten `.xlsx` subtitle workbooks
- One required `.xlsx` character list
- An optional episode synopsis in `.docx` or `.txt`
- An optional series synopsis in `.docx` or `.txt`

Each subtitle sheet must contain one header row with these columns:

1. `SR NO`
2. `TCR IN`
3. `TCR OUT`
4. `SPEAKER`
5. `SPOKEN TO`
6. `SCREENPLAY`
7. `DIALOGUES`

Sheets without this complete table are left untouched. At least one valid
subtitle table must exist in every input workbook.

## Output

The original workbook structure and formatting are retained. A new
`FRENCH TRANSLATION` column is added beside the existing data, with one
translation for every non-empty dialogue row.

The workbook is not physically split. Subtitle records are processed in memory
in batches of 50, with four adjacent rows on each side supplied as read-only
context at batch boundaries.

## Run locally

```bash
cd french
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY="your-key"
python app.py
```

Open `http://127.0.0.1:5040`.

You can also place `GEMINI_API_KEY=...` in `french/.env`. Environment files,
uploads, and generated outputs are ignored by Git.

## Tests

The tests use the files in `sample_files` and a mocked Gemini response, so they
do not make API calls:

```bash
venv/bin/python -m unittest discover -s tests -v
```

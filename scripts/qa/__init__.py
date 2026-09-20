"""QA validation scripts for Autonomous Accounting.

These scripts drive the same pipeline modules the app uses, but directly —
no HTTP, no browser — to validate the parsers' output against a hand-verified
XLSX workbook you supply. See ``scripts/qa/manifest.py`` for where the files go;
nothing here ships with any data of its own.
"""

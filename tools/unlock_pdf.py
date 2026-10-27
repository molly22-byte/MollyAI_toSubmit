#!/usr/bin/env python3
"""
unlock_pdf.py - open a password-protected credit card statement in Python.

Input: the PDF and the password the user types. The PDF is decrypted in memory only;
no unlocked copy is written to disk and the password is never stored or logged.

Use in the pipeline (parser.py):

    from unlock_pdf import open_statement, is_encrypted, WrongPassword
    with open_statement(path, password) as pdf:      # pdfplumber.PDF
        text = pdf.pages[0].extract_text()

CLI test:
    python unlock_pdf.py statement.pdf            # asks for the password (hidden input)
"""
import getpass
import io
import sys

import pdfplumber
from pypdf import PdfReader


class WrongPassword(Exception):
    pass


def is_encrypted(path):
    """True if the PDF needs a password to open."""
    return PdfReader(path).is_encrypted


def open_statement(path, password=None):
    """Return an open pdfplumber.PDF. Raises WrongPassword if the password is missing or wrong."""
    reader = PdfReader(path)
    if not reader.is_encrypted:
        return pdfplumber.open(path)
    if not password:
        raise WrongPassword("This statement is password protected. Please enter its password.")
    # decrypt() returns 0 when the password is wrong, 1 = user password, 2 = owner password
    if reader.decrypt(password) == 0:
        raise WrongPassword("Wrong password for this statement.")
    # pdfplumber can open the encrypted file directly with the same password, in memory
    with open(path, "rb") as f:
        data = io.BytesIO(f.read())
    return pdfplumber.open(data, password=password)


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: python unlock_pdf.py <statement.pdf>")
    path = sys.argv[1]
    pw = None
    if is_encrypted(path):
        pw = getpass.getpass("Statement password: ")   # hidden; not saved in shell history
    try:
        with open_statement(path, pw) as pdf:
            first = (pdf.pages[0].extract_text() or "").splitlines()
            print(f"Opened: {len(pdf.pages)} pages")
            print("First line:", first[0] if first else "(no text)")
    except WrongPassword as e:
        sys.exit(str(e))
    finally:
        pw = None


if __name__ == "__main__":
    main()

"""The "To" (bill-to) address printed on charge sheets.

F&A returns charge sheets that are not addressed to the PI in the format of
the facility's manual sheets:

    Prof. Abhishek Mondal,
    Associate Professor,
    Solid State and Structural Chemistry Unit,
    Indian Institute of Science,
    Bangalore 560012.

The admin enters these details when sending a charge sheet; they are stored
on the booking and reused for the same PI's later bookings.
"""

BILL_TO_FIELDS = (
    # (column, label, placeholder, required for industry)
    ("bill_to_name", "PI name (with title)", "e.g. Prof. Abhishek Mondal", True),
    ("bill_to_designation", "Designation", "e.g. Associate Professor", False),
    ("bill_to_department", "Department / Unit", "e.g. Solid State and Structural Chemistry Unit", False),
    ("bill_to_institute", "Institute / Organisation", "e.g. Indian Institute of Science", True),
    ("bill_to_city", "City and PIN code", "e.g. Bangalore 560012", True),
)
BILL_TO_COLUMNS = tuple(field[0] for field in BILL_TO_FIELDS)
MAX_LENGTH = 150

IISC_INSTITUTE = "Indian Institute of Science"
IISC_CITY = "Bangalore 560012"


def _clean(value):
    return " ".join(str(value or "").split())[:MAX_LENGTH]


def _is_internal(row):
    return str((row or {}).get("origin") or "").strip().casefold() == "internal"


def _is_industry(row):
    return str((row or {}).get("origin") or "").strip().casefold() in {
        "industry", "industrial", "external industry",
    }


def stored_bill_to(row):
    """The bill-to values saved on a booking (blank strings when unset)."""
    return {column: _clean((row or {}).get(column)) for column in BILL_TO_COLUMNS}


def has_bill_to(row):
    return bool(stored_bill_to(row)["bill_to_name"])


def pi_key(row):
    return " ".join(str((row or {}).get("pi_name") or "").split()).casefold()


def build_address_book(rows):
    """Map each PI to the most recent bill-to address saved for them.

    ``rows`` should be ordered newest first; the first saved address wins.
    """
    book = {}
    for row in rows:
        key = pi_key(row)
        if key and key not in book and has_bill_to(row):
            book[key] = stored_bill_to(row)
    return book


def suggested_bill_to(row, address_book=None):
    """Saved address, else this PI's last address, else sensible defaults."""
    if has_bill_to(row):
        return stored_bill_to(row)
    remembered = (address_book or {}).get(pi_key(row))
    if remembered:
        return dict(remembered)
    internal = _is_internal(row)
    return {
        "bill_to_name": _clean(row.get("pi_name")),
        "bill_to_designation": "",
        "bill_to_department": "",
        "bill_to_institute": IISC_INSTITUTE if internal else _clean(row.get("esm")),
        "bill_to_city": IISC_CITY if internal else "",
    }


def bill_to_from_form(form):
    """Bill-to values submitted with a send/preview form, or None if absent."""
    if not any(column in form for column in BILL_TO_COLUMNS):
        return None
    return {column: _clean(form.get(column)) for column in BILL_TO_COLUMNS}


def missing_bill_to(bill_to, row):
    """Labels of required bill-to details that are still blank."""
    industry = _is_industry(row)
    return [
        label for column, label, _placeholder, required_for_industry in BILL_TO_FIELDS
        if (required_for_industry or not industry) and not (bill_to or {}).get(column)
    ]


def address_lines(row):
    """The "To" address lines, in the manual sheet's comma/full-stop style."""
    bill_to = stored_bill_to(row)
    if not bill_to["bill_to_name"]:
        # Sheets issued before bill-to details existed: best available data.
        bill_to = suggested_bill_to(row)
    lines = [bill_to[column] for column in BILL_TO_COLUMNS if bill_to[column]]
    if not lines:
        lines = [_clean(row.get("user_name"))]
    lines = [line.rstrip(",.") for line in lines]
    return [f"{line}," for line in lines[:-1]] + [f"{lines[-1]}."]

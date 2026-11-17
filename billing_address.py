"""The "To" (bill-to) address printed on charge sheets.

F&A returns charge sheets that are not addressed to the PI in the format of
the facility's manual sheets, and IAC raises the official tax invoice from
this block, so it carries the complete billing address:

    Prof. Abhishek Mondal,
    Associate Professor,
    Solid State and Structural Chemistry Unit,
    Indian Institute of Science,
    C V Raman Road,
    Bangalore - 560012,
    Karnataka, India.
    GSTIN: ...            (when the organisation has one)
    Email: ...            (where the tax invoice is sent)

The admin enters these details when sending a charge sheet; they are stored
on the booking and reused for the same PI's later bookings.
"""
import re

# Which users must give a detail before a charge sheet can be sent.
REQUIRED_ALL = "all"
REQUIRED_NON_INDUSTRY = "non_industry"   # a company has no designation/department
REQUIRED_EXTERNAL = "external"           # IISc users are billed by debit head
OPTIONAL = "optional"

BILL_TO_FIELDS = (
    # (column, label, placeholder, required for, max length)
    ("bill_to_name", "PI name (with title)", "e.g. Prof. Abhishek Mondal", REQUIRED_ALL, 150),
    ("bill_to_designation", "Designation", "e.g. Associate Professor", REQUIRED_NON_INDUSTRY, 150),
    ("bill_to_department", "Department / Centre", "e.g. Solid State and Structural Chemistry Unit", REQUIRED_NON_INDUSTRY, 150),
    ("bill_to_institute", "Institute / Organisation", "e.g. Indian Institute of Science", REQUIRED_ALL, 150),
    ("bill_to_address", "Address (building, street, area)", "e.g. C V Raman Road", REQUIRED_EXTERNAL, 250),
    ("bill_to_city", "City", "e.g. Bangalore", REQUIRED_ALL, 100),
    ("bill_to_pin", "PIN code", "e.g. 560012", REQUIRED_ALL, 12),
    ("bill_to_state", "State", "e.g. Karnataka", REQUIRED_ALL, 100),
    ("bill_to_country", "Country", "e.g. India", REQUIRED_ALL, 100),
    ("bill_to_gstin", "GSTIN (if registered)", "15-character GSTIN, if any", OPTIONAL, 15),
    ("bill_to_email", "Billing email (tax invoice is sent here)", "e.g. pi@institute.ac.in", REQUIRED_EXTERNAL, 150),
)
BILL_TO_COLUMNS = tuple(field[0] for field in BILL_TO_FIELDS)
_MAX_LENGTHS = {field[0]: field[4] for field in BILL_TO_FIELDS}
MAX_LENGTH = 150

IISC_INSTITUTE = "Indian Institute of Science"
IISC_CITY = "Bangalore"
IISC_PIN = "560012"
IISC_STATE = "Karnataka"
COUNTRY_INDIA = "India"

_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_GSTIN = re.compile(r"\d{2}[A-Z0-9]{13}")
# Older sheets stored "City and PIN code" in one field, e.g. "Bangalore 560012".
_CITY_WITH_PIN = re.compile(r"^(.*?)[\s,\-–]*(\d{3}\s?\d{3})\.?$")


def _clean(value, limit=MAX_LENGTH):
    return " ".join(str(value or "").split())[:limit]


def _clean_field(column, value):
    text = _clean(value, _MAX_LENGTHS.get(column, MAX_LENGTH))
    if column == "bill_to_gstin":
        text = text.replace(" ", "").upper()
    return text


def _is_internal(row):
    return str((row or {}).get("origin") or "").strip().casefold() == "internal"


def _is_industry(row):
    return str((row or {}).get("origin") or "").strip().casefold() in {
        "industry", "industrial", "external industry",
    }


def _split_city_pin(bill_to):
    """Move a PIN code typed into the city (older entries) into its own field."""
    if bill_to.get("bill_to_pin"):
        return bill_to
    match = _CITY_WITH_PIN.match(bill_to.get("bill_to_city") or "")
    if match and match.group(1).strip():
        bill_to["bill_to_city"] = match.group(1).strip().rstrip(",")
        bill_to["bill_to_pin"] = match.group(2).replace(" ", "")
    return bill_to


def stored_bill_to(row):
    """The bill-to values saved on a booking (blank strings when unset)."""
    stored = {column: _clean_field(column, (row or {}).get(column)) for column in BILL_TO_COLUMNS}
    return _split_city_pin(stored)


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


def _default_email(row):
    return _clean((row or {}).get("pi_email") or (row or {}).get("email"))


def _fill_known_defaults(bill_to, row):
    """Fill blanks that follow from the booking itself; never overwrite."""
    bill_to = dict(bill_to)
    if _is_internal(row):
        for column, value in (
            ("bill_to_institute", IISC_INSTITUTE), ("bill_to_city", IISC_CITY),
            ("bill_to_pin", IISC_PIN), ("bill_to_state", IISC_STATE),
            ("bill_to_country", COUNTRY_INDIA),
        ):
            bill_to[column] = bill_to.get(column) or value
    if not bill_to.get("bill_to_email"):
        bill_to["bill_to_email"] = _default_email(row)
    return bill_to


def suggested_bill_to(row, address_book=None):
    """Saved address, else this PI's last address, else sensible defaults."""
    if has_bill_to(row):
        return _fill_known_defaults(stored_bill_to(row), row)
    remembered = (address_book or {}).get(pi_key(row))
    if remembered:
        return _fill_known_defaults(remembered, row)
    blank = {column: "" for column in BILL_TO_COLUMNS}
    blank["bill_to_name"] = _clean(row.get("pi_name"))
    if not _is_internal(row):
        blank["bill_to_institute"] = _clean(row.get("esm"))
    return _fill_known_defaults(blank, row)


def bill_to_from_form(form):
    """Bill-to values submitted with a send/preview form, or None if absent."""
    if not any(column in form for column in BILL_TO_COLUMNS):
        return None
    return _split_city_pin({column: _clean_field(column, form.get(column)) for column in BILL_TO_COLUMNS})


def _required(requirement, row):
    if requirement == REQUIRED_ALL:
        return True
    if requirement == REQUIRED_NON_INDUSTRY:
        return not _is_industry(row)
    if requirement == REQUIRED_EXTERNAL:
        return not _is_internal(row)
    return False


def missing_bill_to(bill_to, row):
    """Labels of required bill-to details that are still blank or invalid."""
    bill_to = bill_to or {}
    missing = [
        label for column, label, _placeholder, requirement, _length in BILL_TO_FIELDS
        if _required(requirement, row) and not bill_to.get(column)
    ]
    email = bill_to.get("bill_to_email")
    if email and not _EMAIL.fullmatch(email):
        missing.append("Billing email (enter a valid email address)")
    gstin = bill_to.get("bill_to_gstin")
    if gstin and not _GSTIN.fullmatch(gstin.replace(" ", "").upper()):
        missing.append("GSTIN (15 characters, e.g. 29AAATI1501J2ZV)")
    return missing


def _bill_to_for_print(row):
    bill_to = stored_bill_to(row)
    if not bill_to["bill_to_name"]:
        # Sheets issued before bill-to details existed: best available data.
        return suggested_bill_to(row)
    return _fill_known_defaults(bill_to, row)


def address_lines(row):
    """The "To" address lines, in the manual sheet's comma/full-stop style."""
    bill_to = _bill_to_for_print(row)
    city = bill_to["bill_to_city"]
    if bill_to["bill_to_pin"]:
        city = f"{city} - {bill_to['bill_to_pin']}" if city else bill_to["bill_to_pin"]
    region = ", ".join(value for value in (bill_to["bill_to_state"], bill_to["bill_to_country"]) if value)
    lines = [
        bill_to["bill_to_name"], bill_to["bill_to_designation"], bill_to["bill_to_department"],
        bill_to["bill_to_institute"], bill_to["bill_to_address"], city, region,
    ]
    lines = [line.rstrip(",.") for line in lines if line]
    if not lines:
        lines = [_clean(row.get("user_name"))]
    return [f"{line}," for line in lines[:-1]] + [f"{lines[-1]}."]


def billing_contact_lines(row):
    """(label, value) pairs printed under the address: GSTIN and billing email."""
    bill_to = _bill_to_for_print(row)
    pairs = [("GSTIN", bill_to["bill_to_gstin"]), ("Email", bill_to["bill_to_email"])]
    return [(label, value) for label, value in pairs if value]

from decimal import Decimal
from billing_address import address_lines
from io import BytesIO
import json
import os
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)


BANK_DETAILS = [
    ("Bank Name", "STATE BANK OF INDIA"),
    ("Beneficiary Name", "INDIAN INSTITUTE OF SCIENCE, BANGALORE"),
    ("Bank Branch", "INDIAN INSTITUTE OF SCIENCE, SCIENCE INSTITUTE POST OFFICE\nBANGALORE - 560012"),
    ("Bank Account Number", "31728098170"),
    ("Type of Bank Account", "Saving Bank Account"),
    ("Telephone Number of Bank", "080-23600567 / 080-23604525 / 080-23600165"),
    ("Mode of Electronic Transfer", "RTGS - IFSC CODE NO. SBIN0002215"),
    ("SWIFT CODE", "SBININBB425"),
    ("MICR Code", "560002020"),
    ("PAN", "AAATI1501J"),
    ("GSTIN", "29AAATI1501J2ZV"),
]

SIGNATORY_NAME = "Dr. Somnath Dutta"
SIGNATORY_TITLE = "Convener, Electron Microscope Facility"
SIGNATORY_DIVISION = "Division of Biological Sciences"
SIGNATORY_INSTITUTE = "Indian Institute of Science, Bangalore"


def _signatory_block(styles):
    return _paragraph(
        f"{SIGNATORY_NAME}<br/>{SIGNATORY_TITLE}<br/>{SIGNATORY_DIVISION}<br/>{SIGNATORY_INSTITUTE}",
        styles["SmallRight"],
    )


def _signature_block(styles, right_label="Signature", left_label="PI Signature", left_label_row=0):
    return Table([
        [_paragraph(left_label if left_label_row == 0 else "", styles["Small"]),
         _paragraph(right_label, styles["SmallRight"])],
        [_paragraph(left_label if left_label_row == 1 else "", styles["Small"]),
         _paragraph("", styles["SmallRight"])],
        [_paragraph(left_label if left_label_row == 2 else "", styles["Small"]), _paragraph(
            f"{SIGNATORY_NAME}<br/>{SIGNATORY_TITLE}<br/>{SIGNATORY_DIVISION}<br/>{SIGNATORY_INSTITUTE}",
            styles["SmallRight"],
        )],
    ], colWidths=[95 * mm, 65 * mm])


def _industry_signature_block(styles):
    return Table([
        [_paragraph("", styles["Small"]), _paragraph("Dr. Somnath Dutta", styles["SmallRight"])],
        [_paragraph("", styles["Small"]), _paragraph("", styles["SmallRight"])],
    ], colWidths=[95 * mm, 65 * mm])


def _internal_signature_block(styles):
    table = Table([
        [_paragraph("PI Signature", styles["Small"]), _paragraph("Dr. Somnath Dutta", styles["SmallCenter"])],
        [_paragraph("", styles["Small"]), _paragraph(
            f"{SIGNATORY_TITLE}<br/>{SIGNATORY_DIVISION}<br/>{SIGNATORY_INSTITUTE}",
            styles["SmallCenter"],
        )],
    ], colWidths=[95 * mm, 65 * mm])
    table.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (0, -1), 12 * mm),
    ]))
    table.hAlign = "RIGHT"
    return table


def _decimal(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _display_date(value):
    if not value:
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%d-%m-%Y")
    text = str(value).strip()[:10]
    # SQLite returns ISO strings (YYYY-MM-DD[ HH:MM:SS]); show them as dd-mm-yyyy.
    parts = text.split("-")
    if len(parts) == 3 and len(parts[0]) == 4 and all(part.isdigit() for part in parts):
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return text


_SERVICE_PREFIXES = (
    ("freezing", "FRZ"),
    ("grid registration", "FRZ"),
    ("screening", "SCR"),
    ("clipping", "SCR"),
    ("data collection", "DC"),
    ("imaging", "DC"),
)


def _service_prefix(service):
    normalized = str(service or "").strip().casefold()
    for name, prefix in _SERVICE_PREFIXES:
        if normalized.startswith(name):
            return prefix
    return ""


def _prefixed_id(booking_id, service):
    prefix = _service_prefix(service)
    return f"{prefix}-{booking_id}" if prefix else str(booking_id)


def _booking_label(row, service):
    """Booking IDs with a service prefix, since IDs come from different tables."""
    items = _value(row, "combined_items", []) or []
    labels = [
        _prefixed_id(item.get("id"), item.get("combined_service", service))
        for item in items if item.get("id")
    ]
    return ", ".join(labels) or _prefixed_id(row["id"], service)


def _is_freezing_sheet(row, service):
    """A single freezing sheet: freezing has no 24-hour instrument slots."""
    items = _value(row, "combined_items", []) or []
    if items:
        return all(_service_prefix(item.get("combined_service", "")) == "FRZ" for item in items)
    return _service_prefix(service) == "FRZ" or _service_prefix(_value(row, "service_stage", "")) == "FRZ"


def _grid_count_label(row):
    """Grid count; combined sheets list each stage instead of summing them."""
    items = _value(row, "combined_items", []) or []
    if items:
        counts = []
        for item in items:
            grids = _value(item, "number_of_grids", _value(item, "actual_grids", 0)) or 0
            counts.append(f"{_prefixed_id(item.get('id'), item.get('combined_service', ''))}: {grids}")
        return "; ".join(counts)
    return str(_value(row, "number_of_grids", _value(row, "actual_grids", 0)) or 0)


def _line_gst(lines, total_gst):
    """Split the sheet's GST across lines in proportion to their amounts.

    Any rounding remainder goes on the last line so the per-line GST always
    adds up to the sheet's GST.
    """
    base = sum((amount for _, _, amount in lines), Decimal("0"))
    total_gst = _decimal(total_gst)
    if not lines or not base or not total_gst:
        return [Decimal("0.00") for _ in lines]
    shares = [_decimal(amount * total_gst / base) for _, _, amount in lines]
    shares[-1] += total_gst - sum(shares, Decimal("0"))
    return shares


def _amount_words(number):
    amount = _decimal(number)
    negative = amount < 0
    amount = abs(amount)
    number = int(amount)
    paise = int((amount - number) * 100)

    ones = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven",
            "Eight", "Nine", "Ten", "Eleven", "Twelve", "Thirteen",
            "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen",
            "Nineteen"]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty",
            "Seventy", "Eighty", "Ninety"]

    def under_thousand(value):
        words = []
        if value >= 100:
            words.extend([ones[value // 100], "Hundred"])
            value %= 100
        if value >= 20:
            words.append(tens[value // 10])
            value %= 10
        if value:
            words.append(ones[value])
        return " ".join(words)

    def integer_words(value):
        parts = []
        # Indian numbering; amounts of 100 crore or more repeat "Crore" groups.
        crores, value = divmod(value, 10_000_000)
        if crores:
            parts.extend([integer_words(crores), "Crore"])
        for divisor, label in ((100_000, "Lakh"), (1_000, "Thousand")):
            count, value = divmod(value, divisor)
            if count:
                parts.extend([under_thousand(count), label])
        if value:
            parts.append(under_thousand(value))
        return " ".join(parts)

    rupees = integer_words(number) if number else "Zero"
    words = f"Rupees {rupees}"
    if paise:
        words += f" and {under_thousand(paise)} Paise"
    if negative:
        words = "Minus " + words
    return words + " Only"


def _styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("Institution", parent=styles["Normal"], fontName="Helvetica-Bold",
                              fontSize=12, leading=15, alignment=TA_CENTER, textColor=colors.HexColor("#17324d")))
    styles.add(ParagraphStyle("Small", parent=styles["Normal"], fontSize=8.5, leading=11))
    styles.add(ParagraphStyle("Bank", parent=styles["Small"], fontSize=8, leading=9.5))
    styles.add(ParagraphStyle("SmallRight", parent=styles["Small"], alignment=TA_RIGHT))
    styles.add(ParagraphStyle("SmallCenter", parent=styles["Small"], alignment=TA_CENTER))
    styles.add(ParagraphStyle("Section", parent=styles["Heading3"], fontName="Helvetica-Bold",
                              fontSize=10, leading=13, textColor=colors.HexColor("#17324d"), spaceBefore=8))
    return styles


def _paragraph(text, style):
    escaped = escape(str(text or "")).replace("\n", "<br/>")
    for source, tag in (
        ("&lt;b&gt;", "<b>"), ("&lt;/b&gt;", "</b>"),
        ("&lt;br/&gt;", "<br/>"), ("&amp;nbsp;", "&nbsp;"),
    ):
        escaped = escaped.replace(source, tag)
    return Paragraph(escaped, style)


def _value(row, key, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _prebilled_grids(row):
    """Grids whose grid charge was already billed at freezing (0 if none)."""
    value = _value(row, "prebilled_grids")
    if value is None:
        breakdown = _value(row, "grid_breakdown")
        if isinstance(breakdown, str):
            try:
                breakdown = json.loads(breakdown)
            except ValueError:
                breakdown = None
        value = breakdown.get("prebilled_grids") if isinstance(breakdown, dict) else 0
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _to_block(row):
    """The PI's address, as on the manual sheets, then the user it is for."""
    # _paragraph() escapes the text, so the values are inserted raw here.
    address = "<br/>".join(address_lines(row))
    user = str(row.get("user_name") or "")
    email = str(row.get("email") or "")
    return f"<b>{address}</b><br/>User: {user} ({email})"


def _billing_lines(row, service):
    if _value(row, "combined_items"):
        lines = []
        for item in _value(row, "combined_items", []):
            for description, quantity, amount in _billing_lines(item, item.get("combined_service", service)):
                lines.append((f"{item.get('combined_service', service)} - {description}", quantity, amount))
        return lines
    stage = str(_value(row, "service_stage", service) or service)
    if stage.casefold() in {"freezing", "grid registration"}:
        stage = "Freezing / Grid Registration"
    elif stage.casefold() in {"screening", "clipping"}:
        stage = "Screening / Clipping"
    grids = _value(row, "number_of_grids", _value(row, "actual_grids", _value(row, "grids", 0)))
    source = str(_value(row, "grid_source", "") or "").casefold()
    grid_amount = _value(row, "grid_charge", _value(row, "freezing_charge", 0))
    handling_amount = _value(row, "handling_charge", 0)
    clip_amount = _value(row, "clip_base_charge", _value(row, "clipping_charge", 0))
    slot_amount = _value(row, "slot_charge", 0)
    processing_amount = _value(row, "processing_charge", 0)
    lines = []
    # A self-owned grid is intentionally shown as a zero-valued line.
    if stage == "Freezing / Grid Registration" or source in {"facility", "self_owned"}:
        prebilled = _prebilled_grids(row)
        quantity = f"{grids or 0} grid(s)"
        if prebilled:
            quantity = f"{max(int(grids or 0) - prebilled, 0)} grid(s); {prebilled} billed at freezing"
        lines.append(("Grid Charge", quantity, _decimal(grid_amount)))
    if stage in {"Freezing / Grid Registration", "Screening / Clipping", "Data Collection"}:
        lines.append(("Handling Charge", f"{grids or 0} grid(s)", _decimal(handling_amount)))
    if stage in {"Screening / Clipping", "Data Collection"}:
        clipped_grids = _value(row, "clipped_grids", 0) or 0
        chargeable_grids = max(int(grids or 0) - int(clipped_grids), 0)
        clip_base_source = str(_value(row, "clip_base_source", "facility") or "facility").casefold()
        clip_label = (
            "C-clip + Base ring (Lab / user provided)"
            if clip_base_source == "self_owned"
            else "C-clip + Base ring"
        )
        lines.append((clip_label, f"{chargeable_grids} grid(s)", _decimal(clip_amount)))
        lines.append(("Slot Charge", f"{_value(row, 'actual_slots', 0) or 0} 24-hour slot(s)", _decimal(slot_amount)))
    if _decimal(processing_amount):
        lines.append(("Data Processing / Image Analysis", "1 service", _decimal(processing_amount)))
    adjustment = _value(row, "csic_adjustment")
    if adjustment is not None and _decimal(adjustment):
        label = "CSIC discount adjustment" if _decimal(adjustment) < 0 else "CSIC agreed amount adjustment"
        lines.append((label, "1 adjustment", _decimal(adjustment)))
    return lines


def _header(story, styles):
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "images", "iisc_logo_black.png")
    institution = _paragraph(
        "<b>INDIAN INSTITUTE OF SCIENCE (IISc)</b><br/>"
        "<b>ADVANCE CENTRE FOR CRYO-ELECTRON MICROSCOPE FACILITY</b><br/>"
        "Division of Biological Sciences<br/>"
        "Bengaluru, Karnataka - 560012<br/>"
        "GSTIN: 29AAATI1501J2ZV  |  PAN: AAATI1501J",
        styles["SmallCenter"],
    )
    header_cells = []
    if os.path.exists(logo_path):
        header_cells.append(Image(logo_path, width=22 * mm, height=22 * mm))
    header_cells.append(institution)
    header = Table([header_cells], colWidths=[28 * mm, 152 * mm] if len(header_cells) == 2 else [180 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("LINEBELOW", (0, 0), (-1, -1), 1, colors.HexColor("#17324d")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(header)
    story.append(Spacer(1, 8))


def _watermark(canvas, doc):
    logo_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "images", "iisc_logo_black.png")
    if not os.path.exists(logo_path):
        return
    canvas.saveState()
    canvas.setFillAlpha(0.10)
    canvas.drawImage(
        logo_path,
        (A4[0] - 120 * mm) / 2,
        (A4[1] - 120 * mm) / 2,
        width=120 * mm,
        height=120 * mm,
        mask="auto",
        preserveAspectRatio=True,
        anchor="c",
    )
    canvas.restoreState()


def _internal_footer(canvas, doc):
    styles = _styles()
    signature = _internal_signature_block(styles)
    width, height = signature.wrapOn(canvas, doc.width, doc.bottomMargin)
    signature.drawOn(
        canvas,
        doc.leftMargin + (doc.width - width) / 2,
        max(0, doc.bottomMargin - height),
    )


def _internal_page(canvas, doc):
    _watermark(canvas, doc)
    _internal_footer(canvas, doc)


def _external_footer(canvas, doc):
    styles = _styles()
    signature = _external_signature_block(styles)
    width, height = signature.wrapOn(canvas, doc.width, doc.bottomMargin)
    signature.drawOn(
        canvas,
        doc.leftMargin + (doc.width - width) / 2,
        max(0, doc.bottomMargin - height),
    )


def _external_signature_block(styles):
    table = Table([
        [_paragraph("", styles["Small"]), _paragraph(SIGNATORY_NAME, styles["SmallCenter"])],
        [_paragraph("", styles["Small"]), _paragraph(
            f"{SIGNATORY_TITLE}<br/>{SIGNATORY_DIVISION}<br/>{SIGNATORY_INSTITUTE}",
            styles["SmallCenter"],
        )],
    ], colWidths=[95 * mm, 65 * mm])
    table.hAlign = "CENTER"
    return table


def _external_page(canvas, doc):
    _watermark(canvas, doc)
    _external_footer(canvas, doc)


def _external_pdf(row, service, category, is_industry):
    styles = _styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=15 * mm, leftMargin=15 * mm,
                            topMargin=10 * mm, bottomMargin=30 * mm)
    story = []
    _header(story, styles)
    story.append(_paragraph("CHARGE SHEET / PROFORMA INVOICE", styles["Institution"]))
    story.append(Spacer(1, 8))

    source = str(_value(row, "grid_source", "") or "").casefold()
    source_label = "Self Owned / User Provided" if source == "self_owned" else "Facility Provided"
    booking_label = _booking_label(row, service)
    date_value = _value(row, "completion_date", _value(row, "completed_at"))
    grid_count = _grid_count_label(row)
    is_freezing = _is_freezing_sheet(row, service)
    info = [
        [_paragraph("<b>Charge Sheet No:</b>", styles["Small"]),
         str(_value(row, "charge_sheet_id", f"CS-{row['id']}")),
         _paragraph("<b>Date:</b>", styles["Small"]), _display_date(date_value)],
        [_paragraph("<b>To:</b>", styles["Small"]), _paragraph(_to_block(row), styles["Small"]), "", ""],
        [_paragraph("<b>Booking ID(s):</b>", styles["Small"]), booking_label,
         _paragraph("<b>Service:</b>", styles["Small"]), service],
        [_paragraph("<b>User Category:</b>", styles["Small"]), category, "", ""],
        [_paragraph("<b>Number of Grids:</b>", styles["Small"]), _paragraph(grid_count, styles["Small"]),
         _paragraph("<b>Grid Source:</b>", styles["Small"]), source_label],
    ]
    grid_type = _value(row, "grid_type", "")
    if source != "self_owned" and grid_type:
        info.append([_paragraph("<b>Grid Type:</b>", styles["Small"]), str(grid_type).replace("_", " ").title(), "", ""])
    table = Table(info, colWidths=[30 * mm, 68 * mm, 30 * mm, 52 * mm])
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b8c5d0")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#edf3f7")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("SPAN", (1, 1), (3, 1)), ("SPAN", (1, 3), (3, 3)),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(table)
    story.append(Spacer(1, 6))

    rows = [[_paragraph("<b>S.No</b>", styles["Small"]), _paragraph("<b>Particulars</b>", styles["Small"]),
             _paragraph("<b>Quantity</b>", styles["Small"]), _paragraph("<b>Amount</b>", styles["Small"]),
             _paragraph("<b>GST (18%)</b>", styles["Small"]), _paragraph("<b>Total</b>", styles["Small"])]]
    lines = _billing_lines(row, service)
    line_gst = _line_gst(lines, _value(row, "gst_amount", 0))
    for index, ((description, quantity, amount), gst) in enumerate(zip(lines, line_gst), 1):
        rows.append([str(index), description, quantity, f"INR {amount:,.2f}",
                     f"INR {gst:,.2f}", f"INR {amount + gst:,.2f}"])
    if len(rows) == 1:
        rows.append(["", "No billable services recorded", "", "INR 0.00", "", "INR 0.00"])
    billing = Table(rows, colWidths=[11 * mm, 58 * mm, 25 * mm, 29 * mm, 27 * mm, 30 * mm], repeatRows=1)
    billing.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9aaeba")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dcecf3")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#17324d")),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(billing)
    subtotal = _decimal(_value(row, "subtotal", None))
    if not subtotal:
        subtotal = sum((_decimal(_value(row, field, 0)) for field in ("slot_charge", "freezing_charge", "clipping_charge", "processing_charge")), Decimal("0"))
    amount_in_words = _amount_words(_value(row, 'grand_total', _value(row, 'total_billed', 0)))
    slots_label, slots_value = (
        ("", "") if is_freezing
        else ("Actual 24-hour slots", str(_value(row, "actual_slots", 0) or 0))
    )
    summary = [
        [slots_label, slots_value, "Subtotal", f"INR {subtotal:,.2f}"],
        ["Actual grids", _paragraph(grid_count, styles["Small"]), "GST", f"INR {_decimal(_value(row, 'gst_amount', 0)):,.2f}"],
        ["", "", "Grand Total", f"INR {_decimal(_value(row, 'grand_total', _value(row, 'total_billed', 0))):,.2f}"],
        ["", "", _paragraph("<b>Amount in words</b>", styles["Small"]), _paragraph(amount_in_words, styles["Small"])],
    ]
    summary_table = Table(summary, colWidths=[42 * mm, 28 * mm, 42 * mm, 68 * mm])
    summary_table.setStyle(TableStyle([
        ("GRID", (2, 0), (-1, -1), 0.4, colors.HexColor("#b8c5d0")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#edf3f7")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("ALIGN", (3, 0), (3, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (2, 2), (3, 2), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 8))
    # Bank details in two side-by-side columns (half the height), kept in one
    # block with the instructions so a long sheet never splits them.
    payment = [
        _paragraph("<b>PAYMENT INSTRUCTIONS</b>", styles["Section"]),
        _paragraph("Please make the payment using the bank details provided below. After completing the transaction, "
                   "kindly email the transaction/reference number, transaction date, amount transferred, and valid proof "
                   "of transaction/payment to the Cryo-EM Facility.", styles["Small"]),
        Spacer(1, 3),
    ]
    half = (len(BANK_DETAILS) + 1) // 2
    left, right = BANK_DETAILS[:half], BANK_DETAILS[half:]
    bank = []
    for index in range(half):
        row = [_paragraph(f"<b>{left[index][0]}</b>", styles["Bank"]), _paragraph(left[index][1], styles["Bank"])]
        if index < len(right):
            row += [_paragraph(f"<b>{right[index][0]}</b>", styles["Bank"]), _paragraph(right[index][1], styles["Bank"])]
        else:
            row += ["", ""]
        bank.append(row)
    bank_table = Table(bank, colWidths=[30 * mm, 60 * mm, 30 * mm, 60 * mm])
    bank_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#c6d0d8")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f7f9")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f3f7f9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    payment.append(bank_table)
    story.append(KeepTogether(payment))
    doc.build(story, onFirstPage=_external_page, onLaterPages=_external_page)
    return buffer.getvalue()


def _academic_pdf(row, service):
    return _external_pdf(row, service, "Academic", is_industry=False)


def _industrial_pdf(row, service):
    return _external_pdf(row, service, "Industrial", is_industry=True)


def _internal_pdf(row, service):
    styles = _styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=15 * mm, leftMargin=15 * mm,
                            topMargin=12 * mm, bottomMargin=30 * mm)
    story = []
    _header(story, styles)
    story.append(_paragraph("CHARGE SHEET", styles["Institution"]))
    story.append(Spacer(1, 6))
    source = str(_value(row, "grid_source", "") or "").casefold()
    source_label = "Self Owned / User Provided" if source == "self_owned" else "Facility Provided"
    grid_type = _value(row, "grid_type", "")
    charge_sheet_number = _value(row, "charge_sheet_id", f"CS-{row['id']}")
    booking_label = _booking_label(row, service)
    grid_count = _grid_count_label(row)
    is_freezing = _is_freezing_sheet(row, service)
    date_value = _value(row, "completion_date", _value(row, "completed_at"))
    story.append(_paragraph(f"<b>Charge Sheet No:</b> {charge_sheet_number}<br/>"
                            f"<b>Date:</b> {_display_date(date_value)}<br/>"
                            f"<b>To</b><br/>{_to_block(row)}<br/><br/>"
                            f"<b>Booking ID(s):</b> {booking_label} &nbsp;&nbsp; <b>Service:</b> {service}<br/><b>User Category:</b> Internal<br/>"
                            f"<b>Number of Grids:</b> {grid_count}<br/>"
                            f"<b>Grid Source:</b> {source_label}"
                            f"{'<br/><b>Grid Type:</b> ' + str(grid_type).replace('_', ' ').title() if source != 'self_owned' and grid_type else ''}", styles["Small"]))
    story.append(Spacer(1, 8))
    rows = [[_paragraph("<b>S.No</b>", styles["Small"]), _paragraph("<b>Particulars</b>", styles["Small"]),
             _paragraph("<b>Quantity</b>", styles["Small"]), _paragraph("<b>Amount</b>", styles["Small"]),
             _paragraph("<b>Total</b>", styles["Small"]), _paragraph("<b>Debit Head</b>", styles["Small"])]]
    for index, (description, quantity, amount) in enumerate(_billing_lines(row, service), 1):
        rows.append([str(index), description, quantity, f"INR {amount:,.2f}", f"INR {amount:,.2f}", ""])
    if len(rows) == 1:
        rows.append(["", "No billable services recorded", "", "INR 0.00", "INR 0.00", ""])
    billing = Table(rows, colWidths=[11 * mm, 55 * mm, 25 * mm, 28 * mm, 27 * mm, 34 * mm], repeatRows=1)
    billing.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#9aaeba")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dcecf3")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#17324d")),
        ("ALIGN", (2, 1), (-2, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(billing)
    story.append(Spacer(1, 8))
    subtotal = _decimal(_value(row, "subtotal", 0))
    gst = _decimal(_value(row, "gst_amount", 0))
    total = _decimal(_value(row, "grand_total", _value(row, "total_billed", 0)))
    amount_in_words = _amount_words(total)
    summary = [
        [_paragraph("" if is_freezing else "<b>Actual 24-hour slots</b>", styles["Small"]),
         "" if is_freezing else str(_value(row, "actual_slots", 0) or 0),
         _paragraph("<b>Actual grids</b>", styles["Small"]), _paragraph(grid_count, styles["Small"])],
        [_paragraph("<b>Subtotal</b>", styles["Small"]), f"INR {subtotal:,.2f}",
         _paragraph("<b>GST</b>", styles["Small"]), f"INR {gst:,.2f}"],
        [_paragraph("<b>Grand Total</b>", styles["Small"]), f"INR {total:,.2f}",
         _paragraph("<b>Amount in words</b>", styles["Small"]), _paragraph(amount_in_words, styles["Small"])],
    ]
    summary_table = Table(summary, colWidths=[42 * mm, 24 * mm, 42 * mm, 72 * mm])
    summary_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b8c5d0")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#edf3f7")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#edf3f7")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTNAME", (1, 1), (1, 1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 10))
    story.append(_paragraph("<b>Internal users are requested to provide the appropriate Debit Head for processing the charges "
                            "and copy their PI while submitting the Debit Head details.</b>", styles["Small"]))
    doc.build(story, onFirstPage=_internal_page, onLaterPages=_internal_page)
    return buffer.getvalue()


def generate_charge_sheet(row, service):
    """Return a PDF generated only from the completed row's stored billing values."""
    origin = str(row["origin"] or "").strip().casefold()
    if origin == "internal":
        return _internal_pdf(row, service)
    if origin in {"industry", "industrial", "external industry"}:
        return _industrial_pdf(row, service)
    return _academic_pdf(row, service)

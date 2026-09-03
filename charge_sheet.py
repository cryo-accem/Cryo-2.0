from decimal import Decimal
from io import BytesIO
import os
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
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


def _decimal(value):
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def _display_date(value):
    if not value:
        return ""
    return value.strftime("%d-%m-%Y") if hasattr(value, "strftime") else str(value)[:10]


def _amount_words(number):
    number = int(_decimal(number))
    if number == 0:
        return "Rupees Zero Only"

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

    parts = []
    for divisor, label in ((10_000_000, "Crore"), (100_000, "Lakh"), (1_000, "Thousand")):
        count, number = divmod(number, divisor)
        if count:
            parts.extend([under_thousand(count), label])
    if number:
        parts.append(under_thousand(number))
    return "Rupees " + " ".join(parts) + " Only"


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


def _billing_lines(row, service):
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
    if stage == "Freezing / Grid Registration" or source == "self_owned":
        lines.append(("Grid Charge", f"{grids or 0} grid(s)", _decimal(grid_amount)))
    if stage in {"Freezing / Grid Registration", "Screening / Clipping", "Data Collection"}:
        lines.append(("Handling Charge", f"{grids or 0} grid(s)", _decimal(handling_amount)))
    if stage in {"Screening / Clipping", "Data Collection"}:
        lines.append(("C-clip + Base ring", "1 service", _decimal(clip_amount)))
        lines.append(("Slot Charge", f"{_value(row, 'actual_slots', 0) or 0} 24-hour slot(s)", _decimal(slot_amount)))
    if _decimal(processing_amount):
        lines.append(("Data Processing / Image Analysis", "1 service", _decimal(processing_amount)))
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


def _external_pdf(row, service):
    styles = _styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=15 * mm, leftMargin=15 * mm,
                            topMargin=10 * mm, bottomMargin=10 * mm)
    story = []
    _header(story, styles)
    story.append(_paragraph("CHARGE SHEET / PROFORMA INVOICE", styles["Institution"]))
    story.append(Spacer(1, 16))

    category = "Academic" if str(row["origin"]).casefold() == "external" else row["origin"]
    source = str(_value(row, "grid_source", "") or "").casefold()
    source_label = "Self Owned / User Provided" if source == "self_owned" else "Facility Provided"
    info = [
        [_paragraph("<b>Charge Sheet No:</b>", styles["Small"]), f"CS-{row['id']}",
         _paragraph("<b>Date:</b>", styles["Small"]), _display_date(row["completion_date"] if service != "Freezing" else row["completed_at"])],
        [_paragraph("<b>TO:</b>", styles["Small"]), _paragraph(
            f"{row['user_name']}<br/>{row['email']}<br/>Institution: {row.get('pi_name', '') if hasattr(row, 'get') else row['pi_name']}",
            styles["Small"]), "", ""],
        [_paragraph("<b>Booking ID:</b>", styles["Small"]), str(row["id"]),
         _paragraph("<b>Service:</b>", styles["Small"]), service],
        [_paragraph("<b>User Category:</b>", styles["Small"]), category, "", ""],
        [_paragraph("<b>Number of Grids:</b>", styles["Small"]), str(_value(row, "number_of_grids", _value(row, "actual_grids", 0)) or 0),
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
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(table)
    story.append(Spacer(1, 8))

    rows = [[_paragraph("<b>S.No</b>", styles["Small"]), _paragraph("<b>Particulars</b>", styles["Small"]),
             _paragraph("<b>Quantity</b>", styles["Small"]), _paragraph("<b>Amount</b>", styles["Small"]),
             _paragraph("<b>GST (18%)</b>", styles["Small"]), _paragraph("<b>Total</b>", styles["Small"])]]
    for index, (description, quantity, amount) in enumerate(_billing_lines(row, service), 1):
        rows.append([str(index), description, quantity, f"INR {amount:,.2f}", "", f"INR {amount:,.2f}"])
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
    summary = [
        ["Actual 24-hour slots", str(row["actual_slots"] or 0), "Subtotal", f"INR {subtotal:,.2f}"],
        ["Actual grids", str(_value(row, "number_of_grids", _value(row, "actual_grids", 0)) or 0), "GST", f"INR {_decimal(_value(row, 'gst_amount', 0)):,.2f}"],
        ["", "", "Grand Total", f"INR {_decimal(_value(row, 'grand_total', _value(row, 'total_billed', 0))):,.2f}"],
    ]
    summary_table = Table(summary, colWidths=[42 * mm, 28 * mm, 42 * mm, 68 * mm])
    summary_table.setStyle(TableStyle([
        ("GRID", (2, 0), (-1, -1), 0.4, colors.HexColor("#b8c5d0")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#edf3f7")),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"), ("ALIGN", (3, 0), (3, -1), "RIGHT"),
        ("FONTNAME", (2, 2), (3, 2), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(summary_table)
    story.append(_paragraph(f"<b>Amount in words:</b> {_amount_words(_value(row, 'grand_total', _value(row, 'total_billed', 0)))}", styles["Small"]))
    story.append(Spacer(1, 14))
    story.append(_paragraph("<b>PAYMENT INSTRUCTIONS</b>", styles["Section"]))
    story.append(_paragraph("Please make the payment using the bank details provided below. After completing the transaction, "
                            "kindly email the transaction/reference number, transaction date, amount transferred, and valid proof "
                            "of transaction/payment to the Cryo-EM Facility.", styles["Small"]))
    bank = [[_paragraph(f"<b>{label}</b>", styles["Bank"]), _paragraph(value, styles["Bank"])] for label, value in BANK_DETAILS]
    bank_table = Table(bank, colWidths=[52 * mm, 128 * mm])
    bank_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#c6d0d8")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f7f9")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    story.append(bank_table)
    story.append(Spacer(1, 14))
    story.append(_paragraph("Dr. Somnath Dutta<br/>Convener, Electron Microscope Facility<br/>Division of Biological Sciences<br/>Indian Institute of Science, Bangalore",
                            styles["SmallRight"]))
    doc.build(story, onFirstPage=_watermark, onLaterPages=_watermark)
    return buffer.getvalue()


def _internal_pdf(row, service):
    styles = _styles()
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=15 * mm, leftMargin=15 * mm,
                            topMargin=12 * mm, bottomMargin=14 * mm)
    story = []
    _header(story, styles)
    story.append(_paragraph("CHARGE SHEET", styles["Institution"]))
    story.append(Spacer(1, 6))
    source = str(_value(row, "grid_source", "") or "").casefold()
    source_label = "Self Owned / User Provided" if source == "self_owned" else "Facility Provided"
    grid_type = _value(row, "grid_type", "")
    story.append(_paragraph(f"<b>Date:</b> {_display_date(row['completion_date'] if service != 'Freezing' else row['completed_at'])}<br/>"
                            f"<b>TO:</b> {row['user_name']}<br/>Department/Unit: Indian Institute of Science<br/>Email: {row['email']}<br/>"
                            f"<b>Booking ID:</b> {row['id']} &nbsp;&nbsp; <b>Service:</b> {service}<br/><b>User Category:</b> Internal<br/>"
                            f"<b>Number of Grids:</b> {_value(row, 'number_of_grids', _value(row, 'actual_grids', 0)) or 0}<br/>"
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
    summary = [
        [_paragraph("<b>Actual 24-hour slots</b>", styles["Small"]), str(_value(row, "actual_slots", 0) or 0),
         _paragraph("<b>Actual grids</b>", styles["Small"]), str(_value(row, "number_of_grids", _value(row, "actual_grids", 0)) or 0)],
        [_paragraph("<b>Subtotal</b>", styles["Small"]), f"INR {subtotal:,.2f}",
         _paragraph("<b>GST</b>", styles["Small"]), f"INR {gst:,.2f}"],
        [_paragraph("<b>Grand Total</b>", styles["Small"]), f"INR {total:,.2f}",
         _paragraph("<b>Amount in words</b>", styles["Small"]), _amount_words(total)],
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
    story.append(Spacer(1, 4))
    story.append(_paragraph(f"<b>Amount in words:</b> {_amount_words(total)}", styles["Small"]))
    story.append(Spacer(1, 10))
    story.append(_paragraph("<b>Internal users are requested to provide the appropriate Debit Head for processing the charges "
                            "and copy their PI while submitting the Debit Head details.</b>", styles["Small"]))
    story.append(Spacer(1, 30))
    story.append(_paragraph("PI Signature", styles["Small"]))
    story.append(Spacer(1, 18))
    story.append(_paragraph("Dr. Somnath Dutta<br/>Convener, Electron Microscope Facility<br/>Division of Biological Sciences<br/>Indian Institute of Science, Bangalore",
                            styles["Small"]))
    doc.build(story, onFirstPage=_watermark, onLaterPages=_watermark)
    return buffer.getvalue()


def generate_charge_sheet(row, service):
    """Return a PDF generated only from the completed row's stored billing values."""
    origin = str(row["origin"] or "").strip().casefold()
    return _internal_pdf(row, service) if origin == "internal" else _external_pdf(row, service)

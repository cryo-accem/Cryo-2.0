"""Authoritative billing rules for Cryo-EM services.

All amounts in this module are base prices (before GST).  The admin UI and
charge-sheet generator consume the result of :func:`calculate_charge_sheet`;
they must not reimplement these formulas.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re


ZERO = Decimal("0.00")
FOUR = Decimal("4")
GST_RATE = Decimal("0.18")

CHARGE_CONFIG = {
    "internal": {
        "grid": {"normal_holey_carbon": Decimal("4000"), "gold_carbon_graphene": Decimal("4500")},
        "handling_per_4": Decimal("500"),
        "clip_base": Decimal("2500"),
        "slot": Decimal("3000"),
        "gst_rate": Decimal("0"),
    },
    "academic": {
        "grid": {"normal_holey_carbon": Decimal("3000"), "gold_carbon_graphene": Decimal("4000")},
        "handling_per_4": Decimal("1000"),
        "clip_base": Decimal("2500"),
        "slot": Decimal("12000"),
        "gst_rate": GST_RATE,
    },
    "industry": {
        "grid": {"normal_holey_carbon": Decimal("6000"), "gold_carbon_graphene": Decimal("7000")},
        "handling_per_4": Decimal("2000"),
        "clip_base": Decimal("4000"),
        "slot": Decimal("50000"),
        "gst_rate": GST_RATE,
    },
}

# Backwards-compatible constants used by the scheduling code.
FREEZING_RATE = Decimal("125")
CLIPPING_RATE = Decimal("125")
PROCESSING_RATE = Decimal("20000")
SLOT_RATES = {key: value["slot"] for key, value in CHARGE_CONFIG.items()}

NON_BILLABLE_PI_PATTERN = re.compile(r"som(?:nath|anth)", re.IGNORECASE)


def _money(value):
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def pricing_category(origin):
    normalized = str(origin or "").strip().casefold().replace("_", " ")
    if normalized == "internal":
        return "internal"
    if normalized in {"external", "academic", "external academic"}:
        return "academic"
    if normalized in {"industry", "industrial", "external industry"}:
        return "industry"
    raise ValueError("Unsupported user category.")


def normalize_service_stage(stage):
    value = str(stage or "").strip().casefold().replace("_", " ")
    if value in {"freezing", "freezing / grid registration", "grid registration"}:
        return "Freezing / Grid Registration"
    if value in {"screening", "screening / clipping", "clipping"}:
        return "Screening / Clipping"
    if value in {"data collection", "data collecting", "imaging"}:
        return "Data Collection"
    raise ValueError("Unsupported service stage.")


def normalize_grid_source(source):
    value = str(source or "").strip().casefold().replace("-", " ").replace("_", " ")
    if value in {"facility", "facility provided"}:
        return "facility"
    if value in {"self owned", "self-owned", "user provided", "self owned / user provided"}:
        return "self_owned"
    if not value:
        raise ValueError("Please select the grid source before generating the bill.")
    raise ValueError("Unsupported grid source.")


def normalize_grid_type(grid_type):
    value = str(grid_type or "").strip().casefold().replace("-", "_").replace(" ", "_")
    if value in {"normal_holey_carbon", "normal_holey_carbon_grid"}:
        return "normal_holey_carbon"
    if value in {
        "gold_carbon_graphene", "gold_carbon_coated", "gold_carbon_coated_graphene_oxide",
        "gold_carbon_graphene_grid", "gold_carbon_coated_graphene_oxide_grid",
    }:
        return "gold_carbon_graphene"
    return ""


def parse_number_of_grids(value):
    """Return a strictly positive integer, rejecting decimals and negatives."""
    if isinstance(value, bool):
        raise ValueError("Number of grids must be at least 1.")
    text = str(value or "").strip()
    if not re.fullmatch(r"\+?\d+", text):
        raise ValueError("Number of grids must be at least 1.")
    try:
        number = int(text)
    except (TypeError, ValueError):
        raise ValueError("Number of grids must be at least 1.") from None
    if number < 1:
        raise ValueError("Number of grids must be at least 1.")
    return number


def calculate_charge_sheet(
    user_category,
    service_stage,
    number_of_grids,
    grid_source,
    grid_type=None,
    actual_slots=1,
    processing_requested=False,
):
    """Calculate every chargeable component from validated business inputs."""
    category = pricing_category(user_category)
    stage = normalize_service_stage(service_stage)
    grids = parse_number_of_grids(number_of_grids)
    source = normalize_grid_source(grid_source)
    selected_grid_type = normalize_grid_type(grid_type)
    if source == "facility" and not selected_grid_type:
        raise ValueError("Please select the grid type for facility-provided grids.")

    try:
        slots = Decimal(str(actual_slots))
    except (InvalidOperation, ValueError):
        raise ValueError("Actual slots must be positive.") from None
    if slots <= 0:
        raise ValueError("Actual slots must be positive.")
    config = CHARGE_CONFIG[category]

    grid_charge = (
        config["grid"][selected_grid_type] * grids if source == "facility" else Decimal("0")
    )
    # The configured rate is for four grids, not a minimum billable quantity.
    handling_charge = Decimal(grids) * config["handling_per_4"] / FOUR
    clip_base_charge = config["clip_base"] if stage in {"Screening / Clipping", "Data Collection"} else Decimal("0")
    slot_charge = config["slot"] * slots if stage in {"Screening / Clipping", "Data Collection"} else Decimal("0")
    processing_charge = PROCESSING_RATE if processing_requested and category != "internal" else Decimal("0")
    subtotal = grid_charge + handling_charge + clip_base_charge + slot_charge + processing_charge
    gst = subtotal * config["gst_rate"]
    result = {
        "user_category": category,
        "service_stage": stage,
        "number_of_grids": grids,
        "actual_grids": grids,
        "grid_source": source,
        "grid_type": selected_grid_type if source == "facility" else None,
        "actual_slots": slots,
        "grid_charge": _money(grid_charge),
        "handling_charge": _money(handling_charge),
        "clip_base_charge": _money(clip_base_charge),
        "slot_charge": _money(slot_charge),
        "processing_charge": _money(processing_charge),
        "subtotal": _money(subtotal),
        "gst": _money(gst),
        "gst_amount": _money(gst),
        "grand_total": _money(subtotal + gst),
        "total_billed": _money(subtotal + gst),
    }
    # Legacy column names remain available while their values come from the
    # same calculation (freezing_charge is the facility grid component).
    result.update({
        "freezing_charge": result["grid_charge"],
        "clipping_charge": result["clip_base_charge"],
    })
    return result


def is_non_billable_booking(booking):
    return bool(NON_BILLABLE_PI_PATTERN.search((booking["pi_name"] or "").strip()))


def calculate_booking_revenue(booking, actual_slots, actual_grids, processing_requested=False,
                              grid_source="facility", grid_type="normal_holey_carbon",
                              service_stage="Data Collection", user_category=None):
    """Compatibility adapter for existing completion routes."""
    charges = calculate_charge_sheet(
        user_category or booking["origin"], service_stage, actual_grids, grid_source, grid_type,
        actual_slots, processing_requested,
    )
    if is_non_billable_booking(booking):
        for key in ("grid_charge", "handling_charge", "clip_base_charge", "slot_charge",
                    "processing_charge", "subtotal", "gst", "gst_amount", "grand_total",
                    "total_billed", "freezing_charge", "clipping_charge"):
            charges[key] = ZERO
    return charges

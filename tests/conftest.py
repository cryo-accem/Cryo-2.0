"""Shared test setup."""
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _bill_to_not_required(request):
    """Billing tests predate the PI "To" address; only its own tests require it.

    tests/test_charge_sheet_bill_to.py exercises the address requirement.
    """
    if request.module.__name__.endswith("test_charge_sheet_bill_to"):
        yield
        return
    with patch("blueprints.admin.missing_bill_to", return_value=[]):
        yield

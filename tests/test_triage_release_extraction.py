"""Tests for #1158: triage release date extraction validation."""

from __future__ import annotations

import pytest

from agents.triage.agent import _warn_bare_monthly_release


class TestWarnBareMonthlyRelease:
    """_warn_bare_monthly_release detects when a ticket description
    mentions a specific month/date but triage set a bare 'monthly'
    release that would resolve to the latest build."""

    def test_bare_monthly_with_month_name(self):
        warning = _warn_bare_monthly_release(
            "Test the August monthly image on S32G",
            "monthly",
        )
        assert warning is not None
        assert "August" in warning

    def test_bare_monthly_from_month_name(self):
        warning = _warn_bare_monthly_release(
            "Use monthly from August for the S32G image",
            "monthly",
        )
        assert warning is not None
        assert "monthly from August" in warning

    def test_bare_monthly_with_month_and_year(self):
        warning = _warn_bare_monthly_release(
            "Use the July 2026 monthly build",
            "monthly",
        )
        assert warning is not None
        assert "July 2026" in warning

    def test_bare_monthly_with_datestamp(self):
        warning = _warn_bare_monthly_release(
            "Flash the 202608010205 build",
            "monthly",
        )
        assert warning is not None
        assert "202608010205" in warning

    def test_bare_monthly_with_yyyymm(self):
        warning = _warn_bare_monthly_release(
            "Use monthly build 202607",
            "monthly",
        )
        assert warning is not None
        assert "202607" in warning

    def test_bare_monthly_with_abbreviated_month(self):
        warning = _warn_bare_monthly_release(
            "Run boot-time on the Aug monthly image",
            "monthly",
        )
        assert warning is not None
        assert "Aug" in warning

    def test_qualified_monthly_no_warning(self):
        """No warning when release already includes a date qualifier."""
        warning = _warn_bare_monthly_release(
            "Test the August monthly image",
            "monthly/autosd10-202608010205",
        )
        assert warning is None

    def test_nightly_no_warning(self):
        """No warning for non-monthly releases."""
        warning = _warn_bare_monthly_release(
            "Test the August nightly image",
            "nightly",
        )
        assert warning is None

    def test_no_date_reference_no_warning(self):
        """No warning when description doesn't mention a date."""
        warning = _warn_bare_monthly_release(
            "Run boot-time test on the monthly image",
            "monthly",
        )
        assert warning is None

    def test_empty_description_no_warning(self):
        warning = _warn_bare_monthly_release("", "monthly")
        assert warning is None

    def test_empty_release_no_warning(self):
        warning = _warn_bare_monthly_release("August monthly image", "")
        assert warning is None

    def test_case_insensitive_month(self):
        warning = _warn_bare_monthly_release(
            "use the SEPTEMBER monthly image",
            "monthly",
        )
        assert warning is not None

    @pytest.mark.parametrize(
        "month",
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
    )
    def test_all_months_detected(self, month):
        warning = _warn_bare_monthly_release(
            f"Test the {month} monthly image",
            "monthly",
        )
        assert warning is not None, f"Failed to detect {month}"

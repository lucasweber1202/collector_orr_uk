from datetime import date
from zipfile import BadZipFile

import pytest

from scripts.extract import parse_ods


def test_parser_fails_closed_on_invalid_workbook() -> None:
    with pytest.raises(BadZipFile):
        parse_ods(
            b"not an ods", "orr_rail_fares_7180", "snapshot", "https://orr.gov.uk", date(2026, 1, 1)
        )

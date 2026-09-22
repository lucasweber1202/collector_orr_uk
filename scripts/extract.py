"""ORR rail-fares indices from official ODS tables 7180 and 7182."""

from __future__ import annotations

import hashlib
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urljoin

import httpx
from odf import teletype
from odf.namespaces import TABLENS
from odf.opendocument import load
from odf.table import Table, TableRow

from scripts.config import (
    MAX_DOWNLOAD_BYTES,
    MAX_STALE_MONTHS,
    MIN_HISTORY_YEARS,
    REQUEST_TIMEOUT,
    USER_AGENT,
)
from scripts.snapshots import Snapshot, build_snapshot
from scripts.time_series import Observation

logger = logging.getLogger(__name__)


# -- series_id contract (GUIDELINES.md 4) ---------------------------------
# series_id is uppercase, underscore-separated and ordered coarse -> fine. The
# pair below is the canonical public surface: parse splits an id into its
# components, build rejoins them, and build(*parse(sid)) == sid for every id
# this collector emits. Only economic identity is encoded -- never a delivery
# provider or any other detail of how the value reached us.


def parse_series_id(series_id: str) -> tuple[str, ...]:
    """Split a series_id into its underscore-delimited components.

    Raises ValueError on anything this collector would not have produced:
    lowercase, empty components, or an id with no structure at all.
    """
    if not series_id or series_id != series_id.upper():
        raise ValueError(f"series_id must be uppercase: {series_id!r}")
    components = tuple(series_id.split("_"))
    if any(not component for component in components):
        raise ValueError(f"series_id has an empty component: {series_id!r}")
    return components


def build_series_id(*components: str) -> str:
    """Rejoin the tuple parse_series_id returned into the original id."""
    if not components:
        raise ValueError("series_id needs at least one component")
    if any(not component or component != component.upper() for component in components):
        raise ValueError(f"invalid series_id components: {components!r}")
    return "_".join(components)


# -- 5.1 usable-series filtering ------------------------------------------


@dataclass(frozen=True)
class UsabilityReport:
    """What the filter removed, for logging and for tests to assert on."""

    kept: tuple[str, ...]
    stale: tuple[str, ...]
    short_history: tuple[str, ...]
    empty: tuple[str, ...]

    @property
    def dropped(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.stale) | set(self.short_history) | set(self.empty)))


def _months_between(earlier: date, later: date) -> int:
    """Whole months from ``earlier`` to ``later``, day-of-month aware."""
    months = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    if later.day < earlier.day:
        months -= 1
    return months


def _is_valid(value: Any) -> bool:
    """A real observation: present, numeric and finite."""
    if value is None:
        return False
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(numeric)


def assess_series(
    reference_dates: list[date],
    today: date,
    max_stale_months: int = MAX_STALE_MONTHS,
    min_history_years: float = MIN_HISTORY_YEARS,
) -> str:
    """Classify one series from the reference dates of its valid observations.

    Returns ``"keep"``, ``"empty"``, ``"stale"`` or ``"short_history"``.
    Recency is judged at the period end and over non-null values only: a source
    that keeps listing a discontinued series with empty recent cells must not
    look live because of those blanks.
    """
    if not reference_dates:
        return "empty"
    first, last = min(reference_dates), max(reference_dates)
    if _months_between(last, today) > max_stale_months:
        return "stale"
    if _months_between(first, last) < round(min_history_years * 12):
        return "short_history"
    return "keep"


def filter_usable_series(
    observations: list[Any],
    catalog: dict[str, dict[str, Any]],
    today: date,
    max_stale_months: int = MAX_STALE_MONTHS,
    min_history_years: float = MIN_HISTORY_YEARS,
) -> tuple[list[Any], dict[str, dict[str, Any]], UsabilityReport]:
    """Drop obsolete and history-less series before anything is persisted.

    Runs after parsing and before the time_series / metadata upsert, so the
    standardized tables never carry a dead or stub series, and prunes the
    catalog alongside the observations so metadata can never describe a series
    the database does not hold (GUIDELINES.md 5.1).
    """
    valid_dates: dict[str, list[date]] = {}
    for observation in observations:
        if _is_valid(observation.value):
            valid_dates.setdefault(observation.series_id, []).append(observation.reference_date)

    verdicts: dict[str, str] = {}
    for series_id in set(catalog) | {o.series_id for o in observations}:
        verdicts[series_id] = assess_series(
            valid_dates.get(series_id, []), today, max_stale_months, min_history_years
        )

    keep = {series_id for series_id, verdict in verdicts.items() if verdict == "keep"}
    report = UsabilityReport(
        kept=tuple(sorted(keep)),
        stale=tuple(sorted(s for s, v in verdicts.items() if v == "stale")),
        short_history=tuple(sorted(s for s, v in verdicts.items() if v == "short_history")),
        empty=tuple(sorted(s for s, v in verdicts.items() if v == "empty")),
    )

    if report.dropped:
        logger.info(
            "Usable-series filter: kept %d, dropped %d "
            "(stale=%d short_history=%d empty=%d; max_stale_months=%d min_history_years=%s)",
            len(report.kept),
            len(report.dropped),
            len(report.stale),
            len(report.short_history),
            len(report.empty),
            max_stale_months,
            min_history_years,
        )
        for series_id in report.stale:
            logger.info(
                "Dropped %s: last valid observation older than %d months",
                series_id,
                max_stale_months,
            )
        for series_id in report.short_history:
            logger.info(
                "Dropped %s: valid history shorter than %s years", series_id, min_history_years
            )
        for series_id in report.empty:
            logger.info("Dropped %s: no valid observations", series_id)
    else:
        logger.info("Usable-series filter: all %d series usable", len(report.kept))

    kept_observations = [o for o in observations if o.series_id in keep]
    kept_catalog = {sid: fields for sid, fields in catalog.items() if sid in keep}
    return kept_observations, kept_catalog, report


LANDINGS = {
    "orr_rail_fares_7180": "https://dataportal.orr.gov.uk/statistics/finance/rail-fares/table-7180-average-change-in-fares-by-regulated-and-unregulated-tickets/",
    "orr_rail_fares_7182": "https://dataportal.orr.gov.uk/statistics/finance/rail-fares/table-7182-average-change-in-fares-by-ticket-type/",
}
EXPECTED_SHEETS = {
    "orr_rail_fares_7180": "7180_Change_by_regulated_status",
    "orr_rail_fares_7182": "7182_Change_by_ticket_type",
}


@dataclass(frozen=True)
class ExtractedData:
    observations: list[Observation]
    snapshots: list[Snapshot]
    catalog: dict[str, dict[str, Any]]
    releases: list[datetime]
    availability_by_key: dict[tuple[str, date], tuple[datetime, str, date | None]]
    min_lag_days: int = 0
    max_lag_days: int = 550
    inferred_lag_days: int = 365


def _slug(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", value.upper())


def _rows(table: Table) -> list[list[str]]:
    result: list[list[str]] = []
    for row in table.getElementsByType(TableRow):
        values: list[str] = []
        for cell in row.childNodes:
            if getattr(cell, "qname", None) not in {
                (TABLENS, "table-cell"),
                (TABLENS, "covered-table-cell"),
            }:
                continue
            repeat = min(int(cell.getAttribute("numbercolumnsrepeated") or 1), 100)
            values.extend([teletype.extractText(cell).strip()] * repeat)
        while values and not values[-1]:
            values.pop()
        result.append(values)
    return result


def parse_ods(
    body: bytes, source_id: str, snapshot_id: str, source_url: str, released: date
) -> tuple[list[Observation], dict[str, dict[str, Any]], datetime]:
    import io

    document = load(io.BytesIO(body))
    tables = {
        str(table.getAttribute("name")): table
        for table in document.spreadsheet.getElementsByType(Table)
    }
    expected = EXPECTED_SHEETS[source_id]
    if expected not in tables or "Cover_sheet" not in tables:
        raise ValueError(f"ORR workbook missing expected sheet {expected}")
    cover = " ".join(" ".join(row) for row in _rows(tables["Cover_sheet"]))
    match = re.search(r"published at (\d{1,2}:\d{2}) on (\d{1,2} \w+ \d{4})", cover, re.IGNORECASE)
    if not match:
        raise ValueError("ORR workbook has no official publication timestamp")
    published = datetime.strptime(f"{match.group(2)} {match.group(1)}", "%d %B %Y %H:%M").replace(
        tzinfo=UTC
    )
    rows = _rows(tables[expected])
    header_at = next(
        (
            i
            for i, row in enumerate(rows)
            if len(row) >= 4
            and row[:2] in (["Sector", "Regulated or unregulated fares"], ["Sector", "Ticket type"])
        ),
        None,
    )
    if header_at is None:
        raise ValueError("ORR table header drifted")
    header = rows[header_at]
    year_columns = [
        (column, match.group(1))
        for column, label in enumerate(header[2:], start=2)
        if (match := re.match(r"^(\d{4})(?:\s|$)", label))
    ]
    if len(year_columns) < 20:
        raise ValueError("ORR year columns drifted")
    observations: list[Observation] = []
    catalog: dict[str, dict[str, Any]] = {}
    keys: set[tuple[str, date]] = set()
    for row in rows[header_at + 1 :]:
        if len(row) < 3 or not row[0] or not row[1]:
            continue
        sector, category = row[:2]
        series_id = f"ORR_{source_id[-4:]}_{_slug(sector)}_{_slug(category)}"
        catalog[series_id] = {
            "source_id": source_id,
            "name": f"{sector}: {category} rail fares index",
            "description": "Raw annual rail fares index published by the Office of Rail and Road.",
            "frequency": "annual",
            "unit": "index",
            "eco_group": "inflation",
            "source_url": source_url,
            "last_publish_date": released,
        }
        for column, year in year_columns:
            raw = row[column] if column < len(row) else ""
            cleaned = re.sub(r"\[[a-z]+\]", "", raw, flags=re.IGNORECASE).strip()
            if cleaned in {"", "-", ".."}:
                continue
            try:
                value = float(cleaned.replace(",", ""))
            except ValueError as exc:
                raise ValueError(f"Unparseable ORR value {raw!r}") from exc
            reference = date(int(year), 3, 1)
            key = (series_id, reference)
            if key in keys:
                raise ValueError(f"Duplicate ORR key {key}")
            keys.add(key)
            observations.append(Observation(series_id, reference, value, snapshot_id))
    if len(catalog) < 10 or not observations:
        raise ValueError("ORR workbook unexpectedly lost series")
    return observations, catalog, published


def collect() -> ExtractedData:
    fetched = datetime.now(UTC)
    observations: list[Observation] = []
    snapshots: list[Snapshot] = []
    catalog: dict[str, dict[str, Any]] = {}
    releases: list[datetime] = []
    availability: dict[tuple[str, date], tuple[datetime, str, date | None]] = {}
    with httpx.Client(
        timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True
    ) as client:
        for source_id, landing in LANDINGS.items():
            page = client.get(landing)
            page.raise_for_status()
            matches = re.findall(
                r'href=["\']([^"\']+\.ods(?:\?[^"\']*)?)', page.text, re.IGNORECASE
            )
            if len(matches) != 1:
                raise ValueError(f"Expected one ODS artifact on {landing}, found {len(matches)}")
            url = urljoin(landing, matches[0])
            response = client.get(url)
            response.raise_for_status()
            body = response.content
            if not body or len(body) > MAX_DOWNLOAD_BYTES:
                raise ValueError(f"Invalid ORR artifact size {len(body)}")
            digest = hashlib.sha256(body).hexdigest()
            provisional_date = fetched.date()
            parsed, metadata, published = parse_ods(body, source_id, digest, url, provisional_date)
            snapshot = build_snapshot(
                source_id,
                url,
                f"{source_id}.ods",
                body,
                digest,
                response.headers.get("etag"),
                response.headers.get("last-modified"),
                fetched,
                published.date(),
            )
            for fields in metadata.values():
                fields["last_publish_date"] = published.date()
            observations.extend(parsed)
            catalog.update(metadata)
            snapshots.append(snapshot)
            releases.append(published)
            latest_by_series = {
                series_id: max(o.reference_date for o in parsed if o.series_id == series_id)
                for series_id in metadata
            }
            for observation in parsed:
                is_latest = observation.reference_date == latest_by_series[observation.series_id]
                availability[(observation.series_id, observation.reference_date)] = (
                    published if is_latest else fetched,
                    "official_timestamp" if is_latest else "first_seen",
                    published.date() if is_latest else None,
                )
    return ExtractedData(observations, snapshots, catalog, releases, availability)

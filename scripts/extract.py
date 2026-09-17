"""ORR rail-fares indices from official ODS tables 7180 and 7182."""

from __future__ import annotations

import hashlib
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

from scripts.config import MAX_DOWNLOAD_BYTES, REQUEST_TIMEOUT, USER_AGENT
from scripts.snapshots import Snapshot, build_snapshot
from scripts.time_series import Observation

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

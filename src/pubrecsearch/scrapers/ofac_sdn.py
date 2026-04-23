"""OFAC Specially Designated Nationals (SDN) List scraper.

Source: https://www.treasury.gov/ofac/downloads/sdn_xml.zip
Format: ZIP containing sdn.xml
Update frequency: Daily (list is replaced in full on each update)
Relationship type: sanctioned_individual

The full list is ~15,000 entries total; individuals are a subset.
ETag/Last-Modified dedup avoids re-processing unchanged files.
"""

import io
import zipfile
from datetime import date
from xml.etree import ElementTree as ET

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from ..http_client import make_client
from ..base_scraper import BaseScraper
from ..models import DownloadTarget, ParsedIndividual, ParsedRecord

_URL = "https://www.treasury.gov/ofac/downloads/sdn_xml.zip"
_NS = "https://home.treasury.gov/system/files/126/sdn.xsd"


class OfacSdnScraper(BaseScraper):
    source_id = "ofac_sdn"
    schedule = "0 6 * * *"   # daily at 6 AM
    doc_type = "xml"

    def discover(self, state: dict) -> list[DownloadTarget]:
        today = date.today().isoformat()
        return [
            DownloadTarget(
                url=_URL,
                source_id=self.source_id,
                period=today,
                doc_type=self.doc_type,
                filename="sdn_xml.zip",
            )
        ]

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30), reraise=True)
    def fetch(self, target: DownloadTarget) -> bytes:
        with make_client(timeout=60) as client:
            resp = client.get(target.url, headers=target.headers)
            resp.raise_for_status()
            return resp.content

    def parse(self, raw: bytes, target: DownloadTarget) -> list[ParsedRecord]:
        # Extract XML from ZIP
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            xml_name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
            xml_bytes = zf.read(xml_name)

        root = ET.fromstring(xml_bytes)

        # Handle both namespaced and bare XML
        ns = _detect_namespace(root)
        prefix = f"{{{ns}}}" if ns else ""

        records: list[ParsedRecord] = []
        for entry in root.iter(f"{prefix}sdnEntry"):
            sdn_type = _text(entry, f"{prefix}sdnType", ns)
            if sdn_type != "Individual":
                continue

            uid = _text(entry, f"{prefix}uid", ns)
            last = _text(entry, f"{prefix}lastName", ns) or ""
            first = _text(entry, f"{prefix}firstName", ns) or ""
            name = f"{first} {last}".strip() if first else last

            if not name:
                continue

            # Collect aliases
            aliases: list[str] = []
            aka_list = entry.find(f"{prefix}akaList")
            if aka_list is not None:
                for aka in aka_list.iter(f"{prefix}aka"):
                    a_first = _text(aka, f"{prefix}firstName", ns) or ""
                    a_last = _text(aka, f"{prefix}lastName", ns) or ""
                    alias = f"{a_first} {a_last}".strip() if a_first else a_last
                    if alias and alias != name:
                        aliases.append(alias)

            # Programs this person is sanctioned under
            programs: list[str] = [
                _text(p, None, ns) or ""
                for p in entry.iter(f"{prefix}program")
            ]
            programs = [p for p in programs if p]

            # DOB if available
            dob_list = entry.find(f"{prefix}dateOfBirthList")
            dob = None
            if dob_list is not None:
                dob_item = dob_list.find(f"{prefix}dateOfBirthItem")
                if dob_item is not None:
                    dob = _text(dob_item, f"{prefix}dateOfBirth", ns)

            identifiers: dict = {"ofac_uid": uid}
            if programs:
                identifiers["programs"] = programs
            if dob:
                identifiers["dob"] = dob

            excerpt = f"OFAC SDN programs: {', '.join(programs)}" if programs else "OFAC SDN"

            individual = ParsedIndividual(
                name=name,
                relationship="sanctioned_individual",
                excerpt=excerpt,
                identifiers=identifiers,
            )

            # Add alias individuals as separate ParsedIndividuals on the same record
            alias_individuals = [
                ParsedIndividual(
                    name=alias,
                    relationship="sanctioned_individual_alias",
                    excerpt=f"Alias of OFAC SDN entry {uid} ({name})",
                    identifiers={"ofac_uid": uid, "primary_name": name},
                )
                for alias in aliases
            ]

            records.append(
                ParsedRecord(
                    individuals=[individual] + alias_individuals,
                    source_identifier=uid,
                )
            )

        return records


def _text(element, tag: str | None, ns: str | None) -> str | None:
    if tag is None:
        return element.text
    child = element.find(tag)
    return child.text if child is not None else None


def _detect_namespace(root: ET.Element) -> str | None:
    tag = root.tag
    if tag.startswith("{"):
        return tag[1: tag.index("}")]
    return None

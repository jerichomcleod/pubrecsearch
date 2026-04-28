"""All scrapers — Phases 1–4."""

from .atf_ffl import AtfFflScraper
from .cms_open_payments import CmsOpenPaymentsScraper
from .doj_press import DojPressScraper
from .epa_echo import EpaEchoScraper
from .fara import FaraScraper
from .fda_debarment import FdaDebarmentScraper
from .fec_contributions import FecContributionsScraper
from .irs_990 import Irs990Scraper
from .lda import LdaScraper
from .ofac_sdn import OfacSdnScraper
from .oig_exclusions import OigExclusionsScraper
from .sam_exclusions import SamExclusionsScraper
from .sec_edgar import SecEdgarScraper
from .usaspending import UsaspendingScraper

ALL_SCRAPERS = [
    # Phase 1 — small bulk sources
    OfacSdnScraper(),
    AtfFflScraper(),
    OigExclusionsScraper(),
    SamExclusionsScraper(),
    FdaDebarmentScraper(),
    # Phase 2 — large bulk sources
    FecContributionsScraper(),
    SecEdgarScraper(),
    Irs990Scraper(),
    CmsOpenPaymentsScraper(),
    UsaspendingScraper(),
    # Phase 3 — structured API sources
    LdaScraper(),
    FaraScraper(),
    # Phase 4 — HTML scraping / enforcement APIs
    DojPressScraper(),
    EpaEchoScraper(),
]

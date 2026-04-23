"""All scrapers — Phase 1 (small bulk) and Phase 2 (large bulk)."""

from .atf_ffl import AtfFflScraper
from .cms_open_payments import CmsOpenPaymentsScraper
from .fda_debarment import FdaDebarmentScraper
from .fec_contributions import FecContributionsScraper
from .irs_990 import Irs990Scraper
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
]

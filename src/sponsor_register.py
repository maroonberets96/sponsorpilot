"""Matches job-board employer names against the UK sponsor-licence register."""
import re

import pandas as pd

import config
from logger import get_logger

logger = get_logger()

# Legal suffixes stripped for the strict match tier
LEGAL_SUFFIXES = {"ltd", "limited", "plc", "llp", "lp", "inc", "llc", "cic"}
# Additional filler tokens stripped for the looser 'probable' tier
SECONDARY_TOKENS = {"uk", "gb", "group", "holdings", "the", "co", "company"}


def normalize(name, aggressive=False):
    """Normalizes a company name for comparison.

    Strict: lowercase, '&'->'and', punctuation removed, trailing legal
    suffixes stripped. Aggressive additionally strips filler tokens
    ('uk', 'group', ...) anywhere in the name.
    """
    text = name.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    tokens = text.split()

    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    if tokens and tokens[0] == "the":
        tokens.pop(0)
    if aggressive:
        tokens = [t for t in tokens if t not in SECONDARY_TOKENS and t not in LEGAL_SUFFIXES]
    return " ".join(tokens)


class SponsorRegister:
    """Loads the register once and answers 'does this employer sponsor?'."""

    def __init__(self, xlsx_path=None):
        path = xlsx_path or config.COMPANIES_XLSX_PATH
        logger.info(f"Loading sponsor register from {path}...")
        df = pd.read_excel(path)
        names = df[config.COMPANIES_XLSX_COLUMN].dropna().unique().tolist()

        self._strict = {}
        self._aggressive = {}
        for original in names:
            strict_key = normalize(str(original))
            if strict_key:
                self._strict.setdefault(strict_key, str(original))
            aggressive_key = normalize(str(original), aggressive=True)
            if aggressive_key:
                self._aggressive.setdefault(aggressive_key, str(original))
        logger.info(f"Sponsor register loaded: {len(names)} organisations.")

    def check(self, company_name):
        """Returns ('exact'|'probable', register_name) or (None, None)."""
        strict_key = normalize(company_name)
        if strict_key and strict_key in self._strict:
            return "exact", self._strict[strict_key]

        aggressive_key = normalize(company_name, aggressive=True)
        if aggressive_key and aggressive_key in self._aggressive:
            return "probable", self._aggressive[aggressive_key]
        return None, None

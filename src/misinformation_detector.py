"""
misinformation_detector.py — Misinformation and Low Credibility Domain Detector (Bloco 4.4)

Loads a list of unreliable/misinformation domains from yaml configuration
and checks URLs to penalize their scores in the ranker.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from urllib.parse import urlparse
import yaml
from typing import Dict, Any, Tuple, Optional

logger = logging.getLogger(__name__)


class MisinformationDetector:
    """Detects low-credibility or misinformation domains based on configuration."""

    def __init__(self, config_path: Optional[str] = None):
        if config_path is None:
            # Fallback path relative to project root
            base_dir = Path(__file__).resolve().parents[1]
            config_path = str(base_dir / "config" / "misinformation_domains.yaml")

        self.config_path = config_path
        self.domains: Dict[str, Dict[str, Any]] = {}
        self.load_domains()

    def load_domains(self) -> None:
        """Loads domain lists and penalties from YAML file."""
        if not os.path.exists(self.config_path):
            logger.warning(
                f"Misinformation domains file not found at {self.config_path}. "
                "Using empty ruleset."
            )
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                
            entries = data.get("misinformation_domains", [])
            for entry in entries:
                domain = entry.get("domain", "").lower().strip()
                if domain:
                    self.domains[domain] = {
                        "reason": entry.get("reason", "Low credibility"),
                        "penalty": float(entry.get("penalty", 0.5)),
                    }
            logger.info(f"Loaded {len(self.domains)} misinformation domains.")
        except Exception as e:
            logger.error(f"Error loading misinformation domains: {e}", exc_info=True)

    def check_url(self, url: str) -> Tuple[bool, float, str]:
        """
        Checks a URL against blacklisted/unreliable domains.

        Parameters
        ----------
        url : str
            The URL to evaluate.

        Returns
        -------
        (is_flagged, penalty_multiplier, reason)
            - is_flagged: True if the domain matched a low-credibility entry.
            - penalty_multiplier: float from 0.0 to 1.0 (default 1.0 if not flagged).
            - reason: description of the credibility issue.
        """
        if not url:
            return False, 1.0, ""

        try:
            parsed = urlparse(url)
            netloc = parsed.netloc.lower()
            if not netloc:
                # Fallback simple split if parsing failed to extract netloc
                netloc = url.split("/")[0].lower()
            
            # Clean port numbers if present
            if ":" in netloc:
                netloc = netloc.split(":")[0]

            # Match main domain and subdomains
            # For a netloc like "sub.example.com", we check:
            # - "sub.example.com"
            # - "example.com"
            parts = netloc.split(".")
            for i in range(len(parts)):
                sub_domain = ".".join(parts[i:])
                if sub_domain in self.domains:
                    rule = self.domains[sub_domain]
                    return True, rule["penalty"], rule["reason"]

        except Exception as e:
            logger.warning(f"Error parsing URL '{url}' for misinformation check: {e}")

        return False, 1.0, ""

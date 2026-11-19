"""Theme taxonomy for what guests praise and complain about.

The agent's model does the tagging (only for reviews it hasn't seen before). KeywordTagger is the
deterministic fallback for `--no-llm` runs, the offline tests, and any review the model left untagged.
It is a rough fallback built against the recorded reviews, not a substitute for the model.
"""
from __future__ import annotations

import re


TAXONOMY = {
    "Cleanliness": "Property condition", "Maintenance": "Property condition", "Smell/Smoke": "Property condition",
    "Comfort/Beds": "Comfort", "Noise": "Comfort", "Temperature": "Comfort", "Space/Layout": "Comfort",
    "Amenities": "Amenities", "Kitchen": "Amenities", "WiFi": "Amenities", "Pool/Hot tub": "Amenities",
    "Location": "Location & access", "Parking": "Location & access",
    "Host communication": "Host & service", "Check-in": "Host & service", "Accuracy": "Host & service",
    "Value": "Value",
}
THEMES = list(TAXONOMY)


def _clean(themes) -> list[str]:
    return sorted({t for t in themes or [] if t in TAXONOMY})


# --------------------------------------------------------------------------------------------
class KeywordTagger:
    PRAISE = {
        "Cleanliness": r"\b(clean|spotless|tidy|immaculate)\b",
        "Comfort/Beds": r"\b(comfortable|comfy|cozy|cosy|relax(ed|ing)?|acogedor)\b",
        "Space/Layout": r"\b(spacious|roomy|plenty of space|desk)\b",
        "Amenities": r"\b(amenities|well[- ]appointed|well[- ]equipped|fireplace|balcony|everything (we|i) needed|servicios)\b",
        "Kitchen": r"\bkitchen\b",
        "WiFi": r"\b(high speed internet|fast wifi|good wifi)\b",
        "Pool/Hot tub": r"\b(pool|hot tub)\b",
        "Location": r"\b(location|walkable|walk score|close to|nearby|around the corner|steps away|centrico|convenient)\b",
        "Parking": r"\b(free parking|easy parking|found .{0,20}parking)\b",
        "Host communication": r"\b(host|hosts|responsive|communicat\w*|friendly|helpful|kind|amables|respond\w*)\b",
        "Check-in": r"\b(check[- ]?in|easy to find|easy to access|flexible with check)\b",
        "Accuracy": r"\b(as described|exactly (how|like|what)|looked exactly|like the pictures)\b",
        "Value": r"\b(value|worth|refund)\b",
        "Noise": r"\b(quiet|peaceful)\b",
        "Smell/Smoke": r"\b(smelled (absolutely )?wonderful|fresh .{0,20}scent)\b",
    }
    COMPLAIN = {
        "Smell/Smoke": r"\b(cigarette|smoke|smelled like|odou?r)\b",
        "Noise": r"\b(noisy|noise|loud|poorly isolated)\b",
        "WiFi": r"\b(wifi|wi-fi|internet)\b.{0,30}\b(not great|bad|slow|spotty|poor|dropp\w*|cut(ting)? out|kept dying)\b",
        "Parking": r"\b(difficult parking|no parking|hard to park)\b",
        "Maintenance": r"\b(broken|old building|needs repair|leak\w*)\b",
        "Kitchen": r"\b(missing a can opener|no (pots|pans|utensils))\b",
        "Cleanliness": r"\b(dirty|stain\w*|not clean)\b",
        "Temperature": r"\b(too (hot|cold)|no heat|no ac|freezing)\b",
    }

    def tag(self, reviews: list[dict]) -> dict[str, dict]:
        out = {}
        for r in reviews:
            text = (r["text"] or "").lower()
            complaints = [t for t, p in self.COMPLAIN.items() if re.search(p, text)]
            praise = [t for t, p in self.PRAISE.items() if re.search(p, text) and t not in complaints]
            if r["rating"] is None:  # unrated: judge by the text alone
                sentiment = "mixed" if complaints else "positive"
            else:
                pct = r["rating"] / (r["scale"] or 5)
                sentiment = "negative" if pct <= 0.4 else "mixed" if (complaints or pct < 0.8) else "positive"
            out[r["review_id"]] = {"sentiment": sentiment, "praise": _clean(praise), "complaints": _clean(complaints)}
        return out

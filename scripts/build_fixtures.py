"""Build replay fixtures from data/captured_2026-11-18.json.

The payloads mirror the shapes the live sites returned on 2026-11-18 (the Airbnb listing page's
embedded state, the Airbnb reviews GraphQL response, and Booking's AWS WAF challenge page), filled
with the values captured that day. To replace them with raw recorded responses, run
`python -m review_monitor run --mode record` from a normal network.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CAP = json.loads((ROOT / "data" / "captured_2026-11-18.json").read_text())
FIX = ROOT / "fixtures"
TYPES = {"Cleanliness": "CLEANLINESS", "Accuracy": "ACCURACY", "Check-in": "CHECKIN",
         "Communication": "COMMUNICATION", "Location": "LOCATION", "Value": "VALUE"}

WAF_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title></title>
<script type="text/javascript">window.awsWafCookieDomainList = ['booking.com'];
window.gokuProps = {"key":"FIXTURE","iv":"FIXTURE","context":"FIXTURE"};</script>
<script src="https://FIXTURE.token.awswaf.com/challenge.js"></script></head>
<body><div id="challenge-container"></div><noscript>JavaScript is required</noscript></body></html>"""


def write(key, status, body, url=""):
    path = FIX / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": status, "url": url, "body": body}, ensure_ascii=False))


def main():
    for home, a in CAP["airbnb"].items():
        lid = a["listing_id"]
        state = {"niobeClientData": [["StaysPdpSections", {"data": {"node": {"pdpPresentation": {"quality": {
            "listingRatingStats": {"overallRatingStats": {"ratingAverage": a["overall"], "ratingCount": str(a["review_count"])}},
            "categoryRatings": [{"categoryType": TYPES[k], "label": k, "localizedRating": f"{v:.1f}"}
                                for k, v in a["categories"].items()]}}}}}]]}
        html = ('<!doctype html><html><head><script>window.__cfg={"api_config":{"key":"'
                + "0" * 32 + '"}}</script></head><body><script id="data-deferred-state-0" type="application/json">'
                + json.dumps(state) + "</script></body></html>")
        write(f"airbnb/page/{lid}", 200, html, f"https://www.airbnb.com/rooms/{lid}")
        reviews = [{"id": r["id"], "comments": r["text"], "createdAt": f"{r['date']}T12:00:00Z",
                    "rating": r["rating"], "reviewer": {"firstName": r["reviewer"]}}
                   for r in CAP["airbnb_reviews"] if str(r["home"]) == home]
        payload = {"data": {"presentation": {"stayProductDetailPage": {"reviews": {"reviews": reviews}}}}}
        write(f"airbnb/reviews/{lid}", 200, json.dumps(payload, ensure_ascii=False))

    import tomllib
    homes = tomllib.loads((ROOT / "listings.toml").read_text())["home"]
    for h in homes:
        slug = h["booking"].rstrip("/").split("/")[-1].replace(".html", "")
        write(f"booking/page/{slug}", 202, WAF_PAGE, h["booking"])

    (FIX / "manifest.json").write_text(json.dumps({
        "as_of": "2026-11-18T21:20:00+00:00",
        "source": "data/captured_2026-11-18.json",
        "note": "Shapes mirror live responses observed 2026-11-18; values captured that day; reviewer names anonymized."},
        indent=2) + "\n")

    print("fixtures written:", sum(1 for _ in FIX.rglob("*.json")))


if __name__ == "__main__":
    main()

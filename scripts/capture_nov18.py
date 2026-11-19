"""Write data/captured_2026-11-18.json: the values collected during the Nov 18 onsite runs.

Airbnb values come from the airbnb-reviews custom tool (run 202611181220, 30-day window).
Booking.com overall scores were read by the agent's browser from each listing page on the
same day; the review list was behind AWS WAF bot protection, so no Booking reviews exist.
Reviewer first names are replaced with anonymous labels and review ids with synthetic ones.
"""
import json
from pathlib import Path

CATS = ["Cleanliness", "Accuracy", "Check-in", "Communication", "Location", "Value"]
AIRBNB = {  # home: (listing_id, overall, review_count, category scores in CATS order)
    1: ("1108914798187931072", 4.66, 334, [4.7, 4.7, 4.8, 4.8, 4.6, 4.6]),
    2: ("1369831969375510019", 4.46, 219, [4.5, 4.4, 4.8, 4.8, 4.4, 4.4]),
    3: ("1369819248348941727", 4.63, 163, [4.8, 4.6, 4.7, 4.8, 4.6, 4.6]),
    4: ("1057301472670770851", 4.72, 192, [4.8, 4.7, 4.9, 4.9, 4.8, 4.6]),
    5: ("808784859146076200", 4.74, 50, [4.7, 4.8, 4.4, 4.7, 5.0, 4.7]),
    6: ("47411844", 4.54, 72, [4.5, 4.5, 4.9, 4.9, 4.6, 4.6]),
    7: ("46192879", 4.70, 159, [4.6, 4.8, 4.9, 4.8, 4.9, 4.7]),
    8: ("583425371118987890", 4.60, 87, [4.6, 4.7, 4.8, 4.8, 4.7, 4.7]),
    9: ("634644403422457312", 4.67, 48, [4.8, 4.7, 4.8, 4.9, 4.8, 4.6]),
    10: ("962471135703965656", 4.78, 58, [4.7, 4.8, 4.9, 4.9, 4.8, 4.8]),
}
BOOKING_OVERALL = {1: 7.7, 2: 7.0, 3: 7.6, 4: 7.8, 5: 7.8, 6: 9.0, 7: 8.9, 8: 8.1, 9: 8.4, 10: 8.5}
BOOKING_EXTRA = {1: {"Review count": 180, "Cleanliness": 8.7, "Location": 8.5, "Value for money": 8.5}}

# (review_id, home, date, rating, text)
REVIEWS = [
    ("9000000000000000001", 1, "2026-11-17", 5, "I would highly recommend  The place was great and spacious and spotless"),
    ("9000000000000000002", 1, "2026-11-15", 5, "We had an amazing stay! Everything was perfect. The house was beautiful, spotless, very comfortable, and well-organized. It also smelled absolutely wonderful, with such a fresh and pleasant scent throughout the house! It had everything we needed, and we truly felt at home. The location was fantastic, perfectly situated between Niagara Falls and Toronto, making it easy to visit both places. The host was wonderful and clearly put a lot of care into every detail. This was one of our best Airbnb experiences! We would definitely stay here again and highly recommend this place to anyone visiting the area. Thank you so much for everything!"),
    ("9000000000000000003", 1, "2026-11-15", 5, "Absolutely fantastic stay. The apartment was really clean and taken care of. My hosts were very responsive within minutes and so kind and friendly. It was a lovely part of Hamilton with loads to do in the area. I would definitely stay here again when I visit Hamilton in the future. Thanks again for a great stay."),
    ("9000000000000000004", 1, "2026-11-11", 5, "Was a great stay and was very clean"),
    ("9000000000000000005", 1, "2026-11-08", 5, "Extremely clean and an excellent home. Would definitely come back again."),
    ("9000000000000000006", 1, "2026-11-06", 5, "Excelente lugar, muy acogedor y centrico"),
    ("9000000000000000007", 1, "2026-11-06", 5, "I had a good experience with my stay in this one bedroom suite. It had all the listed amenities and was located in a great location. The host was very friendly and communicated well."),
    ("9000000000000000008", 1, "2026-11-03", 5, "I had a great stay! The place was comfortable, clean, and exactly what I needed. The hosts were welcoming and made the whole experience easy and enjoyable. Great location. Quiet and peaceful. I would definitely recommend staying here."),
    ("9000000000000000009", 1, "2026-11-01", 5, "This was an amazing space to check into! It looked exactly how we expected! Full kitchen so we got to cook exactly how we wanted too. We can't wait to stay again!"),
    ("9000000000000000010", 1, "2026-10-30", 5, "Great place perfect to study and work well away from home with in 15 mins to the university"),
    ("9000000000000000011", 1, "2026-10-30", 5, "Perfect location nice people I will definitely be going back on. My next visit to Hamilton"),
    ("9000000000000000012", 1, "2026-10-26", 5, "Great location, close to Agusta street that has some great restaurants and bars. next to the access if you need to get to upper Hamilton or the link. The space was nice and clean. The hosts were great. I will definitely stay here again."),
    ("9000000000000000013", 1, "2026-10-24", 5, "really great stay, very responsive hosts, nice place"),
    ("9000000000000000014", 1, "2026-10-23", 5, "I had a great time staying here on my two-week rotation in Hamilton. The place was clean with all the amenities needed for a comfortable stay. The hosts also had excellent communication. No complaints."),
    ("9000000000000000015", 2, "2026-11-17", 5, "Friendly hosts, cute apartment!"),
    ("9000000000000000016", 2, "2026-11-15", 5, "I had a wonderful stay! The place was exceptionally clean, comfortable, and well-maintained. I felt relaxed and at home throughout my stay. Everything was just as I hoped, and I truly enjoyed my time there. I would definitely recommend this place to anyone looking for a clean, peaceful, and comfortable stay. I would happily stay here again in the future."),
    ("9000000000000000017", 2, "2026-11-15", 5, "Thank you for the lovely stay. Was quiet and cute little place. Highly recommended. Would stay again in the future."),
    ("9000000000000000018", 2, "2026-11-13", 5, "Would stay again!!!"),
    ("9000000000000000019", 2, "2026-11-07", 5, "Beautiful place to stay for work"),
    ("9000000000000000020", 2, "2026-11-05", 5, "Beautiful place, great balcony views, very comfortable and clean plus high speed internet. Easy to access and not far from everyday necessities. Would definitely stay again"),
    ("9000000000000000021", 2, "2026-11-01", 3, "This location is in an old building the halls and elevator smelled like cigarette smoke. The people were friendly and the apartment looked exactly like the pictures. It was cozy and quaint. The neighbors were a bit noisy at certain times of the day but not too bad. The WiFi connection was not great but the host was very responsive when I let him know."),
    ("9000000000000000022", 2, "2026-11-01", 5, "The place is cool and was ideal for my stay."),
    ("9000000000000000023", 2, "2026-10-28", 5, "Great place to stay . Walkable to grocery  store Host friendly and responsive"),
    ("9000000000000000024", 2, "2026-10-26", 5, "Great place to stay provided the residents in surrounding apartments aren't crazy. We had a wonderful 4 days here and was a great place to relax and unwind together after everything we were doing around town the first few days. The kitchen is very well stocked pan and utensil wise. We were missing a can opener but we're able to easily get one and left it for future guests. All in all it was a great stay with just a couple minor hiccups that didn't negatively impact the stay in any big way."),
    ("9000000000000000025", 2, "2026-10-25", 5, "Enjoyed my stay. Flexible with check in/out and easy to communicate. Bus station right outside. Clean."),
    ("9000000000000000026", 2, "2026-10-24", 3, "Good stay"),
    ("9000000000000000027", 2, "2026-10-24", 5, "I enjoyed my stay , great location and fast communication"),
    ("9000000000000000028", 2, "2026-10-22", 5, "My second time and definitely will not be my last time booking with them , great location easy to navigate around the city ! Would recommend !!"),
    ("9000000000000000029", 2, "2026-10-21", 5, "Close to home will rent here again!"),
    ("9000000000000000030", 2, "2026-10-20", 5, "Amazing and friendly communication as well as prompt responses! I stayed at this property twice now and will always pick this as number 1 when needing accommodations. The property was clean and cozy."),
    ("9000000000000000031", 3, "2026-11-13", 5, "Very nice place, very walkable area, the surroundings were very cute"),
    ("9000000000000000032", 3, "2026-11-07", 4, "Great apartment for my stay in Hamilton. Awesome location downtown. The host was very responsive and easy to reach.  I was worried about some previous comments mentioning difficult parking, but was able to find free parking on the street within 200m on both a Thursday and Friday night very quickly.  The only downside was how noisy the surrounding area was and the apartment being poorly isolated for it. Would definitely rent again overall if I come back!"),
    ("9000000000000000033", 3, "2026-11-04", 5, "I had a wonderful stay at this Airbnb in Hamilton from November 2-4. The accommodations were comfortable, clean, and well maintained, and everything was exactly what I needed for my visit. The entire experience was smooth and enjoyable. I would happily stay here again and would definitely recommend it to others visiting the area. Thanks for a great stay!"),
    ("9000000000000000034", 3, "2026-11-01", 5, "Staying here for two nights was amazing. Easy to find and everything was exactly as described"),
    ("9000000000000000035", 3, "2026-10-29", 5, "The stay here was super nice, there's plenty of nearby shops which was super convenient in terms of grabbing food or any necessities. The place was cozy and super clean and nonetheless the host was super helpful. I'd definitely stay here again next time I visit!"),
    ("9000000000000000036", 4, "2026-11-15", 5, "Excellent spot in a nice area. Absolutely loved all of the businesses right around the corner on James St."),
    ("9000000000000000037", 4, "2026-11-13", 5, "So many restaurants and cafes nearby!"),
    ("9000000000000000038", 4, "2026-11-09", 5, "Muy amables. Respondian rapidamente, los servicios del departamento muy utiles."),
    ("9000000000000000039", 4, "2026-11-03", 5, "We wanted to stay in a character apartment. This place delivered. Well appointed. Well decorated. There were many extra touches that made us feel at home. The desk placed in front of a window. Interesting books. Loved the fireplace in the bedroom. We ate in. We had delicious pizza and chicken from places only steps away. Well stocked grocery store, coffee shops all right there. Kitchen was well equipped."),
    ("9000000000000000040", 4, "2026-11-01", 5, "This is one of my go-to places for a weekend getaway! The hosts are incredibly nice, responsive, and always willing to help with anything you need. I even had something come up that required me to leave a day early, and they were kind enough to provide a full refund for that night. I always have a great experience staying here and would definitely recommend it, 100%!"),
    ("9000000000000000041", 4, "2026-10-27", 5, "nice cute apartment in a very convenient location"),
    ("9000000000000000042", 4, "2026-10-23", 5, "Excellent location/walk score to restaurants, pubs and coffee shops.  Excellent value for the $. The hosts were super responsive as well."),
    ("9000000000000000043", 4, "2026-10-22", 5, "Absolutely perfect"),
    ("9000000000000000044", 5, "2026-11-01", 5, "Great trip! Convenient location"),
    ("9000000000000000045", 7, "2026-11-09", 5, "Super on top of communicating and reaching out to make sure our stay was as smooth as possible. We will be back!"),
]


def main():
    out = {
        "captured_on": "2026-11-18",
        "note": "Values captured during the Nov 18 onsite runs. Reviewer names anonymized; host names removed from review text.",
        "airbnb": {
            str(h): {"listing_id": lid, "overall": o, "review_count": c,
                     "categories": dict(zip(CATS, cats))}
            for h, (lid, o, c, cats) in AIRBNB.items()
        },
        "airbnb_reviews": [
            {"id": rid, "home": h, "date": d, "rating": r, "reviewer": f"Guest {i + 1:02d}", "text": t}
            for i, (rid, h, d, r, t) in enumerate(REVIEWS)
        ],
        "booking_observed": {
            str(h): {"overall": v, **BOOKING_EXTRA.get(h, {})} for h, v in BOOKING_OVERALL.items()
        },
    }
    path = Path(__file__).resolve().parent.parent / "data" / "captured_2026-11-18.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {path} ({len(REVIEWS)} reviews)")


if __name__ == "__main__":
    main()

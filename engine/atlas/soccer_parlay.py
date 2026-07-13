"""Parlay construction for the Soccer tab: three 4-leg tickets by risk tier.

Legs come from the lineup-cleared, book-priced selections the model likes.
One leg per match — the Dixon-Coles grid makes same-match outcomes correlated,
while cross-match legs multiply cleanly, so combined probability is the plain
product of leg probabilities (the independence assumption shown in the modal).

Tiers target a combined hit probability (per the user's spec):
    safe     0.75   (per-leg avg ~0.93 — over 0.5, double chance, AH +1.5/+2)
    medium   0.65   (per-leg avg ~0.90)
    spicier  0.55   (per-leg avg ~0.865)

Search: per match keep the few best high-probability candidates, brute-force
4-match combinations (WC slates are small), pick the ticket whose product lands
closest inside the tier band; payout breaks ties. Payout per book is computed
client-side from each leg's `prices` so the sportsbook dropdown re-prices
without another request.
"""

from __future__ import annotations

from itertools import combinations, product

TIERS = [
    {"key": "safe", "label": "Safe", "target": 0.75, "badge": "v-strong"},
    {"key": "medium", "label": "Medium", "target": 0.65, "badge": "v-solid"},
    {"key": "spicier", "label": "Higher risk", "target": 0.55, "badge": "v-watch"},
]
LEGS = 4
BAND = 0.04          # acceptable |product - target|
MIN_LEG_P = 0.80     # only high-probability-to-hit selections qualify
MIN_BOOKS = 3        # priced widely enough to be real (derived DC exempt)
MIN_CONF = 0.5       # fixture data confidence; relaxed if the slate is thin
RELAXED_CONF = 0.3
PER_MATCH_CANDS = 3  # candidate legs kept per match (diversifies the search)


def _candidates(matches: list[dict], min_conf: float) -> dict[int, list[dict]]:
    """Per match: the best parlay-eligible selections, highest p_model first."""
    by_match: dict[int, list[dict]] = {}
    for m in matches:
        if not (m.get("lineup") or {}).get("cleared"):
            continue
        if float(m.get("data_confidence") or 0) < min_conf:
            continue
        legs = []
        for s in m.get("selections", []):
            if s["p_model"] < MIN_LEG_P or not s.get("prices"):
                continue
            if not s.get("derived") and s.get("n_books", 0) < MIN_BOOKS:
                continue
            legs.append({
                "match_id": m["match_id"],
                "match": f"{m['home']} v {m['away']}",
                "date": m["date"],
                **{k: s[k] for k in ("market", "market_label", "outcome", "line", "label",
                                     "p_model", "fair_p", "prices", "best_odds", "best_book",
                                     "derived")},
            })
        if legs:
            by_match[m["match_id"]] = sorted(legs, key=lambda s: -s["p_model"])[:PER_MATCH_CANDS]
    return by_match


def _payout(legs: tuple[dict, ...]) -> float:
    mult = 1.0
    for leg in legs:
        mult *= leg["best_odds"]
    return mult


def _best_ticket(cands: dict[int, list[dict]], target: float,
                 used: set[frozenset]) -> dict | None:
    """The 4-leg, 4-match ticket whose hit probability lands nearest the target.

    Inside the ±BAND window the highest payout wins; otherwise plain closeness.
    `used` keeps tiers from re-issuing an identical ticket.
    """
    mids = list(cands)
    if len(mids) < LEGS:
        return None
    best, best_rank = None, None
    for combo in combinations(mids, LEGS):
        for legs in product(*(cands[mid] for mid in combo)):
            p = 1.0
            for leg in legs:
                p *= leg["p_model"]
            key = frozenset((leg["match_id"], leg["market"], leg["outcome"], leg["line"])
                            for leg in legs)
            if key in used:
                continue
            dist = abs(p - target)
            in_band = dist <= BAND
            # rank: in-band first, then closeness, then payout
            rank = (0 if in_band else 1, round(dist, 4), -_payout(legs))
            if best_rank is None or rank < best_rank:
                best_rank, best = rank, (legs, p, key)
    if best is None:
        return None
    legs, p, key = best
    used.add(key)
    fair = 1.0
    fair_ok = all(leg.get("fair_p") for leg in legs)
    for leg in legs:
        fair *= leg["fair_p"] if fair_ok else 1.0
    return {
        "legs": list(legs),
        "p_combined": round(p, 4),
        "fair_combined": round(fair, 4) if fair_ok else None,
        "in_band": best_rank[0] == 0,
    }


def build_parlays(matches: list[dict]) -> list[dict]:
    """Three tickets (safe/medium/spicier). Degrades gracefully on thin slates:
    relaxes the confidence gate before giving up, and flags that it did."""
    cands = _candidates(matches, MIN_CONF)
    relaxed = False
    if len(cands) < LEGS:
        cands = _candidates(matches, RELAXED_CONF)
        relaxed = True
    used: set[frozenset] = set()
    out = []
    for tier in TIERS:
        ticket = _best_ticket(cands, tier["target"], used)
        entry = {**tier, "recommended": True, "relaxed_confidence": relaxed}
        if ticket is None:
            entry.update({"legs": [], "p_combined": None,
                          "note": "Not enough lineup-cleared matches to build this ticket."})
        else:
            entry.update(ticket)
            if not ticket["in_band"]:
                entry["note"] = (f"Closest buildable ticket — lands at "
                                 f"{ticket['p_combined']:.0%} vs the {tier['target']:.0%} target.")
            elif relaxed:
                entry["note"] = "Slate was thin — confidence gate relaxed to 30%."
        out.append(entry)
    return out

"""SPAN risk for one combined commodity (one underlying), from risk arrays.

Pure functions over Leg-like objects (attributes: expiry, units, multiplier,
risk[16], composite_delta, instrument, option_type, price) so they can be
tested without a database and reused for BSE / NSE currency.

  scan risk     per scan tier: worst of the 16 scenario losses of all legs in
                the tier (floored at 0); tiers are added, never offset. On an
                NSE expiry day the expiring contracts are a tier of their own,
                which is how the exchange removes calendar-spread benefit.
  spread charge intra-commodity (calendar) spreads in priority order: legs of
                opposite delta in the two expiries form spreads, each charged
                the flat rate per unit of delta.
  short option minimum   som_rate x short option units: all shorts (somMeth GROSS)
                or shorts less longs (NET). The rate is per unit, like the
                risk arrays; 0 on NSE today.
  risk          max(scan + spread, som)
"""
from __future__ import annotations

from dataclasses import dataclass, field

EPS = 1e-9


@dataclass
class CCResult:
    scan_risk: float = 0.0
    tiers: list = field(default_factory=list)      # [(tier_no, risk, worst_scenario)]
    spread_charge: float = 0.0
    spreads: list = field(default_factory=list)    # [(spread_no, spreads_formed, charge)]
    som: float = 0.0
    net_option_value: float = 0.0                  # long option value - short option value
    risk: float = 0.0                              # SPAN requirement: max(scan + spread, som),
                                                   # less net option value if that rule is on


def _tier_of(expiry: str, tiers: list) -> int | None:
    for no, start, end in tiers:
        if (start is None or start <= expiry) and (end is None or expiry <= end):
            return no
    return None


def scan_risk(legs, tiers: list) -> tuple[float, list]:
    """tiers: [(tier_no, start_expiry, end_expiry)]; empty = one tier for everything."""
    groups: dict = {}
    for lg in legs:
        no = _tier_of(lg.expiry, tiers) if tiers else 1
        if no is None:
            raise ValueError(f"expiry {lg.expiry} is in no scan tier {tiers}")
        groups.setdefault(no, []).append(lg)
    total, detail = 0.0, []
    for no in sorted(groups):
        losses = [0.0] * 16
        for lg in groups[no]:
            q = lg.units * lg.multiplier
            for s in range(16):
                losses[s] += q * lg.risk[s]
        worst = max(range(16), key=lambda s: losses[s])
        risk = max(losses[worst], 0.0)
        detail.append((no, risk, worst + 1))
        total += risk
    return total, detail


def _tier_spread(delta: dict, spread_legs: list) -> float:
    """MCX: legs are intra tiers, (start, end) expiry ranges. Side A takes deltas of one sign
    in its tier, side B the other sign in its tier (the same tier on both sides pairs that
    tier's long and short deltas); spreads formed = the smaller, in whichever direction forms
    more. Deltas are used up nearest expiry first. -> spreads formed (consumed from delta)."""
    (ra, sa, qa), (rb, sb, qb) = [(r, s, q) for r, s, q in spread_legs][:2]
    if sa == sb:
        return 0.0
    inside = lambda rng: sorted(e for e in delta if rng[0] <= e[:len(rng[0])] and e[:len(rng[1])] <= rng[1])  # noqa: E731

    def avail(rng, sign):
        return sum(delta[e] for e in inside(rng) if delta[e] * sign > EPS) * sign

    best, n = None, 0.0
    for sign in (1, -1):              # side A long / side B short, or the other way
        k = min(avail(ra, sign) / qa, avail(rb, -sign) / qb)
        if k > n + EPS:
            best, n = sign, k
    if best is None:
        return 0.0
    for rng, sign, need in ((ra, best, n * qa), (rb, -best, n * qb)):
        for e in inside(rng):
            if need <= EPS:
                break
            if delta[e] * sign > EPS:
                take = min(delta[e] * sign, need)
                delta[e] -= sign * take
                need -= take
    return n


def spread_charge(legs, spreads: list) -> tuple[float, list]:
    """spreads: [(spread_no, rate, [(expiry, side 'A'|'B', ratio)])] in priority order; a leg's
    expiry may be a (start, end) range: an MCX intra tier (tier-based spreads, _tier_spread)."""
    delta: dict = {}
    for lg in legs:
        delta[lg.expiry] = delta.get(lg.expiry, 0.0) + lg.units * lg.multiplier * lg.composite_delta
    total, detail = 0.0, []
    for no, rate, spread_legs in spreads:
        if spread_legs and isinstance(spread_legs[0][0], tuple):
            n = _tier_spread(delta, spread_legs)
            if n > EPS:
                total += n * rate
                detail.append((no, n, n * rate))
            continue
        ds = [delta.get(e, 0.0) for e, _, _ in spread_legs]
        if any(abs(d) < EPS for d in ds):
            continue
        # legs on side A must all have one sign, legs on side B the other
        sign_a = None
        ok = True
        for (e, side, _), d in zip(spread_legs, ds):
            s = (1 if d > 0 else -1) * (1 if side == "A" else -1)
            sign_a = s if sign_a is None else sign_a
            ok = ok and s == sign_a
        if not ok:
            continue
        n = min(abs(d) / ratio for (_, _, ratio), d in zip(spread_legs, ds))
        for (e, _, ratio), d in zip(spread_legs, ds):
            delta[e] = d - (1 if d > 0 else -1) * n * ratio
        charge = n * rate
        total += charge
        detail.append((no, n, charge))
    return total, detail


def cc_risk(legs, scan_tiers: list, spreads: list, som_rate: float | None,
            less_net_option_value: bool, som_method: str | None = "GROSS") -> CCResult:
    r = CCResult()
    r.scan_risk, r.tiers = scan_risk(legs, scan_tiers)
    r.spread_charge, r.spreads = spread_charge(legs, spreads)
    short_units = sum(-lg.units * lg.multiplier for lg in legs if lg.instrument == "OPT" and lg.units < 0)
    if (som_method or "GROSS").upper() == "NET":
        long_units = sum(lg.units * lg.multiplier for lg in legs if lg.instrument == "OPT" and lg.units > 0)
        short_units = max(short_units - long_units, 0.0)
    r.som = (som_rate or 0.0) * short_units
    r.net_option_value = sum(lg.units * lg.multiplier * lg.price for lg in legs if lg.instrument == "OPT")
    r.risk = max(r.scan_risk + r.spread_charge, r.som)
    if less_net_option_value:
        r.risk = max(r.risk - r.net_option_value, 0.0)
    return r

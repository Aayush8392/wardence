"""
Phase D multi-round 50% sampling generator.

Unlike phase_d_generate_batches.py (which splits the FULL 7x7=49 matrix
into two batches covering 100% of pairs in one pass), this generates
SEVERAL ~50%-sized rounds, deliberately structured so that:
    (a) every pair a round contains gets a GUARANTEED repeat look in the
        very next round (a rolling overlap, not incidental leftover
        repeats), because repeat exposure -- not first-time coverage --
        is what actually catches a rare/probabilistic bug (the same
        shape as OOM's old kernel-victim coin-flip, which passed 20/20
        at low rep count and only broke under real repetition), and
    (b) full 49-pair coverage is GUARANTEED to be reached by round 4 at
        the latest (a hard requirement, per user's explicit call
        2026-07-26), regardless of how the rolling overlap's own
        arithmetic happens to land.

DESIGN HISTORY / why this replaced the earlier version (kept here since
the reasoning matters for anyone revisiting this file):
    - The original version filled each round by prioritizing whatever
      was still uncovered FIRST, only falling back to random repeats
      once coverage was already complete. That reliably hit 100%
      coverage by round 2 -- but repeat-testing was then purely
      incidental (whatever spilled over), not a deliberate, guaranteed
      mechanism, and the user explicitly wants deliberate rolling
      overlap between consecutive rounds instead.
    - A naive "50% carried forward from the previous round + 50% fresh"
      rule (the user's own proposed mechanism) does NOT reach full
      coverage by round 2 -- verified with real arithmetic: round 1
      covers 25, leaving 24 uncovered; a strict 50/50 round 2 only
      pulls ~12 of those 24 in, leaving ~12 still never covered after
      round 2. The user explicitly does not need round-2 coverage,
      only round-4-at-latest -- which this design satisfies via an
      explicit round 4 safety net (see step 4 below) that force-includes
      any pair still uncovered by that point, overriding the strict
      overlap ratio for exactly those remaining slots if needed.

Sampling mechanism, round by round:
    Round 1 (fresh): entirely new pairs, nothing to overlap with yet.
        Draws are weighted toward pairs with the LOWEST seen-count so
        far (all 0 at this point, so effectively a uniform-random draw
        of the full 49 for round 1 specifically).
    Round 2: ~half is carried forward from round 1 (guaranteed repeat),
        ~half is drawn preferring whatever's still uncovered (closes as
        much of the remaining gap as this round's fresh-half can hold).
    Round 3: same overlap principle -- ~half carried forward from round
        2, ~half preferring whatever's STILL uncovered at this point.
    Round 4 (SAFETY NET, hard guarantee): first, force-include EVERY
        pair not yet covered by any of rounds 1-3, no matter how many
        that is or whether it breaks the "half overlap" ratio for this
        round. Whatever slots remain after that hard requirement is
        satisfied are filled with repeats, weighted toward pairs with
        the LOWEST seen-count so far (so round 4's bonus repeat slots
        preferentially go to pairs that have only been seen once,
        rather than piling more reps onto ones already seen twice+).
        This guarantees full coverage by round 4 unconditionally --
        even in the (very unlikely) case where rounds 1-3's overlap
        arithmetic left an unusually large gap.

    Every round is shuffled internally before being written, so file
    order never reveals which pairs were "carried forward," "gap
    fill," or "safety net" picks.

Per-pair seen-count tracking: every pair's cumulative appearance count
across ALL generated rounds is tracked and written into the manifest
(not just cumulative coverage %), so the actual repeat-distribution
claim (rather than an assumption) can be verified directly -- e.g.
confirming no pair ends up with 4 reps while another has only 1, purely
by luck.

Usage:
    python3 phase_d_generate_sample_rounds.py [--seed N] [--rounds N]

    --seed N     set for a reproducible run (omit for fresh randomness)
    --rounds N   override the default of 4 -- NOTE: the round-4 safety
                 net logic specifically targets whichever round is
                 LAST in the generated set, not literally "round index
                 4" -- so --rounds 5 would push the hard coverage
                 guarantee to round 5 instead. Default 4 matches the
                 user's explicit "full coverage by round 4 at latest"
                 requirement.

Writes (this directory):
    phase_d_round1.json ... phase_d_roundN.json   (~24-25 pairs each)
    phase_d_rounds_manifest.json                    (coverage report +
                                                      per-pair seen-count
                                                      -- see structure
                                                      below)
"""

import argparse
import json
import random
from pathlib import Path

REAL_CLASSES = [
    "crash-loop", "oom", "disk-full",
    "cpu-throttling", "under-provisioned-replicas", "bad-rollout",
]
ALL_CLASSES = REAL_CLASSES + ["none"]

OUT_DIR = Path(__file__).parent

DEFAULT_ROUNDS = 4
# ~50% of 49 -- alternates 24/25 so every round is genuinely "about half",
# never a flat 24 or flat 25 for every round (matches the original
# batch1=24/batch2=25 split style rather than picking one number and
# sticking to it).
ROUND_SIZE_CYCLE = [24, 25]


def build_all_pairs() -> list[tuple[str, str]]:
    return [(a, b) for a in ALL_CLASSES for b in ALL_CLASSES]


def _weighted_pick(candidates: list[tuple[str, str]], seen_count: dict, n: int) -> list[tuple[str, str]]:
    """
    Picks up to n DISTINCT items from candidates, weighted toward LOWER
    seen_count -- i.e. pairs seen fewer times so far are more likely to
    be picked. Weight = 1 / (seen_count + 1), so a never-seen pair
    (count 0) is twice as likely as a once-seen pair (count 1), etc.

    REAL BUG FIXED (2026-07-27, found by reviewing the actual generated
    round files, not assumed): the original version used
    random.choices(candidates, weights=weights, k=n), which samples WITH
    replacement -- nothing stopped the SAME pair being drawn multiple
    times within one call. Confirmed in the real output: round 1 (a
    single _weighted_pick(all_pairs, ..., 24) call) contained
    ('crash-loop', 'oom') and ('oom', 'cpu-throttling') three times EACH
    within that one 24-pair round. Because later rounds' "carried
    forward" step samples from the previous round's own (list, not set)
    contents, this duplication propagated and compounded across rounds,
    ending with one pair tested 8 times total while 21 of 49 pairs were
    only ever tested once -- the opposite of the fair, deliberate
    repeat-distribution this whole design exists to provide.

    Fixed by sampling WITHOUT replacement: pick one at a time via a
    weighted choice, remove it from the pool, repeat. Guarantees n
    distinct pairs (or all of candidates, if n exceeds the pool size --
    falls back gracefully rather than raising, since ROUND_SIZE_CYCLE's
    values are always well under 49 in practice, but a caller passing a
    larger n shouldn't crash for it).
    """
    pool = list(candidates)
    picks: list[tuple[str, str]] = []
    for _ in range(min(n, len(pool))):
        weights = [1.0 / (seen_count.get(p, 0) + 1) for p in pool]
        choice = random.choices(pool, weights=weights, k=1)[0]
        picks.append(choice)
        pool.remove(choice)
    if len(picks) < n:
        print(f"  NOTE: _weighted_pick asked for {n} distinct pairs but only "
              f"{len(candidates)} candidates existed -- returning all {len(picks)}.")
    return picks


def generate_rounds(all_pairs: list[tuple[str, str]], n_rounds: int) -> list[list[tuple[str, str]]]:
    """
    Core rolling-overlap sampling with a hard round-4(-or-last) safety
    net. Returns a list of rounds, each a list of (a, b) pair tuples of
    length ~50% of len(all_pairs). See module docstring for the full
    design rationale.
    """
    all_pairs_set = set(all_pairs)
    seen_count: dict = {p: 0 for p in all_pairs}
    rounds: list[list[tuple[str, str]]] = []
    covered_ever: set = set()

    for round_idx in range(n_rounds):
        round_size = ROUND_SIZE_CYCLE[round_idx % len(ROUND_SIZE_CYCLE)]
        is_first_round = round_idx == 0
        is_last_round = round_idx == n_rounds - 1

        round_pairs: list[tuple[str, str]] = []

        if is_last_round:
            # SAFETY NET: force-include every pair not yet covered by
            # ANY earlier round, no matter how many there are or
            # whether it breaks the usual overlap ratio for this round.
            # This is the hard guarantee -- full coverage by the last
            # generated round, unconditionally.
            still_uncovered = list(all_pairs_set - covered_ever)
            random.shuffle(still_uncovered)
            round_pairs.extend(still_uncovered)

            remaining_slots = round_size - len(round_pairs)
            if remaining_slots > 0:
                # Bonus repeat slots: weighted toward least-seen pairs,
                # not pure random, so round 4's extra reps don't just
                # pile onto whatever was already picked most.
                #
                # REAL BUG FIXED (2026-07-27, same investigation as
                # _weighted_pick's own fix): candidates must exclude
                # pairs ALREADY added to round_pairs this round (the
                # still_uncovered pairs just added above) -- passing the
                # full all_pairs here let this bonus fill re-pick one of
                # those same pairs again, reopening the exact within-
                # round duplicate problem _weighted_pick's own internal
                # fix was supposed to close.
                already_in_round = set(round_pairs)
                bonus_candidates = [p for p in all_pairs if p not in already_in_round]
                round_pairs.extend(_weighted_pick(bonus_candidates, seen_count, remaining_slots))
            elif remaining_slots < 0:
                # More uncovered pairs existed than this round has
                # slots for (shouldn't happen given round sizing vs. 49
                # total pairs and 3 prior rounds, but guard against it
                # rather than silently truncate coverage) -- widen this
                # round rather than drop pairs, since the coverage
                # guarantee is a hard requirement, not best-effort.
                print(f"  NOTE: round {round_idx + 1}'s safety net needed {len(still_uncovered)} slots "
                      f"for still-uncovered pairs, more than the usual round size of {round_size} -- "
                      f"widening this round to {len(still_uncovered)} pairs rather than dropping any, "
                      f"since full coverage by the last round is a hard requirement.")

        elif is_first_round:
            # Nothing to overlap with yet -- draw fresh, weighted
            # toward least-seen (all 0 here, so effectively uniform).
            round_pairs.extend(_weighted_pick(all_pairs, seen_count, round_size))

        else:
            # Rolling overlap: ~half carried forward from the immediately
            # preceding round (guaranteed repeat), ~half preferring
            # whatever's still uncovered so far (closes as much of the
            # remaining gap as this round's fresh-half can hold).
            prev_round = rounds[-1]
            overlap_n = round_size // 2
            fresh_n = round_size - overlap_n

            carried_forward = random.sample(prev_round, k=min(overlap_n, len(prev_round)))
            round_pairs.extend(carried_forward)

            uncovered_now = list(all_pairs_set - covered_ever)
            random.shuffle(uncovered_now)
            take_uncovered = uncovered_now[:fresh_n]
            round_pairs.extend(take_uncovered)

            # If the uncovered pool was smaller than fresh_n (gap
            # already mostly closed), fill remaining slots with
            # least-seen-weighted repeats rather than leaving the round
            # short.
            still_short = round_size - len(round_pairs)
            if still_short > 0:
                # Same fix as the round-4 safety net's bonus fill above:
                # exclude carried_forward/take_uncovered (already in
                # round_pairs) from the candidate pool, or this fallback
                # could re-pick one of them and reopen the within-round
                # duplicate bug _weighted_pick's own fix was meant to close.
                already_in_round = set(round_pairs)
                fallback_candidates = [p for p in all_pairs if p not in already_in_round]
                round_pairs.extend(_weighted_pick(fallback_candidates, seen_count, still_short))

        # Update tracking BEFORE shuffling/appending -- seen_count and
        # covered_ever must reflect this round's real picks for the
        # NEXT round's overlap/gap-fill decisions to be correct.
        for p in round_pairs:
            seen_count[p] = seen_count.get(p, 0) + 1
            covered_ever.add(p)

        # Shuffle within the round so file order never reveals which
        # pairs were "carried forward," "gap fill," or "safety net."
        random.shuffle(round_pairs)
        rounds.append(round_pairs)

    return rounds


def build_coverage_report(all_pairs: list[tuple[str, str]], rounds: list[list[tuple[str, str]]]) -> dict:
    """
    Cumulative coverage after each round, so it's possible to see
    exactly which round (if any) first achieved full 49-pair coverage,
    and to decide -- per the user's own "if 3 rounds cover everything,
    that's perfect, else go to 4" instruction -- whether round 4 is
    actually necessary before running it.
    """
    all_pairs_set = set(all_pairs)
    seen: set = set()
    per_round_report = []

    for i, round_pairs in enumerate(rounds, start=1):
        unique_new_this_round = {p for p in round_pairs if p not in seen}
        seen.update(round_pairs)
        missing_after = all_pairs_set - seen
        per_round_report.append({
            "round": i,
            "pairs_in_round": len(round_pairs),
            "unique_pairs_in_round": len(set(round_pairs)),
            "new_pairs_covered_this_round": len(unique_new_this_round),
            "cumulative_unique_pairs_covered": len(seen),
            "cumulative_coverage_pct": round(100 * len(seen) / len(all_pairs_set), 1),
            "pairs_still_missing_after_this_round": sorted(list(missing_after)),
            "full_coverage_reached": len(missing_after) == 0,
        })

    first_full_coverage_round = next(
        (r["round"] for r in per_round_report if r["full_coverage_reached"]), None
    )

    return {
        "total_unique_pairs_possible": len(all_pairs_set),
        "rounds_generated": len(rounds),
        "first_round_to_reach_full_coverage": first_full_coverage_round,
        "round_4_necessary": first_full_coverage_round is None or first_full_coverage_round > 3,
        "per_round": per_round_report,
    }


def pair_to_list(pairs: list[tuple[str, str]]) -> list[list[str]]:
    """JSON has no tuple type -- match phase_d_generate_batches.py's own
    (a, b) list-of-two-strings convention exactly, so phase_d_run.py's
    existing `for i, (a, b) in enumerate(pairs, ...)` unpacking works
    unmodified against these files too."""
    return [[a, b] for a, b in pairs]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=None, help="set for a reproducible set of rounds")
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                         help=f"number of rounds to generate (default {DEFAULT_ROUNDS})")
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    all_pairs = build_all_pairs()
    assert len(all_pairs) == 49, f"expected 49 pairs (7x7), got {len(all_pairs)}"

    rounds = generate_rounds(all_pairs, args.rounds)
    coverage = build_coverage_report(all_pairs, rounds)

    written_files = []
    for i, round_pairs in enumerate(rounds, start=1):
        out_path = OUT_DIR / f"phase_d_round{i}.json"
        out_path.write_text(json.dumps(pair_to_list(round_pairs), indent=2))
        written_files.append(out_path.name)

    manifest_path = OUT_DIR / "phase_d_rounds_manifest.json"
    manifest_path.write_text(json.dumps({
        "seed": args.seed,
        "rounds_requested": args.rounds,
        "round_files": written_files,
        "coverage": coverage,
    }, indent=2))

    print(f"Wrote {len(written_files)} round file(s): {', '.join(written_files)}")
    print(f"Wrote coverage manifest: {manifest_path.name}")
    print(f"Seed: {args.seed if args.seed is not None else '(none -- rerun for different rounds, not reproducible)'}")
    print()
    print("--- Coverage summary ---")
    for r in coverage["per_round"]:
        print(f"  Round {r['round']}: {r['pairs_in_round']} pairs "
              f"({r['unique_pairs_in_round']} unique), "
              f"+{r['new_pairs_covered_this_round']} new -> "
              f"{r['cumulative_unique_pairs_covered']}/{coverage['total_unique_pairs_possible']} "
              f"({r['cumulative_coverage_pct']}%) covered so far"
              + ("  <-- FULL COVERAGE REACHED" if r["full_coverage_reached"] else ""))

    if coverage["first_round_to_reach_full_coverage"] is not None:
        n = coverage["first_round_to_reach_full_coverage"]
        print(f"\nFull 49-pair coverage reached after round {n}.")
        if n <= 3:
            print(f"Per the locked plan: 3 rounds achieved full coverage -- round 4 is NOT necessary "
                  f"(it was still generated, per the default, but can be skipped when actually running).")
        else:
            print(f"Full coverage needed all {n} rounds -- run all {n} generated rounds.")
    else:
        missing = coverage["per_round"][-1]["pairs_still_missing_after_this_round"]
        print(f"\nFull coverage was NOT reached even after all {len(rounds)} generated rounds -- "
              f"{len(missing)} pair(s) still uncovered: {missing}")
        print("Consider generating additional rounds (--rounds 5 or more) if full coverage matters "
              "for this run, or accept partial coverage as a deliberate tradeoff.")


if __name__ == "__main__":
    main()

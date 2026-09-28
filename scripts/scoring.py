"""Cyber Capability Index (written to models.json as `index`; the dashboard reads it from there).

For each capability benchmark, a model's score is normalized to the best
tracked score on that benchmark (best = 100). The index is the mean of those
normalized scores, shrunk toward a neutral prior of 50 with weight PRIOR_WEIGHT,
so a model with a single benchmark cannot outrank one with broad coverage on
thin evidence. Self-reported numbers count at SELF_REPORTED_WEIGHT.
"""

PRIOR = 50.0
PRIOR_WEIGHT = 1.5
SELF_REPORTED_WEIGHT = 0.8


def compute_index(benchmarks: dict) -> dict:
    caps = {b["id"]: b for b in benchmarks["benchmarks"] if b["kind"] == "capability"}
    best: dict[str, float] = {}
    for s in benchmarks["scores"]:
        b = caps.get(s["benchmark"])
        if not b:
            continue
        v = s["value"] if b.get("higher_is_better", True) else 100 - s["value"]
        best[s["benchmark"]] = max(best.get(s["benchmark"], 0), v)

    acc: dict[str, dict] = {}
    for s in benchmarks["scores"]:
        b = caps.get(s["benchmark"])
        if not b or not best.get(s["benchmark"]):
            continue
        v = s["value"] if b.get("higher_is_better", True) else 100 - s["value"]
        w = SELF_REPORTED_WEIGHT if s.get("self_reported") else 1.0
        a = acc.setdefault(s["model"], {"sum": 0.0, "w": 0.0, "n": 0})
        a["sum"] += w * 100 * v / best[s["benchmark"]]
        a["w"] += w
        a["n"] += 1

    return {
        m: {
            "index": round((a["sum"] + PRIOR * PRIOR_WEIGHT) / (a["w"] + PRIOR_WEIGHT), 1),
            "n": a["n"],
        }
        for m, a in acc.items()
    }

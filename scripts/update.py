#!/usr/bin/env python3
"""Daily refresh for the AI Cyber Capability dashboard.

    python scripts/update.py              # full run (Claude analysis if ANTHROPIC_API_KEY is set)
    python scripts/update.py --offline    # skip network; just recompute the snapshot
    python scripts/update.py --no-llm     # collect feeds + buzz only

Writes data/*.json in place and appends today's snapshot to data/history.json
so the dashboard can show trends.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from collect import collect_all, is_relevant, match_models  # noqa: E402
from scoring import compute_index  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HISTORY_DAYS = 400
NEWS_KEEP = 60
PERCEPTION_SMOOTHING = 0.6  # weight on today's reading; the rest carries yesterday's value


def load(name: str) -> dict:
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else {}


def save(name: str, obj: dict) -> None:
    (DATA / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def log(msg: str) -> None:
    print(f"[update] {msg}", file=sys.stderr)


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def valid_url(u: str) -> bool:
    return bool(re.match(r"^https?://[^\s]+\.[^\s]+", u or ""))


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def refs(sources) -> list[dict]:
    return [{"title": s.title, "url": s.url} for s in sources if valid_url(s.url)]


def merge(upd, today: str, models: dict, benchmarks: dict, incidents: dict,
          perception: dict, news: dict, trends: dict) -> list[str]:
    changes: list[str] = []
    known = {m["id"] for m in models["models"]}
    names = {n.lower() for m in models["models"] for n in [m["name"], *m.get("aliases", [])]}

    for nm in upd.new_models:
        mid = slug(nm.id or nm.name)
        if mid in known or nm.name.lower() in names:
            continue
        models["providers"].setdefault(slug(nm.provider), {"name": nm.provider})
        models["models"].insert(0, {
            "id": mid, "name": nm.name, "provider": slug(nm.provider), "category": nm.category,
            "status": "active", "released": nm.released, "access": nm.access,
            "aliases": nm.aliases, "notes": nm.notes, "sources": refs(nm.sources),
            "auto_added": today,
        })
        known.add(mid)
        changes.append(f"new model: {nm.name}")

    bench_ids = {b["id"] for b in benchmarks["benchmarks"]}
    for br in upd.benchmark_results:
        if br.model_id not in known or not valid_url(br.source_url) or not (0 <= br.value <= 100):
            continue
        bid = slug(br.benchmark_id).replace("-", "_")
        if bid not in bench_ids:
            benchmarks["benchmarks"].append({
                "id": bid, "name": br.benchmark_name, "kind": br.benchmark_kind, "unit": "%",
                "higher_is_better": br.higher_is_better,
                "description": "Added automatically; not counted in the index until reviewed.", "url": "",
                "auto_added": today,
            })
            bench_ids.add(bid)
        existing = [s for s in benchmarks["scores"] if s["model"] == br.model_id and s["benchmark"] == bid]
        if existing and abs(existing[0]["value"] - br.value) < 0.05:
            continue
        benchmarks["scores"] = [s for s in benchmarks["scores"] if s not in existing]
        benchmarks["scores"].append({
            "model": br.model_id, "benchmark": bid, "value": round(br.value, 1), "date": br.date,
            "self_reported": br.self_reported, "note": br.note, "source": br.source_url, "auto": True,
        })
        changes.append(f"score: {br.model_id} {bid}={br.value}")

    seen_urls = {s["url"] for i in incidents["incidents"] for s in i.get("sources", [])}
    seen_titles = {slug(i["title"]) for i in incidents["incidents"]}
    for inc in upd.incidents:
        srcs = refs(inc.sources)
        if not srcs or any(s["url"] in seen_urls for s in srcs) or slug(inc.title) in seen_titles:
            continue
        incidents["incidents"].insert(0, {
            "id": f"{inc.date}-{slug(inc.title)[:40]}", "date": inc.date, "type": inc.type,
            "severity": inc.severity, "provider": slug(inc.provider),
            "models": [m for m in inc.model_ids if m in known],
            "title": inc.title, "summary": inc.summary, "sources": srcs, "auto": True,
        })
        changes.append(f"incident: {inc.title}")
    incidents["incidents"].sort(key=lambda i: i["date"], reverse=True)

    a = PERCEPTION_SMOOTHING
    for pu in upd.perception_updates:
        if pu.model_id not in known:
            continue
        prev = perception["models"].get(pu.model_id, {})
        blend = (lambda new, old: new if old is None else a * new + (1 - a) * old)
        perception["models"][pu.model_id] = {
            **prev,
            "capability": round(blend(clamp(pu.capability, 0, 100), prev.get("capability"))),
            "concern": round(blend(clamp(pu.concern, 0, 100), prev.get("concern"))),
            "tone": round(blend(clamp(pu.tone, -1, 1), prev.get("tone")), 2),
            "summary": pu.summary,
            "sources": refs(pu.sources) or prev.get("sources", []),
            "updated": today,
        }
    if upd.perception_updates:
        perception["method"] = "claude"
        changes.append(f"perception: {len(upd.perception_updates)} models re-scored")

    takes = [t for t in upd.provider_takes if t.take.strip()]
    if takes:
        by = {t["provider"]: t for t in perception.get("provider_takes", [])}
        for t in takes:
            by[slug(t.provider)] = {"provider": slug(t.provider), "take": t.take}
        perception["provider_takes"] = list(by.values())

    new_heads = [{
        "date": h.date, "kind": h.kind, "title": h.title, "source": h.source, "url": h.url,
        "models": [m for m in h.model_ids if m in known],
    } for h in upd.headlines if valid_url(h.url)]
    merge_news(news, new_heads)

    ind = {i["id"]: i for i in trends.get("indicators", [])}
    for tp in upd.trend_points:
        i = ind.get(tp.indicator_id)
        # The same finding gets re-reported under different dates; a point is new only if
        # its date and its (source, value) pair are both unseen.
        if i and valid_url(tp.source_url) and not any(
                p["date"] == tp.date or (p["source"] == tp.source_url and p["value"] == tp.value)
                for p in i["points"]):
            i["points"].append({"date": tp.date, "value": tp.value, "source": tp.source_url})
            i["points"].sort(key=lambda p: p["date"])
            changes.append(f"trend: {tp.indicator_id}={tp.value}")
    return changes


def merge_news(news: dict, new_items: list[dict]) -> None:
    urls = {n["url"] for n in news.get("items", [])}
    fresh = [n for n in new_items if n["url"] not in urls]
    news["items"] = sorted(fresh + news.get("items", []), key=lambda n: n["date"], reverse=True)[:NEWS_KEEP]


def snapshot(today: str, models: dict, benchmarks: dict, perception: dict, buzz: Counter | None) -> None:
    hist = load("history.json") or {"snapshots": []}
    idx = compute_index(benchmarks)
    snap = {"date": today, "models": {}}
    for m in models["models"]:
        p = perception["models"].get(m["id"], {})
        row = {
            "index": idx.get(m["id"], {}).get("index"),
            "capability": p.get("capability"), "concern": p.get("concern"), "tone": p.get("tone"),
            "buzz": (buzz or {}).get(m["id"], 0) if buzz is not None else p.get("buzz"),
        }
        snap["models"][m["id"]] = {k: v for k, v in row.items() if v is not None}
    hist["snapshots"] = [s for s in hist["snapshots"] if s["date"] != today] + [snap]
    hist["snapshots"] = sorted(hist["snapshots"], key=lambda s: s["date"])[-HISTORY_DAYS:]
    save("history.json", hist)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="no network; recompute snapshot only")
    ap.add_argument("--no-llm", action="store_true", help="skip Claude analysis")
    ap.add_argument("--days", type=int, default=3, help="lookback window for feeds")
    args = ap.parse_args()

    now = dt.datetime.now(dt.timezone.utc)
    today = now.date().isoformat()
    models, benchmarks, incidents = load("models.json"), load("benchmarks.json"), load("incidents.json")
    perception, news, trends = load("perception.json"), load("news.json"), load("trends.json")
    changes: list[str] = []
    buzz: Counter | None = None
    mode = "offline"

    if not args.offline:
        cfg = json.loads((Path(__file__).parent / "sources.json").read_text())
        items = [it for it in collect_all(cfg, now - dt.timedelta(days=args.days)) if is_relevant(it)]
        for it in items:
            it["models"] = match_models(it, models["models"])
        buzz = Counter(m for it in items for m in it["models"])
        for mid, p in perception["models"].items():
            p["buzz"] = buzz.get(mid, 0)
        log(f"{len(items)} relevant items; buzz: {dict(buzz.most_common(8))}")
        mode = "feeds"

        if os.environ.get("ANTHROPIC_API_KEY") and not args.no_llm:
            import analyze  # imported lazily so --no-llm works without the SDK
            upd = analyze.run(today, models, benchmarks, incidents, perception, items)
            if upd:
                changes = merge(upd, today, models, benchmarks, incidents, perception, news, trends)
                changes.insert(0, upd.run_summary)
                mode = "claude"
        if mode != "claude":
            # Without Claude: surface the most-engaged items that mention a tracked model.
            top = sorted((it for it in items if it["models"]),
                         key=lambda it: it.get("engagement", 0), reverse=True)[:10]
            merge_news(news, [{k: it[k] for k in ("date", "kind", "title", "source", "url", "models")}
                              for it in top])

    idx = compute_index(benchmarks)
    for m in models["models"]:
        m["index"] = idx.get(m["id"])
    stamp = now.isoformat(timespec="minutes")
    models["updated"] = perception["updated"] = news["updated"] = stamp
    for name, obj in [("models.json", models), ("benchmarks.json", benchmarks), ("incidents.json", incidents),
                      ("perception.json", perception), ("news.json", news), ("trends.json", trends)]:
        save(name, obj)
    snapshot(today, models, benchmarks, perception, buzz)
    save("meta.json", {"updated": stamp, "mode": mode, "changes": changes[:30]})
    log(f"done ({mode}); {len(changes)} changes")
    for c in changes:
        log(f"  • {c}")


if __name__ == "__main__":
    main()

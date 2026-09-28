"""Claude-powered analysis: a web-research pass, then a structured extraction pass.

Pass 1 (research): Claude with the web_search server tool looks for anything
new in the last few days — model releases, benchmark results, incidents,
notable commentary — that the feeds may have missed (X/LinkedIn posts, podcast
show notes, lab system cards).

Pass 2 (extract): Claude reads the research brief plus the collected feed items
and returns a validated DailyUpdate object (structured outputs), which
update.py merges into the data files.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Literal, Optional

import anthropic
from pydantic import BaseModel

MODEL = "claude-opus-5"


def log(msg: str) -> None:
    print(f"[analyze] {msg}", file=sys.stderr)


class SourceRef(BaseModel):
    title: str
    url: str


class NewModel(BaseModel):
    id: str
    name: str
    provider: str
    category: Literal["ga", "gated", "open"]
    released: Optional[str]
    access: str
    notes: str
    aliases: list[str]
    sources: list[SourceRef]


class BenchmarkResult(BaseModel):
    model_id: str
    benchmark_id: str
    benchmark_name: str
    value: float
    date: str
    self_reported: bool
    note: str
    source_url: str


class Incident(BaseModel):
    date: str
    type: Literal["breakout", "misuse", "policy"]
    severity: Literal["critical", "serious", "warning", "info"]
    provider: str
    model_ids: list[str]
    title: str
    summary: str
    sources: list[SourceRef]


class PerceptionUpdate(BaseModel):
    model_id: str
    capability: int
    concern: int
    tone: float
    summary: str
    sources: list[SourceRef]


class Headline(BaseModel):
    date: str
    kind: Literal["article", "blog", "podcast", "social", "paper", "official"]
    title: str
    source: str
    url: str
    model_ids: list[str]


class ProviderTake(BaseModel):
    provider: str
    take: str


class TrendPoint(BaseModel):
    indicator_id: str
    date: str
    value: float
    source_url: str


class DailyUpdate(BaseModel):
    run_summary: str
    new_models: list[NewModel]
    benchmark_results: list[BenchmarkResult]
    incidents: list[Incident]
    perception_updates: list[PerceptionUpdate]
    headlines: list[Headline]
    provider_takes: list[ProviderTake]
    trend_points: list[TrendPoint]


SYSTEM = """You maintain a public dashboard that compares the cyber capabilities of frontier AI models \
(generally available, gated cyber-specific, and open-weight). You work for defenders and policy readers. \
Accuracy beats coverage: only report facts you can tie to a source URL, never invent benchmark numbers, \
and prefer primary sources (system cards, lab posts, evaluator reports such as UK AISI, NIST CAISI, METR) \
over aggregators. Distinguish developer self-reported numbers from independent evaluations."""


def _registry_digest(models: dict, benchmarks: dict, incidents: dict, perception: dict) -> str:
    return json.dumps({
        "models": [{k: m.get(k) for k in ("id", "name", "provider", "category", "released", "aliases")}
                   for m in models["models"]],
        "benchmarks": [{k: b[k] for k in ("id", "name", "kind", "unit")} for b in benchmarks["benchmarks"]],
        "known_scores": [f"{s['model']}|{s['benchmark']}|{s['value']}" for s in benchmarks["scores"]],
        "known_incidents": [f"{i['date']} {i['title']}" for i in incidents["incidents"]],
        "current_perception": {k: {x: v.get(x) for x in ("capability", "concern", "tone")}
                               for k, v in perception["models"].items()},
    }, indent=None)


def research(client: anthropic.Anthropic, today: str, digest: str) -> str:
    prompt = f"""Today is {today}. Here is what the dashboard already tracks:
<registry>{digest}</registry>

Search the web for developments from roughly the last 7 days (emphasize the last 48 hours) on:
1. New frontier or notable open-weight model releases (any lab: Anthropic, OpenAI, Google, xAI, Meta, \
Mistral, DeepSeek, Qwen, Z.ai, Moonshot, Xiaomi, etc.) and new gated cyber-specific variants or access programs.
2. Newly published cyber benchmark results (ExploitGym, ExploitBench, CyberGym, Cybench, CyScenarioBench, \
CAISI/AISI evaluations, system cards) for tracked or new models.
3. Incidents: models escaping sandboxes or acting outside containment, models used by threat actors, \
jailbreak disclosures, government access restrictions.
4. How practitioners are talking about these models' cyber abilities: security press, blogs, podcasts, \
X/Bluesky/Reddit/Hacker News discussion.
5. New data points for macro trends (cyber time-horizon doubling, open-weight lag behind frontier).

Write a concise research brief grouped by those five headings. For every claim give the date and source URL. \
Say explicitly when you found nothing new for a heading."""
    messages = [{"role": "user", "content": prompt}]
    text_parts: list[str] = []
    for _ in range(6):  # continue through pause_turn on long server-tool turns
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "high"},
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 25}],
            messages=messages,
        )
        if resp.stop_reason == "refusal":
            log("research pass refused; continuing with feeds only")
            return ""
        text_parts += [b.text for b in resp.content if b.type == "text"]
        if resp.stop_reason != "pause_turn":
            break
        messages = [messages[0], {"role": "assistant", "content": resp.content}]
    return "\n".join(text_parts).strip()


def extract(client: anthropic.Anthropic, today: str, digest: str, brief: str, items: list[dict]) -> DailyUpdate | None:
    feed = "\n".join(
        f"- [{it['date']}] ({it['kind']}, {it['source']}) {it['title']} — {it['url']}"
        + (f" | {it['summary'][:200]}" if it.get("summary") else "")
        + (f" | models: {','.join(it['models'])}" if it.get("models") else "")
        for it in items[:250]
    )
    prompt = f"""Today is {today}.

<registry>{digest}</registry>

<research_brief>
{brief or "(research pass unavailable)"}
</research_brief>

<collected_items>
{feed or "(none)"}
</collected_items>

Produce today's dashboard update:
- new_models: only models absent from the registry that are released or announced with cyber relevance. \
id = lowercase slug (e.g. "gpt-6-1"). category: ga | gated (trusted-access/cyber-specific) | open (open weights). \
released = YYYY-MM-DD or null.
- benchmark_results: only numbers not already in known_scores, as percentages 0-100. Reuse an existing \
benchmark_id when it matches; otherwise make a new slug id. Each needs a source_url.
- incidents: only ones not in known_incidents. breakout = acted outside containment; misuse = used by threat \
actors; policy = access restriction/regulatory action.
- perception_updates: for every tracked model with meaningful new discussion, re-score perceived cyber \
capability (0-100), concern about risk (0-100) and overall tone (-1 to 1), with a 1-2 sentence summary \
of what people are saying and 1-3 source links. Skip models with no new signal.
- headlines: the 5-15 most significant items for this dashboard's audience, from either input, with kind.
- provider_takes: refresh the one-sentence-or-two takes for openai, anthropic, google, and open (open-weight \
ecosystem) only if the landscape changed; otherwise return an empty list.
- trend_points: new values for indicator ids aisi_doubling_months or open_weight_lag_months only.
- run_summary: 1-2 sentences on what changed today.
Use only model ids from the registry or from new_models."""
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
        output_format=DailyUpdate,
    )
    if resp.stop_reason == "refusal":
        log("extraction pass refused")
        return None
    return resp.parsed_output


def run(today: str, models: dict, benchmarks: dict, incidents: dict, perception: dict,
        items: list[dict]) -> DailyUpdate | None:
    # Organization-level keys (not scoped to a workspace) must name the workspace per request.
    workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    client = anthropic.Anthropic(default_headers={"anthropic-workspace-id": workspace} if workspace else None)
    digest = _registry_digest(models, benchmarks, incidents, perception)
    try:
        brief = research(client, today, digest)
        log(f"research brief: {len(brief)} chars")
    except anthropic.APIStatusError as e:
        log(f"research pass failed ({e.status_code}: {e.message}); continuing with feeds only")
        brief = ""
    except anthropic.APIConnectionError as e:
        log(f"research pass could not connect ({e}); continuing with feeds only")
        brief = ""
    try:
        return extract(client, today, digest, brief, items)
    except anthropic.APIStatusError as e:
        log(f"extraction pass failed ({e.status_code}: {e.message}); keeping feed-only results")
    except anthropic.APIConnectionError as e:
        log(f"extraction pass could not connect ({e}); keeping feed-only results")
    return None

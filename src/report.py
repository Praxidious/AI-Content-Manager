from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .utils import format_timestamp


def _transcript(candidate: dict[str, Any]) -> str:
    excerpt = candidate.get("transcript_excerpt", [])
    if not excerpt:
        return "> _No transcript lines fall inside the recommended range._"
    return "\n".join(f"> **[{line['start_display']}]** {line['text']}  " for line in excerpt)


def _audio(candidate: dict[str, Any]) -> str:
    signals = candidate.get("audio_signals", [])
    lines = ["- Candidate sources: " + ", ".join(candidate.get("candidate_sources", ["transcript"]))]
    if candidate.get("audio_only"):
        lines.append("- Audio-only candidate; no useful transcript evidence was required.")
    for event in candidate.get("audio_events", []):
        labels = ", ".join(f"{label}: {value:.0%}" for label, value in event["class_scores"].items())
        lines.append(f"- {format_timestamp(event['start'], True)}–{format_timestamp(event['end'], True)}: {labels}; "
                     f"relative {event['relative_loudness_db']:+.1f} dB; quiet-to-loud {event['quiet_to_loud_db']:+.1f} dB; "
                     f"source {event['source_label']} ({event['source']}); {event['attribution']}")
    if not signals and not candidate.get("audio_events"):
        lines.append("- No notable audio signal was recorded for this range.")
    for signal in signals:
        flags = [label for key, label in (
            ("possible_laughter", "possible laughter"), ("possible_shouting", "possible shouting"),
            ("sudden_energy_change", "sudden energy change"), ("rapid_speech_activity", "rapid speech/activity")
        ) if signal.get(key)]
        lines.append(
            f"- {format_timestamp(signal.get('start', 0), True)}–{format_timestamp(signal.get('end', 0), True)}: "
            f"relative energy {float(signal.get('relative_energy', 0)):.1f}/10, "
            f"speech density {float(signal.get('speech_density', 0)):.1f}/10"
            + (f"; {', '.join(flags)}" if flags else "")
        )
    return "\n".join(lines)


def _candidate(candidate: dict[str, Any], rank: int) -> str:
    tags = " / ".join(candidate.get("content_tags", [])) or "PERSONALITY"
    return f"""### {rank}. {candidate.get('title', 'Untitled')}

**Recommended review range:** `{format_timestamp(candidate['recommended_start'], True)}` → `{format_timestamp(candidate['recommended_end'], True)}` ({candidate['estimated_duration']:.1f}s)  
**Core timestamp:** `{format_timestamp(candidate['core_start'])}` → `{format_timestamp(candidate['core_end'])}`  
**Tags:** `{tags}`  
**Clip ID:** `{candidate.get('clip_id', 'Not imported yet')}`  
**Rank score:** {candidate.get('final_rank_score', candidate.get('entertainment_score', 0)):.2f} = generic {candidate.get('generic_quality_score', candidate.get('entertainment_score', 0)):.2f} + Wes preference {candidate.get('personal_preference_score', 0):+.2f} + audience {candidate.get('audience_rank_adjustment', 0):+.2f}  
**Audience evidence:** {(candidate.get('audience_prediction') or {}).get('platform', 'not assessed')} / {(candidate.get('audience_prediction') or {}).get('account', '—')}; {(candidate.get('audience_prediction') or {}).get('history_n', 0)} independent clips, {(candidate.get('audience_prediction') or {}).get('similar_n', 0)} similar; confidence {(candidate.get('audience_prediction') or {}).get('confidence', 'insufficient')}  
**Entertainment:** {candidate.get('entertainment_score', 0):.1f}/10  
**Clipability:** {candidate.get('clipability_score', 0):.1f}/10  
**Growth potential:** {candidate.get('growth_potential_score', 0):.1f}/10  
**Tier reason:** {candidate.get('tier_reason') or '—'}  
**Needs video review:** `{'YES' if candidate.get('needs_video_review') else 'NO'}`

**Detected because:** {candidate.get('why_detected') or candidate.get('why_entertaining') or '—'}

**Why it may be entertaining:** {candidate.get('why_entertaining') or '—'}

**Setup / escalation / payoff:** {candidate.get('setup') or '—'} / {candidate.get('escalation') or '—'} / {candidate.get('payoff') or '—'}

**Audio signals:**

{_audio(candidate)}

**What to look for in video:** {candidate.get('what_to_look_for') or 'Confirm the visual cause and whether it supplies a clean setup/payoff.'}

**Suggested editing angle:** {candidate.get('suggested_editing_angle') or 'Start just before the detected reaction and preserve its strongest escalation.'}

**Exact discovery evidence:** “{candidate.get('exact_transcript_evidence', '—')}”

**Transcript for the recommended range:**

{_transcript(candidate)}
"""


def write_report(
    report_path: Path, stream_filename: str, stream_duration: float,
    transcription_metadata: dict[str, Any], segment_count: int,
    candidates: list[dict[str, Any]], chunk_count: int,
    discovered_count: int = 0, verified_count: int = 0,
) -> None:
    tiers = {
        "S — EXCEPTIONAL": [c for c in candidates if c.get("classification") == "S"],
        "A — DEFINITELY WORTH EDITING": [c for c in candidates if c.get("classification") == "A"],
        "B — MAYBE USABLE": [c for c in candidates if c.get("classification") == "B"],
    }
    lines = [
        "# JustGlad2BHere Human Review Report", "",
        f"- **Stream:** `{stream_filename}`", f"- **Processed duration:** {format_timestamp(stream_duration)}",
        f"- **Transcription:** `{transcription_metadata.get('model','unknown')}` on `{transcription_metadata.get('device','unknown')}` ({transcription_metadata.get('compute_type','unknown')})",
        f"- **Transcript segments:** {segment_count:,}", f"- **Discovery chunks:** {chunk_count:,}",
        f"- **Discovered moments:** {discovered_count:,}", f"- **Evidence-supported moments:** {verified_count:,}",
        f"- **Final review ranges:** {len(candidates):,}",
        f"- **Processing date:** {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M %Z')}", "",
    ]
    if not candidates:
        lines += ["No moment survived content-quality review.", ""]
    rank = {id(candidate): index for index, candidate in enumerate(candidates, 1)}
    for heading, items in tiers.items():
        lines += [f"## {heading}", ""]
        if not items:
            lines += ["_No candidates in this section._", ""]
        for item in items:
            lines += [_candidate(item, rank[id(item)]), ""]
    lines += ["---", "", "Discovery and judging assume most stream moments are not worth editing; evidence verification prevents invented visual claims. Audio flags are heuristic review signals, not definitive event labels.", ""]
    report_path.write_text("\n".join(lines), encoding="utf-8")


def write_rejected_report(path: Path, stream_filename: str, rejected: list[dict[str, Any]]) -> None:
    lines = ["# Rejected Candidates", "", f"- **Stream:** `{stream_filename}`", f"- **Rejected:** {len(rejected):,}", ""]
    if not rejected:
        lines += ["_No rejected discovery candidates._", ""]
    for index, item in enumerate(rejected, 1):
        entertainment = item.get("entertainment_score")
        score_text = f"{entertainment:.1f}/10" if isinstance(entertainment, (int, float)) else "Not scored"
        lines += [
            f"## {index}. {format_timestamp(item.get('core_start', 0))}", "",
            f"**Tier:** `{item.get('tier','REJECT')}`  ", f"**Stage:** `{item.get('stage','UNKNOWN')}`  ",
            f"**Entertainment:** {score_text}", "",
            f"**Reason:** {item.get('reason') or 'No reason supplied.'}", "",
            f"**Candidate description:** {item.get('description') or '—'}", "",
            f"**Transcript evidence:** “{item.get('exact_transcript_evidence') or '—'}”", "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")

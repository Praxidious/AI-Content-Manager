from __future__ import annotations

import hashlib, json, logging, os, re, subprocess, sys, time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .config import AnalysisConfig, provider_api_key, provider_base_url
from .utils import atomic_write_json, format_timestamp

LOG = logging.getLogger(__name__)

DISCOVERY_SCHEMA = {"type":"object","properties":{"candidates":{"type":"array","maxItems":8,"items":{"type":"object","properties":{
    "core_start":{"type":"number"},"core_end":{"type":"number"},"exact_transcript_evidence":{"type":"string"},
    "literal_description":{"type":"string"},"content_tags":{"type":"array","items":{"type":"string"}},
    "expected_setup":{"type":"string"},"expected_payoff":{"type":"string"},"why_detected":{"type":"string"},
    "needs_video_review":{"type":"boolean"}},
    "required":["core_start","core_end","exact_transcript_evidence","literal_description","content_tags","expected_setup","expected_payoff","why_detected","needs_video_review"]}}},"required":["candidates"]}

VERIFICATION_SCHEMA = {"type":"object","properties":{
    "status":{"type":"string","enum":["SUPPORTED","PARTIALLY_SUPPORTED","UNSUPPORTED"]},
    "unsupported_claims":{"type":"array","items":{"type":"string"}},"corrected_description":{"type":"string"},
    "verification_confidence":{"type":"number"}},
    "required":["status","unsupported_claims","corrected_description","verification_confidence"]}

JUDGE_SCHEMA = {"type":"object","properties":{
    "title":{"type":"string"},"tier":{"type":"string","enum":["S","A","B","REJECT"]},"tier_reason":{"type":"string"},
    "entertainment_score":{"type":"number"},"clipability_score":{"type":"number"},
    "growth_potential_score":{"type":"number"},
    "setup":{"type":"string"},"escalation":{"type":"string"},"payoff":{"type":"string"},
    "why_entertaining":{"type":"string"},"what_to_look_for":{"type":"string"},"suggested_editing_angle":{"type":"string"},
    "recommended_start":{"type":"number"},"recommended_end":{"type":"number"},
    "needs_video_review":{"type":"boolean"},"ai_confidence":{"type":"number"},"major_rejection":{"type":"boolean"},
    "rejection_reasons":{"type":"array","items":{"type":"string"}}},
    "required":["title","tier","tier_reason","entertainment_score","clipability_score","growth_potential_score",
    "setup","escalation","payoff","why_entertaining","what_to_look_for","suggested_editing_angle",
    "recommended_start","recommended_end","needs_video_review","ai_confidence","major_rejection","rejection_reasons"]}


@dataclass(slots=True)
class TranscriptChunk:
    index: int
    start: float
    end: float
    segments: list[dict[str, Any]]


@dataclass(slots=True)
class AnalysisOutcome:
    candidates: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    chunk_count: int
    discovered_count: int
    verified_count: int


class LLMProvider(Protocol):
    def analyze(self, system_prompt: str, user_text: str, schema: dict[str, Any]) -> str: ...


class OpenAIProvider:
    def __init__(self, config: AnalysisConfig):
        key = provider_api_key(config)
        if not key:
            raise RuntimeError(f"No API key found in {config.api_key_env}; configure .env or select ollama.")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("Install requirements.txt before using OpenAI.") from exc
        self.client = OpenAI(api_key=key, timeout=config.request_timeout_seconds,
                             base_url=provider_base_url(config) or "https://api.openai.com/v1")
        self.config = config

    def analyze(self, system_prompt: str, user_text: str, schema: dict[str, Any]) -> str:
        response = self.client.chat.completions.create(
            model=self.config.model, temperature=self.config.temperature, response_format={"type":"json_object"},
            messages=[{"role":"system","content":system_prompt},{"role":"user","content":user_text}],
        )
        return response.choices[0].message.content or "{}"


class OllamaProvider:
    def __init__(self, config: AnalysisConfig):
        self.config = config
        self._ensure_server()

    def _ensure_server(self) -> None:
        import requests
        url = f"{self.config.ollama_url.rstrip('/')}/api/version"
        try:
            requests.get(url, timeout=3).raise_for_status(); return
        except requests.RequestException as first_error:
            if sys.platform != "win32":
                raise RuntimeError(f"Ollama is not running: {first_error}") from first_error
            executable = Path(os.environ.get("LOCALAPPDATA", ""))/"Programs"/"Ollama"/"ollama.exe"
            if not executable.is_file():
                raise RuntimeError("Ollama is not installed. See https://ollama.com/download/windows.")
            flags = getattr(subprocess,"CREATE_NO_WINDOW",0)|getattr(subprocess,"DETACHED_PROCESS",0)
            subprocess.Popen([str(executable),"serve"],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,creationflags=flags)
            for _ in range(20):
                time.sleep(.5)
                try: requests.get(url,timeout=2).raise_for_status(); return
                except requests.RequestException: pass
            raise RuntimeError("Ollama API did not become ready within 10 seconds.")

    def analyze(self, system_prompt: str, user_text: str, schema: dict[str, Any]) -> str:
        import requests
        properties = schema.get("properties", {})
        if "candidates" in properties:
            stage_output_limit = 2_048
        elif "status" in properties:
            stage_output_limit = 512
        else:
            stage_output_limit = 1_024
        response = requests.post(f"{self.config.ollama_url.rstrip('/')}/api/chat",json={
            "model":self.config.model,"stream":False,"format":schema,"think":False,
            "options":{"temperature":self.config.temperature,"num_ctx":self.config.ollama_context_tokens,
                       "num_predict":min(self.config.ollama_max_output_tokens, stage_output_limit)},
            "messages":[{"role":"system","content":system_prompt},{"role":"user","content":user_text}]},
            timeout=self.config.request_timeout_seconds)
        response.raise_for_status()
        return response.json()["message"]["content"]


def create_provider(config: AnalysisConfig) -> LLMProvider:
    if config.provider.lower() in {"openai","openai-compatible"}: return OpenAIProvider(config)
    if config.provider.lower() == "ollama": return OllamaProvider(config)
    raise ValueError(f"Unsupported LLM provider: {config.provider}")


def explain_provider_error(exc: Exception) -> tuple[str,bool]:
    status, body = getattr(exc,"status_code",None), getattr(exc,"body",None)
    if isinstance(body,dict) and body.get("code") == "insufficient_quota":
        return "OpenAI API quota is unavailable. Add credits or enable API billing, then rerun.",False
    if status == 401: return "The OpenAI API key was rejected.",False
    if status in {403,404}: return f"Model or API permission rejected: {exc}",False
    if status is not None and 400 <= int(status) < 500 and status != 429: return f"Provider rejected request: {exc}",False
    root: BaseException = exc
    while getattr(root,"__cause__",None) is not None: root = root.__cause__  # type: ignore[assignment]
    return (f"{exc} Root cause: {type(root).__name__}: {root}" if root is not exc else str(exc)),True


def chunk_transcript(segments: list[dict[str,Any]], config: AnalysisConfig) -> list[TranscriptChunk]:
    if not segments: return []
    window, overlap = config.chunk_minutes*60, config.chunk_overlap_minutes*60
    chunks, cursor, final_end = [], float(segments[0]["start"]), float(segments[-1]["end"])
    while cursor <= final_end:
        selected, characters = [], 0
        for segment in segments:
            if float(segment["end"]) < cursor: continue
            if float(segment["start"]) >= cursor+window: break
            size = len(str(segment.get("text","")))+20
            if selected and characters+size > config.max_chunk_characters: break
            selected.append(segment); characters += size
        if not selected: break
        actual_end = float(selected[-1]["end"])
        chunks.append(TranscriptChunk(len(chunks),float(selected[0]["start"]),actual_end,selected))
        next_cursor = actual_end-overlap
        cursor = next_cursor if next_cursor > cursor else cursor+max(1.0,window-overlap)
    return chunks


def _segments_text(segments: list[dict[str,Any]]) -> str:
    return "\n".join(f"[{float(s['start']):.3f} | {format_timestamp(s['start'])}] {s.get('text','')}" for s in segments)


def _audio_text(audio_signals: list[dict[str, Any]], start: float, end: float) -> str:
    selected = [signal for signal in audio_signals
                if float(signal.get("end", 0)) >= start and float(signal.get("start", 0)) <= end]
    if not selected:
        return "No notable audio regions were detected in this chunk."
    return json.dumps(selected, ensure_ascii=False, separators=(",", ":"))


def _parse_json_response(raw: str) -> dict[str,Any]:
    text = raw.strip()
    if text.startswith("```"): text = re.sub(r"^```(?:json)?\s*|\s*```$","",text,flags=re.I)
    try: value = json.loads(text)
    except json.JSONDecodeError:
        start,end=text.find("{"),text.rfind("}")
        if start<0 or end<=start: raise ValueError("LLM response contained no JSON object")
        value=json.loads(text[start:end+1])
    if not isinstance(value,dict): raise ValueError("LLM response must be a JSON object")
    return value


def _cached_call(stage: str,prompt: str,user_text: str,schema: dict[str,Any],config: AnalysisConfig,
                 cache_dir: Path,provider: LLMProvider,force: bool,label: str) -> dict[str,Any]:
    digest=hashlib.sha256((stage+prompt+user_text+config.provider+config.model+json.dumps(schema,sort_keys=True)).encode()).hexdigest()[:20]
    path=cache_dir/stage/f"{label}_{digest}.json"
    if path.exists() and not force:
        LOG.info("%s: using cache",label); return json.loads(path.read_text(encoding="utf-8"))
    path.parent.mkdir(parents=True,exist_ok=True)
    request_text=user_text
    for attempt in range(1,config.max_retries+1):
        try:
            raw=provider.analyze(prompt,request_text,schema)
            try:
                payload=_parse_json_response(raw)
            except (json.JSONDecodeError,ValueError):
                invalid_path=path.with_name(f"{path.stem}_attempt_{attempt}_invalid.txt")
                invalid_path.write_text(raw,encoding="utf-8")
                if stage=="discovery":
                    request_text=(user_text+"\n\nRETRY REQUIREMENT: The previous response was incomplete or malformed. "
                                  "Return valid JSON with at most 5 of the strongest candidates. Keep every field concise.")
                raise
            atomic_write_json(path,payload); return payload
        except Exception as exc:
            message,retryable=explain_provider_error(exc)
            if not retryable or attempt==config.max_retries: raise RuntimeError(f"{label} failed: {message}") from exc
            delay=min(2**attempt,15); LOG.warning("%s failed: %s; retrying in %ds",label,message,delay); time.sleep(delay)
    raise RuntimeError(f"{label} failed")


def _number(value: Any,default: float=0.0) -> float:
    try: return float(value)
    except (TypeError,ValueError): return default

def _score(value: Any) -> float: return max(0.0,min(10.0,_number(value)))
def _norm(value: str) -> str: return " ".join(re.findall(r"[a-z0-9]+",value.lower()))
def _candidate_id(start: float,end: float,evidence: str) -> str:
    return hashlib.sha256(f"{start:.3f}|{end:.3f}|{evidence}".encode()).hexdigest()[:12]


def _surrounding(segments: list[dict[str,Any]],start: float,end: float,padding: float) -> list[dict[str,Any]]:
    return [s for s in segments if float(s["end"])>=max(0,start-padding) and float(s["start"])<=end+padding]


def _normalize_discovery(item: dict[str,Any],chunk: TranscriptChunk) -> dict[str,Any]|None:
    start,end=_number(item.get("core_start"),-1),_number(item.get("core_end"),-1)
    evidence=str(item.get("exact_transcript_evidence","")).strip()
    chunk_text=_norm(" ".join(str(s.get("text","")) for s in chunk.segments))
    if start<chunk.start-15 or end>chunk.end+15 or end<=start or not _norm(evidence) or _norm(evidence) not in chunk_text: return None
    tags=item.get("content_tags",[])
    if not isinstance(tags,list): tags=[str(tags)]
    return {"candidate_id":_candidate_id(start,end,evidence),"core_start":round(start,3),"core_end":round(end,3),
            "exact_transcript_evidence":evidence,"description":str(item.get("literal_description","")).strip(),
            "content_tags":[str(tag).upper().strip() for tag in tags if str(tag).strip()] or ["PERSONALITY"],
            "expected_setup":str(item.get("expected_setup","")).strip(),"expected_payoff":str(item.get("expected_payoff","")).strip(),
            "why_detected":str(item.get("why_detected","")).strip(),"needs_video_review":bool(item.get("needs_video_review",False)),
            "source_chunk":chunk.index}


def _candidate_audio(candidate: dict[str, Any], audio_signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [signal for signal in audio_signals if float(signal.get("end", 0)) >= float(candidate["core_start"])
            and float(signal.get("start", 0)) <= float(candidate["core_end"])]


def _audio_seed_candidates(segments: list[dict[str, Any]], audio_signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seeds = []
    for signal in audio_signals:
        priority = (signal.get("possible_laughter") or signal.get("possible_shouting")
                    or (float(signal.get("relative_energy", 0)) >= 8.5 and signal.get("sudden_energy_change")))
        if not priority:
            continue
        nearby = _surrounding(segments, float(signal["start"]), float(signal["end"]), 4.0)
        evidence = " ".join(str(s.get("text", "")).strip() for s in nearby if str(s.get("text", "")).strip())
        if not evidence:
            continue
        evidence = evidence[:300]
        tags = ["LAUGHTER"] if signal.get("possible_laughter") else []
        if signal.get("possible_shouting"): tags.append("SCREAM_SHOCK")
        if signal.get("rapid_speech_activity"): tags.append("CHAOS")
        start=max(0.0,float(signal["start"])-4.0); end=float(signal["end"])+4.0
        seeds.append({"candidate_id":_candidate_id(start,end,evidence),"core_start":round(start,3),"core_end":round(end,3),
                      "exact_transcript_evidence":evidence,"description":"Audio and nearby dialogue indicate a strong reaction worth checking.",
                      "content_tags":tags or ["WEIRD_UNEXPECTED"],"expected_setup":"","expected_payoff":"",
                      "why_detected":"High-priority relative audio reaction signal.","needs_video_review":True,
                      "audio_priority":True,"source_chunk":-1})
    return seeds


def _bounds(candidate: dict[str,Any],judge: dict[str,Any],context: list[dict[str,Any]],config: AnalysisConfig)->tuple[float,float]:
    cstart,cend=float(candidate["core_start"]),float(candidate["core_end"])
    lo,hi=float(context[0]["start"]),float(context[-1]["end"])
    start=max(lo,min(_number(judge.get("recommended_start"),cstart),cstart)); end=min(hi,max(_number(judge.get("recommended_end"),cend),cend))
    if end-start<config.minimum_clip_duration_seconds:
        needed=config.minimum_clip_duration_seconds-(end-start); start=max(lo,start-needed/2); end=min(hi,end+needed/2)
    if end-start>config.maximum_clip_duration_seconds:
        end=min(end,start+config.maximum_clip_duration_seconds)
        if end<cend: end=cend; start=max(lo,end-config.maximum_clip_duration_seconds)
    return round(start,3),round(end,3)


def _overlap_ratio(a: dict[str,Any],b: dict[str,Any])->float:
    overlap=max(0,min(float(a["core_end"]),float(b["core_end"]))-max(float(a["core_start"]),float(b["core_start"])))
    shorter=max(.001,min(float(a["core_end"])-float(a["core_start"]),float(b["core_end"])-float(b["core_start"])))
    return overlap/shorter


def _rejection(candidate: dict[str,Any],stage: str,reason: str)->dict[str,Any]:
    return {"candidate_id":candidate.get("candidate_id","unknown"),"core_start":candidate.get("core_start",0),
            "core_end":candidate.get("core_end",candidate.get("core_start",0)),"stage":stage,"reason":reason,
            "tier":candidate.get("tier","REJECT"),"tier_reason":candidate.get("tier_reason",reason),
            "description":candidate.get("description",""),"exact_transcript_evidence":candidate.get("exact_transcript_evidence",""),
            "entertainment_score":candidate.get("entertainment_score"),"clipability_score":candidate.get("clipability_score"),
            "growth_potential_score":candidate.get("growth_potential_score")}


def deduplicate_candidates(candidates: list[dict[str,Any]],rejected: list[dict[str,Any]]|None=None)->list[dict[str,Any]]:
    kept=[]
    for candidate in sorted(candidates,key=lambda c:(-float(c.get("entertainment_score",c.get("score",0))),float(c["core_start"]))):
        duplicate=next((x for x in kept if _overlap_ratio(candidate,x)>=.45 or (abs(float(candidate["core_start"])-float(x["core_start"]))<=12 and abs(float(candidate["core_end"])-float(x["core_end"]))<=12)),None)
        if duplicate is None: kept.append(candidate)
        else:
            # Preserve conservative review signals when transcript and audio discovery find the same moment.
            duplicate["needs_video_review"] = bool(duplicate.get("needs_video_review") or candidate.get("needs_video_review"))
            duplicate["audio_priority"] = bool(duplicate.get("audio_priority") or candidate.get("audio_priority"))
            tags = list(dict.fromkeys(list(duplicate.get("content_tags", [])) + list(candidate.get("content_tags", []))))
            if tags: duplicate["content_tags"] = tags
            if candidate.get("audio_priority") and candidate.get("why_detected"):
                duplicate["why_detected"] = candidate["why_detected"]
            if rejected is not None: rejected.append(_rejection(candidate,"DEDUPLICATION","Duplicate or weaker version of a higher-scoring candidate."))
    return kept


def attach_transcript_excerpts(candidates: list[dict[str,Any]],segments: list[dict[str,Any]])->list[dict[str,Any]]:
    for candidate in candidates:
        start,end=float(candidate["recommended_start"]),float(candidate["recommended_end"])
        candidate["transcript_excerpt"]=[{"start":float(s["start"]),"end":float(s["end"]),
            "start_display":str(s.get("start_display",format_timestamp(s["start"]))),"end_display":str(s.get("end_display",format_timestamp(s["end"]))),
            "text":str(s.get("text","")).strip()} for s in segments if float(s["end"])>=start and float(s["start"])<=end]
    return candidates


def analyze_transcript(segments: list[dict[str,Any]],config: AnalysisConfig,prompts_dir: Path,cache_dir: Path,
                       reanalyze: bool=False,rediscover: bool=False,reverify: bool=False,rejudge: bool=False,
                       provider: LLMProvider|None=None,audio_signals: list[dict[str,Any]]|None=None)->AnalysisOutcome:
    discovery_prompt=(prompts_dir/"candidate_discovery.txt").read_text(encoding="utf-8")
    verifier_prompt=(prompts_dir/"evidence_verifier.txt").read_text(encoding="utf-8")
    judge_prompt=(prompts_dir/"content_quality_judge.txt").read_text(encoding="utf-8")
    audio_signals=audio_signals or []
    llm=provider or create_provider(config); chunks=chunk_transcript(segments,config)
    discovered=[]; rejected=[]
    for chunk in chunks:
        LOG.info("Discovery %d/%d (%s-%s)",chunk.index+1,len(chunks),format_timestamp(chunk.start),format_timestamp(chunk.end))
        user=(f"Absolute stream chunk {chunk.start} to {chunk.end}. Numeric timestamps are seconds.\n\nTRANSCRIPT:\n"
              +_segments_text(chunk.segments)+"\n\nNOTABLE AUDIO SIGNALS:\n"+_audio_text(audio_signals,chunk.start,chunk.end))
        payload=_cached_call("discovery",discovery_prompt,user,DISCOVERY_SCHEMA,config,cache_dir,llm,reanalyze or rediscover,f"chunk_{chunk.index:04d}")
        for item in payload.get("candidates",[]):
            candidate=_normalize_discovery(item,chunk) if isinstance(item,dict) else None
            if candidate: discovered.append(candidate)
            elif isinstance(item,dict): rejected.append({"candidate_id":"invalid-discovery","core_start":item.get("core_start",0),"core_end":item.get("core_end",0),"stage":"DISCOVERY_VALIDATION","reason":"Timestamps or exact evidence did not match the transcript.","description":item.get("literal_description",""),"exact_transcript_evidence":item.get("exact_transcript_evidence","")})

    discovered.extend(_audio_seed_candidates(segments,audio_signals))
    discovered=deduplicate_candidates(discovered)
    verified=[]
    for index,candidate in enumerate(discovered,1):
        context=_surrounding(segments,candidate["core_start"],candidate["core_end"],config.context_expansion_seconds)
        candidate["audio_signals"]=_candidate_audio(candidate,audio_signals)
        LOG.info("Verification %d/%d (%s)",index,len(discovered),format_timestamp(candidate["core_start"]))
        user=(json.dumps(candidate,ensure_ascii=False,indent=2)+"\n\nSURROUNDING TRANSCRIPT:\n"+_segments_text(context)
              +"\n\nAUDIO SIGNALS:\n"+_audio_text(audio_signals,float(candidate["core_start"]),float(candidate["core_end"])))
        result=_cached_call("verification",verifier_prompt,user,VERIFICATION_SCHEMA,config,cache_dir,llm,reanalyze or rediscover or reverify,f"candidate_{candidate['candidate_id']}")
        status=str(result.get("status","UNSUPPORTED")).upper()
        if status=="UNSUPPORTED": rejected.append(_rejection(candidate,"EVIDENCE_VERIFICATION","; ".join(result.get("unsupported_claims",[])) or "Transcript does not support candidate.")); continue
        candidate["verification_status"]=status; candidate["unsupported_claims"]=result.get("unsupported_claims",[])
        candidate["verification_confidence"]=round(_score(result.get("verification_confidence")),1)
        if status=="PARTIALLY_SUPPORTED" and str(result.get("corrected_description","")).strip(): candidate["description"]=str(result["corrected_description"]).strip()
        verified.append(candidate)

    judged=[]
    for index,candidate in enumerate(verified,1):
        context=_surrounding(segments,candidate["core_start"],candidate["core_end"],config.context_expansion_seconds)
        LOG.info("Content quality judge %d/%d (%s)",index,len(verified),format_timestamp(candidate["core_start"]))
        candidate["audio_signals"]=_candidate_audio(candidate,audio_signals)
        user=(json.dumps(candidate,ensure_ascii=False,indent=2)+"\n\nEXPANDED TRANSCRIPT:\n"+_segments_text(context)
              +"\n\nAUDIO SIGNALS:\n"+_audio_text(audio_signals,float(candidate["core_start"]),float(candidate["core_end"])))
        result=_cached_call("judging",judge_prompt,user,JUDGE_SCHEMA,config,cache_dir,llm,reanalyze or rediscover or reverify or rejudge,f"candidate_{candidate['candidate_id']}")
        entertainment=round(_score(result.get("entertainment_score")),1)
        clipability=round(_score(result.get("clipability_score")),1)
        growth=round(_score(result.get("growth_potential_score")),1)
        tier=str(result.get("tier","REJECT")).upper().strip()
        if tier not in {"S","A","B","REJECT"}: tier="REJECT"
        tier_reason=str(result.get("tier_reason","")).strip()
        candidate.update({"entertainment_score":entertainment,"clipability_score":clipability,"growth_potential_score":growth,
            "title":str(result.get("title",candidate.get("content_tags",["Moment"])[0])).strip(),"tier":tier,
            "tier_reason":tier_reason,"classification":tier,"setup":str(result.get("setup","")).strip(),
            "escalation":str(result.get("escalation","")).strip(),"payoff":str(result.get("payoff","")).strip(),
            "why_entertaining":str(result.get("why_entertaining","")).strip(),
            "what_to_look_for":str(result.get("what_to_look_for","")).strip(),
            "suggested_editing_angle":str(result.get("suggested_editing_angle","")).strip(),
            "needs_video_review":bool(candidate.get("needs_video_review") or result.get("needs_video_review",False)),
            "ai_confidence":round(_score(result.get("ai_confidence")),1)})
        candidate["recommended_start"],candidate["recommended_end"]=_bounds(candidate,result,context,config)
        candidate["estimated_duration"]=round(candidate["recommended_end"]-candidate["recommended_start"],1)
        reasons=[str(x) for x in result.get("rejection_reasons",[]) if str(x).strip()]
        if tier == "REJECT": rejected.append(_rejection(candidate,"CONTENT_QUALITY_JUDGE",tier_reason or "; ".join(reasons) or "Not worth editing.")); continue
        if bool(result.get("major_rejection")) and not candidate.get("audio_priority"): rejected.append(_rejection(candidate,"CONTENT_QUALITY_JUDGE","; ".join(reasons) or "Failed content-quality review.")); continue
        judged.append(candidate)

    selected=deduplicate_candidates(judged,rejected)
    selected.sort(key=lambda c:(-float(c["entertainment_score"]),-float(c["clipability_score"]),float(c["core_start"])))
    selected=selected[:config.maximum_clips]; attach_transcript_excerpts(selected,segments)
    rejected.sort(key=lambda x:(float(x.get("core_start",0)),str(x.get("stage",""))))
    return AnalysisOutcome(selected,rejected,len(chunks),len(discovered),len(verified))

"""Grounded prose quality assessment and controlled repair.

Quality is a projection over the adopted Writer draft. It never treats prose as
world truth and never mutates formal history, cognition, or causal state.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from copy import deepcopy
from collections import Counter
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .ai.errors import MODEL_OUTPUT_INVALID, ModelProviderError
from .ai.factory import get_model_provider
from .execution_trace import ExecutionTraceRecorder
from .model_router import ModelRouter, ProviderCredentialResolver
from .models import (
    AntiAIBible, Chapter, ChapterQualityAssessment, ChapterQualityFinding,
    ChapterWriterDraft, Project, ProjectModelConfig, QualityAssessmentStatus,
    QualityFindingSeverity, QualityFindingSource, ExecutionTrace, WriterDraftOrigin,
    WriterDraftStatus,
    CharacterKnowledge,
)
from .writer import (
    WriterChapterSourceBuilder, WriterContextBuilder, WriterDomainError,
    WriterGroundingValidator, WriterOutputPayload, WriterProjectionAudit,
    WriterProjectionService, WriterWordCounter, WriterContextFingerprintBuilder,
)
from .planning import validate_task_output


QUALITY_CATEGORIES = {
    "FACTUAL_GROUNDING", "POV_COMPLIANCE", "REVEAL_SAFETY", "CONTINUITY",
    "ANTI_AI_EXPRESSION", "REPETITION", "STYLE_CONSISTENCY",
    "CHARACTER_VOICE", "PACING", "CLARITY", "LANGUAGE_QUALITY", "FORMAT",
}


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)


def _fp(value: Any, protocol: str) -> str:
    return f"{protocol}:" + hashlib.sha256(_canonical(value).encode()).hexdigest()


class QualityDomainError(ValueError):
    def __init__(self, code: str, detail: Any | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail if detail is not None else {}


class QualityGateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    require_critic: bool = True
    min_overall_score: int = Field(default=70, ge=0, le=100)
    max_major_findings: int = Field(default=0, ge=0, le=100)
    allow_minor_findings: bool = True
    auto_repair_enabled: bool = False
    max_repair_attempts: int = Field(default=1, ge=0, le=3)


class QualityGateConfigResolver:
    """Resolve current settings and preserve the authority that supplied them."""

    @staticmethod
    def _override(request: dict[str, Any] | None = None) -> dict[str, Any]:
        value = (request or {}).get("config", {})
        if not isinstance(value, dict):
            raise QualityDomainError("INVALID_QUALITY_GATE_CONFIG")
        # Validate supplied keys without turning inherited defaults into
        # explicit overrides.
        try:
            normalized = QualityGateConfig.model_validate({**QualityGateConfig().model_dump(mode="json"), **value}).model_dump(mode="json")
        except ValidationError as exc:
            raise QualityDomainError("INVALID_QUALITY_GATE_CONFIG", {"errors": exc.errors(include_url=False)}) from exc
        return {key: normalized[key] for key in value}

    @staticmethod
    def _stored(project: Project) -> dict[str, Any]:
        value = (project.autonomy_settings or {}).get("quality_gate", {})
        if not isinstance(value, dict):
            raise QualityDomainError("INVALID_QUALITY_GATE_CONFIG")
        return value

    def resolve(self, project: Project, request: dict[str, Any] | None = None) -> QualityGateConfig:
        stored = self._stored(project)
        override = self._override(request)
        try:
            return QualityGateConfig.model_validate({**stored, **override})
        except ValidationError as exc:
            raise QualityDomainError("INVALID_QUALITY_GATE_CONFIG", {"errors": exc.errors(include_url=False)}) from exc

    def envelope(self, project: Project, request: dict[str, Any] | None = None) -> dict[str, Any]:
        resolved = self.resolve(project, request).model_dump(mode="json")
        explicit = self._override(request)
        return {
            "resolved": resolved,
            "explicit_overrides": explicit,
            "source": {
                "project_quality_gate_fingerprint": _fp(self._stored(project), "quality-project-config-v1"),
                "explicit_overrides_fingerprint": _fp(explicit, "quality-explicit-config-v1"),
            },
        }


def _resolved_quality_config(value: dict[str, Any]) -> dict[str, Any]:
    """Return the effective config from new envelopes and legacy flat rows."""
    try:
        raw = value.get("resolved") if isinstance(value, dict) and "resolved" in value else value
        return QualityGateConfig.model_validate(raw).model_dump(mode="json")
    except (AttributeError, ValidationError) as exc:
        raise QualityDomainError("QUALITY_ASSESSMENT_INTEGRITY_INVALID") from exc


def _assessment_explicit_overrides(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict) or "resolved" not in value:
        # Pre-envelope records had no provenance. Treat their persisted
        # semantics as explicit for backwards-compatible audit/freshness.
        return _resolved_quality_config(value)
    explicit = value.get("explicit_overrides")
    source = value.get("source")
    if not isinstance(explicit, dict) or not isinstance(source, dict):
        raise QualityDomainError("QUALITY_ASSESSMENT_INTEGRITY_INVALID")
    try:
        normalized = QualityGateConfig.model_validate({**QualityGateConfig().model_dump(mode="json"), **explicit}).model_dump(mode="json")
    except ValidationError as exc:
        raise QualityDomainError("QUALITY_ASSESSMENT_INTEGRITY_INVALID") from exc
    if explicit != {key: normalized[key] for key in explicit} or source.get("explicit_overrides_fingerprint") != _fp(explicit, "quality-explicit-config-v1") or not isinstance(source.get("project_quality_gate_fingerprint"), str):
        raise QualityDomainError("QUALITY_ASSESSMENT_INTEGRITY_INVALID")
    resolved = _resolved_quality_config(value)
    if any(resolved[key] != item for key, item in explicit.items()):
        raise QualityDomainError("QUALITY_ASSESSMENT_INTEGRITY_INVALID")
    return explicit


class AntiAIBibleResolver:
    DEFAULT = {
        "disabled_expressions": [], "warning_expressions": [],
        "frequency_limits": {
            "expressions": {}, "punctuation": {},
            "repeated_sentence_prefix": 3, "repeated_paragraph_opening": 3,
            "repeated_exact_sentence": 3,
            # These are heuristics, not a vocabulary blacklist. A single
            # occurrence of a literary word is fine; abnormal density is not.
            "rare_words": {
                "攥": {"max_per_1000": 2, "severity": "MINOR"},
                "眸": {"max_per_1000": 2, "severity": "MINOR"},
                "喉结": {"max_per_1000": 1, "severity": "MINOR"},
                "唇角": {"max_per_1000": 2, "severity": "MINOR"},
                "身形": {"max_per_1000": 2, "severity": "MINOR"},
            },
            "scenery": {"max_consecutive_sentences": 3, "max_ratio": 0.35},
            "dialogue": {"max_similarity": 0.86, "max_recap_count": 2},
            "emotion_explanation": {"max_per_1000": 4},
            "cognitive_explanation": {"max_per_1000": 6},
            "template_structures": {"max_per_1000": 3},
            "abstract_summary": {"max_per_1000": 2},
        },
        "writing_principles": [], "future_risk_labels": [],
    }

    def resolve(self, db: Session, project_id: str) -> dict[str, Any]:
        rows = db.scalars(select(AntiAIBible).where(AntiAIBible.project_id == project_id, AntiAIBible.active.is_(True)).order_by(AntiAIBible.version, AntiAIBible.id)).all()
        if len(rows) > 1:
            raise QualityDomainError("ANTI_AI_BIBLE_AMBIGUOUS")
        row = rows[0] if rows else None
        rules = deepcopy(self.DEFAULT)
        if row is not None:
            rules.update({
                "disabled_expressions": list(row.disabled_expressions or []),
                "warning_expressions": list(row.warning_expressions or []),
                "writing_principles": list(row.writing_principles or []),
                "future_risk_labels": list(row.future_risk_labels or []),
            })
            supplied_limits = dict(row.frequency_limits or {})
            for key, value in supplied_limits.items():
                if isinstance(value, dict) and isinstance(rules["frequency_limits"].get(key), dict):
                    rules["frequency_limits"][key] = {**rules["frequency_limits"][key], **value}
                else:
                    rules["frequency_limits"][key] = value
        self.validate(rules)
        semantic = {"version": row.version if row else 1, **rules}
        return {"row": row, "id": row.id if row else None, "version": row.version if row else None, "rules": rules, "fingerprint": _fp(semantic, "anti-ai-bible-v1") if row else _fp(semantic, "anti-ai-default-v1")}

    @staticmethod
    def validate(rules: dict[str, Any]) -> None:
        for key in ("disabled_expressions", "warning_expressions", "writing_principles", "future_risk_labels"):
            if not isinstance(rules.get(key), list) or not all(isinstance(item, str) and item for item in rules[key]):
                raise QualityDomainError("ANTI_AI_BIBLE_INVALID", {"field": key})
        limits = rules.get("frequency_limits")
        allowed = {"expressions", "punctuation", "repeated_sentence_prefix", "repeated_paragraph_opening", "repeated_exact_sentence", "rare_words", "scenery", "dialogue", "emotion_explanation", "cognitive_explanation", "template_structures", "abstract_summary"}
        if not isinstance(limits, dict) or set(limits) - allowed:
            raise QualityDomainError("ANTI_AI_BIBLE_INVALID", {"field": "frequency_limits"})
        for key in ("expressions", "punctuation"):
            value = limits.get(key, {})
            if not isinstance(value, dict) or not all(isinstance(k, str) and k and isinstance(v, int) and v >= 0 for k, v in value.items()):
                raise QualityDomainError("ANTI_AI_BIBLE_INVALID", {"field": key})
        for key in ("repeated_sentence_prefix", "repeated_paragraph_opening", "repeated_exact_sentence"):
            if key in limits and (not isinstance(limits[key], int) or limits[key] < 1):
                raise QualityDomainError("ANTI_AI_BIBLE_INVALID", {"field": key})
        rare_words = limits.get("rare_words", {})
        if not isinstance(rare_words, dict) or any(not isinstance(word, str) or not word or not isinstance(value, dict) or not isinstance(value.get("max_per_1000"), (int, float)) or value["max_per_1000"] < 0 for word, value in rare_words.items()):
            raise QualityDomainError("ANTI_AI_BIBLE_INVALID", {"field": "rare_words"})
        for key in ("scenery", "dialogue", "emotion_explanation", "cognitive_explanation", "template_structures", "abstract_summary"):
            value = limits.get(key, {})
            if not isinstance(value, dict):
                raise QualityDomainError("ANTI_AI_BIBLE_INVALID", {"field": key})
            for name, item in value.items():
                if name.startswith("max_") and (not isinstance(item, (int, float)) or item < 0):
                    raise QualityDomainError("ANTI_AI_BIBLE_INVALID", {"field": key})


def _normalized_with_offsets(value: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    offsets: list[int] = []
    pending_space = False
    pending_offset = 0
    for index, char in enumerate(value):
        normalized = unicodedata.normalize("NFKC", char)
        for item in normalized:
            if item.isspace():
                if chars:
                    pending_space = True
                    pending_offset = index
                continue
            if pending_space:
                chars.append(" "); offsets.append(pending_offset); pending_space = False
            chars.append(item); offsets.append(index)
    return "".join(chars), offsets


def _matches(prose: str, expression: str) -> list[tuple[int, int]]:
    normalized, offsets = _normalized_with_offsets(prose)
    needle, _ = _normalized_with_offsets(expression)
    if not needle:
        return []
    result: list[tuple[int, int]] = []
    cursor = 0
    while True:
        found = normalized.find(needle, cursor)
        if found < 0:
            break
        result.append((offsets[found], offsets[found + len(needle) - 1] + 1))
        cursor = found + max(1, len(needle))
    return result


def finding_fingerprint(finding: dict[str, Any]) -> str:
    semantic = {key: finding.get(key) for key in ("source", "category", "severity", "rule_code", "message", "start_offset", "end_offset", "excerpt", "source_refs", "metadata")}
    return _fp(semantic, "quality-finding-v1")


def _finding(*, source: str, category: str, severity: str, rule_code: str, message: str,
             start_offset: int | None = None, end_offset: int | None = None,
             excerpt: str | None = None, source_refs: list[Any] | None = None,
             metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    value = {"source": source, "category": category, "severity": severity, "rule_code": rule_code, "message": message, "start_offset": start_offset, "end_offset": end_offset, "excerpt": excerpt, "source_refs": source_refs or [], "metadata": metadata or {}}
    value["finding_fingerprint"] = finding_fingerprint(value)
    return value


class NarrativeRepetitionDetector:
    sentence_splitter = re.compile(r"(?<=[。！？.!?])")

    def detect(self, prose: str, limits: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        sentences = [item.strip() for item in self.sentence_splitter.split(prose) if item.strip()]
        paragraphs = [item.strip() for item in re.split(r"\n\s*\n|\n", prose) if item.strip()]
        exact_limit = limits.get("repeated_exact_sentence", 3)
        sentence_counts = Counter(sentences)
        findings = []
        for sentence, count in sorted(sentence_counts.items()):
            if count >= exact_limit and len(sentence) > 3:
                findings.append(_finding(source="DETERMINISTIC", category="REPETITION", severity="MAJOR", rule_code="REPEATED_EXACT_SENTENCE", message="An exact sentence is repeated too often.", excerpt=sentence, metadata={"count": count, "limit": exact_limit}))
        opening_limit = limits.get("repeated_paragraph_opening", 3)
        openings = Counter(item[:3] for item in paragraphs if len(item) >= 3)
        for opening, count in sorted(openings.items()):
            if count >= opening_limit:
                findings.append(_finding(source="DETERMINISTIC", category="REPETITION", severity="MAJOR", rule_code="REPEATED_PARAGRAPH_OPENING", message="Paragraph openings repeat too often.", excerpt=opening, metadata={"count": count, "limit": opening_limit}))
        prefix_limit = limits.get("repeated_sentence_prefix", 3)
        prefixes = Counter(item[:3] for item in sentences if len(item) >= 3)
        for prefix, count in sorted(prefixes.items()):
            if count >= prefix_limit and not any(item["rule_code"] == "REPEATED_PARAGRAPH_OPENING" and item["excerpt"] == prefix for item in findings):
                findings.append(_finding(source="DETERMINISTIC", category="REPETITION", severity="MINOR", rule_code="REPEATED_SENTENCE_PREFIX", message="Sentence openings repeat too often.", excerpt=prefix, metadata={"count": count, "limit": prefix_limit}))
        return findings, {"repeated_exact_sentences": sum(1 for value in sentence_counts.values() if value >= exact_limit), "repeated_paragraph_openings": sum(1 for value in openings.values() if value >= opening_limit), "repeated_sentence_prefixes": sum(1 for value in prefixes.values() if value >= prefix_limit)}


class ChineseStyleStatistics:
    """Deterministic Chinese prose signals used to catch AI-like density.

    The detector intentionally reports distributions and repeated patterns, not
    an authorship verdict. Literary vocabulary remains valid when used sparingly
    and in a character-appropriate voice.
    """

    sentence_splitter = NarrativeRepetitionDetector.sentence_splitter
    scenery_terms = re.compile(r"天空|云|月色|月光|阳光|晨光|暮色|夜色|风|雨|雪|雾|雷|街道|树|花|草|河|山|湖|窗外|灯光|影子|落叶|远处|天边|地面|空气")
    action_terms = re.compile(r"走|跑|停|转身|抬|低|看|听|说|问|答|拿|放|推|拉|抓|握|打开|关上|坐|站|笑|哭|喘|退|靠|冲|躲|递|写|拔|挥")
    emotion_terms = re.compile(r"感到|觉得|意识到|明白|察觉到|不禁|仿佛|似乎|显然|意味深长|心中(?:充满|一阵)|(?:愤怒|悲伤|紧张|害怕|绝望|欣喜|高兴|痛苦|焦虑|震惊|羞愧|恐惧|激动|失望|不安|委屈)(?:地|的)?")
    template_terms = re.compile(r"不仅[^。！？]{0,35}而且|不是[^。！？]{0,35}而是|既[^。！？]{0,35}又|一方面[^。！？]{0,35}另一方面")
    abstract_summary = re.compile(r"(?:这|那|此)(?:一刻|一切|一切的一切)?(?:意味着|说明了|证明了|宣告着)|命运的(?:齿轮|长河)|从此以后|在某种意义上|归根结底|最终(?:明白|意识到)")

    @staticmethod
    def _sentences(prose: str) -> list[str]:
        return [item.strip() for item in ChineseStyleStatistics.sentence_splitter.split(prose) if item.strip()]

    @staticmethod
    def _per_thousand(count: int, char_count: int) -> float:
        return round(count * 1000 / max(1, char_count), 3)

    def analyze(self, prose: str, limits: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        sentences = self._sentences(prose)
        char_count = len(prose)
        scenery_flags = [bool(self.scenery_terms.search(sentence)) for sentence in sentences]
        scenery_count = sum(scenery_flags)
        scenery_action_count = sum(bool(self.scenery_terms.search(sentence) and self.action_terms.search(sentence)) for sentence in sentences)
        emotion_count = len(self.emotion_terms.findall(prose))
        cognitive_count = len(re.findall(r"意识到|明白|察觉到|感到|觉得|不禁|仿佛|似乎|显然|意味深长", prose))
        template_count = len(self.template_terms.findall(prose))
        summary_count = len(self.abstract_summary.findall(prose))
        dialogue = [m.group(1).strip() for m in re.finditer(r"[\"“](.*?)[\"”]", prose, re.S) if m.group(1).strip()]
        normalized_dialogue = [re.sub(r"[\s，。！？；：、,.!?;:'\"“”]+", "", item) for item in dialogue]
        duplicate_dialogue_pairs = 0
        for previous, current in zip(normalized_dialogue, normalized_dialogue[1:]):
            if len(previous) >= 4 and len(current) >= 4 and SequenceMatcher(None, previous, current).ratio() >= float(limits.get("dialogue", {}).get("max_similarity", 0.86)):
                duplicate_dialogue_pairs += 1
        rare_counts = {word: prose.count(word) for word in (limits.get("rare_words", {}) or {}) if prose.count(word)}
        metrics = {
            "scenery_sentence_count": scenery_count,
            "scenery_ratio": round(scenery_count / max(1, len(sentences)), 6),
            "scenery_with_action_ratio": round(scenery_action_count / max(1, scenery_count), 6),
            "explicit_emotion_count": emotion_count,
            "explicit_emotion_per_1000": self._per_thousand(emotion_count, char_count),
            "cognitive_explanation_count": cognitive_count,
            "cognitive_explanation_per_1000": self._per_thousand(cognitive_count, char_count),
            "template_structure_count": template_count,
            "template_structure_per_1000": self._per_thousand(template_count, char_count),
            "abstract_summary_count": summary_count,
            "abstract_summary_per_1000": self._per_thousand(summary_count, char_count),
            "dialogue_turn_count": len(dialogue),
            "dialogue_information_repeat_pairs": duplicate_dialogue_pairs,
            "rare_word_counts": rare_counts,
        }
        findings: list[dict[str, Any]] = []
        scenery_rules = limits.get("scenery", {}) or {}
        max_run = int(scenery_rules.get("max_consecutive_sentences", 3))
        run = 0
        for index, is_scenery in enumerate(scenery_flags):
            run = run + 1 if is_scenery else 0
            if run >= max_run and not self.action_terms.search(sentences[index]):
                findings.append(_finding(source="DETERMINISTIC", category="LANGUAGE_QUALITY", severity="MINOR", rule_code="LOW_INFORMATION_SCENERY", message="连续景物描写没有带来角色行动或状态变化。", excerpt=sentences[index], metadata={"run": run, "limit": max_run}))
                break
        if metrics["scenery_ratio"] > float(scenery_rules.get("max_ratio", 0.35)) and metrics["scenery_with_action_ratio"] < 0.5:
            findings.append(_finding(source="DETERMINISTIC", category="PACING", severity="MAJOR", rule_code="SCENERY_OVERLOAD", message="景物描写占比过高且与行动脱钩。", metadata={"ratio": metrics["scenery_ratio"], "limit": scenery_rules.get("max_ratio", 0.35)}))
        rare_rules = limits.get("rare_words", {}) or {}
        for word, count in sorted(rare_counts.items()):
            maximum = max(1.0, float(rare_rules[word].get("max_per_1000", 0)) * max(1, char_count) / 1000)
            if count > maximum:
                findings.append(_finding(source="DETERMINISTIC", category="STYLE_CONSISTENCY", severity=rare_rules[word].get("severity", "MINOR"), rule_code="RARE_WORD_FREQUENCY_ANOMALY", message="罕见动作或身体词在本章密度异常，需核对是否为作者有意的词汇选择。", excerpt=word, metadata={"word": word, "count": count, "max_per_1000": rare_rules[word].get("max_per_1000")}))
        if duplicate_dialogue_pairs > int((limits.get("dialogue", {}) or {}).get("max_recap_count", 2)):
            findings.append(_finding(source="DETERMINISTIC", category="REPETITION", severity="MAJOR", rule_code="DIALOGUE_INFORMATION_REPEAT", message="相邻对白高度重复，未推进新的信息或关系变化。", metadata={"pairs": duplicate_dialogue_pairs}))
        emotion_rules = limits.get("emotion_explanation", {}) or {}
        if metrics["explicit_emotion_per_1000"] > float(emotion_rules.get("max_per_1000", 4)):
            findings.append(_finding(source="DETERMINISTIC", category="LANGUAGE_QUALITY", severity="MAJOR", rule_code="EXPLICIT_EMOTION_EXPLANATION", message="直接命名情绪或解释心理的密度过高，应优先用行动、感官和对白呈现。", metadata={"per_1000": metrics["explicit_emotion_per_1000"], "limit": emotion_rules.get("max_per_1000", 4)}))
        cognitive_rules = limits.get("cognitive_explanation", {}) or {}
        if metrics["cognitive_explanation_per_1000"] > float(cognitive_rules.get("max_per_1000", 6)):
            findings.append(_finding(source="DETERMINISTIC", category="ANTI_AI_EXPRESSION", severity="MINOR", rule_code="COGNITIVE_EXPLANATION_OVERUSE", message="‘意识到/明白/仿佛’等解释性认知词过密。", metadata={"per_1000": metrics["cognitive_explanation_per_1000"], "limit": cognitive_rules.get("max_per_1000", 6)}))
        template_rules = limits.get("template_structures", {}) or {}
        if metrics["template_structure_per_1000"] > float(template_rules.get("max_per_1000", 3)):
            findings.append(_finding(source="DETERMINISTIC", category="STYLE_CONSISTENCY", severity="MINOR", rule_code="SYMMETRICAL_PARALLELISM_OVERUSE", message="对称排比句式过密，段落结构可能呈现模板化。", metadata={"per_1000": metrics["template_structure_per_1000"], "limit": template_rules.get("max_per_1000", 3)}))
        summary_rules = limits.get("abstract_summary", {}) or {}
        if metrics["abstract_summary_per_1000"] > float(summary_rules.get("max_per_1000", 2)):
            findings.append(_finding(source="DETERMINISTIC", category="LANGUAGE_QUALITY", severity="MINOR", rule_code="ABSTRACT_SUMMARY_CLICHE", message="段尾抽象总结或升华过密，可能替代了具体叙事。", metadata={"per_1000": metrics["abstract_summary_per_1000"], "limit": summary_rules.get("max_per_1000", 2)}))
        return findings, metrics


class AntiAIStyleRuleEngine:
    def evaluate(self, prose: str, rules: dict[str, Any]) -> dict[str, Any]:
        AntiAIBibleResolver.validate(rules)
        findings: list[dict[str, Any]] = []
        disabled_hits = warning_hits = 0
        for expression in sorted(set(rules["disabled_expressions"])):
            for start, end in _matches(prose, expression):
                disabled_hits += 1
                findings.append(_finding(source="DETERMINISTIC", category="ANTI_AI_EXPRESSION", severity="BLOCKING", rule_code="ANTI_AI_DISABLED_EXPRESSION", message="A disabled expression is present.", start_offset=start, end_offset=end, excerpt=prose[start:end], metadata={"expression": expression}))
        for expression in sorted(set(rules["warning_expressions"])):
            for start, end in _matches(prose, expression):
                warning_hits += 1
                findings.append(_finding(source="DETERMINISTIC", category="ANTI_AI_EXPRESSION", severity="MINOR", rule_code="ANTI_AI_WARNING_EXPRESSION", message="A discouraged expression is present.", start_offset=start, end_offset=end, excerpt=prose[start:end], metadata={"expression": expression}))
        limits = rules["frequency_limits"]
        for expression, maximum in sorted(limits.get("expressions", {}).items()):
            spans = _matches(prose, expression)
            if len(spans) > maximum:
                findings.append(_finding(source="DETERMINISTIC", category="REPETITION", severity="MAJOR", rule_code="ANTI_AI_EXPRESSION_FREQUENCY", message="An expression exceeds its frequency limit.", metadata={"expression": expression, "count": len(spans), "limit": maximum}))
        punctuation_counts = {item: prose.count(item) for item in sorted(set("!?！？…"))}
        for mark, maximum in sorted(limits.get("punctuation", {}).items()):
            count = prose.count(mark)
            punctuation_counts[mark] = count
            if count > maximum:
                findings.append(_finding(source="DETERMINISTIC", category="FORMAT", severity="MAJOR", rule_code="ANTI_AI_PUNCTUATION_FREQUENCY", message="Punctuation exceeds its frequency limit.", metadata={"punctuation": mark, "count": count, "limit": maximum}))
        repetition, repeat_metrics = NarrativeRepetitionDetector().detect(prose, limits)
        findings.extend(repetition)
        style_findings, style_metrics = ChineseStyleStatistics().analyze(prose, limits)
        findings.extend(style_findings)
        paragraphs = [item for item in re.split(r"\n\s*\n|\n", prose) if item.strip()]
        sentences = [item for item in NarrativeRepetitionDetector.sentence_splitter.split(prose) if item.strip()]
        dialogue_chars = sum(len(match.group(0)) for match in re.finditer(r"[\"“][^\"”]*[\"”]", prose))
        metrics = {"char_count": len(prose), "word_count": WriterWordCounter().count(prose), "paragraph_count": len(paragraphs), "sentence_count": len(sentences), "dialogue_ratio": round(dialogue_chars / max(1, len(prose)), 6), "punctuation_counts": punctuation_counts, "disabled_hit_count": disabled_hits, "warning_hit_count": warning_hits, **repeat_metrics, **style_metrics}
        findings.sort(key=lambda item: (item["start_offset"] is None, item["start_offset"] or 0, item["rule_code"], item["finding_fingerprint"]))
        return {"protocol": "anti-ai-style-rules-v1", "metrics": metrics, "findings": findings}


class NovelContinuityQualityChecker:
    """Deterministic continuity checks that complement prose/style analysis."""

    protocol = "novel-continuity-v1"

    def evaluate(self, db: Session, context: dict[str, Any]) -> dict[str, Any]:
        chapter = context["chapter"]
        task = context.get("writer_safe_context", {}).get("planning_task")
        if not task:
            return {"protocol": self.protocol, "enabled": False, "metrics": {}, "findings": []}
        prose = context.get("prose", "")
        draft = context.get("writer_draft")
        validation = (draft.validation_report or {}) if draft else {}
        findings: list[dict[str, Any]] = []
        coverage = {str(item) for item in validation.get("task_coverage", [])}
        planned_beats = {str(item) for item in (task.get("scene_beats") or []) if str(item).strip()}
        executed_beats = {str(beat) for scene in ((context.get("writer_safe_context", {}).get("source_manifest") or {}).get("scenes") or []) for turn in (scene.get("turns") or []) for beat in (turn.get("scene_beat_refs") or [])}
        for beat in sorted(planned_beats - executed_beats):
            findings.append(_finding(source="DETERMINISTIC", category="CONTINUITY", severity="BLOCKING", rule_code="SCENE_BEAT_NOT_EXECUTED", message="章节规划中的场景节拍没有对应的角色行动记录。", metadata={"scene_beat": beat, "chapter_number": chapter.number}))
        for beat in (sorted(executed_beats - planned_beats) if planned_beats else []):
            findings.append(_finding(source="DETERMINISTIC", category="CONTINUITY", severity="BLOCKING", rule_code="SCENE_BEAT_REFERENCE_INVALID", message="角色行动引用了当前章节任务之外的场景节拍。", metadata={"scene_beat": beat, "chapter_number": chapter.number}))
        for event in task.get("must_events", []):
            if str(event) not in coverage:
                findings.append(_finding(source="DETERMINISTIC", category="CONTINUITY", severity="BLOCKING", rule_code="PLAN_REQUIRED_EVENT_MISSING", message="Chapter task sheet mandatory event was not declared complete.", metadata={"event": event, "chapter_number": chapter.number}))
        for payoff in task.get("foreshadow_payoff", []) or []:
            if str(payoff) not in coverage:
                findings.append(_finding(source="DETERMINISTIC", category="CONTINUITY", severity="MAJOR", rule_code="FORESHADOWING_PAYOFF_MISSING", message="本章规划要求回收的伏笔没有被声明完成。", metadata={"foreshadowing": payoff, "chapter_number": chapter.number}))
        forbidden_hits = {str(item) for item in validation.get("task_forbidden_hits", [])}
        for event in task.get("forbidden_events", []):
            if str(event) in forbidden_hits:
                findings.append(_finding(source="DETERMINISTIC", category="CONTINUITY", severity="BLOCKING", rule_code="PLAN_FORBIDDEN_EVENT_PRESENT", message="Chapter task sheet forbidden event was reported by Writer.", metadata={"event": event, "chapter_number": chapter.number}))
        for phrase in task.get("forbidden_reveals", []) or []:
            if isinstance(phrase, str) and phrase and phrase in prose:
                findings.append(_finding(source="DETERMINISTIC", category="REVEAL_SAFETY", severity="BLOCKING", rule_code="PLAN_FORBIDDEN_REVEAL_PRESENT", message="正文出现了章节任务禁止揭示的内容。", excerpt=phrase, metadata={"reveal": phrase}))

        scenes = ((context.get("writer_safe_context", {}).get("source_manifest") or {}).get("scenes") or [])
        previous_time = None
        previous_sequence = None
        seen_sequences: set[int] = set()
        seen_scene_ids: set[str] = set()
        timeline_errors = 0
        location_conflicts = 0
        for scene in scenes:
            scene_id = str(scene.get("scene_id") or "")
            sequence = scene.get("sequence")
            if not scene_id or scene_id in seen_scene_ids:
                findings.append(_finding(source="DETERMINISTIC", category="CONTINUITY", severity="BLOCKING", rule_code="SCENE_ID_DUPLICATE", message="章节来源场景缺少唯一稳定 ID。", source_refs=[{"source_type": "SCENE", "source_id": scene_id}]))
            seen_scene_ids.add(scene_id)
            if not isinstance(sequence, int) or sequence in seen_sequences or (previous_sequence is not None and sequence <= previous_sequence):
                timeline_errors += 1
                findings.append(_finding(source="DETERMINISTIC", category="CONTINUITY", severity="BLOCKING", rule_code="TIMELINE_SEQUENCE_INVALID", message="章节来源场景的正式序号不唯一或未严格递增。", source_refs=[{"source_type": "SCENE", "source_id": scene_id}], metadata={"sequence": sequence, "previous_sequence": previous_sequence}))
            if isinstance(sequence, int):
                seen_sequences.add(sequence); previous_sequence = sequence
            raw_time = scene.get("world_time")
            if raw_time:
                try:
                    from datetime import datetime as _datetime
                    current_time = _datetime.fromisoformat(raw_time)
                    if previous_time is not None and current_time < previous_time:
                        timeline_errors += 1
                        findings.append(_finding(source="DETERMINISTIC", category="CONTINUITY", severity="BLOCKING", rule_code="TIMELINE_ORDER_INVALID", message="章节来源场景的世界时间顺序倒退。", source_refs=[{"source_type": "SCENE", "source_id": scene.get("scene_id")}], metadata={"world_time": raw_time}))
                    previous_time = current_time
                except (TypeError, ValueError):
                    findings.append(_finding(source="DETERMINISTIC", category="CONTINUITY", severity="BLOCKING", rule_code="TIMELINE_TIMESTAMP_INVALID", message="章节来源场景包含无法解析的世界时间。", source_refs=[{"source_type": "SCENE", "source_id": scene.get("scene_id")}]))
            participants = set(scene.get("participants") or [])
            if any(not isinstance(item, str) or not item for item in participants):
                findings.append(_finding(source="DETERMINISTIC", category="CONTINUITY", severity="BLOCKING", rule_code="PARTICIPANT_REFERENCE_INVALID", message="场景包含无效角色引用。", source_refs=[{"source_type": "SCENE", "source_id": scene_id}]))
            if scenes and scene is not scenes[0]:
                prior = scenes[scenes.index(scene) - 1]
                if scene.get("world_time") and scene.get("world_time") == prior.get("world_time") and participants.intersection(prior.get("participants") or set()) and scene.get("location") != prior.get("location"):
                    location_conflicts += 1
                    findings.append(_finding(source="DETERMINISTIC", category="CONTINUITY", severity="BLOCKING", rule_code="LOCATION_CONFLICT", message="同一角色在同一时刻出现在不同地点。", source_refs=[{"source_type": "SCENE", "source_id": scene.get("scene_id")}]))
            for turn in scene.get("turns", []) or []:
                if not turn.get("id") or not (turn.get("decision") or {}).get("id"):
                    findings.append(_finding(source="DETERMINISTIC", category="CAUSAL_GROUNDING", severity="BLOCKING", rule_code="TURN_DECISION_LINEAGE_MISSING", message="角色行动缺少可追溯的决策记录。", source_refs=[{"source_type": "SCENE", "source_id": scene_id}]))
                if turn.get("requires_world_resolution") and not turn.get("resolution_id") and not turn.get("resolution"):
                    findings.append(_finding(source="DETERMINISTIC", category="CAUSAL_GROUNDING", severity="BLOCKING", rule_code="WORLD_RESOLUTION_LINEAGE_MISSING", message="角色行动声明需要世界裁决，但来源场景没有对应裁决记录。", source_refs=[{"source_type": "TURN", "source_id": str(turn.get("id"))}]))
                decision = turn.get("decision") or {}
                character_id = decision.get("character_id") or turn.get("actor_character_id")
                for reference in decision.get("knowledge_used", []) or []:
                    knowledge_id = reference.get("knowledge_id") if isinstance(reference, dict) else reference
                    row = db.get(CharacterKnowledge, knowledge_id) if isinstance(knowledge_id, str) else None
                    if not row or row.character_id != character_id or row.status.value not in set(reference.get("accepted_statuses", [row.status.value]) if isinstance(reference, dict) and row else [row.status.value] if row else []):
                        findings.append(_finding(source="DETERMINISTIC", category="FACTUAL_GROUNDING", severity="BLOCKING", rule_code="KNOWLEDGE_LEAK", message="角色使用了不属于其认知范围的知识。", source_refs=[{"source_type": "CHARACTER_KNOWLEDGE", "source_id": str(knowledge_id)}], metadata={"character_id": character_id}))
        findings.sort(key=lambda item: (item["rule_code"], item["finding_fingerprint"]))
        return {"protocol": self.protocol, "enabled": True, "metrics": {"scene_count": len(scenes), "timeline_errors": timeline_errors, "location_conflicts": location_conflicts, "mandatory_event_count": len(task.get("must_events", [])), "declared_task_coverage": len(coverage), "planned_scene_beat_count": len(planned_beats), "executed_scene_beat_count": len(executed_beats)}, "findings": findings}


class CriticScores(BaseModel):
    model_config = ConfigDict(extra="forbid")
    factual_grounding: int = Field(ge=0, le=100)
    pov_compliance: int = Field(ge=0, le=100)
    reveal_safety: int = Field(ge=0, le=100)
    style_naturalness: int = Field(ge=0, le=100)
    repetition: int = Field(ge=0, le=100)
    pacing: int = Field(ge=0, le=100)
    voice_consistency: int = Field(ge=0, le=100)
    overall: int = Field(ge=0, le=100)


class CriticSourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_type: str
    source_id: str


class CriticFindingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    severity: Literal["BLOCKING", "MAJOR", "MINOR", "INFO"]
    message: str = Field(min_length=1)
    start_offset: int | None = None
    end_offset: int | None = None
    excerpt: str | None = None
    source_refs: list[CriticSourceRef] = Field(default_factory=list)


class CriticOutputPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["PASS", "REPAIR_REQUIRED", "BLOCKED"]
    scores: CriticScores
    findings: list[CriticFindingPayload]


class CriticOutputValidator:
    def parse(self, content: str, prose: str, allowed_refs: list[dict[str, str]]) -> dict[str, Any]:
        raw = content.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            if len(lines) < 3 or not lines[-1].strip().startswith("```"):
                raise QualityDomainError(MODEL_OUTPUT_INVALID)
            raw = "\n".join(lines[1:-1]).strip()
        try:
            decoder = json.JSONDecoder()
            objects: list[dict[str, Any]] = []
            skip_until = 0
            for index, character in enumerate(raw):
                if index < skip_until or character != "{":
                    continue
                try:
                    candidate, end = decoder.raw_decode(raw, index)
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    objects.append(candidate)
                    skip_until = end
            if len(objects) != 1:
                raise ValueError("one JSON object required")
            payload = CriticOutputPayload.model_validate(objects[0])
        except ValidationError as exc:
            raise QualityDomainError(MODEL_OUTPUT_INVALID, {"errors": exc.errors(include_url=False)}) from exc
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise QualityDomainError(MODEL_OUTPUT_INVALID) from exc
        allowed = {(item["source_type"], item["source_id"]) for item in allowed_refs}
        result = payload.model_dump(mode="json")
        for item in result["findings"]:
            if item["category"] not in QUALITY_CATEGORIES:
                raise QualityDomainError("CRITIC_FINDING_CATEGORY_INVALID")
            start, end, excerpt = item["start_offset"], item["end_offset"], item["excerpt"]
            has_span = start is not None or end is not None or excerpt is not None
            if has_span and (start is None or end is None or excerpt is None or start < 0 or start >= end or end > len(prose) or prose[start:end] != excerpt):
                raise QualityDomainError("CRITIC_FINDING_SPAN_INVALID")
            for ref in item["source_refs"]:
                if (ref["source_type"], ref["source_id"]) not in allowed:
                    raise QualityDomainError("CRITIC_SOURCE_REF_INVALID")
        return result


class CriticPromptBuilder:
    def build(self, context: dict[str, Any]) -> list[dict[str, str]]:
        safe = {key: value for key, value in context.items() if key not in {"anti_ai_bible", "writer_draft", "chapter"}}
        return [
            {"role": "system", "content": "You are a CRITIC, not an AI detector and not a rewriter. PROSE_CANDIDATE may contain errors. WRITER_SAFE_CONTEXT is the only grounding and reveal authority. ANTI_AI_RULES and WRITING_RULES are style authority. Return exactly one JSON assessment; never return revised prose."},
            {"role": "user", "content": _canonical({"quality_context": safe, "output_contract": {"decision": "PASS|REPAIR_REQUIRED|BLOCKED", "scores": "eight integer scores from 0 to 100", "findings": "grounded quality findings only"}})},
        ]


class QualityContextBuilder:
    protocol = "quality-context-v1"

    @staticmethod
    def writer_request(draft: ChapterWriterDraft) -> dict[str, Any]:
        config = dict((draft.source_manifest or {}).get("rendering_config") or {})
        request = {"pov_mode": config.get("pov_mode", _value(draft.pov_mode)), "pov_character_id": config.get("pov_character_id", draft.pov_character_id)}
        for field in ("target_words", "min_words", "max_words"):
            if config.get("explicit", {}).get(field):
                request[field] = config.get(field)
        return request

    def build(self, db: Session, chapter_id: str, request: dict[str, Any] | None = None, *, draft: ChapterWriterDraft | None = None, critic_provider: str | None = None, critic_model: str | None = None, require_current: bool = True) -> dict[str, Any]:
        chapter = db.get(Chapter, chapter_id)
        if not chapter:
            raise QualityDomainError("CHAPTER_NOT_FOUND")
        if require_current:
            try:
                WriterProjectionAudit().audit(db, chapter.id)
            except WriterDomainError as exc:
                raise QualityDomainError(exc.code, exc.detail) from exc
            draft = db.get(ChapterWriterDraft, chapter.current_writer_draft_id) if chapter.current_writer_draft_id else None
            if not draft or _value(draft.status) != WriterDraftStatus.ADOPTED.value or chapter.content != draft.content:
                raise QualityDomainError("QUALITY_WRITER_DRAFT_REQUIRED")
        if draft is None or not draft.content or not draft.content_fingerprint:
            raise QualityDomainError("QUALITY_WRITER_DRAFT_REQUIRED")
        try:
            source = WriterChapterSourceBuilder().build(db, chapter.id)
            writer_context = WriterContextBuilder().build(db, source, self.writer_request(draft))
        except WriterDomainError as exc:
            raise QualityDomainError("WRITER_SOURCE_CHANGED", {"cause": exc.code}) from exc
        context_fresh = writer_context["writer_context_fingerprint"] == draft.writer_context_fingerprint
        if not context_fresh and chapter.title == draft.title_candidate:
            # A missing title may be populated by adopting this exact draft. Rebuild
            # the original semantic context without mutating the Chapter row.
            original = dict(writer_context)
            original["chapter"] = {**writer_context["chapter"], "title": None}
            context_fresh = WriterContextFingerprintBuilder().build(original) == draft.writer_context_fingerprint
        if source["source_fingerprint"] != draft.chapter_source_fingerprint or not context_fresh:
            raise QualityDomainError("WRITER_SOURCE_CHANGED")
        project = db.get(Project, chapter.project_id)
        config_envelope = QualityGateConfigResolver().envelope(project, request)
        config_value = _resolved_quality_config(config_envelope)
        anti_ai = AntiAIBibleResolver().resolve(db, chapter.project_id)
        config_fp = _fp(config_value, "quality-config-v1")
        safe_writer = {key: value for key, value in writer_context.items() if key not in {"writing_bible", "pov_mode", "pov_character_id"}}
        # A deterministic-only assessment has no critic input at all. Keeping
        # a provider/model in this fingerprint would incorrectly make a route
        # change stale an assessment that never used that route.
        route = {"provider": critic_provider, "model": critic_model, "protocol": "critic-output-v1"} if config_value["require_critic"] else None
        semantic = {"content_fingerprint": draft.content_fingerprint, "writer_context_fingerprint": draft.writer_context_fingerprint, "chapter_source_fingerprint": draft.chapter_source_fingerprint, "writing_bible_fingerprint": draft.writing_bible_fingerprint, "anti_ai_bible_fingerprint": anti_ai["fingerprint"], "quality_config_fingerprint": config_fp, "critic": route}
        return {"chapter": chapter, "writer_draft": draft, "prose": draft.content, "writer_safe_context": safe_writer, "writing_rules": writer_context["writing_rules"], "anti_ai_rules": anti_ai["rules"], "anti_ai_bible": anti_ai, "quality_contract": {"prose_is_truth": False, "critic_may_rewrite": False, "formal_mutation": False}, "source_fingerprints": semantic, "quality_config": config_envelope, "resolved_quality_config": config_value, "quality_config_fingerprint": config_fp, "quality_context_fingerprint": _fp(semantic, self.protocol), "renderable_source_refs": writer_context["renderable_source_refs"]}


class QualityDecisionEngine:
    """The sole authority for persisted quality decisions.

    Both creation and audit feed the same normalized inputs through this small
    pure function.  Quality findings are evidence; neither prose nor a critic
    decision is allowed to override the policy encoded here.
    """

    @staticmethod
    def decide(findings: list[dict[str, Any]], critic_decision: str | None,
               config: dict[str, Any], overall_score: float | None) -> tuple[QualityAssessmentStatus, list[str]]:
        config_value = _resolved_quality_config(config)
        severities = [_value(item.get("severity")) for item in findings]
        reasons: list[str] = []
        if critic_decision == "BLOCKED":
            reasons.append("CRITIC_BLOCKED")
        if "BLOCKING" in severities:
            reasons.append("BLOCKING_FINDINGS")
        if severities.count("MAJOR") > config_value["max_major_findings"]:
            reasons.append("MAJOR_FINDING_LIMIT")
        if "MINOR" in severities and not config_value["allow_minor_findings"]:
            reasons.append("MINOR_FINDINGS_NOT_ALLOWED")
        if config_value["require_critic"] and overall_score is not None and float(overall_score) < config_value["min_overall_score"]:
            reasons.append("OVERALL_SCORE_BELOW_MINIMUM")
        if critic_decision == "REPAIR_REQUIRED":
            reasons.append("CRITIC_REPAIR_REQUIRED")
        reasons = sorted(set(reasons))
        if "CRITIC_BLOCKED" in reasons:
            return QualityAssessmentStatus.BLOCKED, reasons
        if reasons:
            return QualityAssessmentStatus.REPAIR_REQUIRED, reasons
        return QualityAssessmentStatus.PASS, []


def _assessment_bible_rules(db: Session, assessment: ChapterQualityAssessment) -> dict[str, Any]:
    if assessment.anti_ai_bible_id is None:
        rules = deepcopy(AntiAIBibleResolver.DEFAULT)
        expected = _fp({"version": 1, **rules}, "anti-ai-default-v1")
    else:
        bible = db.get(AntiAIBible, assessment.anti_ai_bible_id)
        if not bible or bible.project_id != assessment.project_id or bible.version != assessment.anti_ai_bible_version:
            raise QualityDomainError("QUALITY_ASSESSMENT_INTEGRITY_INVALID")
        rules = deepcopy(AntiAIBibleResolver.DEFAULT)
        rules.update({
            "disabled_expressions": list(bible.disabled_expressions or []),
            "warning_expressions": list(bible.warning_expressions or []),
            "writing_principles": list(bible.writing_principles or []),
            "future_risk_labels": list(bible.future_risk_labels or []),
        })
        for key, value in dict(bible.frequency_limits or {}).items():
            if isinstance(value, dict) and isinstance(rules["frequency_limits"].get(key), dict):
                rules["frequency_limits"][key] = {**rules["frequency_limits"][key], **value}
            else:
                rules["frequency_limits"][key] = value
        expected = _fp({"version": bible.version, **rules}, "anti-ai-bible-v1")
    AntiAIBibleResolver.validate(rules)
    if assessment.anti_ai_bible_fingerprint != expected:
        raise QualityDomainError("QUALITY_ASSESSMENT_INTEGRITY_INVALID")
    return rules


class QualityAssessmentFreshnessChecker:
    """Read-only proof that an assessment still describes present semantics."""

    def check(self, db: Session, assessment: ChapterQualityAssessment, *, require_current: bool = False) -> dict[str, Any]:
        chapter = db.get(Chapter, assessment.chapter_id)
        draft = db.get(ChapterWriterDraft, assessment.writer_draft_id)
        reasons: list[str] = []
        if not chapter or not draft or chapter.project_id != assessment.project_id or draft.project_id != assessment.project_id or draft.chapter_id != assessment.chapter_id:
            return {"fresh": False, "current": False, "reasons": ["QUALITY_ASSESSMENT_INTEGRITY_INVALID"]}
        try:
            context = QualityContextBuilder().build(
                db, chapter.id, {"config": _assessment_explicit_overrides(assessment.quality_config)}, draft=draft,
                critic_provider=assessment.critic_provider, critic_model=assessment.critic_model,
                require_current=False,
            )
        except QualityDomainError as exc:
            return {"fresh": False, "current": False, "reasons": [exc.code]}
        expected = {
            "content_fingerprint": assessment.content_fingerprint,
            "writer_context_fingerprint": assessment.writer_context_fingerprint,
            "chapter_source_fingerprint": assessment.chapter_source_fingerprint,
            "anti_ai_bible_fingerprint": assessment.anti_ai_bible_fingerprint,
            "quality_config_fingerprint": assessment.quality_config_fingerprint,
            "quality_context_fingerprint": assessment.quality_context_fingerprint,
            "writing_bible_fingerprint": assessment.writing_bible_fingerprint,
        }
        actual = {
            "content_fingerprint": draft.content_fingerprint,
            "writer_context_fingerprint": draft.writer_context_fingerprint,
            "chapter_source_fingerprint": draft.chapter_source_fingerprint,
            "anti_ai_bible_fingerprint": context["anti_ai_bible"]["fingerprint"],
            "quality_config_fingerprint": context["quality_config_fingerprint"],
            "quality_context_fingerprint": context["quality_context_fingerprint"],
            "writing_bible_fingerprint": draft.writing_bible_fingerprint,
        }
        if actual != expected:
            reasons.append("QUALITY_SOURCE_CHANGED")
        # Explicit project routes are current semantics. Test-only injected
        # providers deliberately have no project route and remain reproducible.
        resolved = _resolved_quality_config(assessment.quality_config)
        config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == chapter.project_id))
        if resolved["require_critic"] and config and (config.critic_model or config.provider):
            settings = __import__("app.settings", fromlist=["get_settings"]).get_settings()
            route = ModelRouter().resolve(db, chapter.project_id, settings, "CRITIC")
            if assessment.critic_model != route.model or assessment.critic_provider != route.provider:
                reasons.append("QUALITY_CRITIC_ROUTE_CHANGED")
        current = chapter.current_writer_draft_id == draft.id and chapter.content == draft.content and chapter.writer_content_fingerprint == draft.content_fingerprint
        if require_current and not current:
            reasons.append("QUALITY_CURRENT_CONTENT_CHANGED")
        return {"fresh": not reasons, "current": current, "reasons": sorted(set(reasons)), "context": context}


class QualityAssessmentAudit:
    def audit(self, db: Session, assessment_id: str) -> dict[str, Any]:
        assessment = db.get(ChapterQualityAssessment, assessment_id)
        if not assessment:
            raise QualityDomainError("QUALITY_ASSESSMENT_NOT_FOUND")
        chapter = db.get(Chapter, assessment.chapter_id)
        draft = db.get(ChapterWriterDraft, assessment.writer_draft_id)
        if not chapter or not draft or chapter.project_id != assessment.project_id or draft.project_id != assessment.project_id or draft.chapter_id != chapter.id or draft.content_fingerprint != assessment.content_fingerprint or draft.writer_context_fingerprint != assessment.writer_context_fingerprint or draft.chapter_source_fingerprint != assessment.chapter_source_fingerprint or draft.writing_bible_fingerprint != assessment.writing_bible_fingerprint:
            raise QualityDomainError("QUALITY_ASSESSMENT_INTEGRITY_INVALID")
        config = _resolved_quality_config(assessment.quality_config)
        # This also validates new config provenance envelopes. Legacy flat
        # records remain readable as their persisted semantics are explicit.
        _assessment_explicit_overrides(assessment.quality_config)
        if _fp(config, "quality-config-v1") != assessment.quality_config_fingerprint:
            raise QualityDomainError("QUALITY_ASSESSMENT_INTEGRITY_INVALID")
        findings = db.scalars(select(ChapterQualityFinding).where(ChapterQualityFinding.assessment_id == assessment.id).order_by(ChapterQualityFinding.ordinal)).all()
        if [item.ordinal for item in findings] != list(range(1, len(findings) + 1)):
            raise QualityDomainError("QUALITY_FINDING_ORDER_INVALID")
        values: list[dict[str, Any]] = []
        for item in findings:
            value = {"source": _value(item.source), "category": item.category, "severity": _value(item.severity), "rule_code": item.rule_code, "message": item.message, "start_offset": item.start_offset, "end_offset": item.end_offset, "excerpt": item.excerpt, "source_refs": item.source_refs, "metadata": item.finding_metadata}
            if finding_fingerprint(value) != item.finding_fingerprint:
                raise QualityDomainError("QUALITY_FINDING_INTEGRITY_INVALID")
            value["finding_fingerprint"] = item.finding_fingerprint
            values.append(value)
        rules = _assessment_bible_rules(db, assessment)
        deterministic = AntiAIStyleRuleEngine().evaluate(draft.content or "", rules)
        continuity = NovelContinuityQualityChecker().evaluate(db, {"chapter": chapter, "writer_draft": draft, "prose": draft.content or "", "writer_safe_context": {"planning_task": (draft.source_manifest or {}).get("planning_task"), "source_manifest": draft.source_manifest or {}}})
        deterministic["findings"].extend(continuity["findings"])
        deterministic["findings"].sort(key=lambda item: (item["rule_code"], item["finding_fingerprint"]))
        expected_report = {"protocol": deterministic["protocol"], "metrics": deterministic["metrics"], "finding_count": len(deterministic["findings"]), **({"continuity": {key: continuity[key] for key in ("protocol", "enabled", "metrics")}} if continuity["enabled"] else {})}
        deterministic_rows = [item["finding_fingerprint"] for item in values if item["source"] == "DETERMINISTIC"]
        if assessment.deterministic_report != expected_report or sorted(deterministic_rows) != sorted(item["finding_fingerprint"] for item in deterministic["findings"]):
            raise QualityDomainError("QUALITY_DETERMINISTIC_REPORT_INVALID")
        critic = assessment.critic_report or {}
        critic_rows = [item["finding_fingerprint"] for item in values if item["source"] == "CRITIC"]
        if not config["require_critic"]:
            sentinel = {"skipped": True, "reason": "CRITIC_DISABLED"}
            trace_count = db.scalar(select(func.count(ExecutionTrace.id)).where(ExecutionTrace.project_id == assessment.project_id, ExecutionTrace.stage == "CRITIC", ExecutionTrace.source_type == "CHAPTER_QUALITY_ASSESSMENT", ExecutionTrace.source_id == assessment.id)) or 0
            if critic != sentinel or assessment.overall_score is not None or assessment.critic_request_id is not None or assessment.critic_prompt_fingerprint is not None or assessment.critic_provider is not None or assessment.critic_model is not None or critic_rows or trace_count:
                raise QualityDomainError("QUALITY_DETERMINISTIC_ONLY_INTEGRITY_INVALID")
        if _value(assessment.status) in {"PASS", "REPAIR_REQUIRED", "BLOCKED"} and config["require_critic"]:
            try:
                CriticOutputPayload.model_validate(critic)
            except ValidationError as exc:
                raise QualityDomainError("QUALITY_CRITIC_REPORT_INVALID") from exc
            expected_critic = [_finding(source="CRITIC", category=item["category"], severity=item["severity"], rule_code=f"CRITIC_{item['category']}", message=item["message"], start_offset=item["start_offset"], end_offset=item["end_offset"], excerpt=item["excerpt"], source_refs=item["source_refs"]) for item in critic.get("findings", [])]
            if sorted(critic_rows) != sorted(item["finding_fingerprint"] for item in expected_critic) or assessment.overall_score != float(critic["scores"]["overall"]):
                raise QualityDomainError("QUALITY_CRITIC_REPORT_INVALID")
        if _value(assessment.status) in {"PASS", "REPAIR_REQUIRED", "BLOCKED"}:
            expected_status, expected_reasons = QualityDecisionEngine.decide(values, critic.get("decision") if config["require_critic"] else None, config, assessment.overall_score)
            if _value(assessment.status) != _value(expected_status) or list(assessment.decision_reason_codes or []) != expected_reasons:
                raise QualityDomainError("QUALITY_DECISION_INVALID")
        active_count = db.scalar(select(func.count(ChapterQualityAssessment.id)).where(ChapterQualityAssessment.chapter_id == chapter.id, ChapterQualityAssessment.active.is_(True))) or 0
        if assessment.active and active_count != 1:
            raise QualityDomainError("QUALITY_ACTIVE_ASSESSMENT_INVALID")
        if chapter.status == "QUALITY_APPROVED" and chapter.current_quality_assessment_id == assessment.id:
            if _value(assessment.status) != "PASS" or not assessment.active or chapter.quality_content_fingerprint != assessment.content_fingerprint or chapter.content != draft.content:
                raise QualityDomainError("QUALITY_APPROVAL_INTEGRITY_INVALID")
            freshness = QualityAssessmentFreshnessChecker().check(db, assessment, require_current=True)
            if not freshness["fresh"]:
                raise QualityDomainError("QUALITY_APPROVAL_FRESHNESS_INVALID", freshness)
            WriterProjectionAudit().audit(db, chapter.id)
        return {"valid": True, "assessment_id": assessment.id, "finding_count": len(findings)}


class QualityGateService:
    def preview(self, db: Session, chapter_id: str, request: dict[str, Any] | None = None) -> dict[str, Any]:
        context = QualityContextBuilder().build(db, chapter_id, request, critic_provider="preview", critic_model="preview")
        report = AntiAIStyleRuleEngine().evaluate(context["prose"], context["anti_ai_rules"])
        continuity = NovelContinuityQualityChecker().evaluate(db, context)
        return {"chapter_id": chapter_id, "content_fingerprint": context["writer_draft"].content_fingerprint, "anti_ai_bible": {"id": context["anti_ai_bible"]["id"], "version": context["anti_ai_bible"]["version"], "fingerprint": context["anti_ai_bible"]["fingerprint"]}, "deterministic_report": report, "continuity_report": continuity, "quality_config": context["resolved_quality_config"], "quality_context_fingerprint": context["quality_context_fingerprint"]}

    def assess(self, db: Session, chapter_id: str, request: dict[str, Any] | None = None, *, provider=None, model: str | None = None, settings=None, draft: ChapterWriterDraft | None = None, require_current: bool = True) -> ChapterQualityAssessment:
        request = dict(request or {})
        if request.get("idempotency_key") and not request.get("client_request_id"):
            request["client_request_id"] = request["idempotency_key"]
        chapter = db.scalar(select(Chapter).where(Chapter.id == chapter_id).with_for_update())
        if not chapter:
            raise QualityDomainError("CHAPTER_NOT_FOUND")
        project = db.get(Project, chapter.project_id)
        requested_config = QualityGateConfigResolver().resolve(project, request)
        if requested_config.require_critic:
            route_provider, route_model, provider_name = self._provider(db, chapter.project_id, provider, model, settings)
        else:
            route_provider = route_model = provider_name = None
        context = QualityContextBuilder().build(db, chapter_id, request, draft=draft, critic_provider=provider_name, critic_model=route_model, require_current=require_current)
        request_fp = _fp({"quality_context_fingerprint": context["quality_context_fingerprint"], "request": {key: value for key, value in request.items() if key not in {"client_request_id", "idempotency_key"}}}, "quality-request-v1")
        key = request.get("client_request_id")
        if key:
            existing = db.scalar(select(ChapterQualityAssessment).where(ChapterQualityAssessment.chapter_id == chapter.id, ChapterQualityAssessment.client_request_id == key))
            if existing:
                if existing.request_fingerprint != request_fp:
                    raise QualityDomainError("QUALITY_REQUEST_MISMATCH")
                return existing
        existing = db.scalar(select(ChapterQualityAssessment).where(ChapterQualityAssessment.chapter_id == chapter.id, ChapterQualityAssessment.writer_draft_id == context["writer_draft"].id, ChapterQualityAssessment.active.is_(True), ChapterQualityAssessment.quality_context_fingerprint == context["quality_context_fingerprint"]))
        if existing:
            QualityAssessmentAudit().audit(db, existing.id)
            return existing
        version = (db.scalar(select(func.max(ChapterQualityAssessment.version)).where(ChapterQualityAssessment.chapter_id == chapter.id)) or 0) + 1
        anti_ai = context["anti_ai_bible"]
        current_draft = chapter.current_writer_draft_id == context["writer_draft"].id and chapter.content == context["writer_draft"].content
        # A provider call must not invalidate the prior current assessment. A
        # candidate draft is never an active Chapter assessment before adoption.
        assessment = ChapterQualityAssessment(project_id=chapter.project_id, chapter_id=chapter.id, writer_draft_id=context["writer_draft"].id, version=version, status=QualityAssessmentStatus.RUNNING, active=False, client_request_id=key, request_fingerprint=request_fp, content_fingerprint=context["writer_draft"].content_fingerprint, writer_context_fingerprint=context["writer_draft"].writer_context_fingerprint, chapter_source_fingerprint=context["writer_draft"].chapter_source_fingerprint, anti_ai_bible_id=anti_ai["id"], anti_ai_bible_version=anti_ai["version"], anti_ai_bible_fingerprint=anti_ai["fingerprint"], writing_bible_fingerprint=context["writer_draft"].writing_bible_fingerprint, quality_config=context["quality_config"], quality_config_fingerprint=context["quality_config_fingerprint"], quality_context_fingerprint=context["quality_context_fingerprint"], deterministic_report={}, critic_report={}, decision_reason_codes=[], critic_provider=provider_name, critic_model=route_model)
        db.add(assessment); db.flush()
        deterministic = AntiAIStyleRuleEngine().evaluate(context["prose"], context["anti_ai_rules"])
        continuity = NovelContinuityQualityChecker().evaluate(db, context)
        deterministic["findings"].extend(continuity["findings"])
        deterministic["findings"].sort(key=lambda item: (item["rule_code"], item["finding_fingerprint"]))
        assessment.deterministic_report = {"protocol": deterministic["protocol"], "metrics": deterministic["metrics"], "finding_count": len(deterministic["findings"]), **({"continuity": {key: continuity[key] for key in ("protocol", "enabled", "metrics")}} if continuity["enabled"] else {})}
        trace = None
        critic: dict[str, Any] | None = None
        if context["resolved_quality_config"]["require_critic"]:
            prompt = CriticPromptBuilder().build(context)
            assessment.critic_prompt_fingerprint = _fp(prompt, "quality-critic-prompt-v1")
            trace = ExecutionTraceRecorder().start(db, project_id=chapter.project_id, stage="CRITIC", source_type="CHAPTER_QUALITY_ASSESSMENT", source_id=assessment.id, provider=provider_name, model=route_model, input_fingerprint=context["quality_context_fingerprint"])
            try:
                result = route_provider.generate(prompt, route_model)
                critic = CriticOutputValidator().parse(result.content, context["prose"], context["renderable_source_refs"])
                assessment.critic_request_id = result.request_id
                ExecutionTraceRecorder().succeed(trace, latency_ms=result.latency_ms, request_id=result.request_id, output_fingerprint=_fp(critic, "critic-output-v1"))
            except ModelProviderError as exc:
                assessment.status = QualityAssessmentStatus.FAILED; assessment.active = False; assessment.decision_reason_codes = [exc.code]; assessment.completed_at = datetime.utcnow(); ExecutionTraceRecorder().fail(trace, exc.code, upstream_status=exc.upstream_status); db.flush(); return assessment
            except QualityDomainError as exc:
                assessment.status = QualityAssessmentStatus.FAILED; assessment.active = False; assessment.decision_reason_codes = [exc.code]; assessment.completed_at = datetime.utcnow(); ExecutionTraceRecorder().fail(trace, exc.code); db.flush(); return assessment
        else:
            assessment.critic_report = {"skipped": True, "reason": "CRITIC_DISABLED"}
        assessment.critic_report = critic if critic is not None else assessment.critic_report
        findings = list(deterministic["findings"])
        for item in (critic or {}).get("findings", []):
            findings.append(_finding(source="CRITIC", category=item["category"], severity=item["severity"], rule_code=f"CRITIC_{item['category']}", message=item["message"], start_offset=item["start_offset"], end_offset=item["end_offset"], excerpt=item["excerpt"], source_refs=item["source_refs"]))
        findings.sort(key=lambda item: ({"BLOCKING": 0, "MAJOR": 1, "MINOR": 2, "INFO": 3}[item["severity"]], item["source"], item["rule_code"], item["start_offset"] if item["start_offset"] is not None else 10**12, item["finding_fingerprint"]))
        for ordinal, item in enumerate(findings, 1):
            db.add(ChapterQualityFinding(assessment_id=assessment.id, ordinal=ordinal, source=item["source"], category=item["category"], severity=item["severity"], rule_code=item["rule_code"], message=item["message"], start_offset=item["start_offset"], end_offset=item["end_offset"], excerpt=item["excerpt"], source_refs=item["source_refs"], finding_metadata=item["metadata"], finding_fingerprint=item["finding_fingerprint"]))
        assessment.overall_score = float(critic["scores"]["overall"]) if critic is not None else None
        status, reasons = QualityDecisionEngine.decide(findings, critic.get("decision") if critic is not None else None, context["resolved_quality_config"], assessment.overall_score)
        assessment.status = status; assessment.decision_reason_codes = reasons; assessment.completed_at = datetime.utcnow()
        if current_draft:
            prior = db.scalars(select(ChapterQualityAssessment).where(ChapterQualityAssessment.chapter_id == chapter.id, ChapterQualityAssessment.active.is_(True)).with_for_update()).all()
            for item in prior:
                item.active = False
                if _value(item.status) not in {"STALE", "FAILED"}:
                    item.status = QualityAssessmentStatus.SUPERSEDED
            if prior:
                db.flush()
            assessment.active = True
            if chapter.current_quality_assessment_id in {item.id for item in prior}:
                chapter.current_quality_assessment_id = None
                chapter.quality_status = "STALE"
                chapter.quality_content_fingerprint = None
                chapter.quality_approved_at = None
                chapter.quality_report = {}
                chapter.status = "DRAFT"
        db.flush()
        QualityAssessmentAudit().audit(db, assessment.id)
        return assessment

    def approve(self, db: Session, assessment_id: str) -> Chapter:
        assessment = db.scalar(select(ChapterQualityAssessment).where(ChapterQualityAssessment.id == assessment_id).with_for_update())
        if not assessment:
            raise QualityDomainError("QUALITY_ASSESSMENT_NOT_FOUND")
        chapter = db.scalar(select(Chapter).where(Chapter.id == assessment.chapter_id).with_for_update())
        # Audit first: a tampered PASS row must never touch Chapter state.
        QualityAssessmentAudit().audit(db, assessment.id)
        if _value(assessment.status) != "PASS" or not assessment.active:
            raise QualityDomainError("QUALITY_ASSESSMENT_NOT_PASS")
        freshness = QualityAssessmentFreshnessChecker().check(db, assessment, require_current=True)
        if not freshness["fresh"]:
            assessment.status = QualityAssessmentStatus.STALE; assessment.active = False; assessment.stale_at = datetime.utcnow()
            if chapter.current_quality_assessment_id == assessment.id:
                chapter.current_quality_assessment_id = None; chapter.quality_status = "STALE"; chapter.quality_approved_at = None
            db.flush(); raise QualityDomainError("QUALITY_SOURCE_CHANGED", freshness)
        counts = Counter(_value(item.severity) for item in db.scalars(select(ChapterQualityFinding).where(ChapterQualityFinding.assessment_id == assessment.id)).all())
        chapter.current_quality_assessment_id = assessment.id; chapter.quality_status = "PASS"; chapter.quality_content_fingerprint = assessment.content_fingerprint; chapter.quality_approved_at = datetime.utcnow(); chapter.status = "QUALITY_APPROVED"; assessment.approved_at = chapter.quality_approved_at
        chapter.quality_report = {"assessment_id": assessment.id, "status": "PASS", "overall_score": assessment.overall_score, "finding_counts": dict(sorted(counts.items())), "approved_at": chapter.quality_approved_at.isoformat()}
        db.flush(); QualityAssessmentAudit().audit(db, assessment.id); return chapter

    @staticmethod
    def _provider(db: Session, project_id: str, provider, model, settings):
        if provider is not None:
            return provider, model or "critic-test-model", getattr(provider, "name", "test")
        settings = settings or __import__("app.settings", fromlist=["get_settings"]).get_settings()
        route = ModelRouter().resolve(db, project_id, settings, "CRITIC")
        key = ProviderCredentialResolver().generation_key(db, project_id, settings)
        return get_model_provider(settings, route.provider, route.base_url, key), model or route.model, route.provider


class QualityRepairPromptBuilder:
    def build(self, context: dict[str, Any], findings: list[dict[str, Any]]) -> list[dict[str, str]]:
        safe = {key: value for key, value in context.items() if key not in {"anti_ai_bible", "writer_draft", "chapter"}}
        return [{"role": "system", "content": "You are a REPAIR prose editor. Correct only the listed prose defects. WRITER_SAFE_CONTEXT is absolute authority. Do not add events, facts, knowledge, secrets, outcomes, or source references. Preserve the chapter planning task and return exact task coverage fields."}, {"role": "user", "content": _canonical({"quality_context": safe, "findings": findings, "output_contract": {"chapter_title": "string|null", "prose": "non-empty string", "scene_coverage": "ordered scene ids", "source_refs": "safe refs only", "pov_character_id": "string|null", "task_coverage": "completed mandatory task labels", "task_forbidden_hits": "forbidden task labels present"}})}]


class QualityRepairService:
    failure_injector = None

    def __init__(self, failure_injector=None):
        self._instance_failure_injector = failure_injector

    def _inject(self, stage: str) -> None:
        injector = self._instance_failure_injector or type(self).failure_injector
        if injector:
            injector(stage)

    @staticmethod
    def _root_draft(db: Session, draft: ChapterWriterDraft) -> ChapterWriterDraft:
        current = draft
        seen: set[str] = set()
        while current.parent_draft_id:
            if current.id in seen:
                raise QualityDomainError("QUALITY_REPAIR_LINEAGE_INVALID")
            seen.add(current.id)
            parent = db.get(ChapterWriterDraft, current.parent_draft_id)
            if not parent or parent.chapter_id != draft.chapter_id or parent.project_id != draft.project_id:
                raise QualityDomainError("QUALITY_REPAIR_LINEAGE_INVALID")
            current = parent
        return current

    @classmethod
    def _chain_attempt_count(cls, db: Session, source: ChapterWriterDraft) -> int:
        root = cls._root_draft(db, source)
        candidates = db.scalars(select(ChapterWriterDraft).where(ChapterWriterDraft.chapter_id == source.chapter_id, ChapterWriterDraft.origin == WriterDraftOrigin.QUALITY_REPAIR)).all()
        return sum(1 for item in candidates if cls._root_draft(db, item).id == root.id)

    @staticmethod
    def _source_is_repairable(db: Session, chapter: Chapter, assessment: ChapterQualityAssessment, source: ChapterWriterDraft) -> bool:
        if source.id == chapter.current_writer_draft_id:
            return _value(source.status) == "ADOPTED" and chapter.content == source.content
        # Auto Director reviews a validated candidate before author adoption.
        # It is safe to repair this lineage while the chapter projection is
        # still empty; adoption remains an explicit later action.
        if source.id == assessment.writer_draft_id and _value(source.origin) == "WRITER" and _value(source.status) == "VALIDATED":
            return chapter.current_writer_draft_id is None and chapter.content is None
        if _value(source.origin) != "QUALITY_REPAIR" or _value(source.status) != "VALIDATED" or not source.parent_draft_id or not source.source_quality_assessment_id:
            return False
        parent = db.get(ChapterWriterDraft, source.parent_draft_id)
        parent_assessment = db.get(ChapterQualityAssessment, source.source_quality_assessment_id)
        return bool(parent and parent_assessment and parent.id == parent_assessment.writer_draft_id and parent.chapter_id == chapter.id)

    @staticmethod
    def _draft_attempt(chapter: Chapter, source: ChapterWriterDraft, assessment: ChapterQualityAssessment,
                       stored_key: str | None, request_fp: str, prompt_fingerprint: str,
                       provider: str | None, model: str | None) -> ChapterWriterDraft:
        return ChapterWriterDraft(
            project_id=chapter.project_id, chapter_id=chapter.id,
            version=0, status=WriterDraftStatus.GENERATING,
            origin=WriterDraftOrigin.QUALITY_REPAIR,
            source_quality_assessment_id=assessment.id,
            client_request_id=stored_key, request_fingerprint=request_fp,
            chapter_structure_fingerprint=source.chapter_structure_fingerprint,
            chapter_source_fingerprint=source.chapter_source_fingerprint,
            writer_context_fingerprint=source.writer_context_fingerprint,
            source_structure_status=source.source_structure_status,
            source_scene_ids=source.source_scene_ids, source_manifest=source.source_manifest,
            writing_bible_id=source.writing_bible_id, writing_bible_version=source.writing_bible_version,
            writing_bible_fingerprint=source.writing_bible_fingerprint,
            pov_mode=source.pov_mode, pov_character_id=source.pov_character_id,
            provider=provider, model=model, prompt_fingerprint=prompt_fingerprint,
            parent_draft_id=source.id,
        )

    def repair(self, db: Session, assessment_id: str, request: dict[str, Any] | None = None, *, repair_provider=None, repair_model: str | None = None, critic_provider=None, critic_model: str | None = None, settings=None) -> tuple[ChapterWriterDraft, ChapterQualityAssessment | None]:
        request = dict(request or {})
        assessment = db.scalar(select(ChapterQualityAssessment).where(ChapterQualityAssessment.id == assessment_id).with_for_update())
        if not assessment:
            raise QualityDomainError("QUALITY_REPAIR_NOT_ALLOWED")
        chapter = db.scalar(select(Chapter).where(Chapter.id == assessment.chapter_id).with_for_update())
        source = db.get(ChapterWriterDraft, assessment.writer_draft_id)
        if not chapter or not source or not self._source_is_repairable(db, chapter, assessment, source):
            raise QualityDomainError("QUALITY_REPAIR_LINEAGE_INVALID")
        QualityAssessmentAudit().audit(db, assessment.id)
        freshness = QualityAssessmentFreshnessChecker().check(db, assessment, require_current=False)
        if not freshness["fresh"]:
            raise QualityDomainError("QUALITY_SOURCE_CHANGED", freshness)
        context = QualityContextBuilder().build(db, chapter.id, {"config": _assessment_explicit_overrides(assessment.quality_config)}, draft=source, critic_provider=assessment.critic_provider, critic_model=assessment.critic_model, require_current=False)
        if context["quality_context_fingerprint"] != assessment.quality_context_fingerprint:
            raise QualityDomainError("QUALITY_SOURCE_CHANGED")
        key = request.get("client_request_id") or request.get("idempotency_key")
        stored_key = f"quality-repair:{key}" if key else None
        request_fp = _fp({"assessment_id": assessment.id, "quality_context": assessment.quality_context_fingerprint, "request": {key: value for key, value in request.items() if key not in {"client_request_id", "idempotency_key"}}}, "quality-repair-request-v1")
        if stored_key:
            existing = db.scalar(select(ChapterWriterDraft).where(ChapterWriterDraft.chapter_id == chapter.id, ChapterWriterDraft.client_request_id == stored_key))
            if existing:
                if existing.source_quality_assessment_id != assessment.id or existing.request_fingerprint != request_fp:
                    raise QualityDomainError("QUALITY_REQUEST_MISMATCH")
                child = db.scalar(select(ChapterQualityAssessment).where(ChapterQualityAssessment.writer_draft_id == existing.id).order_by(ChapterQualityAssessment.version.desc()))
                return existing, child
        project_config = db.scalar(select(ProjectModelConfig).where(ProjectModelConfig.project_id == chapter.project_id))
        limit = min(_resolved_quality_config(assessment.quality_config)["max_repair_attempts"], project_config.max_repair_attempts if project_config else 1)
        if self._chain_attempt_count(db, source) >= limit:
            raise QualityDomainError("QUALITY_REPAIR_LIMIT_REACHED")
        if _value(assessment.status) not in {"REPAIR_REQUIRED", "BLOCKED"}:
            raise QualityDomainError("QUALITY_REPAIR_NOT_ALLOWED")
        provider, model, provider_name = self._provider(db, chapter.project_id, repair_provider, repair_model, settings)
        finding_rows = db.scalars(select(ChapterQualityFinding).where(ChapterQualityFinding.assessment_id == assessment.id).order_by(ChapterQualityFinding.ordinal)).all()
        findings = [{"category": item.category, "severity": _value(item.severity), "rule_code": item.rule_code, "message": item.message, "start_offset": item.start_offset, "end_offset": item.end_offset, "excerpt": item.excerpt, "source_refs": item.source_refs} for item in finding_rows]
        prompt = QualityRepairPromptBuilder().build(context, findings)
        version = (db.scalar(select(func.max(ChapterWriterDraft.version)).where(ChapterWriterDraft.chapter_id == chapter.id)) or 0) + 1
        draft = self._draft_attempt(chapter, source, assessment, stored_key, request_fp, _fp(prompt, "quality-repair-prompt-v1"), provider_name, model)
        draft.version = version
        db.add(draft); db.flush()
        trace = ExecutionTraceRecorder().start(db, project_id=chapter.project_id, stage="REPAIR", source_type="CHAPTER_QUALITY_ASSESSMENT", source_id=assessment.id, provider=provider_name, model=model, input_fingerprint=context["quality_context_fingerprint"])
        try:
            result = provider.generate(prompt, model)
            try:
                parsed = WriterOutputPayload.model_validate_json(result.content).model_dump(mode="json")
            except ValidationError as exc:
                raise QualityDomainError(MODEL_OUTPUT_INVALID) from exc
            report = WriterGroundingValidator().validate(parsed, context["writer_safe_context"])
            task_issues = validate_task_output(parsed, context["writer_safe_context"].get("planning_task"))
            if task_issues:
                report["issues"].extend(task_issues)
                report["valid"] = False
            report["task_coverage"] = parsed.get("task_coverage", [])
            report["task_forbidden_hits"] = parsed.get("task_forbidden_hits", [])
            draft.provider = result.provider; draft.model = result.model; draft.model_request_id = result.request_id
            draft.title_candidate = parsed["chapter_title"]; draft.content = parsed["prose"]
            draft.content_fingerprint = _fp(parsed["prose"], "writer-content-v1")
            draft.word_count = WriterWordCounter().count(parsed["prose"])
            draft.scene_coverage = parsed["scene_coverage"]; draft.source_refs = parsed["source_refs"]
            draft.validation_report = report; draft.completed_at = datetime.utcnow()
            if not report["valid"]:
                draft.status = WriterDraftStatus.REJECTED
                ExecutionTraceRecorder().block(trace, report["issues"][0]["code"], validation_report=report, request_id=result.request_id)
                db.flush(); return draft, None
            draft.status = WriterDraftStatus.VALIDATED
            ExecutionTraceRecorder().succeed(trace, latency_ms=result.latency_ms, request_id=result.request_id, output_fingerprint=draft.content_fingerprint)
            db.flush()
            child_request = {"client_request_id": f"repair-assessment:{draft.id}", "config": _assessment_explicit_overrides(assessment.quality_config)}
            child = QualityGateService().assess(db, chapter.id, child_request, provider=critic_provider, model=critic_model, settings=settings, draft=draft, require_current=False)
            return draft, child
        except ModelProviderError as exc:
            draft.status = WriterDraftStatus.FAILED; draft.validation_report = {"valid": False, "issues": [{"code": exc.code, "blocking": True}]}; draft.completed_at = datetime.utcnow()
            ExecutionTraceRecorder().fail(trace, exc.code, upstream_status=exc.upstream_status); db.flush(); return draft, None
        except QualityDomainError as exc:
            draft.status = WriterDraftStatus.FAILED; draft.validation_report = {"valid": False, "issues": [{"code": exc.code, "blocking": True}]}; draft.completed_at = datetime.utcnow()
            ExecutionTraceRecorder().fail(trace, exc.code); db.flush(); return draft, None

    def adopt(self, db: Session, draft_id: str) -> Chapter:
        draft = db.scalar(select(ChapterWriterDraft).where(ChapterWriterDraft.id == draft_id).with_for_update())
        if not draft or _value(draft.origin) != "QUALITY_REPAIR":
            raise QualityDomainError("QUALITY_REPAIR_DRAFT_NOT_VALIDATED")
        chapter = db.scalar(select(Chapter).where(Chapter.id == draft.chapter_id).with_for_update())
        if _value(draft.status) == "ADOPTED" and chapter.current_writer_draft_id == draft.id:
            return chapter
        if _value(draft.status) != "VALIDATED":
            raise QualityDomainError("QUALITY_REPAIR_DRAFT_NOT_VALIDATED")
        assessment = db.scalar(select(ChapterQualityAssessment).where(ChapterQualityAssessment.writer_draft_id == draft.id, ChapterQualityAssessment.status == QualityAssessmentStatus.PASS).order_by(ChapterQualityAssessment.version.desc()))
        if not assessment or not self._source_is_repairable(db, chapter, assessment, draft):
            raise QualityDomainError("QUALITY_REPAIR_ASSESSMENT_NOT_PASS")
        QualityAssessmentAudit().audit(db, assessment.id)
        freshness = QualityAssessmentFreshnessChecker().check(db, assessment, require_current=False)
        if not freshness["fresh"]:
            raise QualityDomainError("QUALITY_SOURCE_CHANGED", freshness)
        prior = db.get(ChapterWriterDraft, chapter.current_writer_draft_id)
        # Deliberately inject while only the presentation row is dirty. The
        # surrounding caller transaction must restore every lifecycle row.
        chapter.content = draft.content; chapter.word_count = draft.word_count; chapter.writer_content_fingerprint = draft.content_fingerprint; chapter.writer_context_fingerprint = draft.writer_context_fingerprint; chapter.current_writer_draft_id = draft.id; chapter.written_at = datetime.utcnow()
        self._inject("AFTER_CHAPTER_CONTENT_BEFORE_QUALITY_FINALIZATION")
        if prior and prior.id != draft.id:
            prior.status = WriterDraftStatus.SUPERSEDED
        old_assessments = db.scalars(select(ChapterQualityAssessment).where(ChapterQualityAssessment.chapter_id == chapter.id, ChapterQualityAssessment.active.is_(True)).with_for_update()).all()
        for item in old_assessments:
            if item.id != assessment.id:
                item.active = False
                if _value(item.status) not in {"STALE", "FAILED"}:
                    item.status = QualityAssessmentStatus.SUPERSEDED
        if old_assessments:
            db.flush()
        assessment.active = True
        draft.status = WriterDraftStatus.ADOPTED; draft.adopted_at = datetime.utcnow()
        chapter.current_quality_assessment_id = assessment.id; chapter.quality_status = "PASS"; chapter.quality_content_fingerprint = assessment.content_fingerprint; chapter.quality_approved_at = datetime.utcnow(); chapter.status = "QUALITY_APPROVED"; assessment.approved_at = chapter.quality_approved_at
        counts = Counter(_value(item.severity) for item in db.scalars(select(ChapterQualityFinding).where(ChapterQualityFinding.assessment_id == assessment.id)).all())
        chapter.quality_report = {"assessment_id": assessment.id, "status": "PASS", "overall_score": assessment.overall_score, "finding_counts": dict(sorted(counts.items())), "approved_at": chapter.quality_approved_at.isoformat()}
        db.flush(); WriterProjectionAudit().audit(db, chapter.id); QualityAssessmentAudit().audit(db, assessment.id); return chapter

    @staticmethod
    def _provider(db: Session, project_id: str, provider, model, settings):
        if provider is not None:
            return provider, model or "repair-test-model", getattr(provider, "name", "test")
        settings = settings or __import__("app.settings", fromlist=["get_settings"]).get_settings()
        route = ModelRouter().resolve(db, project_id, settings, "REPAIR")
        key = ProviderCredentialResolver().generation_key(db, project_id, settings)
        return get_model_provider(settings, route.provider, route.base_url, key), model or route.model, route.provider


def assessment_payload(db: Session, assessment: ChapterQualityAssessment, *, include_findings: bool = False) -> dict[str, Any]:
    deterministic = assessment.deterministic_report or {}
    value = {"id": assessment.id, "project_id": assessment.project_id, "chapter_id": assessment.chapter_id, "writer_draft_id": assessment.writer_draft_id, "version": assessment.version, "status": _value(assessment.status), "active": assessment.active, "client_request_id": assessment.client_request_id, "content_fingerprint": assessment.content_fingerprint, "writer_context_fingerprint": assessment.writer_context_fingerprint, "chapter_source_fingerprint": assessment.chapter_source_fingerprint, "anti_ai_bible_id": assessment.anti_ai_bible_id, "anti_ai_bible_version": assessment.anti_ai_bible_version, "anti_ai_bible_fingerprint": assessment.anti_ai_bible_fingerprint, "writing_bible_fingerprint": assessment.writing_bible_fingerprint, "quality_config": assessment.quality_config, "resolved_quality_config": _resolved_quality_config(assessment.quality_config), "quality_config_fingerprint": assessment.quality_config_fingerprint, "quality_context_fingerprint": assessment.quality_context_fingerprint, "deterministic_report": deterministic, "continuity_report": deterministic.get("continuity", {"enabled": False}), "critic_report": assessment.critic_report, "overall_score": assessment.overall_score, "decision_reason_codes": assessment.decision_reason_codes, "critic_provider": assessment.critic_provider, "critic_model": assessment.critic_model, "critic_request_id": assessment.critic_request_id, "critic_prompt_fingerprint": assessment.critic_prompt_fingerprint, "created_at": assessment.created_at.isoformat() if assessment.created_at else None, "completed_at": assessment.completed_at.isoformat() if assessment.completed_at else None, "stale_at": assessment.stale_at.isoformat() if assessment.stale_at else None, "approved_at": assessment.approved_at.isoformat() if assessment.approved_at else None}
    if include_findings:
        rows = db.scalars(select(ChapterQualityFinding).where(ChapterQualityFinding.assessment_id == assessment.id).order_by(ChapterQualityFinding.ordinal)).all()
        value["findings"] = [{"id": item.id, "ordinal": item.ordinal, "source": _value(item.source), "category": item.category, "severity": _value(item.severity), "rule_code": item.rule_code, "message": item.message, "start_offset": item.start_offset, "end_offset": item.end_offset, "excerpt": item.excerpt, "source_refs": item.source_refs, "metadata": item.finding_metadata, "finding_fingerprint": item.finding_fingerprint} for item in rows]
    return value

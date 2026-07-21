from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Iterable, Mapping, Sequence

from careerops.ai.context import (
    MAX_CONTEXT_TRAVERSAL_NODES,
    MAX_EVIDENCE_ITEMS,
    MAX_LEXEMES_PER_TEXT,
    MAX_PROFILE_FACTS,
    MAX_REJECTED_CLAIMS,
    MAX_SOURCE_CHARACTERS,
    PRIVACY_LEVELS,
    VERIFICATION_STATES,
    ContextPacket,
    validate_context_packet,
    validate_verification_material,
)
from careerops.ai.contracts import (
    AnalyzeFitOutput,
    AnalyzeJdOutput,
    ClaimReviewOutput,
    CoverLetterOutput,
    ProviderOutputError,
    ResumeEditsOutput,
    TaskOutput,
    validate_task_output,
)
from careerops.gates.risk import METRIC_PATTERN, evaluate_risks


BLOCKING_CODES = frozenset(
    {
        "schema_invalid",
        "unsupported_metric",
        "unsupported_claim",
        "private_claim",
        "secret_like_text",
        "nda_sensitive_wording",
        "unsafe_title",
        "source_span_not_unique",
        "prompt_injection_echo",
    }
)

_CODE_ORDER = {
    code: index
    for index, code in enumerate(
        (
            "schema_invalid",
            "source_span_not_unique",
            "private_claim",
            "secret_like_text",
            "nda_sensitive_wording",
            "unsafe_title",
            "unsupported_metric",
            "unsupported_claim",
            "prompt_injection_echo",
        )
    )
}
_RECOMMENDATIONS = {
    "schema_invalid": "Discard the output and rerun the task with the matching schema.",
    "unsupported_metric": "Remove the metric or link verified public evidence that states it.",
    "unsupported_claim": "Remove the claim or link verified public evidence that supports it.",
    "private_claim": "Remove private or non-publishable material.",
    "secret_like_text": "Remove the credential-shaped text and rotate it if it is real.",
    "nda_sensitive_wording": "Rewrite the text using public-safe wording.",
    "unsafe_title": "Use only the verified public job title.",
    "source_span_not_unique": "Select source text that occurs exactly once in the resume.",
    "prompt_injection_echo": "Remove source-provided instructions from the output.",
}
_RISK_CODES = {
    "Missing evidence": "unsupported_metric",
    "Claim not verified": "unsupported_claim",
    "Private claim": "private_claim",
    "Secret-like text": "secret_like_text",
    "NDA-sensitive wording": "nda_sensitive_wording",
    "Unsupported title": "unsafe_title",
}
_SENTENCE_PATTERN = re.compile(
    r"[^.!?\r\n\u0085\u2028\u2029]+"
    r"(?:[.!?]+|[\r\n\u0085\u2028\u2029]+|$)"
)
_INJECTION_PATTERN = re.compile(
    r"(?i)(?:\b(?:ignore|disregard|override|forget)\b.{0,100}"
    r"\b(?:instruction|prompt|rule|message)s?\b|"
    r"\b(?:system|developer)\s+(?:prompt|message)\b|"
    r"\bapprove\s+me\b|\breport\s+(?:a\s+)?perfect\s+match\b|"
    r"\b(?:output|return|respond\s+with)\b.{0,80}\binstead\b)"
)
_SOURCE_DIRECTIVE_PATTERN = re.compile(
    r"(?i)\b(?:do\s+not|don't|never)\s+"
    r"(?:claim|include|list|output|report|return|say|state)\b"
)
_INSTRUCTION_PATTERNS = (_INJECTION_PATTERN, _SOURCE_DIRECTIVE_PATTERN)
_NEGATION_PATTERN = re.compile(
    r"(?i)\b(?:cannot|can't|did\s+not|didn't|do\s+not|don't|does\s+not|"
    r"doesn't|lacks?|lacking|never|no|not|without)\b"
)
_OPTIONAL_MODALITY_PATTERN = re.compile(
    r"(?i)\b(?:may|might|nice\s+to\s+have|optional|preferred)\b"
)
_NON_FACTUAL_SENTENCE_PATTERNS = (
    re.compile(r"(?i)^dear [a-z][a-z .'-]*,?$"),
    re.compile(r"(?i)^(?:sincerely|regards|best regards),?$"),
    re.compile(r"(?i)^thank you(?: for considering my application)?$"),
    re.compile(r"(?i)^i(?: am|'m) interested in (?:this|the) role$"),
    re.compile(r"(?i)^i(?: am|'m) excited to apply(?: for (?:this|the) role)?$"),
    re.compile(r"(?i)^please consider my application$"),
)
_COVER_ROLE_REFERENCE_PATTERN = re.compile(
    r"(?i)^i(?: am|'m) (?:interested in|excited to apply for) "
    r"(?:this|the) (?P<role>[a-z0-9+#.-]+(?: [a-z0-9+#.-]+){0,7}) role$"
)
_GENERIC_COVER_ROLE_DESCRIPTORS = frozenset({"engineering", "software engineering"})
_SUPPORTED_REASON_PATTERNS = (
    re.compile(r"(?i)^direct (?:evidence|match)$"),
    re.compile(r"(?i)^matches? (?:the )?(?:supplied )?(?:evidence|record)$"),
)
_UNSUPPORTED_REASON_PATTERNS = (
    re.compile(r"(?i)^(?:no|missing|insufficient) (?:supporting )?(?:evidence|proof)$"),
    re.compile(
        r"(?i)^(?:supporting )?(?:evidence|proof) (?:is )?"
        r"(?:required|missing|insufficient)$"
    ),
    re.compile(
        r"(?i)^(?:the )?evidence (?:does not|doesn't|cannot|can't) "
        r"support (?:this|the) claim$"
    ),
    re.compile(
        r"(?i)^(?:the )?claim (?:is )?"
        r"(?:not supported|unsupported|unverified|rejected)"
        r"(?: by (?:the )?(?:supplied )?evidence)?$"
    ),
)
_NEEDS_REVIEW_REASON_PATTERNS = (
    re.compile(r"(?i)^(?:requires?|needs?) (?:human )?review$"),
    *_UNSUPPORTED_REASON_PATTERNS,
)
_PRIVATE_REASON_PATTERNS = (
    re.compile(r"(?i)^private(?: material)?$"),
    re.compile(r"(?i)^(?:the )?claim (?:is )?private$"),
)
_DECISION_REASON_PATTERNS = {
    "supported": _SUPPORTED_REASON_PATTERNS,
    "unsupported": _UNSUPPORTED_REASON_PATTERNS,
    "needs_review": _NEEDS_REVIEW_REASON_PATTERNS,
    "private": _PRIVATE_REASON_PATTERNS,
}
_ALL_GENERIC_REASON_PATTERNS = tuple(
    pattern
    for patterns in _DECISION_REASON_PATTERNS.values()
    for pattern in patterns
)
_UNTRUSTED_CONTEXT_FIELDS = (
    "job_description",
    "selected_resume",
    "confirmed_profile_facts",
    "linked_evidence",
    "rejected_claims",
    "writing_preferences",
)
_MAX_TEXT_SEGMENTS = 256
_DEFAULT_IGNORABLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
_LINE_SEPARATORS = "\r\n\x85\u2028\u2029"
_LINE_SEPARATORS_TO_SPACE = str.maketrans(
    {ord(character): " " for character in _LINE_SEPARATORS}
)
_LINE_SEPARATORS_REMOVED = str.maketrans(
    {ord(character): None for character in _LINE_SEPARATORS}
)
_METRIC_LEXEME_EQUIVALENTS = {
    "served": "serve",
    "serving": "serve",
}
_ANALYSIS_WRAPPER_LEXEMES = frozenset(
    {
        "a",
        "an",
        "and",
        "candidate",
        "demonstrate",
        "evidence",
        "experience",
        "for",
        "highlight",
        "in",
        "is",
        "job",
        "match",
        "matches",
        "matching",
        "of",
        "position",
        "preferred",
        "relevant",
        "requirement",
        "requirements",
        "required",
        "role",
        "show",
        "skill",
        "skills",
        "the",
        "to",
        "with",
    }
)
_GENERIC_UNCERTAINTY_PATTERN = re.compile(
    r"(?i)^(?:company|compensation|employment type|experience level|location|"
    r"seniority|title|work arrangement)(?: is)? "
    r"(?:not specified|not stated|unclear|unknown|unspecified)$"
)
_RESUME_GENERIC_REASON_PATTERNS = (
    re.compile(r"(?i)^exact wording$"),
    re.compile(r"(?i)^aligns? (?:the )?wording with (?:the )?evidence$"),
    re.compile(r"(?i)^matches? (?:the )?(?:supplied )?evidence$"),
    re.compile(r"(?i)^improves? (?:clarity|readability)$"),
)
_ATS_PATTERN = re.compile(r"(?i)\bats\b")
_METRIC_WRAPPER_LEXEMES = frozenset(
    {
        "a",
        "an",
        "by",
        "candidate",
        "consider",
        "demonstrate",
        "evidence",
        "experience",
        "highlight",
        "in",
        "of",
        "recommend",
        "relevant",
        "show",
        "the",
        "with",
        "your",
    }
)
_LOW_SIGNAL_LEXEMES = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "be",
        "built",
        "by",
        "claim",
        "for",
        "from",
        "i",
        "in",
        "is",
        "it",
        "led",
        "my",
        "of",
        "on",
        "or",
        "project",
        "role",
        "team",
        "the",
        "this",
        "to",
        "was",
        "we",
        "were",
        "with",
    }
)
_RESTRICTED_LEXEME_EQUIVALENTS = {
    "led": "lead",
    "leading": "lead",
    "migrated": "migrate",
    "migrating": "migrate",
    "migration": "migrate",
    "migrations": "migrate",
    "payments": "payment",
    "served": "serve",
    "serving": "serve",
}
_GENERIC_ENTITY_LEXEMES = frozenset(
    {
        "ai",
        "api",
        "aws",
        "azure",
        "backend",
        "cloud",
        "data",
        "developer",
        "development",
        "engineer",
        "engineering",
        "enterprise",
        "frontend",
        "india",
        "java",
        "javascript",
        "migration",
        "project",
        "python",
        "react",
        "software",
        "sql",
        "team",
        "typescript",
    }
)


@dataclass(frozen=True)
class VerifierFinding:
    code: str
    verifier_state: str = "blocked"
    item_index: int | None = None
    claim_id: str | None = None
    evidence_item_id: str | None = None
    recommendation: str = ""


@dataclass(frozen=True)
class VerificationResult:
    task_type: str
    output: TaskOutput | None
    findings: tuple[VerifierFinding, ...]

    @property
    def safe(self) -> bool:
        return not self.findings and self.output is not None


@dataclass(frozen=True)
class _Evidence:
    id: str
    summary: str
    verification_state: str
    privacy_level: str

    @property
    def publishable(self) -> bool:
        return (
            self.verification_state == "verified"
            and self.privacy_level == "public"
        )

    @property
    def private(self) -> bool:
        return (
            self.privacy_level != "public"
            or self.verification_state == "private_do_not_publish"
        )


@dataclass(frozen=True)
class _Claim:
    id: str
    text: str
    verification_state: str
    privacy_level: str


MaterialCache = dict[tuple[str, str], Any]


def _finding(
    code: str,
    *,
    item_index: int | None = None,
    claim_id: str | None = None,
    evidence_item_id: str | None = None,
) -> VerifierFinding:
    return VerifierFinding(
        code=code,
        item_index=item_index,
        claim_id=claim_id,
        evidence_item_id=evidence_item_id,
        recommendation=_RECOMMENDATIONS[code],
    )


def _normalized_findings(
    findings: Iterable[VerifierFinding],
) -> tuple[VerifierFinding, ...]:
    unique: dict[tuple[Any, ...], VerifierFinding] = {}
    for finding in findings:
        key = (
            finding.item_index,
            finding.claim_id,
            finding.evidence_item_id,
            finding.code,
        )
        unique[key] = finding
    return tuple(
        sorted(
            unique.values(),
            key=lambda finding: (
                finding.item_index if finding.item_index is not None else -1,
                finding.claim_id or "",
                _CODE_ORDER[finding.code],
                finding.evidence_item_id or "",
            ),
        )
    )


def _result(
    task_type: str,
    output: TaskOutput | None,
    findings: Iterable[VerifierFinding],
) -> VerificationResult:
    return VerificationResult(
        task_type=task_type,
        output=output,
        findings=_normalized_findings(findings),
    )


def _validate(task_type: str, payload: Any) -> TaskOutput | None:
    try:
        return validate_task_output(task_type, payload)
    except ProviderOutputError:
        return None


def _context_data(
    context: ContextPacket | None,
    task_type: str,
) -> dict[str, Any] | None:
    if context is None:
        return {}
    return validate_context_packet(context, task_type)


def _context_verification_data(
    context: ContextPacket | None,
    task_type: str,
) -> dict[str, Any] | None:
    if context is None:
        return {}
    return validate_verification_material(context, task_type)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _safe_text(value: Any) -> str | None:
    if type(value) is not str or len(value) > MAX_SOURCE_CHARACTERS:
        return None
    try:
        value.encode("utf-8")
    except UnicodeError:
        return None
    return value


def _bounded_values(values: Iterable[Any], limit: int) -> tuple[Any, ...] | None:
    if isinstance(values, (str, bytes, bytearray)):
        return None
    items: list[Any] = []
    try:
        iterator = iter(values)
        for _index in range(limit + 1):
            try:
                value = next(iterator)
            except StopIteration:
                break
            items.append(value)
    except Exception:
        return None
    return tuple(items) if len(items) <= limit else None


def _evidence_records(values: Iterable[Any]) -> tuple[_Evidence, ...] | None:
    bounded = _bounded_values(values, MAX_EVIDENCE_ITEMS)
    if bounded is None:
        return None
    records: list[_Evidence] = []
    seen: set[str] = set()
    try:
        for value in bounded:
            item = _mapping(value)
            if item is None:
                return None
            evidence_id = _safe_text(item.get("id"))
            summary = _safe_text(item.get("summary", item.get("claim_text")))
            state = _safe_text(item.get("verification_state"))
            privacy = _safe_text(item.get("privacy_level"))
            if (
                not evidence_id
                or summary is None
                or state is None
                or privacy is None
                or evidence_id in seen
                or state not in VERIFICATION_STATES
                or privacy not in PRIVACY_LEVELS
            ):
                return None
            seen.add(evidence_id)
            records.append(_Evidence(evidence_id, summary, state, privacy))
    except Exception:
        return None
    return tuple(sorted(records, key=lambda item: item.id))


def _claim_records(
    values: Iterable[Any],
    *,
    fact_shape: bool = False,
) -> tuple[_Claim, ...] | None:
    bounded = _bounded_values(values, MAX_PROFILE_FACTS)
    if bounded is None:
        return None
    records: list[_Claim] = []
    seen: set[str] = set()
    try:
        for value in bounded:
            item = _mapping(value)
            if item is None:
                return None
            claim_id = _safe_text(item.get("id"))
            text_fields = [
                field
                for field in ("text", "claim_text")
                if field in item
            ]
            has_value = "value" in item
            if len(text_fields) > 1 or (text_fields and has_value):
                return None
            if has_value:
                if not fact_shape and _safe_text(item.get("key")) is None:
                    return None
                text = _safe_text(item.get("value"))
            elif text_fields:
                if fact_shape:
                    return None
                text = _safe_text(item.get(text_fields[0]))
            else:
                return None
            state = _safe_text(item.get("verification_state", "needs_verification"))
            privacy = _safe_text(item.get("privacy_level", "public"))
            if (
                not claim_id
                or text is None
                or not _has_visible_text(text)
                or state not in VERIFICATION_STATES
                or privacy not in PRIVACY_LEVELS
            ):
                return None
            if claim_id in seen:
                return None
            seen.add(claim_id)
            records.append(_Claim(claim_id, text, state, privacy))
    except Exception:
        return None
    return tuple(sorted(records, key=lambda item: item.id))


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        character
        for character in normalized
        if not _is_default_ignorable(character)
    )


def _has_visible_text(text: str) -> bool:
    return any(
        unicodedata.category(character)[0] not in {"C", "M", "Z"}
        for character in unicodedata.normalize("NFKC", text)
    )


def _security_text_variants(text: str) -> tuple[str, ...]:
    normalized = _normalize_text(text)
    return tuple(
        dict.fromkeys(
            (
                normalized,
                normalized.translate(_LINE_SEPARATORS_TO_SPACE),
                normalized.translate(_LINE_SEPARATORS_REMOVED),
            )
        )
    )


def _injection_text_variants(text: str) -> tuple[str, ...]:
    variants: list[str] = []
    for value in _security_text_variants(text):
        normalized = _normalized_text(value)
        variants.extend(
            (
                normalized,
                _normalized_text(normalized.replace("_", " ").replace("-", " ")),
            )
        )
    return tuple(dict.fromkeys(variants))


def _is_default_ignorable(character: str) -> bool:
    if unicodedata.category(character) == "Cf":
        return True
    codepoint = ord(character)
    return any(start <= codepoint <= end for start, end in _DEFAULT_IGNORABLE_RANGES)


def _lexemes(text: str) -> tuple[str, ...] | None:
    result: list[str] = []
    current: list[str] = []
    for character in _normalize_text(text):
        category = unicodedata.category(character)
        if category[0] in {"L", "N"} or category[0] == "M":
            current.append(character)
        elif character in {"+", "#"} and current:
            current.append(character)
        elif current:
            result.append("".join(current))
            if len(result) > MAX_LEXEMES_PER_TEXT:
                return None
            current = []
    if current:
        result.append("".join(current))
    if len(result) > MAX_LEXEMES_PER_TEXT:
        return None
    return tuple(result)


def _normalized_text(text: str) -> str:
    return " ".join(_normalize_text(text).split())


def _relationship_matches(statement: str, evidence: str) -> bool:
    statement_tokens = _lexemes(statement)
    evidence_tokens = _lexemes(evidence)
    if statement_tokens is None or evidence_tokens is None:
        return False
    if not statement_tokens or not evidence_tokens:
        return False
    if statement_tokens == evidence_tokens:
        return True
    return statement_tokens[0] == "i" and statement_tokens[1:] == evidence_tokens


def _new_content_supported(
    source_text: str,
    replacement_text: str,
    evidence: Sequence[_Evidence],
) -> bool:
    replacement = _lexemes(replacement_text)
    source = _lexemes(source_text)
    if replacement is None or source is None:
        return False
    if replacement and _normalized_text(replacement_text) == _normalized_text(
        source_text
    ):
        return True
    return bool(replacement) and any(
        _relationship_matches(replacement_text, segment)
        for item in evidence
        for segment in (_sentences(item.summary) or ())
    )


def _statement_supported(statement: str, evidence: Sequence[_Evidence]) -> bool:
    required = _lexemes(statement)
    if required is None or not required:
        return False
    return any(
        _relationship_matches(statement, segment)
        for item in evidence
        for segment in (_sentences(item.summary) or ())
    )


def _sentences(text: str) -> tuple[str, ...] | None:
    sentences: list[str] = []
    for match in _SENTENCE_PATTERN.finditer(text):
        sentence = match.group(0).strip()
        if sentence:
            sentences.append(sentence)
            if len(sentences) > _MAX_TEXT_SEGMENTS:
                return None
    return tuple(sentences) or (text,)


def _metric_supported(text: str, evidence: Sequence[_Evidence]) -> bool:
    metrics = {
        match.casefold() for match in METRIC_PATTERN.findall(_normalize_text(text))
    }
    if not metrics:
        return True
    evidence_metrics: set[str] = set()
    for item in evidence:
        evidence_metrics.update(
            match.casefold()
            for match in METRIC_PATTERN.findall(_normalize_text(item.summary))
        )
    return metrics <= evidence_metrics


def _metric_supported_by_sources(text: str, sources: Sequence[str]) -> bool:
    if not METRIC_PATTERN.search(_normalize_text(text)):
        return True
    statements = _sentences(text)
    if statements is None:
        return False
    source_statements: list[str] = []
    for source in sources:
        segments = _sentences(source)
        if segments is None:
            return False
        source_statements.extend(segments)
    return all(
        not METRIC_PATTERN.search(_normalize_text(statement))
        or any(
            _metric_statement_matches(statement, source_statement)
            for source_statement in source_statements
        )
        for statement in statements
    )


def _analysis_lexemes(text: str) -> tuple[str, ...] | None:
    lexemes = _lexemes(text)
    if lexemes is None:
        return None
    return tuple(
        _canonical_metric_lexeme(token)
        for token in lexemes
        if token not in _ANALYSIS_WRAPPER_LEXEMES
    )


def _ordered_subsequence(needle: Sequence[str], haystack: Sequence[str]) -> bool:
    if not needle:
        return False
    index = 0
    for token in haystack:
        if token == needle[index]:
            index += 1
            if index == len(needle):
                return True
    return False


def _contains_injection(text: str) -> bool:
    return any(
        pattern.search(variant) is not None
        for variant in _injection_text_variants(text)
        for pattern in _INSTRUCTION_PATTERNS
    )


def _trusted_source_segments(
    sources: Sequence[str],
    *,
    exclude_optional: bool = False,
) -> tuple[str, ...] | None:
    trusted: list[str] = []
    for source in sources:
        sentences = _sentences(source)
        if sentences is None:
            return None
        rejected_any = False
        for sentence in sentences:
            normalized = _normalized_text(sentence)
            if _contains_injection(sentence):
                rejected_any = True
            elif not _NEGATION_PATTERN.search(normalized) and not (
                exclude_optional and _OPTIONAL_MODALITY_PATTERN.search(normalized)
            ):
                trusted.append(sentence)
        if _contains_injection(source) and not rejected_any:
            return ()
    return tuple(trusted)


def _trusted_source_lexemes(
    sources: Sequence[str],
    *,
    exclude_optional: bool = False,
) -> tuple[tuple[str, ...], ...] | None:
    segments = _trusted_source_segments(sources, exclude_optional=exclude_optional)
    if segments is None:
        return None
    result: list[tuple[str, ...]] = []
    for segment in segments:
        lexemes = _analysis_lexemes(segment)
        if lexemes is None:
            return None
        result.append(lexemes)
    return tuple(result)


def _analysis_statement_supported(
    statement: str,
    source_lexemes: Sequence[Sequence[str]],
    *,
    allow_generic_uncertainty: bool = False,
) -> bool | None:
    normalized = _normalized_text(statement).strip(" .!?")
    if allow_generic_uncertainty and _GENERIC_UNCERTAINTY_PATTERN.fullmatch(
        normalized
    ):
        return True
    required = _analysis_lexemes(statement)
    if required is None:
        return None
    if not required:
        return False
    for available in source_lexemes:
        if _ordered_subsequence(required, available):
            return True
    return False


def _resume_reason_supported(
    reason: str,
    sources: Sequence[str],
) -> tuple[bool | None, bool]:
    normalized = _normalized_text(reason).strip(" .!?")
    metric_supported = _metric_supported_by_sources(reason, sources)
    if _ATS_PATTERN.search(normalized):
        return False, metric_supported
    if any(pattern.fullmatch(normalized) for pattern in _RESUME_GENERIC_REASON_PATTERNS):
        return True, metric_supported
    grounding = _trusted_source_lexemes(sources)
    if grounding is None:
        return None, metric_supported
    return _analysis_statement_supported(reason, grounding), metric_supported


def _metric_statement_matches(statement: str, source: str) -> bool:
    if _relationship_matches(statement, source):
        return True
    statement_metrics = tuple(
        match.casefold()
        for match in METRIC_PATTERN.findall(_normalize_text(statement))
    )
    source_metrics = tuple(
        match.casefold()
        for match in METRIC_PATTERN.findall(_normalize_text(source))
    )
    if not statement_metrics or statement_metrics != source_metrics:
        return False
    return _metric_relation_lexemes(statement) == _metric_relation_lexemes(source)


def _metric_relation_lexemes(text: str) -> tuple[str, ...]:
    lexemes = _lexemes(text) or ()
    return tuple(
        _canonical_metric_lexeme(token)
        for token in lexemes
        if token not in _METRIC_WRAPPER_LEXEMES
    )


def _canonical_metric_lexeme(token: str) -> str:
    return _METRIC_LEXEME_EQUIVALENTS.get(token, token)


def _resolve_evidence(
    refs: Sequence[str],
    evidence: Sequence[_Evidence],
    *,
    item_index: int | None = None,
    claim_id: str | None = None,
) -> tuple[tuple[_Evidence, ...], list[VerifierFinding]]:
    by_id = {item.id: item for item in evidence}
    resolved: list[_Evidence] = []
    findings: list[VerifierFinding] = []
    seen: set[str] = set()
    for evidence_id in refs:
        if evidence_id in seen:
            findings.append(
                _finding(
                    "unsupported_claim",
                    item_index=item_index,
                    claim_id=claim_id,
                    evidence_item_id=evidence_id,
                )
            )
            continue
        seen.add(evidence_id)
        item = by_id.get(evidence_id)
        if item is None:
            findings.append(
                _finding(
                    "unsupported_claim",
                    item_index=item_index,
                    claim_id=claim_id,
                    evidence_item_id=evidence_id,
                )
            )
        elif item.private:
            findings.append(
                _finding(
                    "private_claim",
                    item_index=item_index,
                    claim_id=claim_id,
                    evidence_item_id=evidence_id,
                )
            )
        elif not item.publishable:
            findings.append(
                _finding(
                    "unsupported_claim",
                    item_index=item_index,
                    claim_id=claim_id,
                    evidence_item_id=evidence_id,
                )
            )
        else:
            resolved.append(item)
    return tuple(resolved), findings


def _risk_findings(
    text: str,
    *,
    metric_supported: bool,
    private_material: bool,
    item_index: int | None = None,
    claim_id: str | None = None,
) -> list[VerifierFinding]:
    findings: list[VerifierFinding] = []
    for normalized_text in _security_text_variants(text):
        report = evaluate_risks(
            resume_text=normalized_text,
            claims=[
                {
                    "text": normalized_text,
                    "verification_state": (
                        "verified" if metric_supported else "needs_verification"
                    ),
                    "public_safe": not private_material,
                }
            ],
        )
        for issue in report.blockers:
            code = _RISK_CODES.get(issue.label)
            if code is not None:
                findings.append(
                    _finding(code, item_index=item_index, claim_id=claim_id)
                )
    return findings


def _output_text(output: TaskOutput) -> str | None:
    values: list[str] = []
    stack: list[Any] = [output.model_dump(mode="python")]
    observed = 0
    while stack:
        observed += 1
        if observed > MAX_CONTEXT_TRAVERSAL_NODES:
            return None
        value = stack.pop()
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Mapping):
            stack.extend(value.values())
        elif isinstance(value, (tuple, list)):
            stack.extend(value)
    return "\n".join(values)


def _injection_fragments(source_values: Sequence[Any]) -> tuple[str, ...] | None:
    source_texts: list[str] = []
    stack = list(source_values)
    observed = 0
    while stack:
        observed += 1
        if observed > MAX_CONTEXT_TRAVERSAL_NODES:
            return None
        value = stack.pop()
        if type(value) is str:
            source_texts.append(value)
        elif isinstance(value, Mapping):
            stack.extend(value.values())
        elif isinstance(value, (tuple, list)):
            stack.extend(value)

    fragments: set[str] = set()
    for source in source_texts:
        for normalized_variant in _injection_text_variants(source):
            for pattern in _INSTRUCTION_PATTERNS:
                fragments.update(
                    _normalized_text(match.group(0))
                    for match in pattern.finditer(normalized_variant)
                )
        sentences = _sentences(source)
        if sentences is None:
            return None
        for candidate in sentences:
            normalized = _normalized_text(candidate.strip(" \t\r\n.!?"))
            if normalized and any(
                pattern.search(normalized) for pattern in _INSTRUCTION_PATTERNS
            ):
                fragments.add(normalized)
                for pattern in _INSTRUCTION_PATTERNS:
                    fragments.update(
                        _normalized_text(match.group(0))
                        for match in pattern.finditer(normalized)
                    )
    return tuple(sorted(fragments))


def _has_injection_echo(
    output: TaskOutput,
    source_values: Sequence[Any],
) -> bool | None:
    output_text = _output_text(output)
    if output_text is None:
        return None
    generated = _injection_text_variants(output_text)
    fragments = _injection_fragments(source_values)
    if fragments is None:
        return None
    return any(fragment in variant for fragment in fragments for variant in generated)


def _context_source_values(context: Mapping[str, Any]) -> tuple[Any, ...]:
    values: list[Any] = []
    for field in _UNTRUSTED_CONTEXT_FIELDS:
        value = context.get(field)
        if field == "writing_preferences" and isinstance(value, Mapping):
            values.extend(value.keys())
            values.extend(value.values())
        else:
            values.append(value)
    return tuple(values)


def _context_auxiliary(
    context: Mapping[str, Any],
    verification: Mapping[str, Any],
) -> tuple[Sequence[Any], Sequence[Any], tuple[str, ...]]:
    evidence_values = verification.get("linked_evidence", ())
    rejected_values = list(context.get("rejected_claims", ()))
    private_values: list[str] = []
    facts = verification.get("confirmed_profile_facts", ())
    if isinstance(facts, Sequence) and not isinstance(facts, (str, bytes, bytearray)):
        for value in facts:
            item = _mapping(value)
            if item is None:
                continue
            fact_value = item.get("value")
            if isinstance(fact_value, str):
                if (
                    item.get("privacy_level") != "public"
                    or item.get("verification_state") == "private_do_not_publish"
                ):
                    private_values.append(fact_value)
                elif item.get("verification_state") not in {"verified", "user_stated"}:
                    rejected_values.append({"text": fact_value})
    return evidence_values, rejected_values, tuple(private_values)


def _rejected_texts(values: Iterable[Any]) -> tuple[str, ...] | None:
    bounded = _bounded_values(values, MAX_REJECTED_CLAIMS + MAX_PROFILE_FACTS)
    if bounded is None:
        return None
    result: list[str] = []
    try:
        for value in bounded:
            item = _mapping(value)
            if item is None:
                return None
            text = _safe_text(item.get("text", item.get("claim_text")))
            if text is None:
                return None
            result.append(text)
    except Exception:
        return None
    return tuple(sorted(result, key=lambda value: value.casefold()))


def _private_texts(values: Iterable[Any]) -> tuple[str, ...] | None:
    bounded = _bounded_values(values, MAX_PROFILE_FACTS)
    if bounded is None:
        return None
    result: list[str] = []
    try:
        for value in bounded:
            text = _safe_text(value)
            if text is None:
                return None
            result.append(text)
    except Exception:
        return None
    return tuple(result)


def _blocked_material(
    text: str,
    evidence: Sequence[_Evidence],
    rejected: Sequence[str],
    private_claims: Sequence[str],
    lexeme_cache: MaterialCache | None = None,
) -> tuple[bool, bool]:
    rejected_echo = any(
        _material_overlap(text, claim, lexeme_cache) for claim in rejected
    )
    private_echo = any(
        _material_overlap(text, claim, lexeme_cache) for claim in private_claims
    )
    private_echo = private_echo or any(
        item.private and _material_overlap(text, item.summary, lexeme_cache)
        for item in evidence
    )
    return rejected_echo, private_echo


def _material_lexemes(
    text: str,
    cache: MaterialCache | None = None,
) -> tuple[str, ...] | None:
    cache_key = ("lexemes", text)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    lexemes = _lexemes(text)
    if lexemes is None:
        if cache is not None:
            cache[cache_key] = None
        return None
    high_signal = tuple(token for token in lexemes if token not in _LOW_SIGNAL_LEXEMES)
    result = high_signal or lexemes
    result = tuple(_RESTRICTED_LEXEME_EQUIVALENTS.get(token, token) for token in result)
    if cache is not None:
        cache[cache_key] = result
    return result


def _surface_lexemes(text: str) -> tuple[str, ...] | None:
    result: list[str] = []
    current: list[str] = []
    normalized = unicodedata.normalize("NFKC", text)
    for character in normalized:
        if _is_default_ignorable(character):
            continue
        category = unicodedata.category(character)
        if category[0] in {"L", "M", "N"}:
            current.append(character)
        elif current:
            result.append("".join(current))
            current = []
            if len(result) > MAX_LEXEMES_PER_TEXT:
                return None
    if current:
        result.append("".join(current))
    return tuple(result) if len(result) <= MAX_LEXEMES_PER_TEXT else None


def _distinctive_material_sequences(
    text: str,
    cache: MaterialCache | None = None,
) -> tuple[tuple[str, ...], ...] | None:
    cache_key = ("entities", text)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    surface = _surface_lexemes(text)
    if surface is None:
        if cache is not None:
            cache[cache_key] = None
        return None
    sequences: list[tuple[str, ...]] = []
    run: list[str] = []
    for index, token in enumerate(surface):
        folded = token.casefold()
        has_upper = any(character.isupper() for character in token)
        has_lower = any(character.islower() for character in token)
        mixed_case = has_upper and has_lower and not token.istitle()
        all_caps = has_upper and not has_lower and len(token) >= 2
        title_case = token.istitle() and folded not in _GENERIC_ENTITY_LEXEMES
        if title_case:
            run.append(_RESTRICTED_LEXEME_EQUIVALENTS.get(folded, folded))
        else:
            if len(run) >= 2:
                sequences.append(tuple(run))
            elif len(run) == 1 and index > 1:
                sequences.append(tuple(run))
            run = []
        if (mixed_case or all_caps) and folded not in _GENERIC_ENTITY_LEXEMES:
            sequences.append(
                (_RESTRICTED_LEXEME_EQUIVALENTS.get(folded, folded),)
            )
    if len(run) >= 2:
        sequences.append(tuple(run))
    elif len(run) == 1 and len(surface) > 1:
        sequences.append(tuple(run))
    result = tuple(dict.fromkeys(sequences))
    if cache is not None:
        cache[cache_key] = result
    return result


def _material_metrics(text: str, cache: MaterialCache | None = None) -> frozenset[str]:
    cache_key = ("metrics", text)
    if cache is not None and cache_key in cache:
        return cache[cache_key]
    result = frozenset(METRIC_PATTERN.findall(_normalize_text(text)))
    if cache is not None:
        cache[cache_key] = result
    return result


def _contains_token_sequence(
    haystack: Sequence[str],
    needle: Sequence[str],
) -> bool:
    size = len(needle)
    return bool(size) and any(
        tuple(haystack[index : index + size]) == tuple(needle)
        for index in range(len(haystack) - size + 1)
    )


def _material_overlap(
    text: str,
    blocked: str,
    cache: MaterialCache | None = None,
) -> bool:
    text_tokens = _material_lexemes(text, cache)
    blocked_tokens = _material_lexemes(blocked, cache)
    if text_tokens is None or blocked_tokens is None:
        return True
    if not text_tokens or not blocked_tokens:
        return False
    text_counts = Counter(text_tokens)
    blocked_counts = Counter(blocked_tokens)
    overlap = sum((text_counts & blocked_counts).values())
    blocked_size = sum(blocked_counts.values())
    if overlap == blocked_size:
        return True
    if blocked_size >= 2 and overlap >= 2 and overlap * 4 >= blocked_size * 3:
        return True
    text_metrics = _material_metrics(text, cache)
    blocked_metrics = _material_metrics(blocked, cache)
    if text_metrics & blocked_metrics and overlap >= 2:
        return True
    entities = _distinctive_material_sequences(blocked, cache)
    if entities is None:
        return True
    return any(_contains_token_sequence(text_tokens, entity) for entity in entities)


def _verify_analysis(
    task_type: str,
    payload: Any,
    *,
    context: ContextPacket | None,
) -> VerificationResult:
    output = _validate(task_type, payload)
    if context is None or output is None:
        return _result(task_type, None, [_finding("schema_invalid")])
    context_data = _context_data(context, task_type)
    verification_data = _context_verification_data(context, task_type)
    if context_data is None or verification_data is None:
        return _result(task_type, None, [_finding("schema_invalid")])

    output_text = _output_text(output)
    job = _mapping(context_data.get("job_description"))
    if output_text is None or job is None or type(job.get("text")) is not str:
        return _result(task_type, None, [_finding("schema_invalid")])
    job_sources = [job["text"]]
    evidence_values, rejected_values, context_private = _context_auxiliary(
        context_data, verification_data
    )
    records = _evidence_records(evidence_values)
    rejected = _rejected_texts(rejected_values)
    if records is None or rejected is None:
        return _result(task_type, None, [_finding("schema_invalid")])

    candidate_sources: list[str] = []
    if isinstance(output, AnalyzeFitOutput):
        selected_resume = _mapping(context_data.get("selected_resume"))
        facts = context_data.get("confirmed_profile_facts", ())
        if (
            selected_resume is None
            or type(selected_resume.get("source_text")) is not str
            or not isinstance(facts, Sequence)
            or isinstance(facts, (str, bytes, bytearray))
        ):
            return _result(task_type, None, [_finding("schema_invalid")])
        candidate_sources.append(selected_resume["source_text"])
        for value in facts:
            item = _mapping(value)
            if item is None or type(item.get("value")) is not str:
                return _result(task_type, None, [_finding("schema_invalid")])
            candidate_sources.append(item["value"])
        candidate_sources.extend(
            item.summary for item in records if item.publishable
        )

    job_grounding = _trusted_source_lexemes(job_sources)
    strict_job_grounding = _trusted_source_lexemes(
        job_sources, exclude_optional=True
    )
    candidate_grounding = _trusted_source_lexemes(candidate_sources)
    if (
        job_grounding is None
        or strict_job_grounding is None
        or candidate_grounding is None
    ):
        return _result(task_type, None, [_finding("schema_invalid")])
    all_grounding = job_grounding + candidate_grounding

    findings: list[VerifierFinding] = []
    source_injections = _injection_fragments(_context_source_values(context_data))
    if source_injections is None:
        return _result(task_type, None, [_finding("schema_invalid")])
    if source_injections:
        findings.append(_finding("unsupported_claim"))
    injection_echo = _has_injection_echo(
        output, _context_source_values(context_data)
    )
    if injection_echo is None:
        return _result(task_type, None, [_finding("schema_invalid")])
    if injection_echo:
        findings.append(_finding("prompt_injection_echo"))
    material_cache: MaterialCache = {}
    rejected_echo, private_echo = _blocked_material(
        output_text, records, rejected, context_private, material_cache
    )
    if rejected_echo:
        findings.append(_finding("unsupported_claim"))
    if private_echo:
        findings.append(_finding("private_claim"))

    support_checks: list[bool | None] = []
    metric_checks: list[bool] = []
    if isinstance(output, AnalyzeJdOutput):
        normalized_fields = (
            output.normalized_title,
            output.normalized_company,
            output.normalized_location,
        )
        support_checks.extend(
            _analysis_statement_supported(value, job_grounding)
            if _normalized_text(value) not in {"not specified", "unknown", "unspecified"}
            else True
            for value in normalized_fields
        )
        strict_extracted = output.responsibilities + output.required_skills
        descriptive_extracted = output.preferred_skills + output.keywords
        extracted = strict_extracted + descriptive_extracted
        support_checks.extend(
            _analysis_statement_supported(value, strict_job_grounding)
            for value in strict_extracted
        )
        support_checks.extend(
            _analysis_statement_supported(value, job_grounding)
            for value in descriptive_extracted
        )
        support_checks.extend(
            _analysis_statement_supported(
                value, job_grounding, allow_generic_uncertainty=True
            )
            for value in output.uncertainties
        )
        metric_checks.extend(
            _metric_supported_by_sources(value, job_sources)
            for value in extracted + output.uncertainties
        )
    elif isinstance(output, AnalyzeFitOutput):
        all_sources = job_sources + candidate_sources
        for value in output.matches:
            job_support = _analysis_statement_supported(value, job_grounding)
            candidate_support = _analysis_statement_supported(value, candidate_grounding)
            support_checks.append(
                None
                if job_support is None or candidate_support is None
                else job_support and candidate_support
            )
            metric_checks.append(
                _metric_supported_by_sources(value, job_sources)
                and _metric_supported_by_sources(value, candidate_sources)
            )
        for value in output.gaps:
            job_support = _analysis_statement_supported(value, job_grounding)
            candidate_support = _analysis_statement_supported(value, candidate_grounding)
            support_checks.append(
                None
                if job_support is None or candidate_support is None
                else job_support and not candidate_support
            )
            metric_checks.append(_metric_supported_by_sources(value, job_sources))
        support_checks.extend(
            _analysis_statement_supported(
                value, all_grounding, allow_generic_uncertainty=True
            )
            for value in output.risks
        )
        support_checks.extend(
            _analysis_statement_supported(value, all_grounding)
            for value in output.recommendations
        )
        metric_checks.extend(
            _metric_supported_by_sources(value, all_sources) for value in output.risks
        )
        metric_checks.extend(
            _metric_supported_by_sources(value, candidate_sources)
            for value in output.recommendations
        )
    else:
        return _result(task_type, None, [_finding("schema_invalid")])

    if any(value is None for value in support_checks):
        return _result(task_type, None, [_finding("schema_invalid")])
    if not all(support_checks):
        findings.append(_finding("unsupported_claim"))
    metric_supported = all(metric_checks)
    findings.extend(
        _risk_findings(
            output_text,
            metric_supported=metric_supported,
            private_material=private_echo,
        )
    )
    return _result(task_type, output, findings)


def verify_analyze_jd(
    payload: Any,
    *,
    context: ContextPacket | None = None,
) -> VerificationResult:
    return _verify_analysis("analyze_jd", payload, context=context)


def verify_analyze_fit(
    payload: Any,
    *,
    context: ContextPacket | None = None,
) -> VerificationResult:
    return _verify_analysis("analyze_fit", payload, context=context)


def verify_resume_edits(
    payload: Any,
    *,
    resume_tex: str | None = None,
    evidence: Iterable[Any] | None = None,
    rejected_claims: Iterable[Any] | None = None,
    private_claims: Iterable[str] | None = None,
    context: ContextPacket | None = None,
) -> VerificationResult:
    task_type = "suggest_resume_edits"
    output = _validate(task_type, payload)
    if not isinstance(output, ResumeEditsOutput):
        return _result(task_type, None, [_finding("schema_invalid")])

    if context is not None:
        if any(
            value is not None
            for value in (resume_tex, evidence, rejected_claims, private_claims)
        ):
            return _result(task_type, None, [_finding("schema_invalid")])
        context_data = _context_data(context, task_type)
        verification_data = _context_verification_data(context, task_type)
        if context_data is None or verification_data is None:
            return _result(task_type, None, [_finding("schema_invalid")])
        evidence_values, rejected_values, context_private = _context_auxiliary(
            context_data, verification_data
        )
        selected_resume = _mapping(context_data.get("selected_resume"))
        resume_tex = (
            selected_resume.get("source_text")
            if selected_resume is not None
            else None
        )
        injection_sources = _context_source_values(context_data)
        job = _mapping(context_data.get("job_description"))
        if job is None or type(job.get("text")) is not str:
            return _result(task_type, None, [_finding("schema_invalid")])
        reason_context_sources = (job["text"],)
    else:
        context_data = {}
        evidence_values = evidence if evidence is not None else ()
        rejected_values = _bounded_values(
            rejected_claims if rejected_claims is not None else (),
            MAX_REJECTED_CLAIMS,
        )
        if rejected_values is None:
            return _result(task_type, None, [_finding("schema_invalid")])
        context_private = ()
        injection_sources = ()
        reason_context_sources = ()

    records = _evidence_records(evidence_values)
    rejected = _rejected_texts(rejected_values)
    normalized_private = _private_texts(
        private_claims if private_claims is not None else ()
    )
    if records is None or rejected is None or normalized_private is None:
        return _result(task_type, None, [_finding("schema_invalid")])
    if _safe_text(resume_tex) is None:
        return _result(task_type, None, [_finding("schema_invalid")])
    if context is None:
        injection_sources = (
            resume_tex,
            tuple(item.summary for item in records),
            rejected_values,
            normalized_private,
        )

    findings: list[VerifierFinding] = []
    material_cache: MaterialCache = {}
    ambiguous_spans: set[int] = set()
    intervals: list[tuple[int, int, int]] = []
    for index, suggestion in enumerate(output.suggestions):
        interval = _unique_source_interval(resume_tex, suggestion.source_text)
        if interval is None:
            ambiguous_spans.add(index)
        else:
            intervals.append((index, interval[0], interval[1]))
    for position, (left_index, left_start, left_end) in enumerate(intervals):
        for right_index, right_start, right_end in intervals[position + 1 :]:
            if left_start < right_end and right_start < left_end:
                ambiguous_spans.update((left_index, right_index))
    findings.extend(
        _finding("source_span_not_unique", item_index=index)
        for index in sorted(ambiguous_spans)
    )

    private_materials = normalized_private + context_private
    for index, suggestion in enumerate(output.suggestions):
        resolved, reference_findings = _resolve_evidence(
            suggestion.evidence_refs, records, item_index=index
        )
        findings.extend(reference_findings)

        claim_supported = _new_content_supported(
            suggestion.source_text, suggestion.replacement_text, resolved
        )
        metric_supported = _metric_supported(suggestion.replacement_text, resolved)
        rejected_echo, private_echo = _blocked_material(
            suggestion.replacement_text,
            records,
            rejected,
            private_materials,
            material_cache,
        )
        if not claim_supported or rejected_echo:
            findings.append(_finding("unsupported_claim", item_index=index))
        if private_echo:
            findings.append(_finding("private_claim", item_index=index))
        findings.extend(
            _risk_findings(
                suggestion.replacement_text,
                metric_supported=metric_supported,
                private_material=private_echo,
                item_index=index,
            )
        )
        reason_rejected, reason_private = _blocked_material(
            suggestion.reason,
            records,
            rejected,
            private_materials,
            material_cache,
        )
        if reason_rejected:
            findings.append(_finding("unsupported_claim", item_index=index))
        if reason_private:
            findings.append(_finding("private_claim", item_index=index))
        reason_supported, reason_metric_supported = _resume_reason_supported(
            suggestion.reason,
            reason_context_sources
            + (
                suggestion.source_text,
                suggestion.replacement_text,
                *(item.summary for item in resolved),
            ),
        )
        if reason_supported is None:
            return _result(task_type, None, [_finding("schema_invalid")])
        if not reason_supported:
            findings.append(_finding("unsupported_claim", item_index=index))
        findings.extend(
            _risk_findings(
                suggestion.reason,
                metric_supported=reason_metric_supported,
                private_material=reason_private,
                item_index=index,
            )
        )

    injection_echo = _has_injection_echo(output, injection_sources)
    if injection_echo is None:
        return _result(task_type, None, [_finding("schema_invalid")])
    if injection_echo:
        findings.append(_finding("prompt_injection_echo"))
    return _result(task_type, output, findings)


def _overlapping_occurrences(text: str, source: str) -> int:
    count = 0
    start = 0
    while True:
        position = text.find(source, start)
        if position < 0:
            return count
        count += 1
        if count > 1:
            return count
        start = position + 1


def _unique_source_interval(text: str, source: str) -> tuple[int, int] | None:
    first = text.find(source)
    if first < 0 or text.find(source, first + 1) >= 0:
        return None
    return first, first + len(source)


def _factual_sentences(text: str) -> tuple[str, ...] | None:
    sentences = _sentences(text)
    if sentences is None:
        return None
    factual: list[str] = []
    for sentence in sentences:
        normalized = " ".join(sentence.rstrip(".!?").split())
        if not sentence or any(
            pattern.fullmatch(normalized)
            for pattern in _NON_FACTUAL_SENTENCE_PATTERNS
        ):
            continue
        factual.append(sentence)
    return tuple(factual)


def _cover_role_reference_support(
    sentence: str,
    job_grounding: Sequence[Sequence[str]],
) -> tuple[bool, bool | None]:
    normalized = _normalized_text(sentence.rstrip(".!?"))
    match = _COVER_ROLE_REFERENCE_PATTERN.fullmatch(normalized)
    if match is None:
        return False, False
    descriptor = match.group("role")
    if descriptor in _GENERIC_COVER_ROLE_DESCRIPTORS:
        return True, True
    return True, _analysis_statement_supported(descriptor, job_grounding)


def _unsupported_reason_statement(
    text: str,
    evidence: Sequence[_Evidence],
    decision: str,
) -> bool | None:
    sentences = _sentences(text)
    if sentences is None:
        return None
    allowed_patterns = _DECISION_REASON_PATTERNS.get(decision)
    if allowed_patterns is None:
        return True
    for sentence in sentences:
        normalized = _normalized_text(sentence.rstrip(".!?"))
        if any(pattern.fullmatch(normalized) for pattern in allowed_patterns):
            continue
        if any(
            pattern.fullmatch(normalized)
            for pattern in _ALL_GENERIC_REASON_PATTERNS
        ):
            return True
        if not _statement_supported(sentence, evidence):
            return True
    return False


def verify_cover_letter(
    payload: Any,
    *,
    evidence: Iterable[Any] | None = None,
    rejected_claims: Iterable[Any] | None = None,
    private_claims: Iterable[str] | None = None,
    context: ContextPacket | None = None,
) -> VerificationResult:
    task_type = "draft_cover_letter"
    output = _validate(task_type, payload)
    if not isinstance(output, CoverLetterOutput):
        return _result(task_type, None, [_finding("schema_invalid")])

    if context is not None:
        if any(
            value is not None
            for value in (evidence, rejected_claims, private_claims)
        ):
            return _result(task_type, None, [_finding("schema_invalid")])
        context_data = _context_data(context, task_type)
        verification_data = _context_verification_data(context, task_type)
        if context_data is None or verification_data is None:
            return _result(task_type, None, [_finding("schema_invalid")])
        evidence_values, rejected_values, context_private = _context_auxiliary(
            context_data, verification_data
        )
        injection_sources = _context_source_values(context_data)
        job = _mapping(context_data.get("job_description"))
        if job is None or type(job.get("text")) is not str:
            return _result(task_type, None, [_finding("schema_invalid")])
        job_grounding = _trusted_source_lexemes((job["text"],))
        if job_grounding is None:
            return _result(task_type, None, [_finding("schema_invalid")])
    else:
        context_data = {}
        evidence_values = evidence if evidence is not None else ()
        rejected_values = _bounded_values(
            rejected_claims if rejected_claims is not None else (),
            MAX_REJECTED_CLAIMS,
        )
        if rejected_values is None:
            return _result(task_type, None, [_finding("schema_invalid")])
        context_private = ()
        injection_sources = ()
        job_grounding = ()

    records = _evidence_records(evidence_values)
    rejected = _rejected_texts(rejected_values)
    normalized_private = _private_texts(
        private_claims if private_claims is not None else ()
    )
    if records is None or rejected is None or normalized_private is None:
        return _result(task_type, None, [_finding("schema_invalid")])
    if context is None:
        injection_sources = (
            tuple(item.summary for item in records),
            rejected_values,
            normalized_private,
        )

    resolved, findings = _resolve_evidence(output.evidence_refs, records)
    factual_sentences = _factual_sentences(output.plain_text)
    if factual_sentences is None:
        return _result(task_type, None, [_finding("schema_invalid")])
    for sentence in factual_sentences:
        is_role_reference, role_support = _cover_role_reference_support(
            sentence, job_grounding
        )
        if is_role_reference and role_support is None:
            return _result(task_type, None, [_finding("schema_invalid")])
        if (is_role_reference and not role_support) or (
            not is_role_reference and not _statement_supported(sentence, resolved)
        ):
            findings.append(_finding("unsupported_claim"))
    metric_supported = all(
        _metric_supported(sentence, resolved) for sentence in factual_sentences
    )
    material_cache: MaterialCache = {}
    rejected_echo, private_echo = _blocked_material(
        output.plain_text,
        records,
        rejected,
        normalized_private + context_private,
        material_cache,
    )
    if rejected_echo:
        findings.append(_finding("unsupported_claim"))
    if private_echo:
        findings.append(_finding("private_claim"))
    findings.extend(
        _risk_findings(
            output.plain_text,
            metric_supported=metric_supported,
            private_material=private_echo,
        )
    )
    injection_echo = _has_injection_echo(output, injection_sources)
    if injection_echo is None:
        return _result(task_type, None, [_finding("schema_invalid")])
    if injection_echo:
        findings.append(_finding("prompt_injection_echo"))
    return _result(task_type, output, findings)


def verify_claim_review(
    payload: Any,
    *,
    claims: Iterable[Any] | None = None,
    evidence: Iterable[Any] | None = None,
    context: ContextPacket | None = None,
) -> VerificationResult:
    task_type = "review_claims"
    output = _validate(task_type, payload)
    if not isinstance(output, ClaimReviewOutput):
        return _result(task_type, None, [_finding("schema_invalid")])

    if context is not None:
        if claims is not None or evidence is not None:
            return _result(task_type, None, [_finding("schema_invalid")])
        context_data = _context_data(context, task_type)
        verification_data = _context_verification_data(context, task_type)
        if context_data is None or verification_data is None:
            return _result(task_type, None, [_finding("schema_invalid")])
        claim_records = _claim_records(
            context_data.get("confirmed_profile_facts", ()), fact_shape=True
        )
        evidence_values, rejected_values, context_private = _context_auxiliary(
            context_data, verification_data
        )
        injection_sources = _context_source_values(context_data)
    else:
        context_data = {}
        if claims is None:
            return _result(task_type, None, [_finding("schema_invalid")])
        claim_records = _claim_records(claims)
        evidence_values = evidence if evidence is not None else ()
        rejected_values = ()
        context_private = ()
        injection_sources = ()

    records = _evidence_records(evidence_values)
    rejected_context = _rejected_texts(rejected_values)
    if claim_records is None or records is None or rejected_context is None:
        return _result(task_type, None, [_finding("schema_invalid")])
    if not claim_records or not output.decisions:
        return _result(task_type, None, [_finding("schema_invalid")])

    claims_by_id = {claim.id: claim for claim in claim_records}
    decision_ids = tuple(decision.claim_id for decision in output.decisions)
    if (
        len(decision_ids) != len(claim_records)
        or len(set(decision_ids)) != len(decision_ids)
        or set(decision_ids) != set(claims_by_id)
    ):
        return _result(task_type, None, [_finding("schema_invalid")])

    rejected_claims = tuple(
        claim.text for claim in claim_records if claim.verification_state == "rejected"
    ) + rejected_context
    private_claims = tuple(
        claim.text
        for claim in claim_records
        if claim.privacy_level != "public"
        or claim.verification_state == "private_do_not_publish"
    ) + context_private
    if context is None:
        injection_sources = (
            tuple(claim.text for claim in claim_records),
            tuple(item.summary for item in records),
            rejected_claims,
            private_claims,
        )

    findings: list[VerifierFinding] = []
    material_cache: MaterialCache = {}
    for index, decision in enumerate(output.decisions):
        claim = claims_by_id[decision.claim_id]
        resolved, reference_findings = _resolve_evidence(
            decision.evidence_refs,
            records,
            item_index=index,
            claim_id=claim.id,
        )
        findings.extend(reference_findings)
        claim_supported = _statement_supported(claim.text, resolved)
        claim_private = (
            claim.privacy_level != "public"
            or claim.verification_state == "private_do_not_publish"
        )
        if claim_private and decision.decision != "private":
            findings.append(
                _finding("private_claim", item_index=index, claim_id=claim.id)
            )
        elif decision.decision == "private" and not claim_private:
            findings.append(
                _finding("unsupported_claim", item_index=index, claim_id=claim.id)
            )
        if claim.verification_state == "rejected" and decision.decision != "unsupported":
            findings.append(
                _finding("unsupported_claim", item_index=index, claim_id=claim.id)
            )
        if decision.decision == "needs_review" and claim.verification_state not in {
            "needs_verification",
            "user_stated",
        }:
            findings.append(
                _finding("unsupported_claim", item_index=index, claim_id=claim.id)
            )
        if decision.decision in {"unsupported", "needs_review"} and claim_supported:
            findings.append(
                _finding("unsupported_claim", item_index=index, claim_id=claim.id)
            )
        if decision.decision == "supported":
            if claim_private:
                findings.append(
                    _finding("private_claim", item_index=index, claim_id=claim.id)
                )
            elif claim.verification_state == "rejected" or not claim_supported:
                findings.append(
                    _finding("unsupported_claim", item_index=index, claim_id=claim.id)
                )
            findings.extend(
                _risk_findings(
                    claim.text,
                    metric_supported=_metric_supported(claim.text, resolved),
                    private_material=claim_private,
                    item_index=index,
                    claim_id=claim.id,
                )
            )

        reason_rejected, reason_private = _blocked_material(
            decision.reason,
            records,
            rejected_claims,
            private_claims,
            material_cache,
        )
        if reason_rejected:
            findings.append(
                _finding("unsupported_claim", item_index=index, claim_id=claim.id)
            )
        if reason_private:
            findings.append(
                _finding("private_claim", item_index=index, claim_id=claim.id)
            )
        unsupported_reason = _unsupported_reason_statement(
            decision.reason,
            resolved,
            decision.decision,
        )
        if unsupported_reason is None:
            return _result(task_type, None, [_finding("schema_invalid")])
        if unsupported_reason:
            findings.append(
                _finding("unsupported_claim", item_index=index, claim_id=claim.id)
            )
        findings.extend(
            _risk_findings(
                decision.reason,
                metric_supported=_metric_supported(decision.reason, resolved),
                private_material=reason_private,
                item_index=index,
                claim_id=claim.id,
            )
        )

    injection_echo = _has_injection_echo(output, injection_sources)
    if injection_echo is None:
        return _result(task_type, None, [_finding("schema_invalid")])
    if injection_echo:
        findings.append(_finding("prompt_injection_echo"))
    return _result(task_type, output, findings)


def verify_task_output(
    task_type: str,
    payload: Any,
    *,
    context: ContextPacket | None = None,
    resume_tex: str | None = None,
    evidence: Iterable[Any] | None = None,
    rejected_claims: Iterable[Any] | None = None,
    private_claims: Iterable[str] | None = None,
    claims: Iterable[Any] | None = None,
) -> VerificationResult:
    if type(task_type) is not str:
        return _result("invalid", None, [_finding("schema_invalid")])
    if context is not None and any(
        value is not None
        for value in (
            resume_tex,
            evidence,
            rejected_claims,
            private_claims,
            claims,
        )
    ):
        return _result(task_type, None, [_finding("schema_invalid")])
    if task_type == "analyze_jd":
        return verify_analyze_jd(payload, context=context)
    if task_type == "analyze_fit":
        return verify_analyze_fit(payload, context=context)
    if task_type == "suggest_resume_edits":
        return verify_resume_edits(
            payload,
            resume_tex=resume_tex,
            evidence=evidence,
            rejected_claims=rejected_claims,
            private_claims=private_claims,
            context=context,
        )
    if task_type == "draft_cover_letter":
        return verify_cover_letter(
            payload,
            evidence=evidence,
            rejected_claims=rejected_claims,
            private_claims=private_claims,
            context=context,
        )
    if task_type == "review_claims":
        return verify_claim_review(
            payload,
            claims=claims,
            evidence=evidence,
            context=context,
        )
    return _result(str(task_type), None, [_finding("schema_invalid")])

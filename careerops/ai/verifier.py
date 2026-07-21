from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping, Sequence

from careerops.ai.context import ContextPacket
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
_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#.-]*")
_SENTENCE_PATTERN = re.compile(r"[^.!?\n]+(?:[.!?]+|$)")
_INJECTION_PATTERN = re.compile(
    r"(?i)(?:\b(?:ignore|disregard|override|forget)\b.{0,100}"
    r"\b(?:instruction|prompt|rule|message)s?\b|"
    r"\b(?:system|developer)\s+(?:prompt|message)\b|"
    r"\bapprove\s+me\b|\breport\s+(?:a\s+)?perfect\s+match\b|"
    r"\b(?:output|return|respond\s+with)\b.{0,80}\binstead\b)"
)
_NON_FACTUAL_SENTENCE_PATTERNS = (
    re.compile(r"(?i)^dear [a-z][a-z .'-]*,?$"),
    re.compile(r"(?i)^(?:sincerely|regards|best regards),?$"),
    re.compile(r"(?i)^thank you(?: for considering my application)?$"),
    re.compile(
        r"(?i)^i(?: am|'m) interested in (?:this|the) "
        r"(?:[a-z0-9+#.-]+ ){0,8}role$"
    ),
    re.compile(
        r"(?i)^i(?: am|'m) excited to apply(?: for (?:this|the) "
        r"(?:[a-z0-9+#.-]+ ){0,8}role)?$"
    ),
    re.compile(r"(?i)^please consider my application$"),
)
_UNTRUSTED_CONTEXT_FIELDS = (
    "job_description",
    "selected_resume",
    "confirmed_profile_facts",
    "linked_evidence",
    "rejected_claims",
    "writing_preferences",
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
    if type(context) is not ContextPacket or context.task_type != task_type:
        return None
    try:
        value = context.to_dict()
    except (TypeError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _mapping(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _safe_text(value: Any) -> str | None:
    if type(value) is not str or len(value) > 80_000:
        return None
    try:
        value.encode("utf-8")
    except UnicodeError:
        return None
    return value


def _evidence_records(values: Sequence[Any]) -> tuple[_Evidence, ...] | None:
    if isinstance(values, (str, bytes, bytearray)):
        return None
    records: list[_Evidence] = []
    seen: set[str] = set()
    try:
        for value in values:
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
            ):
                return None
            seen.add(evidence_id)
            records.append(_Evidence(evidence_id, summary, state, privacy))
    except Exception:
        return None
    return tuple(sorted(records, key=lambda item: item.id))


def _claim_records(values: Sequence[Any]) -> tuple[_Claim, ...] | None:
    if isinstance(values, (str, bytes, bytearray)):
        return None
    records: list[_Claim] = []
    seen: set[str] = set()
    try:
        for value in values:
            item = _mapping(value)
            if item is None:
                return None
            claim_id = _safe_text(item.get("id"))
            text = _safe_text(item.get("text", item.get("claim_text")))
            state = _safe_text(item.get("verification_state", "needs_verification"))
            privacy = _safe_text(item.get("privacy_level", "public"))
            if not claim_id or text is None or state is None or privacy is None:
                return None
            if claim_id in seen:
                return None
            seen.add(claim_id)
            records.append(_Claim(claim_id, text, state, privacy))
    except Exception:
        return None
    return tuple(sorted(records, key=lambda item: item.id))


def _ordered_statement_tokens(text: str) -> tuple[str, ...]:
    tokens = tuple(
        token
        for token in (
            match.group(0).casefold().strip(".-")
            for match in _TOKEN_PATTERN.finditer(text)
        )
        if token
    )
    if tokens and tokens[0] in {"i", "we"}:
        return tokens[1:]
    return tokens


def _normalized_text(text: str) -> str:
    return " ".join(text.casefold().split())


def _contains_claim(text: str, claim: str) -> bool:
    normalized_claim = _normalized_text(claim)
    return len(normalized_claim) >= 4 and normalized_claim in _normalized_text(text)


def _new_content_supported(
    source_text: str,
    replacement_text: str,
    evidence: Sequence[_Evidence],
) -> bool:
    replacement = _ordered_statement_tokens(replacement_text)
    if replacement == _ordered_statement_tokens(source_text):
        return True
    return bool(replacement) and any(
        replacement == _ordered_statement_tokens(segment)
        for item in evidence
        for segment in _sentences(item.summary)
    )


def _statement_supported(statement: str, evidence: Sequence[_Evidence]) -> bool:
    required = _ordered_statement_tokens(statement)
    if not required:
        return True
    return any(
        required == _ordered_statement_tokens(segment)
        for item in evidence
        for segment in _sentences(item.summary)
    )


def _sentences(text: str) -> tuple[str, ...]:
    sentences = tuple(
        match.group(0).strip()
        for match in _SENTENCE_PATTERN.finditer(text)
        if match.group(0).strip()
    )
    return sentences or (text,)


def _metric_supported(text: str, evidence: Sequence[_Evidence]) -> bool:
    metrics = {match.casefold() for match in METRIC_PATTERN.findall(text)}
    if not metrics:
        return True
    evidence_metrics: set[str] = set()
    for item in evidence:
        evidence_metrics.update(
            match.casefold() for match in METRIC_PATTERN.findall(item.summary)
        )
    return metrics <= evidence_metrics


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
    report = evaluate_risks(
        resume_text=text,
        claims=[
            {
                "text": text,
                "verification_state": (
                    "verified" if metric_supported else "needs_verification"
                ),
                "public_safe": not private_material,
            }
        ],
    )
    findings: list[VerifierFinding] = []
    for issue in report.blockers:
        code = _RISK_CODES.get(issue.label)
        if code is not None:
            findings.append(
                _finding(code, item_index=item_index, claim_id=claim_id)
            )
    return findings


def _output_text(output: TaskOutput) -> str:
    values: list[str] = []
    stack: list[Any] = [output.model_dump(mode="python")]
    while stack:
        value = stack.pop()
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, Mapping):
            stack.extend(value.values())
        elif isinstance(value, (tuple, list)):
            stack.extend(value)
    return "\n".join(values)


def _injection_fragments(context: Mapping[str, Any]) -> tuple[str, ...]:
    source_texts: list[str] = []
    stack = [context.get(field) for field in _UNTRUSTED_CONTEXT_FIELDS]
    while stack:
        value = stack.pop()
        if type(value) is str:
            source_texts.append(value)
        elif isinstance(value, Mapping):
            stack.extend(value.values())
        elif isinstance(value, (tuple, list)):
            stack.extend(value)

    fragments: set[str] = set()
    for source in source_texts:
        for candidate in _SENTENCE_PATTERN.findall(source):
            normalized = _normalized_text(candidate.strip(" \t\r\n.!?"))
            if normalized and _INJECTION_PATTERN.search(normalized):
                fragments.add(normalized)
                fragments.update(
                    _normalized_text(match.group(0))
                    for match in _INJECTION_PATTERN.finditer(normalized)
                )
    return tuple(sorted(fragments))


def _has_injection_echo(output: TaskOutput, context: Mapping[str, Any]) -> bool:
    generated = _normalized_text(_output_text(output))
    return any(fragment in generated for fragment in _injection_fragments(context))


def _context_auxiliary(
    context: Mapping[str, Any],
    evidence: Sequence[Any] | None,
    rejected_claims: Sequence[Any] | None,
) -> tuple[Sequence[Any], Sequence[Any], tuple[str, ...]]:
    evidence_values = (
        evidence if evidence is not None else context.get("linked_evidence", ())
    )
    rejected_values = (
        rejected_claims
        if rejected_claims is not None
        else context.get("rejected_claims", ())
    )
    private_values: list[str] = []
    facts = context.get("confirmed_profile_facts", ())
    if isinstance(facts, Sequence) and not isinstance(facts, (str, bytes, bytearray)):
        for value in facts:
            item = _mapping(value)
            if item is not None and item.get("privacy_level") != "public":
                fact_value = item.get("value")
                if isinstance(fact_value, str):
                    private_values.append(fact_value)
    return evidence_values, rejected_values, tuple(private_values)


def _rejected_texts(values: Sequence[Any]) -> tuple[str, ...] | None:
    if isinstance(values, (str, bytes, bytearray)):
        return None
    result: list[str] = []
    try:
        for value in values:
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


def _private_texts(values: Sequence[Any]) -> tuple[str, ...] | None:
    if isinstance(values, (str, bytes, bytearray)):
        return None
    result: list[str] = []
    try:
        for value in values:
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
) -> tuple[bool, bool]:
    rejected_echo = any(_contains_claim(text, claim) for claim in rejected)
    private_echo = any(_contains_claim(text, claim) for claim in private_claims)
    private_echo = private_echo or any(
        item.private and _contains_claim(text, item.summary) for item in evidence
    )
    return rejected_echo, private_echo


def _verify_analysis(
    task_type: str,
    payload: Any,
    *,
    context: ContextPacket | None,
) -> VerificationResult:
    context_data = _context_data(context, task_type)
    output = _validate(task_type, payload)
    if context_data is None or output is None:
        return _result(task_type, None, [_finding("schema_invalid")])
    findings: list[VerifierFinding] = []
    if _has_injection_echo(output, context_data):
        findings.append(_finding("prompt_injection_echo"))
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
    evidence: Sequence[Any] | None = None,
    rejected_claims: Sequence[Any] | None = None,
    private_claims: Sequence[str] = (),
    context: ContextPacket | None = None,
) -> VerificationResult:
    task_type = "suggest_resume_edits"
    context_data = _context_data(context, task_type)
    output = _validate(task_type, payload)
    if context_data is None or not isinstance(output, ResumeEditsOutput):
        return _result(task_type, None, [_finding("schema_invalid")])

    evidence_values, rejected_values, context_private = _context_auxiliary(
        context_data, evidence, rejected_claims
    )
    records = _evidence_records(evidence_values)
    rejected = _rejected_texts(rejected_values)
    normalized_private = _private_texts(private_claims)
    if records is None or rejected is None or normalized_private is None:
        return _result(task_type, None, [_finding("schema_invalid")])
    if resume_tex is None:
        selected_resume = _mapping(context_data.get("selected_resume"))
        resume_tex = (
            selected_resume.get("source_text")
            if selected_resume is not None
            else None
        )
    if _safe_text(resume_tex) is None:
        return _result(task_type, None, [_finding("schema_invalid")])

    findings: list[VerifierFinding] = []
    private_materials = normalized_private + context_private
    for index, suggestion in enumerate(output.suggestions):
        if resume_tex.count(suggestion.source_text) != 1:
            findings.append(
                _finding("source_span_not_unique", item_index=index)
            )
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
        )
        if reason_rejected:
            findings.append(_finding("unsupported_claim", item_index=index))
        if reason_private:
            findings.append(_finding("private_claim", item_index=index))
        findings.extend(
            _risk_findings(
                suggestion.reason,
                metric_supported=True,
                private_material=reason_private,
                item_index=index,
            )
        )

    if _has_injection_echo(output, context_data):
        findings.append(_finding("prompt_injection_echo"))
    return _result(task_type, output, findings)


def _factual_sentences(text: str) -> tuple[str, ...]:
    factual: list[str] = []
    for match in _SENTENCE_PATTERN.finditer(text):
        sentence = match.group(0).strip()
        normalized = " ".join(sentence.rstrip(".!?").split())
        if not sentence or any(
            pattern.fullmatch(normalized)
            for pattern in _NON_FACTUAL_SENTENCE_PATTERNS
        ):
            continue
        factual.append(sentence)
    return tuple(factual)


def verify_cover_letter(
    payload: Any,
    *,
    evidence: Sequence[Any] | None = None,
    rejected_claims: Sequence[Any] | None = None,
    private_claims: Sequence[str] = (),
    context: ContextPacket | None = None,
) -> VerificationResult:
    task_type = "draft_cover_letter"
    context_data = _context_data(context, task_type)
    output = _validate(task_type, payload)
    if context_data is None or not isinstance(output, CoverLetterOutput):
        return _result(task_type, None, [_finding("schema_invalid")])

    evidence_values, rejected_values, context_private = _context_auxiliary(
        context_data, evidence, rejected_claims
    )
    records = _evidence_records(evidence_values)
    rejected = _rejected_texts(rejected_values)
    normalized_private = _private_texts(private_claims)
    if records is None or rejected is None or normalized_private is None:
        return _result(task_type, None, [_finding("schema_invalid")])

    resolved, findings = _resolve_evidence(output.evidence_refs, records)
    factual_sentences = _factual_sentences(output.plain_text)
    if any(not _statement_supported(sentence, resolved) for sentence in factual_sentences):
        findings.append(_finding("unsupported_claim"))
    metric_supported = all(
        _metric_supported(sentence, resolved) for sentence in factual_sentences
    )
    rejected_echo, private_echo = _blocked_material(
        output.plain_text,
        records,
        rejected,
        normalized_private + context_private,
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
    if _has_injection_echo(output, context_data):
        findings.append(_finding("prompt_injection_echo"))
    return _result(task_type, output, findings)


def verify_claim_review(
    payload: Any,
    *,
    claims: Sequence[Any],
    evidence: Sequence[Any] | None = None,
    context: ContextPacket | None = None,
) -> VerificationResult:
    task_type = "review_claims"
    context_data = _context_data(context, task_type)
    output = _validate(task_type, payload)
    claim_records = _claim_records(claims)
    if (
        context_data is None
        or not isinstance(output, ClaimReviewOutput)
        or claim_records is None
    ):
        return _result(task_type, None, [_finding("schema_invalid")])

    evidence_values, _, context_private = _context_auxiliary(
        context_data, evidence, None
    )
    records = _evidence_records(evidence_values)
    if records is None:
        return _result(task_type, None, [_finding("schema_invalid")])
    claims_by_id = {claim.id: claim for claim in claim_records}
    if {decision.claim_id for decision in output.decisions} != set(claims_by_id):
        return _result(task_type, None, [_finding("schema_invalid")])

    findings: list[VerifierFinding] = []
    for index, decision in enumerate(output.decisions):
        claim = claims_by_id[decision.claim_id]
        if decision.decision != "supported":
            continue
        resolved, reference_findings = _resolve_evidence(
            decision.evidence_refs,
            records,
            item_index=index,
            claim_id=claim.id,
        )
        findings.extend(reference_findings)
        if claim.privacy_level != "public" or claim.verification_state == "private_do_not_publish":
            findings.append(
                _finding("private_claim", item_index=index, claim_id=claim.id)
            )
        elif claim.verification_state == "rejected" or not _statement_supported(
            claim.text, resolved
        ):
            findings.append(
                _finding("unsupported_claim", item_index=index, claim_id=claim.id)
            )
        findings.extend(
            _risk_findings(
                claim.text,
                metric_supported=_metric_supported(claim.text, resolved),
                private_material=(
                    claim.privacy_level != "public"
                    or claim.verification_state == "private_do_not_publish"
                    or any(_contains_claim(claim.text, value) for value in context_private)
                ),
                item_index=index,
                claim_id=claim.id,
            )
        )
    if _has_injection_echo(output, context_data):
        findings.append(_finding("prompt_injection_echo"))
    return _result(task_type, output, findings)


def verify_task_output(
    task_type: str,
    payload: Any,
    *,
    context: ContextPacket | None = None,
    resume_tex: str | None = None,
    evidence: Sequence[Any] | None = None,
    rejected_claims: Sequence[Any] | None = None,
    private_claims: Sequence[str] = (),
    claims: Sequence[Any] = (),
) -> VerificationResult:
    if type(task_type) is not str:
        return _result("invalid", None, [_finding("schema_invalid")])
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

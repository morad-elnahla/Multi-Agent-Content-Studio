"""Reusable, deterministic evidence skills shared by Research and Review agents.

This module is intentionally small. It does not call an LLM and never fabricates evidence.
"""
from __future__ import annotations


def select_evidence(evidence: dict[str, str], identifiers) -> dict[str, str]:
    """Resolve evidence IDs to original text; reject missing, empty, or duplicate IDs."""
    selected = {}
    for identifier in identifiers:
        if identifier in selected or identifier not in evidence or not isinstance(evidence[identifier], str) or not evidence[identifier].strip():
            raise ValueError(f'Unknown, empty or repeated evidence ID: {identifier}')
        selected[identifier] = evidence[identifier]
    if not selected:
        raise ValueError('At least one verifiable evidence ID is required')
    return selected


def clean_evidence_text(text: str) -> str:
    """Turn a markdown audit finding into plain prose, without adding or rewording any fact.

    Raw findings contain code blocks, **bold**, bullets and backticks. They made the quote
    candidates fragments such as "- Use a **canonical feed account**", which could never prove
    a whole sentence. Cleaning happens once, before the writer and the judge see the evidence,
    so both work on the same text and quotes still match it exactly.
    """
    text = re.sub(r'```.*?(?:```|$)', ' ', text, flags=re.S)          # code blocks (also unterminated)
    text = re.sub(r'\*\*POC Summary\*\*.*', ' ', text, flags=re.S)      # everything after the PoC marker is test code
    text = text.replace('**', '').replace('`', '').replace('\u2022', '\n')
    text = text.translate(str.maketrans({'\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"',
                                         '\u2013': '-', '\u2014': '-', '\u2011': '-', '\u00a0': ' '}))
    lines = []
    for line in text.splitlines():
        line = re.sub(r'^\s*(?:[-*]\s+|\d+[.)]\s+)', '', line).strip()
        line = re.sub(r'[ \t]+', ' ', line)
        if line and not line.endswith(('.', '!', '?', ':')):
            line += '.'                      # every bullet becomes its own sentence
        if line:
            lines.append(line)
    return ' '.join(lines)


def entry_evidence(entry):
    """Return literal, bounded excerpts from the uploaded finding (no invented facts)."""
    key = 'kb:' + entry['id']
    content = entry.get('content', {})
    return {
        key + ':title': clean_evidence_text(entry['title']).rstrip('.'),
        key + ':description': clean_evidence_text(content.get('description', ''))[:1800],
        key + ':recommendation': clean_evidence_text(content.get('recommendation', ''))[:800],
        key + ':impact': entry['impact'],
        key + ':protocol': entry.get('protocol', ''),
    }


# Writer output is untrusted. Canonicalise only genuine existing evidence IDs;
# never create a missing source or treat a URL as a citation merely by guessing.
import re
import unicodedata

WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]+")


def _words(value: str) -> set[str]:
    stop = {'the', 'and', 'that', 'this', 'with', 'from', 'into', 'when', 'their',
            'there', 'will', 'were', 'have', 'been', 'they', 'them', 'can'}
    return {w.lower() for w in WORD.findall(value) if len(w) > 3 and w.lower() not in stop}


def canonical_evidence_ids(references: list[str], evidence: dict[str, str], claim_text: str):
    """Expand short aliases to actual fetched or KB evidence, with a trace.

    The LLM judge still decides whether that original evidence supports the claim.
    Unresolvable aliases remain invalid for hard validation.
    """
    result = []
    repairs = []
    for ref in references:
        if ref in evidence:
            chosen = ref
        else:
            normalized = ref.removeprefix('kb:')
            if re.fullmatch(r'SS-\d+', normalized):
                candidates = [k for k in evidence if k.startswith('kb:' + normalized + ':')
                              and k.split(':')[-1] in ('description', 'recommendation', 'root_cause')]
            elif ref.startswith(('web:', 'ext:')):
                candidates = [k for k in evidence if k == ref + ':body']
            else:
                candidates = []
            if not candidates:
                chosen = ref  # Fail closed in quality.style_checks
            elif len(candidates) == 1:
                chosen = candidates[0]
            else:
                words = _words(claim_text)
                ranked = sorted(candidates, key=lambda k: (
                    len(words & _words(evidence[k])),
                    1 if k.endswith(':description') else 0
                ), reverse=True)
                chosen = ranked[0]
            if chosen != ref:
                repairs.append({'original': ref, 'canonical': chosen})
        if chosen not in result:
            result.append(chosen)
    return result, repairs


PUNCTUATION_ASCII = str.maketrans({
    '\u2018': "'", '\u2019': "'", '\u201c': '"', '\u201d': '"',
    '\u2013': '-', '\u2014': '-', '\u2011': '-', '\u2010': '-',
    '\u00a0': ' ', '\u2026': '...',
})


def clean_typography(text: str):
    """Convert typographic lookalikes only; never delete words or strip emoji.

    Run 6 lost a good post because the model wrote "10 k" with U+202F (narrow no-break space), which looks like a
    space but is non-ASCII. Every Unicode space becomes a normal space and every dash variant a hyphen; invisible
    formatting characters that carry no meaning (zero-width space, soft hyphen, BOM) are removed.
    """
    out = []
    for ch in text.translate(PUNCTUATION_ASCII):
        category = unicodedata.category(ch)
        if category == 'Zs':
            out.append(' ')
        elif category == 'Pd' or ch == '\u2212':
            out.append('-')
        elif ch in '\u200b\u2060\ufeff\u00ad':
            continue
        else:
            out.append(ch)
    return ''.join(out)


# Some model responses add inline citation labels in Unicode brackets despite
# supplying evidence_ids separately. Strip only IDs that really exist in the
# research evidence; unknown labels remain visible and fail the ASCII check.
_INLINE_CITATION = re.compile(r'\s*【((?:kb|web|ext|brand|fixture):[A-Za-z0-9_:./-]+)】')


def strip_inline_evidence_markers(text: str, evidence: dict[str, str]) -> str:
    """Remove verified citation markup, not prose or unsupported references."""
    return _INLINE_CITATION.sub(
        lambda match: '' if match.group(1) in evidence else match.group(0),
        text,
    ).strip()


def source_snippet(source: str, topic: str = '', max_chars: int = 1500) -> str:
    """Bound LLM context using an original contiguous source excerpt.

    Excerpts are copied verbatim, so reviewer quotes still resolve into full
    original evidence. A score helps find a topical passage rather than an
    arbitrary first N chars from a long web page.
    """
    if len(source) <= max_chars:
        return source
    lower = source.lower()
    terms = _words(topic)
    positions = [0, len(source) // 3, len(source) // 2]
    for term in terms:
        start = 0
        for _ in range(4):
            pos = lower.find(term, start)
            if pos == -1: break
            positions.append(max(0, pos - max_chars // 4))
            start = pos + len(term)
    best = max((source[pos:pos + max_chars] for pos in positions),
               key=lambda t: (len(_words(t) & terms), len(t)))
    return best


def verbatim_quote_choices(source: str, claim: str, count: int = 6, max_chars: int = 240):
    """Offer reviewers exact source substrings; do not assert they prove claims.

    Candidates are single sentences AND windows of two consecutive sentences, so a claim that
    spans two neighbouring sentences can be backed by one exact quote. The judge may also
    combine up to three quotes (Claim.extra_citations).
    """
    segments = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+', source) if len(s.strip()) >= 12]
    if not segments:
        segments = [source.strip()]
    candidates = list(segments)
    for first, second in zip(segments, segments[1:]):
        pair = first + ' ' + second
        if pair in source:                       # keep only genuinely contiguous text
            candidates.append(pair)
    terms = _words(claim)
    # Numerical values may have equivalent renderings: "60-second" = "60s".
    # This affects quote *ranking* only, never whether a claim is supported.
    numeric = []
    for num, unit in re.findall(r'\b(\d+)\s*[- ]?(seconds?|minutes?|s|m)\b', claim, re.I):
        numeric.append(num + ('m' if unit.lower().startswith('m') else 's'))

    def quote_score(piece):
        compact = piece.lower().replace(' ', '')
        return (sum(token in compact for token in numeric) * 20,
                len(_words(piece) & terms), -len(piece))

    result = []
    for snippet in sorted(candidates, key=quote_score, reverse=True):
        limit = max_chars if snippet in segments else max_chars + 100      # two-sentence windows may be longer
        if len(snippet) > limit:
            if len(snippet) <= limit + 140 and numeric and any(t in snippet.lower().replace(' ', '') for t in numeric):
                pass                             # keep a complete sentence that carries a critical number
            else:
                continue                         # never cut a sentence in half; a half quote proves nothing
        if snippet in source and snippet not in result:
            result.append(snippet)
        if len(result) >= count:
            break
    if not result:                               # very long single sentences: fall back to a bounded excerpt
        result = [source_snippet(segments[0], claim, max_chars)]
    return result

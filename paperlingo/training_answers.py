"""Deterministic, unreviewed multiple-choice examples for training references.

These rules choose a source-text answer and plausible grammatical/lexical
alternatives. They do not establish that the answer is uniquely acceptable in
context. Generated examples are never automatically marked quality-approved.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import random
import re

from .dataset import Dataset


TRAINING_ANSWER_VERSION = "rule-based-v1"
_METADATA = {
    "version": TRAINING_ANSWER_VERSION,
    "quality_status": "unreviewed",
    "authorship": "deterministic rules using training reference expressions",
    "label": "규칙 작성 예시 · 보기 품질 미검수",
    "scope": "train only; one question per reference expression, including empty papers",
    "answer_policy": "exact source-text token or phrase; deterministic shuffled answer position",
    "rules": [
        "preposition and phrasal-verb particle confusions",
        "complementizer, relative-pronoun and comparative confusions",
        "verb infinitive, agreement, tense and participle forms",
        "discourse markers with different logical relations",
        "academic adjective, adverb and noun alternatives",
    ],
    "limitations": [
        "문맥별 유일 정답 여부와 오답의 타당성은 검수되지 않았습니다.",
        "시제·전치사·어휘 대안 중 둘 이상이 문맥에 성립할 수 있습니다.",
        "형태·품사 추정은 규칙에 의존하며 난이도와 학습 효과를 보장하지 않습니다.",
        "참조 표현 일치와 제출 구조의 유효성은 보기 품질 점수가 아닙니다.",
    ],
}

_TOKENS = re.compile(r"[^\W\d_]+(?:[-'’][^\W\d_]+)*", re.UNICODE)
_PREPOSITIONS = {
    "of": ("for", "to", "with"), "to": ("with", "for", "of"),
    "with": ("to", "for", "on"), "on": ("in", "at", "for"),
    "in": ("on", "at", "for"), "at": ("in", "on", "to"),
    "for": ("of", "to", "with"), "from": ("of", "with", "in"),
    "by": ("with", "from", "on"), "as": ("like", "than", "that"),
    "into": ("in", "onto", "with"), "onto": ("into", "on", "at"),
    "between": ("among", "from", "within"), "among": ("between", "within", "across"),
    "against": ("with", "for", "from"), "under": ("over", "within", "through"),
    "over": ("under", "through", "across"), "within": ("between", "beyond", "through"),
    "without": ("with", "despite", "beyond"), "beyond": ("within", "under", "through"),
    "through": ("across", "within", "toward"), "across": ("through", "within", "between"),
    "toward": ("from", "within", "against"), "towards": ("from", "within", "against"),
    "upon": ("within", "under", "against"), "about": ("with", "for", "at"),
    "around": ("through", "within", "against"), "above": ("below", "within", "beyond"),
    "unlike": ("like", "despite", "besides"),
    "despite": ("because", "although", "unless"),
}
_CONNECTORS = {
    "that": ("what", "which", "whom"), "which": ("what", "whom", "whose"),
    "whose": ("who", "whom", "which"), "whom": ("who", "whose", "which"),
    "whether": ("that", "which", "what"), "than": ("then", "that", "when"),
    "if": ("whether", "unless", "though"), "although": ("because", "unless", "once"),
    "while": ("because", "unless", "once"), "whilst": ("because", "unless", "once"),
    "whereas": ("because", "unless", "once"), "unless": ("although", "because", "once"),
    "when": ("whether", "which", "what"), "where": ("which", "whose", "what"),
    "because": ("although", "unless", "whereas"), "though": ("because", "unless", "once"),
    "what": ("which", "whose", "whom"), "how": ("whether", "which", "what"),
}
_PARTICLES = {
    "out": ("off", "up", "away"), "up": ("out", "off", "down"),
    "down": ("up", "out", "away"), "forth": ("off", "up", "away"),
    "behind": ("ahead", "aside", "away"),
    "forward": ("aside", "away", "back"),
}

# Each family contains real forms. Irregular verbs and consonant doubling are
# explicit; unknown stems are not blindly inflected into invented words.
_IRREGULAR_VERBS = (
    ("be", "is", "are", "was", "were", "been", "being"),
    ("have", "has", "had", "having"), ("do", "does", "did", "done", "doing"),
    ("show", "shows", "showed", "shown", "showing"),
    ("prove", "proves", "proved", "proven", "proving"),
    ("find", "finds", "found", "finding"), ("take", "takes", "took", "taken", "taking"),
    ("give", "gives", "gave", "given", "giving"), ("make", "makes", "made", "making"),
    ("lead", "leads", "led", "leading"), ("hold", "holds", "held", "holding"),
    ("build", "builds", "built", "building"), ("arise", "arises", "arose", "arisen", "arising"),
    ("underlie", "underlies", "underlay", "underlain", "underlying"),
    ("understand", "understands", "understood", "understanding"),
    ("overcome", "overcomes", "overcame", "overcoming"),
    ("underpin", "underpins", "underpinned", "underpinning"),
    ("begin", "begins", "began", "begun", "beginning"),
    ("draw", "draws", "drew", "drawn", "drawing"),
    ("fall", "falls", "fell", "fallen", "falling"),
    ("break", "breaks", "broke", "broken", "breaking"),
    ("bring", "brings", "brought", "bringing"),
    ("keep", "keeps", "kept", "keeping"),
    ("mean", "means", "meant", "meaning"),
    ("seek", "seeks", "sought", "seeking"),
    ("write", "writes", "wrote", "written", "writing"),
    ("run", "runs", "ran", "running"),
    ("occur", "occurs", "occurred", "occurring"),
    ("refer", "refers", "referred", "referring"),
    ("infer", "infers", "inferred", "inferring"),
    ("permit", "permits", "permitted", "permitting"),
    ("omit", "omits", "omitted", "omitting"),
    ("stop", "stops", "stopped", "stopping"),
    ("map", "maps", "mapped", "mapping"),
    ("plan", "plans", "planned", "planning"),
    ("lie", "lies", "lay", "lain", "lying"),
)
_REGULAR_BASES = """
accommodate account accumulate achieve acquire adapt address adjust adopt affect
aid align alleviate allow analyze analyse appear apply approximate argue assess
assume attain attempt augment avoid benchmark call capture carry cause characterize
characterise clarify classify coincide combine compare compensate complement
comprise compute conceal conclude conduct confirm conflate connect consider consist
constitute constrain construct contribute converge corroborate correspond cover
create define demonstrate depend deploy derive describe design determine develop
devise diminish disentangle distinguish dominate eliminate emerge employ enable
encompass enhance ensure establish estimate evaluate examine exceed exhibit expand
expect explain exploit explore express extend extract facilitate fail feature filter
focus formalize formulate function gain generalize generate govern guarantee guide
handle help highlight hinder identify illustrate imply improve include incorporate
increase indicate induce influence inform initiate integrate intend interact interpret
introduce investigate involve isolate justify lack learn limit look maintain maximize
measure mitigate minimize model motivate need neglect note obtain offer optimize
outperform overlook parallel perform persist play point pose posit predict
present preserve prevent proceed process produce promote propose provide quantify
realize reason receive recognize reduce refine reflect reformulate regard relate
release rely remain remove render represent require resolve respond result reveal
revisit rule satisfy scale search seem serve simplify simulate solve specify state
store strengthen study substantiate subsume succeed suffer suggest summarize
supplement support suppress sustain tackle tend test trace train translate treat
undermine underscore unify update use validate vary verify view yield
concern exist experience form modify observe overestimate pronounce report supersede survey synthesize vanish
""".split()


def _regular_family(base: str) -> tuple[str, str, str, str]:
    if base.endswith("y") and base[-2] not in "aeiou":
        return base, base[:-1] + "ies", base[:-1] + "ied", base + "ing"
    third = base + ("es" if base.endswith(("s", "sh", "ch", "x", "z", "o")) else "s")
    past = base + ("d" if base.endswith("e") else "ed")
    gerund = (base[:-1] if base.endswith("e") and not base.endswith("ee") else base) + "ing"
    return base, third, past, gerund


_VERB_FAMILIES = {}
for _family in [*map(_regular_family, _REGULAR_BASES), *_IRREGULAR_VERBS]:
    for _form in _family:
        _VERB_FAMILIES[_form] = _family

_ADJECTIVES = {
    "feasible": ("infeasible", "irrelevant", "inevitable"),
    "infeasible": ("feasible", "irrelevant", "inevitable"),
    "robust": ("fragile", "inconsistent", "approximate"),
    "novel": ("conventional", "redundant", "established"),
    "distinct": ("identical", "ambiguous", "dependent"),
    "significant": ("negligible", "arbitrary", "incidental"),
    "negligible": ("substantial", "arbitrary", "inevitable"),
    "non-negligible": ("negligible", "arbitrary", "invariant"),
    "consistent": ("inconsistent", "unrelated", "ambiguous"),
    "available": ("unavailable", "inapplicable", "incomplete"),
    "comparable": ("incomparable", "unrelated", "invariant"),
    "complementary": ("redundant", "contradictory", "independent"),
    "arbitrary": ("predetermined", "uniform", "necessary"),
    "promising": ("infeasible", "established", "incidental"),
    "sufficient": ("insufficient", "necessary", "redundant"),
    "necessary": ("optional", "sufficient", "redundant"),
    "precise": ("approximate", "ambiguous", "qualitative"),
    "accurate": ("inaccurate", "incomplete", "arbitrary"),
    "efficient": ("inefficient", "inconsistent", "infeasible"),
    "effective": ("ineffective", "irrelevant", "redundant"),
    "reproducible": ("irreproducible", "inconsistent", "unrelated"),
    "plausible": ("implausible", "inevitable", "established"),
    "tractable": ("intractable", "irrelevant", "inconsistent"),
    "scarce": ("abundant", "uniform", "redundant"),
    "ubiquitous": ("rare", "isolated", "localized"),
    "minimal": ("maximal", "arbitrary", "uniform"),
    "straightforward": ("intricate", "ambiguous", "intractable"),
    "qualitative": ("quantitative", "approximate", "incomplete"),
    "quantitative": ("qualitative", "subjective", "incomplete"),
    "preliminary": ("conclusive", "exhaustive", "retrospective"),
    "mutual": ("unilateral", "independent", "sequential"),
    "intrinsic": ("extrinsic", "incidental", "temporary"),
    "subsequent": ("preceding", "simultaneous", "independent"),
    "latter": ("former", "preceding", "remaining"),
    "former": ("latter", "subsequent", "remaining"),
    "off-the-shelf": ("custom-built", "unavailable", "experimental"),
    "state-of-the-art": ("obsolete", "conventional", "experimental"),
    "proof-of-concept": ("production-ready", "theoretical", "commercial"),
    "unprecedented": ("established", "conventional", "predictable"),
    "costly": ("inexpensive", "efficient", "negligible"),
    "versatile": ("specialized", "inflexible", "limited"),
    "larger": ("large", "largest", "largely"),
    "better": ("good", "best", "well"),
    "less": ("little", "least", "fewer"),
    "unclear": ("clear", "irrelevant", "unrelated"),
}
_ADVERBS = {
    "respectively": ("collectively", "independently", "simultaneously"),
    "simultaneously": ("sequentially", "independently", "respectively"),
    "explicitly": ("implicitly", "approximately", "indirectly"),
    "implicitly": ("explicitly", "approximately", "directly"),
    "quantitatively": ("qualitatively", "subjectively", "approximately"),
    "qualitatively": ("quantitatively", "numerically", "approximately"),
    "independently": ("jointly", "sequentially", "conditionally"),
    "significantly": ("marginally", "approximately", "uniformly"),
    "readily": ("rarely", "conditionally", "indirectly"),
    "necessarily": ("possibly", "incidentally", "approximately"),
}
_NOUNS = {
    "evidence": ("assumption", "conjecture", "contradiction"),
    "hypothesis": ("conclusion", "observation", "definition"),
    "hypotheses": ("conclusions", "observations", "definitions"),
    "assumption": ("conclusion", "measurement", "consequence"),
    "assumptions": ("conclusions", "measurements", "consequences"),
    "accuracy": ("complexity", "variability", "redundancy"),
    "efficiency": ("complexity", "uncertainty", "redundancy"),
    "feasibility": ("infeasibility", "optimality", "necessity"),
    "validity": ("invalidity", "complexity", "generality"),
    "dearth": ("abundance", "distribution", "variation"),
    "discrepancies": ("agreements", "similarities", "dependencies"),
    "limitation": ("advantage", "guarantee", "prerequisite"),
    "limitations": ("advantages", "guarantees", "prerequisites"),
    "insight": ("assumption", "restriction", "uncertainty"),
    "insights": ("assumptions", "restrictions", "uncertainties"),
    "framework": ("hypothesis", "criterion", "observation"),
    "approach": ("assumption", "outcome", "constraint"),
    "approaches": ("assumptions", "outcomes", "constraints"),
}
_DISCOURSE_GROUPS = (
    ({"furthermore", "moreover", "additionally", "in addition"},
     ("however", "therefore", "instead")),
    ({"however", "nevertheless", "nonetheless", "in contrast", "conversely", "on the other hand", "instead"},
     ("therefore", "furthermore", "for example")),
    ({"therefore", "hence", "thus", "consequently", "accordingly", "as a result", "as a consequence"},
     ("however", "furthermore", "for instance")),
    ({"namely", "specifically", "in particular", "for instance", "for example", "that is", "in other words"},
     ("conversely", "consequently", "meanwhile")),
    ({"subsequently", "meanwhile", "at the same time", "in turn", "to this end", "for this purpose"},
     ("in contrast", "as a consequence", "for instance")),
    ({"overall", "in conclusion", "to conclude", "simply put", "roughly speaking", "strictly speaking"},
     ("for instance", "in contrast", "subsequently")),
    ({"rather", "yet", "thereby"}, ("otherwise", "subsequently", "instead")),
    ({"likewise", "similarly"}, ("conversely", "consequently", "instead")),
)


def training_answer_metadata() -> dict:
    """Return independent provenance; quality approval is never inferred."""
    return deepcopy(_METADATA)


def _case_like(value: str, source: str) -> str:
    if source.isupper():
        return value.upper()
    if source[:1].isupper():
        return value[:1].upper() + value[1:]
    return value


def _slot(text: str, expression_type: str) -> tuple[str, tuple[str, ...]]:
    tokens = [match.group() for match in _TOKENS.finditer(text)]
    lower = [token.casefold() for token in tokens]
    if not tokens:
        # Reference labels are nonempty. Keep even an unusual punctuation-only
        # expression representable, without manufacturing a source answer.
        return text, ("observation", "assumption", "conclusion")

    for index in reversed(range(len(tokens))):
        if lower[index] in {"that", "which", "whose", "whom", "whether", "than"}:
            return tokens[index], _CONNECTORS[lower[index]]

    if expression_type == "discourse_marker":
        for group, alternatives in _DISCOURSE_GROUPS:
            if text.casefold() in group:
                return text, alternatives

    # Infinitives and gerunds are a useful target for verb/grammar patterns.
    # Prefer the lexical verb over an auxiliary or a pronoun.
    if expression_type in {"verb_pattern", "grammar_pattern", "rhetorical_frame"}:
        for index in range(len(tokens) - 1):
            if lower[index] == "to" and lower[index + 1] in _VERB_FAMILIES:
                return tokens[index + 1], _VERB_FAMILIES[lower[index + 1]]
        for index, token in enumerate(lower):
            if token.endswith("ing") and token in _VERB_FAMILIES:
                return tokens[index], _VERB_FAMILIES[token]

    if len(tokens) > 1 or expression_type == "preposition_pattern":
        for index in reversed(range(len(tokens))):
            if lower[index] in _PREPOSITIONS:
                return tokens[index], _PREPOSITIONS[lower[index]]
        if expression_type == "verb_pattern" and lower[-1] in _PARTICLES:
            return tokens[-1], _PARTICLES[lower[-1]]

    if expression_type in {"grammar_pattern", "discourse_marker", "rhetorical_frame"}:
        for index, token in enumerate(lower):
            if token in _CONNECTORS:
                return tokens[index], _CONNECTORS[token]

    for lexicon in (_ADJECTIVES, _ADVERBS):
        for index, token in enumerate(lower):
            if token in lexicon:
                return tokens[index], lexicon[token]
    lexical_verbs = [index for index, token in enumerate(lower) if token in _VERB_FAMILIES and token not in {"be", "is", "are", "was", "were", "been", "being", "have", "has", "had", "having", "do", "does", "did", "done", "doing"}]
    if lexical_verbs:
        index = lexical_verbs[-1]
        return tokens[index], _VERB_FAMILIES[lower[index]]
    for index in reversed(range(len(tokens))):
        if lower[index] in _VERB_FAMILIES:
            return tokens[index], _VERB_FAMILIES[lower[index]]

    # Known word families take priority over suffix-based part-of-speech guesses.
    for index in reversed(range(len(tokens))):
        if lower[index] in _NOUNS:
            return tokens[index], _NOUNS[lower[index]]
    for index, token in enumerate(lower):
        if token.endswith("ly"):
            return tokens[index], ("implicitly", "approximately", "independently", "respectively")
        if token.endswith(("ment", "ments", "tion", "tions", "ness", "ity", "ities", "ence", "ance")):
            continue
        if token.endswith(("able", "ible", "ive", "ous", "al", "ic", "ary", "ful", "less", "ant", "ent", "ed")):
            return tokens[index], ("arbitrary", "inconsistent", "negligible", "preliminary")
        if token.endswith("ing"):
            return tokens[index], ("remaining", "decreasing", "emerging", "differing")

    answer = tokens[-1]
    if lower[-1].endswith("s") and not lower[-1].endswith(("ss", "is", "us")):
        return answer, ("assumptions", "constraints", "implications", "observations")
    return answer, ("assumption", "constraint", "implication", "observation")


def _question(paper_id: str, expression: dict) -> dict:
    text = expression["text"]
    answer, candidates = _slot(text, expression.get("expression_type", ""))
    options = [answer]
    seen = {answer.casefold()}
    for candidate in candidates:
        option = _case_like(candidate, answer)
        if option.casefold() not in seen:
            options.append(option)
            seen.add(option.casefold())
        if len(options) == 4:
            break
    if len(options) != 4:
        raise ValueError(f"Training answer rule lacks three distinct alternatives for {text!r}")
    seed = hashlib.sha256((paper_id + "\0" + text).encode("utf-8")).digest()
    random.Random(seed).shuffle(options)
    return {"expression_text": text, "options": options, "answer_id": options.index(answer)}


def generate_training_answers(dataset: Dataset) -> list[dict]:
    """Return fresh compact questions for every training reference, and no others.

    The dataset/reference labels are read only. This intentionally includes
    training papers with no selected expressions as ``questions: []``.
    """
    return [
        {"paper_id": paper_id, "questions": [_question(paper_id, expression) for expression in dataset.labels[paper_id]]}
        for paper_id in dataset.splits["train"]
    ]

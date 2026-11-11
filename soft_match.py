"""
paperLingo B2 — 알고리즘 기반 부분 점수 매칭 (LLM 불필요)

점수 규칙 (선생님 채점 루브릭 근사):
  1.0  정규화 후 일치 또는 lemma 시퀀스 일치        (sheds light on ~ shed light on)
  ~0.8 핵심 표현 포함, 기능어 경계만 다름            (in contrast ~ in contrast to)
  ~0.6 같은 학습 포인트, 덜 완결됨                   (range of ~ a wide range of)
  ~0.3 내용어 하나만 겹치고 학습 포인트 어긋남       (account for the ~ take into account)
  0    내용어 공유 없음 / 기능어만 겹침              (of the ~ the role of)

계산: 가중 토큰 F1 (내용어 1.0, 기능어 0.3) ** GAMMA, 상한 CAP.
매칭: 생성×참조 점수 행렬에서 점수 합이 최대가 되도록 1:1 할당 (헝가리안).

외부 의존성: 없음 (scipy가 있으면 헝가리안, 없으면 그리디).
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata

VERSION = "soft-rq-v4 (algorithmic, gamma=1.5, fw=0.3, cap=0.9, min=0.25)"

# ---- 하이퍼파라미터 (버전에 포함해서 기록) ---------------------------------
FUNC_WEIGHT = 0.3   # 기능어 가중치
GAMMA = 1.5         # 부분 일치 감쇠 지수 (클수록 엄격)
CAP = 0.9           # 완전 일치가 아닌 경우의 상한
MIN_SCORE = 0.25    # 이보다 낮으면 0점 처리

FUNCTION_WORDS = set("""
a an the this that these those such
of in on at to for from by with about as into onto upon within without
through over under between among against during before after above below
and or but nor so yet if than then because while whereas although though
is am are was were be been being have has had do does did
it its they them their we our us he she his her i you your
which who whom whose what where when how there here
not no can could may might must shall should will would
also very more most less least much many some any each every all both
""".split())

IRREGULAR = {
    "is": "be", "am": "be", "are": "be", "was": "be", "were": "be", "been": "be", "being": "be",
    "has": "have", "had": "have", "having": "have",
    "does": "do", "did": "do", "done": "do", "doing": "do",
    "shown": "show", "showed": "show", "made": "make", "taken": "take", "took": "take",
    "given": "give", "gave": "give", "found": "find", "led": "lead", "brought": "bring",
    "thought": "think", "seen": "see", "saw": "see", "known": "know", "knew": "know",
    "drawn": "draw", "drew": "draw", "held": "hold", "kept": "keep", "left": "leave",
    "met": "meet", "paid": "pay", "said": "say", "sought": "seek", "set": "set",
    "put": "put", "cut": "cut", "shed": "shed", "spent": "spend", "built": "build",
    "chosen": "choose", "chose": "choose", "arisen": "arise", "arose": "arise",
    "began": "begin", "begun": "begin", "became": "become", "grown": "grow", "grew": "grow",
    "fell": "fall", "fallen": "fall", "felt": "feel", "won": "win", "written": "write",
    "wrote": "write", "underlying": "underlie", "data": "data", "analyses": "analysis",
    "hypotheses": "hypothesis", "criteria": "criterion", "phenomena": "phenomenon",
    "better": "good", "best": "good", "worse": "bad", "worst": "bad",
    "men": "man", "women": "woman", "children": "child",
}

NO_STRIP = {"this", "thus", "has", "was", "is", "its", "as", "us", "his", "always",
            "less", "across", "process", "analysis", "basis", "bias", "class", "loss",
            "mass", "access", "success", "various", "previous", "news", "series",
            "species", "during", "nothing", "something", "thing", "string", "spring",
            "bring", "king", "ring", "sing", "need", "speed", "seed", "feed", "indeed",
            "red", "bed", "hundred", "proceed", "exceed", "succeed"}

DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
QUOTES = dict.fromkeys(map(ord, "‘’‚‛"), "'") | dict.fromkeys(map(ord, "“”„‟"), '"')


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).translate(DASHES).translate(QUOTES).lower()
    t = re.sub(r"(?<=\w)-(?=\w)", " ", t)          # state-of-the-art -> state of the art
    t = re.sub(r"[^\w\s']", " ", t)                 # 구두점 제거
    return re.sub(r"\s+", " ", t).strip()


def lemma(w: str) -> str:
    if w in IRREGULAR:
        return IRREGULAR[w]
    if w in NO_STRIP or len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("ied") and len(w) > 4:
        return w[:-3] + "y"
    for suf in ("ing", "ed"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            stem = w[: -len(suf)]
            if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] not in "lsz":
                return stem[:-1]                    # shedding -> shed, stopped -> stop
            if stem.endswith(("at", "iz", "is", "bl", "ut", "ur", "uc", "ag", "iv", "ov", "rg", "nc", "rv")):
                return stem + "e"                   # evaluated -> evaluate, using -> use
            return stem
    if w.endswith(("sses", "shes", "ches", "xes", "zes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def tokens(text: str) -> list[str]:
    return [lemma(w) for w in normalize(text).split()]


def weight(lem: str) -> float:
    return FUNC_WEIGHT if lem in FUNCTION_WORDS else 1.0


def pair_score(pred: str, gold: str) -> float:
    """두 표현의 부분 점수 (0~1)."""
    if normalize(pred) == normalize(gold):
        return 1.0
    p, g = tokens(pred), tokens(gold)
    if not p or not g:
        return 0.0
    if p == g:
        return 1.0
    # 가중 multiset 교집합
    g_left = list(g)
    overlap, shared_content = 0.0, False
    for t in p:
        if t in g_left:
            g_left.remove(t)
            overlap += weight(t)
            shared_content |= t not in FUNCTION_WORDS
    if not shared_content:
        return 0.0
    wp, wg = sum(map(weight, p)), sum(map(weight, g))
    prec, rec = overlap / wp, overlap / wg
    f1 = 2 * prec * rec / (prec + rec)
    s = min(CAP, f1 ** GAMMA)
    return round(s, 4) if s >= MIN_SCORE else 0.0


def assign(preds: list[str], golds: list[str]):
    """점수 합 최대 1:1 매칭. [(pred_idx, gold_idx, score), ...]"""
    if not preds or not golds:
        return []
    M = [[pair_score(p, g) for g in golds] for p in preds]
    try:
        from scipy.optimize import linear_sum_assignment
        import numpy as np
        rows, cols = linear_sum_assignment(np.array(M), maximize=True)
        pairs = list(zip(rows.tolist(), cols.tolist()))
    except ImportError:  # 그리디 대체
        cand = sorted(((M[i][j], i, j) for i in range(len(preds)) for j in range(len(golds))), reverse=True)
        used_p, used_g, pairs = set(), set(), []
        for s, i, j in cand:
            if s > 0 and i not in used_p and j not in used_g:
                used_p.add(i); used_g.add(j); pairs.append((i, j))
    return [(i, j, M[i][j]) for i, j in pairs if M[i][j] > 0]


def evaluate(data: dict, max_pred: int | None = None) -> dict:
    """
    data = {paper_id: {"gold": [...], "pred": [...], "qc_pass": [bool,...]?}}
      qc_pass: pred와 같은 순서의 코드 검사 결과 (없으면 Q 계산 생략)
      pred가 없거나 빈 논문 = 미제출 (Recall 분모에는 포함)
    """
    n_gold = n_pred = 0
    sum_soft = n_exact = 0.0
    q_num = q_den = 0.0
    per_paper = {}
    for pid, item in data.items():
        golds = item.get("gold", [])
        preds = list(dict.fromkeys(item.get("pred") or []))  # 중복 제거
        if max_pred:
            preds = preds[:max_pred]
        qc = item.get("qc_pass")
        pairs = assign(preds, golds)
        n_gold += len(golds); n_pred += len(preds)
        soft = sum(s for _, _, s in pairs)
        sum_soft += soft
        n_exact += sum(1 for _, _, s in pairs if s == 1.0)
        if qc is not None:
            for i, _, s in pairs:      # 매칭 점수로 가중한 통과율
                q_den += s
                q_num += s * bool(qc[i])
        per_paper[pid] = {
            "matches": [{"pred": preds[i], "gold": golds[j], "score": s} for i, j, s in pairs],
            "soft_recall": round(soft / len(golds), 4) if golds else None,
        }
    R_soft = sum_soft / n_gold if n_gold else 0.0
    P_soft = sum_soft / n_pred if n_pred else 0.0
    F1_soft = 2 * sum_soft / (n_pred + n_gold) if (n_pred + n_gold) else 0.0
    Q = q_num / q_den if q_den else None
    return {
        "version": VERSION,
        "R_exact_lemma": round(n_exact / n_gold, 4) if n_gold else 0.0,
        "R_soft": round(R_soft, 4),
        "P_soft": round(P_soft, 4),
        "F1_soft": round(F1_soft, 4),
        "Q_matched": round(Q, 4) if Q is not None else None,
        "score": round(100 * R_soft * Q, 2) if Q is not None else None,
        "per_paper": per_paper,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python soft_match.py data.json [max_pred]"); sys.exit(1)
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
    mp = int(sys.argv[2]) if len(sys.argv) > 2 else None
    res = evaluate(data, mp)
    detail = res.pop("per_paper")
    print(json.dumps(res, ensure_ascii=False, indent=2))
    with open("soft_match_detail.json", "w", encoding="utf-8") as f:
        json.dump(detail, f, ensure_ascii=False, indent=2)

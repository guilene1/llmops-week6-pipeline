"""promptfoo assertion: the golden set's checks, on one answer.

The same three checks, with the same normalisation, as check() in
lambda/app/evaluate_handler.py, so a case passes here exactly when it passes there.
pipeline/tests/test_promptfoo.py holds the two to that.

    must_include       every fact appears in the answer
    must_not_include   none of these appear: a leaked salary, an invented benefit
    source             the answer cited this document

The reason names what failed ("missing '26 weeks'"), because that is what tells you
whether to look at the documents, the retrieval or the prompt.
"""


def normalize(text):
    return text.lower().replace(",", "").replace("$", "").replace("*", "")


def problems_for(expected, answer, sources):
    problems = []
    text = normalize(answer)
    for fact in expected.get("must_include", []):
        if normalize(fact) not in text:
            problems.append(f"missing '{fact}'")
    for fact in expected.get("must_not_include", []):
        if normalize(fact) in text:
            problems.append(f"leaked '{fact}'")
    if expected.get("source") and expected["source"] not in set(sources):
        problems.append(f"didn't use '{expected['source']}'")
    return problems


def get_assert(output, context):
    expected = (context.get("test") or {}).get("metadata") or {}
    response = context.get("providerResponse") or {}
    sources = (response.get("metadata") or {}).get("sources", [])
    problems = problems_for(expected, output or "", sources)
    return {
        "pass": not problems,
        "score": 0.0 if problems else 1.0,
        "reason": "; ".join(problems) if problems else "all checks passed",
    }

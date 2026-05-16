"""System prompt for procedural-memory extraction from pipeline runs.

After each autonomous pipeline completes, Pulse distils a single durable learning
from the run summary (which config worked, which signal dominated, etc.) and
commits it to the per-org procedural memory collection for use on future runs."""


EXTRACT_FROM_RUN_PROMPT = """\
You distil ONE durable, generalizable learning from a single Pulse pipeline run.

The run summary you receive describes a full autonomous analysis for one org: \
how many entities were scored, the tier breakdown, recommendations generated, \
RAG evaluation metrics, and per-step timing.

## What's worth committing
- A retrieval config that visibly improved (or hurt) recall@K
- A signal pattern that dominated (e.g. "tenure < 12mo + declining recharge \
  produced 80% of critical tier")
- An agent decision that paid off (or backfired)
- An input characteristic that caused a regression or step failure
- A non-obvious correlation between metrics

## What's NOT worth committing
- "Pipeline ran successfully" — not a learning
- Raw metric snapshots ("47 critical") — those rerun and don't generalize
- Generic statements ("model accuracy was good")
- Anything implied by the step counts alone

## How to write the content field
- Single sentence, third person, generalisable beyond this specific run.
- Mention the actionable insight (what to do next time), not just the observation.
- Max 30 words.
- No quotation marks, no markdown.

## Importance scoring (0.0 - 1.0)
- 0.85 - 1.00: clear, actionable, generalizable insight
- 0.70 - 0.84: useful pattern, may generalize
- 0.50 - 0.69: borderline — set commit=false unless surprising
- 0.00 - 0.49: not worth committing

## Hard rules
- Output STRICTLY one JSON object. No preamble, no markdown.
- If commit=false, content/importance may be empty/zero.
- Conservative: when in doubt, commit=false. Procedural memory should accumulate \
  insights worth re-reading, not run logs.

## Output shape
{
  "commit": true | false,
  "content": "<single generalizable sentence, 30 words max>",
  "importance": 0.0-1.0
}

## Worked examples

Input: {"status":"succeeded","duration_ms":92000,"risk_summary":{"total_scored":642,"critical_count":47,"high_count":158},"rag_eval":{"recall_at_3":0.83,"passed":true},...}
Output: {"commit":true,"content":"Decomposed retrieval lifts recall@3 above 0.80 even on mid-size orgs (~600 entities) when reranking is enabled.","importance":0.8}

Input: {"status":"failed","error":"Schema mapping missing target_column",...}
Output: {"commit":true,"content":"Schema mappings without a target_column force ML training to abort; surface this in onboarding validation.","importance":0.85}

Input: {"status":"succeeded","risk_summary":{"total_scored":12,"critical_count":0},...}
Output: {"commit":false,"content":"","importance":0.0}
"""

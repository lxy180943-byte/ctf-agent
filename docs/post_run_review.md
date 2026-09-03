# Post-Run Review and Knowledge Evolution

Use `ctf-agent review-run <run_dir>` after a solve attempt to create `run_review.md`.

The review summarizes:

- key hypotheses from classifier, planner, specialist, critic, and LLM decisions;
- effective commands that produced useful observations;
- ineffective commands that failed or timed out;
- missed signals such as unverified candidates or absent artifact search;
- next strategies for the next attempt or future similar challenges.

Knowledge items track quality fields:

- `success_count`: how often the item has been promoted or learned from a solved route;
- `failure_count`: how often it has been demoted or learned from a failed retrospective;
- `last_used`: updated when the planner retrieves the item;
- `source_type`: `real`, `benchmark`, or `failure-retrospective`.

Failure retrospectives are intentionally low confidence and marked with `failure-retrospective` so they can warn the planner without overpowering solved routes.

Useful commands:

```bash
ctf-agent review-run ~/ctf-workspace/runs/<id>
ctf-agent memory promote <knowledge_id>
ctf-agent memory demote <knowledge_id>
ctf-agent memory prune --min-confidence 0.2 --source-type failure-retrospective
ctf-agent eval ./evals/datasets/local --max-steps 20
```

Every eval writes `capability_gaps.md` next to `eval_report.md`. Treat benchmark knowledge as lower-trust than real competition experience.

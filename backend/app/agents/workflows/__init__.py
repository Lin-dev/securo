"""Code-driven workflows around the model.

A workflow is a procedure written in Python that owns orchestration (what to
load, in which order, how many times) and asks the model only closed,
schema-constrained questions whose answers are validated before they have any
effect. The chat loop in `app.agents.runtime.executor` stays the fallback for
free-form questions; workflows are the default path for multi-step tasks such
as the categorization review.

Public surface:
- `base.WorkflowContext` — what a workflow gets: in-process tools, structured
  model steps with retry and budget, pinned conventions, event/proposal
  emission, and the final result.
- `registry` — the catalogue (`register`, `get`, slash-command parsing, tool
  definitions the model can call as `workflow__<name>`).
"""

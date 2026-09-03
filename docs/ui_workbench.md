# Web Workbench

The local Web UI is a competition workbench served by `ctf-agent ui`. It keeps CLI behavior unchanged and uses dry-run submission by default.

## Layout

- Left pane: challenge filters for category, state, search, and solved-only; run list is shown below.
- Middle pane: current run state, failure count, latest observation, current hypothesis, trace timeline, and writeup preview.
- Right pane: work files, artifacts, flag candidates, manual observations, and manual notes.

## Actions

The UI can start `solve`, `resume`, generate `report`, dry-run `submit`, add manual observations, add flag candidates, and export artifacts into `~/ctf-artifacts`.

Artifact export returns both the WSL path and a Windows path hint from `wslpath -w` when available.

## Safety

Submit stays dry-run unless the request includes `submit=true` and `confirm=SUBMIT`. Existing platform submit guardrails still apply underneath this UI gate.

Manual takeover actions are traced as `human` events and saved into `state.json`, so they are visible after resume and in generated writeups.

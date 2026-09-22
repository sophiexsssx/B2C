# Project: Wikipedia interest Agent Skill
- Deliverable: wiki-interest/ (SKILL.md + Python scripts doing the data work)
- Target user of the skill: Claude Haiku 4.5 — it calls a CLI, never writes code
- Python 3.11, deps in wiki-interest/requirements.txt, no binaries
- Every conclusion must include a confidence level and a reason
- Run `python -m pytest wiki-interest/tests -q` before saying a task is done

## Git workflow
- Never commit directly to main.
- Before starting a new task, create a branch: `git checkout -b <type>/<short-name>`
  (types: feat, fix, test, docs), e.g. `feat/wiki-api-client`.
- Commit in small steps with clear messages ("Add pageviews fetch with caching").
- When the task is done and tests pass: push the branch and open a PR with
  `gh pr create --fill`. Then stop and wait for my review.
- Subagents never run git commands.
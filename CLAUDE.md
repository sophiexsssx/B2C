# Project: Wikipedia interest Agent Skill
- Deliverable: wiki-interest/ (SKILL.md + Python scripts doing the data work)
- Target user of the skill: Claude Haiku 4.5 — it calls a CLI, never writes code
- Python 3.11, deps in wiki-interest/requirements.txt, no binaries
- Every conclusion must include a confidence level and a reason
- Run `python -m pytest wiki-interest/tests -q` before saying a task is done
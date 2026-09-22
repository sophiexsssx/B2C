---
name: eval-runner
description: Use after changing SKILL.md or CLI, to test the skill as a real user would.
tools: Read, Bash
model: haiku
---

Read ONLY wiki-interest/SKILL.md, not source code. For each prompt in
evals/evals.json: solve it with the CLI, report commands run, errors, confusing
instructions, final answer, and mark each `expect` item PASS or FAIL.

---
name: lead
description: Main entry point - orchestrates specialist agents for code reviews and development tasks, synthesizing findings into unified recommendations and coordinated implementations
---

# Agent: Lead

## Role

Whoever is doing the work in this repo — the Claude Code top-level assistant, or Copilot's Lead
persona — owns **task-scoped orchestration**: interpret the assignment, bring in the right
specialists, synthesize their findings, and see the work through to completion. There is no
separate "Lead" identity to invoke; this file *is* your operating instructions.

Out of scope for a single task session (owned by the `coordinator` agent instead):
- Rewriting agent instruction files as a matter of routine
- Enforcing global agent-system consistency
- Creating or deleting agents

## Available specialist agents

Agent instruction files live in `.github/agents/<name>.md`. In Claude Code, invoke them with the
`Agent` tool and `subagent_type: "<name>"`. In Copilot, use its own subagent dispatch mechanism.

| Agent | Use for |
|---|---|
| `api-backward-compatibility-specialist` | API changes, versioning, backward compatibility |
| `architecture-domain-specialist` | Domain model, invariants, architectural boundaries |
| `coordinator` | Auditing/updating the agent system itself (see below — invoke rarely, not per task) |
| `data-time-semantics-specialist` | Timezone-aware datetimes, unit conversions |
| `documentation-developer-experience-specialist` | Docs, error messages, developer workflows |
| `performance-scalability-specialist` | Performance/scalability concerns |
| `test-specialist` | Test coverage, quality, correctness |
| `tooling-ci-specialist` | GitHub Actions, pre-commit, CI/CD |
| `ui-specialist` | Flask/Jinja2 templates, permission gating, JS interaction patterns |

## Delegation guidance

Prefer delegating to a specialist over doing the specialist's job yourself when a task falls
squarely in their domain — a specialist forces a narrower, more careful pass than doing everything
solo. But this is a judgment call, not a per-task ritual: for small or tightly-scoped changes,
acting directly and citing the relevant `.github/instructions/*.instructions.md` convention is
fine. Match delegation effort to task size.

Rough mapping:

| Task type | Consider delegating to |
|---|---|
| API changes | API & Backward Compatibility Specialist |
| User-facing changes | Documentation Specialist |
| Time/unit-handling changes | Data & Time Specialist |
| Performance-sensitive changes | Performance Specialist |
| UI/template changes | UI Specialist |
| Any nontrivial code change | Test Specialist (coverage of the change) |

## When to invoke the Coordinator

The `coordinator` agent is a meta-agent for the health of the agent system itself, not a
per-task governance gate. Invoke it when:

- The user explicitly asks to audit, review, or update agent/instruction files
- A genuinely new cross-cutting pattern or gap is discovered that should become a durable rule
  shared across agents
- Agent files are suspected to have drifted (overlapping scope, stale content, inconsistent
  structure)

Don't invoke it as a mandatory last step of every session — most tasks don't touch the agent
system at all.

## Updating agent instructions

Update an agent's instruction file (or this one) only when a genuinely new, non-obvious, and
repeatable lesson was learned — not after every session. When you do update one:

- Edit the relevant section in place; don't append a dated "lesson learned" narrative
- State the rule and, if the reason isn't obvious, why — skip the who/when/PR-number storytelling
- Keep the change as its own atomic commit, separate from the task's functional changes

## Working practices

- **Pre-commit hooks**: run `pre-commit run --all-files` before every commit; only commit once
  hooks pass. See `.github/instructions/pre-commit-hooks.instructions.md`.
- **Run the actual tests, don't just claim they passed.** `uv sync --group test && uv run poe test`,
  and report real output (counts, pass/fail, warnings). When fixing or adding a test, run the
  whole test module (not just `-k test_name`) — module-scoped fixtures can share mutable state,
  so a fix for one test can silently break its neighbors.
- **Investigate test design intent before changing a test.** A failing test is more often
  revealing a real production bug than a bad test. Read the production code path first; only
  change the test once you can articulate why its original design was wrong.
- **Auth concerns need a regression test, not code inspection.** Citing a schema/decorator/ACL
  check as proof a concern is handled is not sufficient — write a test that reproduces the
  concern, watch it fail, then fix the code and watch it pass.
- **Atomic commits, no mixed changes.** See `.github/instructions/atomic-commits.instructions.md`.
- **No temporary analysis/planning files committed** (e.g. `ARCHITECTURE_ANALYSIS.md`). Use
  `/tmp/` or working memory instead.
- **Preserve existing inline comments** when refactoring code; update them if they reference
  renamed things, but don't drop them just because you added a docstring.
- **PRs**: don't open one until there's at least one substantive commit to push. Use
  `.github/PULL_REQUEST_TEMPLATE.md` as the description base. When following up on review
  comments, edit the existing title/description surgically — never wholesale-replace them.
- **Changelog entry required** for every PR/task — see
  `.github/instructions/changelog.instructions.md`.
- **Branch sync**: check `git log --oneline origin/main...HEAD --left-right` before starting
  substantial work; if `origin/main` has commits the branch lacks, merge first. `git status` alone
  only shows uncommitted changes, not how far behind the branch is. See
  `.github/instructions/feature-branch-sync.instructions.md`.
- **UI terminology**: "organisation", not "account" — see
  `.github/instructions/ui-terminology.instructions.md`.
- **Docstrings/comments**: exactly one space after punctuation — see
  `.github/instructions/docstrings.instructions.md`.
- **The primary checkout is shared** by multiple agent sessions. Never `git checkout`/`switch`/
  `merge`/`rebase`/`reset`/`cherry-pick`/`stash` there — it can switch the branch or rewrite
  history out from under another agent, and `stash` pockets their uncommitted work. Do your work
  in your own `git worktree` instead (`git worktree add --detach <scratchpad>/<name> <ref>`), and
  target it with a **literal path**: `git -C /abs/path/to/worktree switch <branch>`. Read-only git
  commands are fine in the primary checkout. A `PreToolUse` hook
  (`.claude/hooks/worktree-guard.sh`) enforces this; it can't see your shell's cwd or variables,
  so a bare `git switch` after an earlier `cd`, or a `-C "$VAR"`, is blocked on the safe side.

## Session close checklist

Before considering a task done:

- [ ] Pre-commit hooks pass on everything staged
- [ ] Full test suite run (not just the feature's tests) and passing
- [ ] Changelog entry added (if user-facing)
- [ ] Commits are atomic; no temporary files committed
- [ ] If the task touched the agent system itself, `coordinator` was consulted

## Agent Mail Identity

<!-- agent-mail: name=BluePine project=/data/projects/flexmeasures -->

This autonomous repository participates in the laboratory EMS/BMS program governed from
`/data/projects/deye-imex`; governance does not transfer ownership of FlexMeasures source. The
stable identity and primary bus are the exact marker above. The shared Product Bus is
`lab-ems-energy-stack`, and the canonical program snapshot is
`/data/projects/deye-imex/docs/where-we-are.md`.

This marker supersedes the previous repository marker that pointed at
`/data/projects/felectra-deployment`. The existing `BluePine` registration and messages there
are retained as historical pre-Product state, but that Project is not linked to
`lab-ems-energy-stack` and must not carry new laboratory-program traffic. Do not delete or
rewrite that history without a separate owner decision.

All three linked project buses contain recipient aliases `LavenderPuma`, `YellowHeron`, and
`BluePine`. Send from this bus only as `BluePine`, with explicit recipients. Product Bus in
`am 0.3.30` is a federated read surface: create each update once on its owning repository bus;
use the same `thread_id` for cross-repo replies. Never use `broadcast`, and do not pass `topic`.
Shared threads use `lab-ems-energy-stack.shared.<kind>[.<subject>]`; repository-only threads use
`lab-ems-energy-stack.repo.flexmeasures.<topic>`.

Program work status and dependencies live only in `/data/projects/deye-imex/.beads/` (prefix
`labems`). Do not run `br init` or `ee init` here. Update the governing status file only when
the program snapshot changes, and record the updater plus this repository's exact SHA.

Do not run bare program-level `ee` commands from this repo: `/data/projects/.ee` is an
unscoped ancestor marker, not the program workspace. Use
`ee --workspace /data/projects/deye-imex ...`.

Agent Mail coordination never authorizes access to a live pilot, seeding/migration, deployment,
an EMS command, or a physical laboratory scenario. Those require explicit user authorization
for the exact action and the safety gates in `/data/projects/deye-imex/docs/scenarios.md`.

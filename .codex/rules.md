# Codex Development Rules

## Skill-Driven Workflow
When a task matches one of these patterns, enforce the corresponding workflow:

- **Feature / new functionality** → spec first, then plan, then incremental implementation with TDD
- **Bug / failure** → reproduce → isolate → root-cause → fix → add regression test
- **Code review** → five-axis: correctness, design, readability, security, performance
- **Refactoring** → simplify without changing behavior; no mixing with feature work
- **Security-sensitive code** → audit: input validation, auth, secrets, injection, data exposure

## Before Writing Code
1. Determine if the change needs a spec (`spec-driven-development`)
2. Break work into ordered, verifiable tasks (`planning-and-task-breakdown`)
3. Small changes (< 3 files): plan mentally. Larger: write down

## While Coding
1. Write a failing test first, then minimal code to pass (`test-driven-development`)
2. Commit each logical increment separately
3. Run the full test suite after each increment
4. Don't mix refactoring with behavior changes

## Before Committing
1. All tests pass
2. No lint errors
3. No secrets or credentials in diff
4. Review your own diff across 5 axes (correctness, design, readability, security, performance)

## Never
- Commit secrets, API keys, or tokens
- Remove failing tests to "make CI pass"
- Skip verification steps from a skill workflow
- Mix formatting-only changes with behavior changes
- Force-push to shared branches

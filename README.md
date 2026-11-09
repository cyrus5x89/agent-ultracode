# Ultracode

Version: `2.0.0`

Ultracode is a multi-agent orchestration skill for Codex. It turns a substantive
coding task into a controlled workflow with separate discovery, challenge,
implementation, review, and final-gate stages.

## What it does

When Ultracode is active, the orchestrator:

1. Maps the codebase with small, read-only scouts.
2. Cross-checks important findings with independent skeptics.
3. Writes an explicit implementation spec and splits work into disjoint file sets.
4. Assigns executors to make the changes within their declared scope.
5. Gives each executor's work to a verifier that reviews and fixes real defects.
6. Runs the available tests, or performs a read-only final review when no test
   command is available.
7. Reports the actual agents, checks, verifiers, and gate result in a ledger.

The goal is not to use more agents for its own sake. Trivial edits can be handled
directly; orchestration is reserved for work where independent inspection and
review reduce risk.

## Installation

Copy this directory into the Codex skills directory as `ultracode`:

```text
$CODEX_HOME/skills/ultracode/
```

The skill consists of `SKILL.md`, the role configuration in `agents/`, protocol
references in `references/`, and the deterministic runner in `scripts/`.

## Usage

Use the skill explicitly in a task:

```text
Use ultracode to fix the authentication race condition.
```

It can also be invoked implicitly when the task contains the word `ultracode`.

To inspect the effective role configuration:

```bash
python3 scripts/ultracode.py --show-roles
```

To run the deterministic pipeline from a JSON spec:

```bash
python3 scripts/ultracode.py spec.json
```

For write-enabled execution, pass `--allow-write` and provide a test command
when one exists, for example:

```bash
python3 scripts/ultracode.py spec.json --allow-write --test-cmd 'pytest -q'
```

## Scope and safety

- Scouts and skeptics are read-only.
- Executors and verifiers are limited to their declared file sets.
- Changes outside those sets are reverted and reported.
- Verifiers inspect and fix the executor's changes before the final gate.
- The final report distinguishes passing tests from a read-only review.
- The skill does not publish or deploy application changes by itself.

See [`SKILL.md`](SKILL.md) for the complete protocol and configuration options.

## License

No license has been added yet.

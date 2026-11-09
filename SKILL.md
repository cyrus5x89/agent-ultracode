---
name: ultracode
description: Multi-agent orchestration mode. Use when the prompt contains the word "ultracode" (or ultracode standing mode is on) for any substantive task. Small scouts read and report with file:line evidence, big-model skeptics cross-check the scout findings, executors implement on disjoint files, then one big-model verifier per executor reviews, advises and fixes exactly that executor's files, a final gate runs the tests (or a read-only review), then a synthesis. Runs at whatever reasoning effort the session already uses; every role inherits the session's model and effort unless configured.
metadata:
  version: "2.0.0"
---

# ultracode

When ultracode is on, you are the **orchestrator**. The user has explicitly asked for
multi-agent orchestration, so you may spawn sub-agents (`collaboration.spawn_agent`) as this
skill describes. Quality comes from the structure: fan-out, a cross-check of the scouts'
evidence, disjoint executors, a big-model review-and-fix per executor, an independent final
gate, then synthesis. It does not come from raising effort.

You plan, split the work, write the spec, route each piece to a role, spot-check evidence and
decide. Do not do heavy edits yourself: executors make the edits. A trivial request, such as a
one-line answer or a single obvious edit, needs no orchestration, so just do it.

## Effort and model rules (always apply)

- **Never change your own reasoning effort or model for ultracode.** It works at low, medium,
  high, xhigh, max or ultra, whatever the user picked.
- Every role **inherits** this session's current model and effort unless a role is configured
  in `~/.codex/ultracode.json`, `.codex/ultracode.json`, the `ultracode on k=v` toggle, or the
  spec.
- To get the effective table, run this. It is read-only and safe in any sandbox:
  `python3 <this skill dir>/scripts/ultracode.py --show-roles`
  If the ultracode hook is installed, the table is also already in your context.
- Pass a role's `spawn_agent args` **exactly as the table prints them**. An empty `{}` means
  you pass neither `model` nor `reasoning_effort`, and Codex inherits both. When the table
  read this session's effort live, it prints the effort explicitly even though it is
  inherited, because some models replace an omitted effort with a catalog default. If a
  printed model is not in spawn_agent's `model` enum, omit `model` and the role inherits.
- If `--show-roles` prints `note: parent effort came from config...` and you are using the
  script engine, add `--parent-effort <your current effort>` when you know it.
- You may choose a per-role override yourself only when the task clearly benefits, for
  example `scout effort low` for purely mechanical grep or listing work (the cheap-scouts
  preset does this), or `verifier effort high` for a subtle concurrency fix. State the
  reason in one line. The default is inherit.
- The skeptic and the verifier are BIG-model roles: never give them the small scout model
  (the engine refuses it and moves them to the executor's model). An unconfigured verifier
  uses the executor's MODEL; an unconfigured skeptic uses the verifier's, else the
  executor's, else this session's. Their EFFORT is never borrowed: unless set, it is this
  session's effort, so `workers=low` / `scout=low` never makes checking or fixing cheap.

## Roles

| role         | who (preset)                    | tools / sandbox                                   | output |
|--------------|---------------------------------|---------------------------------------------------|--------|
| scout        | small reader (gpt-6-luna)       | READ-ONLY: rg, ls, cat, sed -n, git log/show      | structured report, every claim with file:line + exact quote |
| skeptic      | big model (gpt-6.1-sol), never the scout model | read-only                          | `{refuted, confidence, reason}` on a SCOUT FINDING |
| orchestrator | you (this session)              | everything                                        | plan, spec, routing, spot-checks, decisions |
| executor     | strongest (gpt-6.1-sol)         | writes ONLY its own disjoint file set             | change + summary + tests run |
| verifier     | big model (gpt-6.1-sol), never the scout model | writes ONLY its executor's file set | review + advice + FIXES: verdict, issues, fixes (diff), advice |
| final gate   | tests, else synthesizer (you)   | test command / read-only review                   | PASSED / FAILED / CONCERNS / UNVERIFIED |
| synthesizer  | you (default)                   | read-only                                         | final answer |

Flow: scout (read) -> skeptic check of scout findings -> you decide -> executor (write) ->
verifier (review + advise + fix) -> final gate (tests or read-only review) -> synthesize.
"(preset)" models come from `ultracode.example.json`; with no config every role inherits.

**Native sandbox caveat.** `spawn_agent` (Codex 0.162.1) has no per-spawn sandbox argument,
so every child you spawn runs with THIS session's sandbox. "READ-ONLY" / "writes ONLY" above
are instructions you put in the message, not enforcement: put them in every message, and
enforce the write limits yourself with the `git status` checks in steps 4-5. If you need
sandbox-enforced read-only children, use the deterministic engine.

## Protocol

Name every `task_name` with its role prefix: `scout_<slug>`, `skeptic_<item>_<k>`,
`exec_<slug>`, `verify_<exec-slug>`, `gate_review`, `synth_final`. The optional PreToolUse
hook uses the prefix to apply the role's model and effort (`gate_` uses the synthesizer's,
moved off the scout model). The prefix must be a whole word followed by `_`, `-`, `.`, `:`
or a digit: `readme_reviewer` is NOT a scout and gets no override.

1. **Scout inline.** Spend 1-3 quick commands (`ls`, `rg`) building a work-list of
   independent items. Respect `width` (default 8).
2. **Fan out the scouts.** For each item, call `spawn_agent` with `task_name: "scout_<slug>"`,
   `fork_turns: "none"`, a self-contained `message` (template in
   `references/protocol.md`), plus the scout role's args. Launch in waves of at most
   `concurrency` (default 4) and call `wait_agent` after each wave. Before each wave, post
   one status line, for example
   `ultracode: wave 1/2 - 4 scouts running (gpt-6-luna)`.
3. **Cross-check the scout findings (read-only, big model).** Scouts are small models, so
   their evidence is checked before you decide. For each finding that matters (at most
   `max_verify_items`, default 12), spawn `votes` (default 3) skeptics,
   `skeptic_<id>_<k>`, with the REFUTE template, each from a different angle, using the
   skeptic role's args (never the scout model). A finding dies when refuted votes are more
   than half of the valid votes. Votes that do NOT count: an ABSTAIN, a failed or unparsable
   answer, and any skeptic that ran no command (check its result: it must show the commands
   it ran and quote their output). Replace each such skeptic once. **Quorum:** a verdict
   needs valid votes from a strict majority of the skeptics requested (2 of 2, 2 of 3); below
   that the item is UNVERIFIED. Then spot-check one or two cited lines yourself.
4. **Decide and write the spec.** Split the implementation into **disjoint file sets** and
   give one executor to each set (`exec_<slug>`). Its message holds the spec, its allowed
   files, the confirmed findings, and the KILLED claims with the skeptics' reasons, marked as
   refuted (refuting "X is correct" often shows that X is the bug). Never put two writers on
   the same file. Executors run in parallel only when their sets are disjoint. After the
   executors finish, check `git status --porcelain` against the state before them and revert
   every changed path outside ALL their sets (list it as a violation). If an executor's whole
   change was outside its set, nothing of it is applied: say so, do not verify it, and do not
   report success for that task.
5. **Verify = review + advise + fix (verify_mode `fix`, the default).** Only AFTER an
   executor has finished (`wait_agent` returned for it), spawn ONE verifier for it,
   `verify_<exec-slug>`, with the verifier template: its allowed files are exactly that
   executor's set, plus the task and `git diff -- <its files>`. A verifier and its executor
   never run at the same time on the same files. The verifier inspects the code, fixes real
   defects in place, and returns verdict (`ok|fixed|needs_work|reject`), issues (file:line,
   severity, fixed?), fixes summary, advice for the executor and advice for you. **Enforce
   its scope:** before it starts, note `git status --porcelain`; after it finishes, run
   `git status --porcelain` / `git diff --name-only` again and revert every path outside
   its set that it changed (`git checkout -- <path>` for a tracked file, delete a new file),
   and list those reverts as violations. A verifier that ran no inspection command is
   UNREVIEWED (spawn one replacement). A verifier that says `fixed` while `git diff -- <its
   files>` shows no change since it started is NOT fixed: count it as `needs_work`.
   `needs_work` / `reject`: send the advice back to the
   executor through `followup_task`, or decide yourself.
   `verify_mode=vote` instead runs the old check: `votes` skeptics vote on each executor
   diff (majority of valid votes kills, no fixes). `verify_mode=off` skips the review.
6. **Final gate (independent of the fixers).** A verifier approving its own fixes is not
   independent, so after all verifiers: if a test command is configured (`test_command`,
   `--test-cmd`) or obviously present (pytest config + pytest installed, `package.json`
   `scripts.test` + npm), run it. If it fails, run at most `gate_retries` (default 1) more
   verifier fix rounds, each given the failing output, then run the tests again. Still
   failing: report FAILED honestly with the output tail. With no test command, review the
   combined diff READ-ONLY yourself (or spawn `gate_review` with the synthesizer's args):
   PASSED / CONCERNS / FAILED, citing file:line. Never say tests pass without a passing run.
7. **Report what really ran (mandatory).** End the final answer with this ledger, built ONLY
   from real `spawn_agent` results (the agent id it returned, e.g. `/root/scout_a`),
   `wait_agent` results and commands you ran. One line per spawned agent, then one line per
   checked item, per verifier and for the gate:

   ```
   ultracode ledger
   agents: /root/scout_a (scout, done) | /root/skeptic_a_1 (skeptic, done) | /root/verify_auth (verifier, done) | ...
   check:  <finding> refuted <r>/<valid> valid, quorum <q> -> CONFIRMED|KILLED|CONTESTED|UNVERIFIED
   verify: <exec> -> OK|FIXED|NEEDS_WORK|REJECT|UNREVIEWED, fixed <files>, reverted <paths or none>
   gate:   tests `<cmd>` PASSED|FAILED after <n> fix round(s) | read-only review PASSED|CONCERNS|FAILED
   ```

   If you spawned nothing, or skeptics did not reach quorum, write exactly that, e.g.
   `ultracode ledger: no sub-agents spawned; all findings UNVERIFIED (single-agent review)`.
   Never write "upheld in adversarial review", "3/3", "verified" or "tests pass" without the
   agent ids or the test run behind it. If `spawn_agent` is unavailable or fails, use the
   deterministic engine below (escalated) instead of simulating the fan-out yourself. Note:
   `codex exec --json` does not emit spawn_agent items, so this ledger is the only record.
8. **Synthesize.** By default you do this inline. If the synthesizer role is configured with
   a different model, spawn `synth_final` instead. The final answer covers: answer;
   established facts (file:line); changes (files); verifier fixes and advice; final gate;
   rejected claims and why; open questions.
9. **Loop until dry (optional, at most 2 extra rounds).** If the gate or a quick
   completeness check finds gaps, run one more scout, check, execute, verify round on just
   those gaps.

Keep each child's final answer to about 300 words or 2 KB, with evidence. Read children's
results instead of re-doing their work.

## Deterministic engine: `scripts/ultracode.py`

The same pipeline runs as separate `codex exec` processes. Use it when you want sandbox
enforced read-only scouts, mechanical voting, MECHANICAL verifier scope enforcement and the
test gate, and JSON plus markdown output. Run it with escalated permissions. Inside a
sandboxed Codex command, `codex exec` children fail with `failed to initialize in-process
app-server client: Operation not permitted`, and the script aborts with exit code 3 and tells
you so. Request escalation with the justification "ultracode: run parallel codex exec
children". It works without escalation when the session is danger-full-access.

```bash
python3 <skill>/scripts/ultracode.py spec.json            # or: ... - < spec.json
python3 <skill>/scripts/ultracode.py --task "scout task A" --task "scout task B" --goal "..."
python3 <skill>/scripts/ultracode.py spec.json --allow-write [--worktree] --test-cmd "pytest -q"
python3 <skill>/scripts/ultracode.py spec.json --allow-write --verify-mode vote   # old voting check
python3 <skill>/scripts/ultracode.py --show-roles | --detect | --dry-run
```

Spec (all keys optional except `tasks`; full reference in `references/protocol.md`):

```json
{
  "goal": "Fix the token refresh race",
  "context": "Shared spec / constraints for every child",
  "tasks": [
    {"id": "scan-refresh", "role": "scout", "task": "Map every caller of refresh_token()", "files": ["src/auth/"]},
    {"id": "fix-refresh", "role": "executor", "task": "Add a lock around ...", "files": ["src/auth/refresh.py"]}
  ],
  "roles": {"scout": {"effort": "low"}},
  "testCommand": "pytest -q tests/auth",
  "votes": 3, "concurrency": 4
}
```

Stages: scouts (`-s read-only`) -> skeptics check the scout findings (read-only) ->
executors (`-s workspace-write` only with `--allow-write`, otherwise they return a diff
proposal; each one owns a disjoint `files` set, and overlapping sets are refused) -> one
verifier per finished executor (`workspace-write`; after it, every changed path outside that
executor's set is reverted from a snapshot / git and reported as a violation; parallel
verifiers are attributed by their edit events, `--serial-verify` makes attribution exact;
in proposal mode the verifier is read-only and returns a corrected diff) -> final gate
(tests, with at most `gate_retries` verifier fix rounds on failure; else a read-only review
of the combined diff by the synthesizer role, never on the scout model) -> synthesizer.
Executor edits outside the declared sets are reverted (in place) or dropped from the patch
(`--worktree`). Exit code 4 = the gate failed; exit code 1 = an executor's change could not be
applied at all (or every child failed). `codex exec --ephemeral` children still write to
`~/.codex` (project trust entries, sqlite state, caches); add `--isolate-home` to give them a
throw-away `CODEX_HOME` instead.
Progress streams to stderr. Each child's answer is capped (`--output-cap`). The run
directory holds `result.json`, `report.md`, per-child prompts, events, diffs, verifier fix
diffs and test logs. Run scouts and executors as **separate invocations** when you, the
orchestrator, need to decide between them.

Flags: `--width --votes --concurrency --timeout --output-cap --max-verify-items`;
`--{scout,skeptic,executor,worker,verify,synth}-{model,effort}`;
`--set role.model=X --set role.effort=Y` (also `workers=low`, `verifier=gpt-6.1-sol@high`,
`verify_mode=vote`, `gate=review`); `--verify-mode fix|vote|off --serial-verify`;
`--test-cmd CMD|none --gate auto|tests|review|off --no-gate --gate-retries N`;
`--parent-effort/--parent-model` (skip detection); `--lean` (children ignore user config);
`--isolate-home` (children use a temporary CODEX_HOME, so nothing lands in `~/.codex`);
`--no-verify --no-synth --json --dry-run -q`.

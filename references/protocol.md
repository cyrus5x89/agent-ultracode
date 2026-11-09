# ultracode protocol reference

## Message templates for native `spawn_agent` (fork_turns: "none")

Every message must be self-contained, because the child does not see your conversation.
Keep the word "ultracode" out of child messages, so that children do not re-trigger the
skill.

### scout_<slug>
```
You are a SCOUT in a parallel multi-agent run. READ ONLY: do not create, edit or delete files
and do not run commands that change state. Do not spawn sub-agents.
Goal: <overall goal>
Task: <one independent item>
Start here: <paths>
Report: summary, then findings. Each finding must have evidence as file:line plus an exact
short quote from that line. Anything without evidence goes under "open questions".
Under 300 words.
```

### skeptic_<item>_<k>  (scout-finding cross-check; k = 1..votes, a different angle for each)
Big-model role (never the scout model), read-only. Angles: 1 evidence check (open every cited
line and confirm the quote supports the claim) · 2 counterexample hunt · 3 scope and
completeness · 4 regressions elsewhere · 5 re-derive independently.
```
You are ADVERSARIAL SKEPTIC #k of V (read-only, no sub-agents). Your job is to REFUTE the
item below. Inspect the code yourself first, using rg and nl -ba FILE | sed -n 'A,Bp'.
Angle: <angle k>
Item: <claim + evidence>   (verify_mode=vote: task + allowed files + diff)
Answer ONLY with JSON: {"refuted": true|false, "confidence": 0..1, "reason": "... file:line ..."}
refuted=true: concrete evidence that it is false, unsupported, buggy, incomplete, or outside
its allowed files. refuted=false: only if you tried to break it and it held.
If you cannot inspect anything: refuted=false, confidence=0, and a reason that starts with ABSTAIN.
```
Valid votes: a parsed `{refuted, confidence, reason}` from a skeptic that actually ran at least
one inspection command. ABSTAIN, failures, unparsable answers and 0-command verdicts are
abstentions (the engine re-runs each abstaining skeptic once; `--no-revote` disables this).
Quorum: valid votes >= floor(votes_requested/2)+1 (2 of 2, 2 of 3, 3 of 5).
Tally: below quorum the item is **unverified**; otherwise it is **killed** when refuted > valid/2,
**contested** when refuted = valid/2, and **confirmed** otherwise.

### exec_<slug>
```
You are an EXECUTOR in a parallel multi-agent run. Do not spawn sub-agents.
You may modify ONLY: <disjoint file list>. Other executors own every other file.
Spec: <the orchestrator's spec>
Confirmed findings: <only the findings that survived the skeptic check>
Refuted claims (do not act on the claim; the reasons may point at the real issue): <...>
Do the change. Run quick existing checks (tests or linters) if present. Never install packages.
Final answer: summary, files changed, tests run, notes. Under 300 words.
```

### verify_<exec-slug>  (verify_mode=fix; spawn only AFTER that executor finished)
Big-model role (never the scout model). One per executor. Write access = exactly that
executor's file set; the orchestrator (or the engine) reverts anything else it changes.
```
You are the VERIFIER (reviewer + advisor + fixer) for executor <slug>. Do not spawn sub-agents.
The executor has FINISHED; its change is applied. You may modify ONLY: <its file set>.
Edits to any other path will be reverted and reported as violations.
Task: <the executor's task>      Executor summary: <...>
Current diff: <git diff -- its files>
[Round 2+: the final-gate tests FAILED; output tail: <...>. Fix it only if your files cause it.]
First INSPECT the code (nl -ba, rg, git diff). Fix real defects in place with minimal edits;
no refactors. A problem outside your files: describe it, do not touch it.
Answer ONLY with JSON: {"verdict": "ok|fixed|needs_work|reject",
 "issues": [{"severity": "high|medium|low", "file": "...", "line": N, "description": "...", "fixed": true|false}],
 "fixes_summary": "...", "advice_executor": "...", "advice_orchestrator": "...",
 "tests_run": ["..."], "proposed_diff": ""}
```
Read-only (proposal) runs: the diff is not applied; the verifier puts the complete corrected
diff (against the original files, replacing the executor's) in `proposed_diff`.
A verifier that ran 0 inspection commands and changed nothing is **unreviewed** (re-run once).
The engine trusts the disk, not the claim: its fix diff is computed from snapshots, `ok` with
changed files counts as `fixed`, and `fixed` with no change on disk is flagged.

### gate_review  (final gate when no test command is configured or detected)
```
You are the FINAL GATE REVIEWER (read-only, independent; the verifiers fixed their own findings).
Inspect the current files first (nl -ba, rg, git diff); never edit.
Goal: <...>   Per executor: task, files, verifier verdict + issues + fixes, COMBINED diff.
Answer ONLY with JSON: {"verdict": "pass|concerns|fail", "summary": "...",
 "issues": [{"severity": "...", "file": "...", "line": N, "description": "..."}]}
```
Test gate: run the command; on failure give its output tail to one more verifier round per
executor (at most `gate_retries`, default 1, cap 2), re-run, and report FAILED if it still fails.

### synth_final (only if the synthesizer role differs from you)
```
You are the SYNTHESIZER (read-only). Use only these inputs. Never present KILLED items as true.
Report the final gate exactly as given; never say tests pass without a passing run.
Output markdown: Answer / Established facts (file:line) / Changes / Verifier fixes & advice /
Final gate / Rejected claims / Open questions.
<goal, confirmed items, killed items with reasons, executor summaries, verifier reports, gate>
```

## Spec reference for `scripts/ultracode.py`

| key | type | default | meaning |
|---|---|---|---|
| `goal` | str | "" | overall objective, shown to every child |
| `context` | str | "" | shared spec and constraints for every child |
| `cwd` | str | current dir | working root (`-C`) for the children |
| `tasks[]` | list | required | `{id?, role: scout\|executor, task, files?, model?, effort?, verify?}` |
| `roles` | obj | inherit | `{scout\|skeptic\|executor\|verifier\|synthesizer\|workers\|all: {model, effort}}`. `model` can be a list, and the first one that is available wins |
| `width` | int | 8 (cap 16) | maximum tasks per run |
| `votes` | int | 3 (cap 5) | skeptics per checked item; 0 disables the skeptic checks |
| `concurrency` | int | 4 (cap 6) | children running at once |
| `timeout` | int s | 900 | timeout for each child |
| `output_cap` | int B | 6000 | bytes of each answer that are passed on |
| `max_verify_items` | int | 12 | findings (and, in vote mode, changes) sent to the skeptics, in total |
| `allow_write` | bool | false | executors and verifiers get `-s workspace-write`; otherwise they return diff proposals |
| `verify_mode` | str | `fix` | `fix` = verifier reviews + advises + fixes each executor's files; `vote` = skeptic majority vote on each change (no fixes); `off` |
| `verify_serial` | bool | false | verifiers run one at a time (exact attribution of every out-of-set edit) |
| `testCommand` / `test_command` | str | detect | final-gate test command (run with the shell in `cwd`); `"none"` disables tests |
| `gate` | str | `auto` | `auto` (tests if available and writes are on, else read-only review), `tests`, `review`, `off` |
| `gate_retries` | int | 1 (cap 2) | verifier fix rounds after failing gate tests |
| `verify` / `synthesize` | bool | true | turn those stages off (`verify: false` = no skeptics and no verifier) |

Test detection (only when nothing is configured, and only if obviously present): `pytest -q`
when `pytest.ini`, `conftest.py`, `[tool.pytest...]` in `pyproject.toml`, `[tool:pytest]` in
`setup.cfg` or `[pytest]` in `tox.ini` exists and pytest is installed; `npm test --silent`
when `package.json` has a real `scripts.test` and npm is on PATH. Otherwise: read-only review.

The same option keys work in config files and in `ultracode on k=v` (`verify_mode=vote`,
`gate=review`, `test_cmd="pytest -q"`, `gate_retries=2`).

Layers, from lowest to highest precedence: code default (inherit everything) <
`<skill>/ultracode.json` < `$CODEX_HOME/ultracode.json` < nearest `.codex/ultracode.json`
in the project < `$ULTRACODE_CONFIG` < session toggle (`ultracode on k=v`) < spec < CLI flags.

## How the parent's model and effort are detected

The script prints which method it used (`--detect`):
0. explicit: `--parent-effort/--parent-model`, or `ULTRACODE_PARENT_EFFORT/_MODEL`
1. `$CODEX_THREAD_ID`, then the **last** `turn_context` in that thread's rollout jsonl
   (the path comes from `state_N.sqlite`, opened read-only, or from a glob under
   `~/.codex/sessions`). This is the current turn, so it follows `/model` changes.
2. `state_N.sqlite` `threads.model` / `threads.reasoning_effort`
3. the active profile (`$ULTRACODE_PROFILE` / `$CODEX_PROFILE`): `$CODEX_HOME/<p>.config.toml`
   or `[profiles.<p>]`
4. `config.toml` top-level `model` / `model_reasoning_effort`
5. nothing: no `-m` and no `-c model_reasoning_effort`, so the child uses Codex's default

Efforts are clamped to the target model's `supported_reasoning_levels` in
`models_cache.json`. For example, `minimal` becomes `low` and `ultra` becomes `max` on
gpt-6-luna. Pi's `off` maps to `none` and is then clamped.

## Verifier scope enforcement (engine)
Before the verifier stage the engine snapshots each verifier's file set and records the tree
state (`git status` plus the content of already-dirty paths, or, outside git, a size+mtime
manifest with a content cache of small files). Afterwards every changed path that no verifier
owns is restored (dirty-before content, HEAD for clean files, deleted if new) and reported as a
violation; a `fixed` verdict with no change on disk is downgraded to `needs_work`; an edit
event inside another verifier's set reverts that file to its pre-verifier
state (dropping the owner's fixes on it, reported). Gitignored files and build artifacts
(`__pycache__`, `.pytest_cache`, `*.pyc`, `node_modules`, ...) are not watched. Shell edits by
parallel verifiers into another verifier's set cannot be attributed; use `--serial-verify`.

## Executor scope enforcement (engine)
In place (no `--worktree`) the same guard runs around the executor stage: every changed path
outside ALL executors' `files` is restored and reported on the executor that edited it (its
file_change events; a single executor gets every unattributed path). An edit inside ANOTHER
executor's set is flagged, not reverted (that owner's verifier reviews the file). If some
executor declared no `files`, the scope is unknown and nothing is reverted (warning only).
With `--worktree`, out-of-set paths are dropped from the patch (`<id>.full.patch` keeps the
original) and the rest is applied. The run dir (`--out-dir`) is never watched by any guard.

## Exit codes
0 ok · 1 every child failed, or an executor's change could not be applied at all (every
changed path out of set, or `git apply` failed) · 2 spec or config error (for example overlapping executor files)
· 3 sandbox blocked `codex exec` (re-run escalated, or use the native path) · 4 the final gate
FAILED (tests still failing after the fix rounds, or the gate review verdict was fail)

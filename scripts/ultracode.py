#!/usr/bin/env python3
"""ultracode.py - deterministic scout / check / execute / review-and-fix / gate / synthesize orchestrator for Codex.

Runs parallel `codex exec --json` children, one per task, routed by ROLE:

  scout        read-only (-s read-only), structured report with file:line + quote evidence
  skeptic      V read-only skeptics per SCOUT FINDING, prompted to REFUTE; majority kills (big model,
               never the scout model). Also the voters of the optional --verify-mode vote.
  executor     writes (-s workspace-write with --allow-write), one per DISJOINT file set
  verifier     (--verify-mode fix, default) AFTER each executor finishes: one big-model child reviews
               exactly that executor's file set, FIXES bugs in place (write access limited to that set;
               edits outside it are reverted), and reports issues / fixes (diff) / advice
  final gate   the project's tests (test_command / --test-cmd, or an obvious pytest / npm test), else
               a read-only review of the combined diff by the synthesizer role; failing tests get at
               most gate_retries (default 1) more verifier fix rounds, then the failure is reported
  synthesizer  one read-only child writes the final markdown answer

Every role's model and reasoning effort default to INHERIT: the parent Codex session's
current model/effort, detected from $CODEX_THREAD_ID -> latest rollout turn_context
(-> state db -> profile -> config.toml -> nothing). Override per role with
--scout-effort/--skeptic-effort/--executor-effort/--verify-effort/--synth-effort (and *-model),
--set, the spec's "roles", ~/.codex/ultracode.json, or `ultracode on k=v`.

Python 3.9 compatible, standard library only.

Examples
  ultracode.py --show-roles
  ultracode.py --detect
  ultracode.py spec.json                       # JSON spec file
  echo '{"goal":"...","tasks":[...]}' | ultracode.py -
  ultracode.py --task "Find every caller of parse_config and report arg types" --task "..."
  ultracode.py spec.json --allow-write --test-cmd "pytest -q" --verify-effort high
  ultracode.py spec.json --allow-write --verify-mode vote --dry-run
"""

import argparse
import atexit
import concurrent.futures
import difflib
import fnmatch
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uc_core as core  # noqa: E402

SANDBOX_BLOCK_MARKERS = (
    "failed to initialize in-process app-server client",
)
EXIT_OK, EXIT_FAIL, EXIT_SPEC, EXIT_SANDBOX, EXIT_GATE = 0, 1, 2, 3, 4

_LOG_LOCK = threading.Lock()
_QUIET = False
ABORT = threading.Event()


def log(msg: str, force: bool = False) -> None:
    if _QUIET and not force:
        return
    with _LOG_LOCK:
        sys.stderr.write("[ultracode %s] %s\n" % (time.strftime("%H:%M:%S"), msg))
        sys.stderr.flush()


def cap_text(text: str, cap: int) -> Tuple[str, bool]:
    raw = (text or "").encode("utf-8")
    if len(raw) <= cap:
        return text or "", False
    cut = raw[:cap].decode("utf-8", errors="ignore")
    return cut + "\n...[truncated by ultracode: %d of %d bytes kept]" % (cap, len(raw)), True


def one_line(s: str, n: int = 100) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 3] + "..."


# --------------------------------------------------------------------------- JSON schemas

EVIDENCE = {
    "type": "object",
    "properties": {"file": {"type": "string"}, "line": {"type": "integer"}, "quote": {"type": "string"}},
    "required": ["file", "line", "quote"],
    "additionalProperties": False,
}
SCOUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"claim": {"type": "string"}, "evidence": {"type": "array", "items": EVIDENCE}},
                "required": ["claim", "evidence"],
                "additionalProperties": False,
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "findings", "open_questions"],
    "additionalProperties": False,
}
EXECUTOR_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "tests_run": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
        "diff": {"type": "string"},
    },
    "required": ["summary", "files_changed", "tests_run", "notes", "diff"],
    "additionalProperties": False,
}
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "refuted": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["refuted", "confidence", "reason"],
    "additionalProperties": False,
}

ISSUE = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        "file": {"type": "string"},
        "line": {"type": "integer"},
        "description": {"type": "string"},
        "fixed": {"type": "boolean"},
    },
    "required": ["severity", "file", "line", "description", "fixed"],
    "additionalProperties": False,
}
VERIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "fixed", "needs_work", "reject"]},
        "issues": {"type": "array", "items": ISSUE},
        "fixes_summary": {"type": "string"},
        "advice_executor": {"type": "string"},
        "advice_orchestrator": {"type": "string"},
        "tests_run": {"type": "array", "items": {"type": "string"}},
        "proposed_diff": {"type": "string"},
    },
    "required": ["verdict", "issues", "fixes_summary", "advice_executor", "advice_orchestrator", "tests_run",
                 "proposed_diff"],
    "additionalProperties": False,
}
GATE_ISSUE = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        "file": {"type": "string"},
        "line": {"type": "integer"},
        "description": {"type": "string"},
    },
    "required": ["severity", "file", "line", "description"],
    "additionalProperties": False,
}
GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "concerns", "fail"]},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": GATE_ISSUE},
    },
    "required": ["verdict", "summary", "issues"],
    "additionalProperties": False,
}

SKEPTIC_ANGLES = [
    "Evidence check: open every cited file and line, confirm each quote exists verbatim and actually supports the claim.",
    "Counterexample hunt: look for code paths, callers, configs or edge cases that contradict the claim or break the change.",
    "Scope and completeness: does it overreach, miss cases the task requires, or confuse similar-looking code?",
    "Regression check: what does it break elsewhere (callers, tests, types, imports, docs)?",
    "Fresh eyes: re-derive the answer independently from the code first, then compare with the item.",
]

CHILD_RULES = (
    "You are one child process in a parallel multi-agent run that another agent orchestrates.\n"
    "- Work only on your assigned task. Do not spawn sub-agents and do not delegate.\n"
    "- Be terse. Your final message is consumed by a program, not a human.\n"
)


# --------------------------------------------------------------------------- prompts


def scout_prompt(goal: str, context: str, task: Dict[str, Any], words: int) -> str:
    files = task.get("files") or []
    return (
        CHILD_RULES
        + "ROLE: SCOUT (read-only reader / reporter).\n"
        "- READ ONLY: never create, edit or delete files; never run commands that change state.\n"
        "  Read-only shell is fine (rg, grep, ls, cat, sed -n, git log/show/diff/blame).\n"
        "- Every claim needs evidence: file path, line number, and a short quote copied exactly\n"
        "  from that line. No evidence -> put it in open_questions instead of findings.\n"
        "- Report facts; do not fix anything. Keep the whole answer under about %d words.\n\n"
        "OVERALL GOAL:\n%s\n\n%s"
        "YOUR TASK (%s):\n%s\n\n%s"
        "Return JSON matching the schema: summary, findings[{claim, evidence[{file,line,quote}]}], open_questions.\n"
        % (
            words,
            goal or "(see task)",
            ("SHARED CONTEXT:\n%s\n\n" % context) if context else "",
            task["id"],
            task["task"],
            ("SCOPE HINT (start here): %s\n\n" % ", ".join(files)) if files else "",
        )
    )


def executor_prompt(goal: str, context: str, task: Dict[str, Any], scout_digest: str, can_write: bool, words: int) -> str:
    files = task.get("files") or []
    if can_write:
        mode = (
            "- You MAY modify ONLY these paths (create them if needed): %s\n"
            "  Other executors own every other file. Touching anything else is a failure.\n"
            "- Make the change directly in the workspace. Put an empty string in `diff`.\n"
            % (", ".join(files) if files else "(the files your task names - you are the only executor)")
        )
    else:
        mode = (
            "- The sandbox is READ-ONLY (dry-run proposal mode). Do not try to edit files.\n"
            "- Put the complete proposed change as a unified diff (git apply format) in `diff`,\n"
            "  touching only: %s\n" % (", ".join(files) if files else "the files your task names")
        )
    return (
        CHILD_RULES
        + "ROLE: EXECUTOR (implementer).\n"
        + mode
        + "- Follow the spec exactly; do not refactor unrelated code. Run quick relevant checks\n"
        "  (tests/linters already in the repo) if they exist; never install packages.\n"
        "- Keep summary/notes under about %d words.\n\n"
        "OVERALL GOAL / SPEC:\n%s\n\n%s%s"
        "YOUR TASK (%s):\n%s\n\n"
        "Return JSON matching the schema: summary, files_changed, tests_run, notes, diff.\n"
        % (
            words,
            goal or "(see task)",
            ("SHARED CONTEXT:\n%s\n\n" % context) if context else "",
            ("SCOUT REPORTS (verified where marked):\n%s\n\n" % scout_digest) if scout_digest else "",
            task["id"],
            task["task"],
        )
    )


def skeptic_prompt(goal: str, item: Dict[str, Any], k: int, votes: int, retry: bool = False) -> str:
    angle = SKEPTIC_ANGLES[(k - 1) % len(SKEPTIC_ANGLES)]
    return (
        CHILD_RULES
        + ("NOTE: a previous skeptic for this slot did not inspect the code; its vote was discarded.\n"
           "You MUST run at least one read-only shell command before answering.\n" if retry else "")
        + "ROLE: ADVERSARIAL SKEPTIC #%d of %d (read-only).\n"
        "Your job is to REFUTE the item below. Assume it may be wrong and try hard to break it.\n"
        "Angle for you: %s\n"
        "- refuted=true means the ITEM IS WRONG. If your inspection agrees with the item, refuted=false.\n"
        "- refuted=true  if you find concrete evidence it is false, unsupported by its evidence,\n"
        "                or (for a code change) buggy, incomplete, or outside its allowed files.\n"
        "- refuted=false only if you actively tried to break it and it held up.\n"
        "- First INSPECT the code yourself with read-only shell commands (e.g. nl -ba FILE | sed -n 'A,Bp',\n"
        "  rg PATTERN). Never decide from the item text alone. A verdict given without running at\n"
        "  least one command is discarded automatically. Report only what a command actually printed.\n"
        "- If you truly cannot inspect anything, answer refuted=false, confidence=0 and start reason\n"
        "  with ABSTAIN (an abstention is not counted as a vote).\n"
        "- Cite file:line in `reason`. Under 120 words. confidence is 0..1.\n\n"
        "OVERALL GOAL:\n%s\n\n"
        "ITEM UNDER REVIEW (%s from %s):\n%s\n\n"
        "Return JSON: refuted, confidence, reason.\n"
        % (k, votes, angle, goal or "(none given)", item["kind"], item["source"], item["text"])
    )


def synth_prompt(goal: str, sections: str) -> str:
    return (
        CHILD_RULES
        + "ROLE: SYNTHESIZER (read-only). Write the final answer for the orchestrating agent.\n"
        "- Use ONLY the inputs below (you may open files to double-check a citation).\n"
        "- Items marked KILLED were refuted by a majority of independent skeptics: never present\n"
        "  them as true; mention them only as rejected, with the reason.\n"
        "- UNVERIFIED items did not reach a quorum of valid skeptic votes: report them as\n"
        "  unverified, neither confirmed nor rejected. Never claim verification that is not shown.\n"
        "- VERIFIER sections are big-model reviews that may have FIXED code; list their fixes and advice.\n"
        "  A verifier approving its own fixes is not independent: the FINAL GATE is the independent check.\n"
        "- Report the FINAL GATE exactly as given (PASSED / FAILED / CONCERNS / UNVERIFIED / SKIPPED). Never\n"
        "  say tests pass unless the gate shows a passing test run.\n"
        "- Output concise markdown: Answer / Established facts (file:line) / Changes made (files)\n"
        "  / Verifier fixes & advice / Final gate / Rejected claims / Open questions & next steps.\n"
        "  No preamble.\n\n"
        "OVERALL GOAL:\n%s\n\n%s" % (goal or "(none given)", sections)
    )


def verifier_prompt(goal: str, context: str, task: Dict[str, Any], exec_summary: str, diff_text: str,
                    allowed: List[str], write: bool, scope_note: str, round_no: int, failure_text: str,
                    retry: bool, words: int) -> str:
    if write:
        mode = (
            "STATE: the executor has FINISHED and its change IS applied in the workspace; inspect the current files.\n"
            "- If you find bugs, FIX them in place with minimal edits. You may modify ONLY: %s\n"
            "  Edits to any other path are detected, automatically REVERTED and reported as violations.\n"
            "  A problem outside these files: do not touch it, describe it in advice_orchestrator.\n"
            "- Put an empty string in proposed_diff (ultracode computes your real fix diff from disk).\n"
            % ", ".join(allowed)
        )
    else:
        mode = (
            "STATE: the executor's change is a PROPOSED diff only (read-only run): the files still hold the ORIGINAL\n"
            "code. Do not try to edit files. Judge whether APPLYING the diff would correctly and completely do the\n"
            "task. If it needs fixes, put the complete corrected unified diff (git apply format, against the ORIGINAL\n"
            "files, replacing the executor's diff, touching only: %s) in proposed_diff; otherwise put \"\".\n"
            % (", ".join(allowed) or "the executor's files")
        )
    return (
        CHILD_RULES
        + ("NOTE: a previous verifier for this change ran no inspection command; its report was discarded.\n"
           "You MUST inspect the code with at least one read-only shell command before answering.\n" if retry else "")
        + "ROLE: VERIFIER (big-model reviewer + advisor + fixer) for executor %s, round %d.\n" % (task["id"], round_no)
        + mode
        + "- First INSPECT the code yourself (nl -ba FILE | sed -n 'A,Bp', rg, git diff -- FILES). Never decide from\n"
        "  the text below alone. Run quick existing tests/linters if present; never install packages.\n"
        "- Look for: correctness bugs, missed cases the task requires, regressions for callers, scope creep.\n"
        "- Do not refactor or restyle; fix only real defects. Keep each text field under about %d words.\n"
        "- verdict: ok (no defect found) | fixed (you found defects and fixed them all) | needs_work (defects remain;\n"
        "  say what in advice) | reject (the approach is wrong). issues[].fixed says whether you fixed that issue.\n"
        "- advice_executor: what the executor should learn / do next; advice_orchestrator: risks, follow-ups,\n"
        "  problems outside your files.\n\n"
        "OVERALL GOAL / SPEC:\n%s\n\n%s"
        "EXECUTOR TASK (%s):\n%s\n\nALLOWED FILES: %s\nEXECUTOR SUMMARY: %s\nSCOPE NOTES: %s\n\n%s"
        "CURRENT CHANGE (diff against the state before the executor ran%s):\n%s\n\n"
        "Return JSON: verdict, issues[{severity,file,line,description,fixed}], fixes_summary, advice_executor,\n"
        "advice_orchestrator, tests_run, proposed_diff.\n"
        % (
            words,
            goal or "(see task)",
            ("SHARED CONTEXT:\n%s\n\n" % context) if context else "",
            task["id"], task["task"], ", ".join(allowed) or "(not declared)", exec_summary or "(none)",
            scope_note or "none",
            ("FINAL-GATE TESTS FAILED after the previous round. Fix the failure if it is caused by your files;\n"
             "otherwise change nothing and explain in advice_orchestrator. Test output (tail):\n%s\n\n" % failure_text)
            if failure_text else "",
            ", including earlier verifier fixes" if round_no > 1 else "",
            diff_text or "(no diff captured)",
        )
    )


def gate_review_prompt(goal: str, sections: str, applied: bool) -> str:
    return (
        CHILD_RULES
        + "ROLE: FINAL GATE REVIEWER (read-only, independent). Verifiers below reviewed AND fixed the code, so their\n"
        "approval of their own fixes is not independent: you are the independent check.\n"
        + ("STATE: the combined change IS applied in the workspace; inspect the current files.\n" if applied else
           "STATE: PROPOSED diffs only; the files still hold the ORIGINAL code. Judge the diffs as if applied.\n")
        + "- READ ONLY: never edit files. INSPECT the code with read-only commands (nl -ba, rg, git diff) first;\n"
        "  a verdict without inspection is discarded.\n"
        "- verdict pass = correct and complete as far as you can tell; concerns = minor or uncertain issues;\n"
        "  fail = a concrete bug, regression or unfinished task (cite file:line). Under 200 words.\n\n"
        "OVERALL GOAL:\n%s\n\n%s\n\nReturn JSON: verdict, summary, issues[{severity,file,line,description}].\n"
        % (goal or "(none given)", sections)
    )


# --------------------------------------------------------------------------- child runner


class Ctx(object):
    def __init__(self, args, out_dir: str, codex_bin: str):
        self.args = args
        self.out_dir = out_dir
        self.codex_bin = codex_bin
        self.running = {}  # type: Dict[str, float]
        self.lock = threading.Lock()
        self.child_env = {}  # type: Dict[str, str]


def build_cmd(ctx: Ctx, job: Dict[str, Any], last_path: str, schema_path: Optional[str]) -> List[str]:
    cmd = [
        ctx.codex_bin, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
        "-s", job["sandbox"], "-C", job["cwd"], "-o", last_path, "-c", "notify=[]",
    ]
    if schema_path:
        cmd += ["--output-schema", schema_path]
    if job.get("model"):
        cmd += ["-m", job["model"]]
    if job.get("effort"):
        cmd += ["-c", 'model_reasoning_effort="%s"' % job["effort"]]
    if ctx.args.lean:
        cmd += ["--ignore-user-config"]
    for extra in ctx.args.child_arg or []:
        cmd.append(extra)
    cmd.append("-")
    return cmd


ISO_COPY = ("config.toml", "models_cache.json", "installation_id", "version.json")
ISO_LINK = ("auth.json", "AGENTS.md", "AGENTS.override.md", "rules")


def make_isolated_home() -> Tuple[str, List[str]]:
    """A throw-away CODEX_HOME for the children, so Codex's own writes do not land in ~/.codex.

    `codex exec --ephemeral` still writes to $CODEX_HOME: a workspace-write child appends
    `[projects."<cwd>"] trust_level = "trusted"` to config.toml, and every child touches the
    state/log sqlite files and the model cache. Here config.toml / models_cache.json are COPIED
    (writes go to the copy) and auth.json / AGENTS.md / rules are SYMLINKED (never copied; a
    token refresh written in place still updates your real auth.json). Deleted after the run.
    """
    real = core.codex_home()
    home = tempfile.mkdtemp(prefix="ultracode-codex-home-")
    notes = []  # type: List[str]
    for name in ISO_COPY:
        src = os.path.join(real, name)
        if os.path.isfile(src):
            try:
                shutil.copy2(src, os.path.join(home, name))
            except OSError as e:
                notes.append("isolated home: could not copy %s: %s" % (name, e))
    for name in ISO_LINK:
        src = os.path.join(real, name)
        if os.path.exists(src):
            try:
                os.symlink(src, os.path.join(home, name))
            except OSError as e:
                notes.append("isolated home: could not link %s: %s" % (name, e))
    cfg = ""
    try:
        with open(os.path.join(real, "config.toml"), "r", encoding="utf-8", errors="replace") as fh:
            cfg = fh.read()
    except OSError:
        pass
    if re.search(r'(?m)^\s*cli_auth_credentials_store\s*=\s*"(keyring|auto)"', cfg):
        notes.append("isolated home: credentials are in the OS keyring (cli_auth_credentials_store), which may be keyed "
                     "by the CODEX_HOME path; children may fail to authenticate")
    if not os.path.exists(os.path.join(real, "auth.json")) and not (os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY")):
        notes.append("isolated home: no %s/auth.json and no API key in the environment; children may fail to authenticate" % real)
    return home, notes


def isolated_home_report(home: str) -> str:
    """What Codex wrote into the isolated home (instead of ~/.codex)."""
    real = core.codex_home()
    added = []
    try:
        with open(os.path.join(home, "config.toml"), "r", encoding="utf-8", errors="replace") as fh:
            new = fh.read().splitlines()
        try:
            with open(os.path.join(real, "config.toml"), "r", encoding="utf-8", errors="replace") as fh:
                old = set(fh.read().splitlines())
        except OSError:
            old = set()
        added = [ln for ln in new if ln.strip() and ln not in old]
    except OSError:
        pass
    files = []
    for root, dirs, fs in os.walk(home):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for f in fs:
            full = os.path.join(root, f)
            if not os.path.islink(full):
                files.append(_norm(os.path.relpath(full, home)))
    return "isolated CODEX_HOME absorbed %d file(s)%s" % (
        len(files), ("; config.toml additions: %s" % " | ".join(one_line(a, 200) for a in added[:6])) if added else "")


def parse_json_answer(text: str) -> Optional[Any]:
    t = (text or "").strip()
    if not t:
        return None
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    try:
        return json.loads(t)
    except ValueError:
        pass
    start, end = t.find("{"), t.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(t[start : end + 1])
        except ValueError:
            return None
    return None


def run_child(ctx: Ctx, job: Dict[str, Any]) -> Dict[str, Any]:
    jid = job["id"]
    res = {
        "id": jid, "role": job["role"], "model": job.get("model"), "effort": job.get("effort"),
        "sandbox": job["sandbox"], "status": "skipped", "text": "", "parsed": None,
        "usage": None, "duration_sec": 0.0, "commands": 0, "error": None, "file_changes": [],
    }  # type: Dict[str, Any]
    if ABORT.is_set():
        res["error"] = "skipped: run aborted"
        return res
    jdir = os.path.join(ctx.out_dir, "children", jid)
    os.makedirs(jdir, exist_ok=True)
    prompt_path = os.path.join(jdir, "prompt.txt")
    last_path = os.path.join(jdir, "last.txt")
    err_path = os.path.join(jdir, "stderr.txt")
    events_path = os.path.join(jdir, "events.jsonl")
    with open(prompt_path, "w", encoding="utf-8") as fh:
        fh.write(job["prompt"])
    schema_path = None
    if job.get("schema") and not ctx.args.no_schema:
        schema_path = os.path.join(jdir, "schema.json")
        with open(schema_path, "w", encoding="utf-8") as fh:
            json.dump(job["schema"], fh)
    cmd = build_cmd(ctx, job, last_path, schema_path)
    res["cmd"] = cmd
    if ctx.args.dry_run:
        res["status"] = "dry-run"
        log("dry-run %s: %s" % (jid, " ".join(_shq(c) for c in cmd)))
        return res

    env = dict(os.environ)
    env["ULTRACODE_CHILD"] = "1"  # hooks see this and stay silent (no recursive fan-out)
    env.update(ctx.child_env)  # e.g. CODEX_HOME for --isolate-home
    t0 = time.time()
    with ctx.lock:
        ctx.running[jid] = t0
    log("start %-14s %-11s model=%s effort=%s sandbox=%s" % (
        jid, job["role"], job.get("model") or "(default)", job.get("effort") or "(default)", job["sandbox"]))
    last_msg = ""
    timed_out = [False]
    try:
        with open(prompt_path, "rb") as fin, open(err_path, "wb") as ferr, open(events_path, "wb") as fev:
            proc = subprocess.Popen(cmd, stdin=fin, stdout=subprocess.PIPE, stderr=ferr, cwd=job["cwd"],
                                    env=env, start_new_session=True)

            def killer():
                timed_out[0] = True
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    time.sleep(3)
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass

            timer = threading.Timer(job["timeout"], killer)
            timer.daemon = True
            timer.start()
            try:
                for raw in proc.stdout:  # type: ignore
                    fev.write(raw)
                    if ABORT.is_set() and not timed_out[0]:
                        killer()
                        break
                    try:
                        ev = json.loads(raw.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    et = ev.get("type")
                    item = ev.get("item") or {}
                    it = item.get("type")
                    if et == "item.started" and it in ("command_execution", "mcp_tool_call"):
                        res["commands"] += 1
                        if ctx.args.verbose >= 1:
                            log("  %s $ %s" % (jid, one_line(item.get("command", ""), 110)))
                    elif et == "item.completed" and it == "file_change":
                        changes = item.get("changes") or []
                        paths = [os.path.relpath(c.get("path"), job["cwd"]) if os.path.isabs(str(c.get("path"))) else str(c.get("path"))
                                 for c in changes if isinstance(c, dict) and c.get("path")]
                        res["file_changes"].extend(_norm(p) for p in paths)
                        log("  %s edited %s" % (jid, one_line(", ".join(paths) or "files", 110)))
                    elif et == "item.completed" and it == "agent_message":
                        last_msg = item.get("text") or last_msg
                    elif et == "item.completed" and it == "collab_tool_call":
                        log("  %s WARNING child used %s (sub-agents are discouraged)" % (jid, item.get("tool")))
                    elif et == "turn.completed":
                        res["usage"] = ev.get("usage")
                    elif et in ("turn.failed", "error"):
                        msg = ev.get("message") or (ev.get("error") or {}).get("message") or json.dumps(ev)[:300]
                        res["error"] = one_line(str(msg), 400)
                proc.wait()
            finally:
                timer.cancel()
    except OSError as e:
        res["status"] = "error"
        res["error"] = "could not start codex: %s" % e
        return res
    finally:
        with ctx.lock:
            ctx.running.pop(jid, None)

    res["duration_sec"] = round(time.time() - t0, 1)
    text = ""
    try:
        with open(last_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        text = last_msg
    if not text:
        text = last_msg
    try:
        with open(err_path, "r", encoding="utf-8", errors="replace") as fh:
            err_text = fh.read()
    except OSError:
        err_text = ""
    if any(m in err_text for m in SANDBOX_BLOCK_MARKERS):
        res["status"] = "error"
        res["error"] = ("codex exec cannot start inside the Codex sandbox (%s). Re-run this command "
                        "with escalated permissions / outside the sandbox, or use the native "
                        "spawn_agent path from SKILL.md." % one_line(err_text.strip().splitlines()[-1], 160))
        ABORT.set()
        log("ABORT %s: %s" % (jid, res["error"]), force=True)
        return res
    with open(os.path.join(jdir, "final.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    capped, truncated = cap_text(text, job["cap"])
    res["text"] = capped
    res["truncated"] = truncated
    res["parsed"] = parse_json_answer(text) if job.get("schema") else None
    if timed_out[0] and not ABORT.is_set():
        res["status"] = "timeout"
        res["error"] = "timed out after %ds" % job["timeout"]
    elif proc.returncode != 0 and not text:
        res["status"] = "error"
        res["error"] = res["error"] or ("exit %s: %s" % (proc.returncode, one_line(err_text[-400:], 300)))
    else:
        res["status"] = "ok" if text else "empty"
    u = res.get("usage") or {}
    log("done  %-14s %-7s %5.1fs cmds=%d tokens in=%s out=%s%s" % (
        jid, res["status"], res["duration_sec"], res["commands"], u.get("input_tokens", "?"),
        u.get("output_tokens", "?"), (" err=" + one_line(res["error"], 80)) if res["error"] else ""))
    return res


def _shq(s: str) -> str:
    if s and all(c.isalnum() or c in "-_./=:@," for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


def run_stage(ctx: Ctx, name: str, jobs: List[Dict[str, Any]], concurrency: int) -> List[Dict[str, Any]]:
    if not jobs:
        return []
    log("stage %s: %d job(s), concurrency %d" % (name, len(jobs), min(concurrency, len(jobs))), force=True)
    results = {}  # type: Dict[str, Dict[str, Any]]
    done_count = [0]
    stop_hb = threading.Event()

    def heartbeat():
        while not stop_hb.wait(30):
            with ctx.lock:
                running = dict(ctx.running)
            if running:
                now = time.time()
                log("stage %s: %d/%d done; running: %s" % (
                    name, done_count[0], len(jobs),
                    ", ".join("%s(%ds)" % (k, now - v) for k, v in sorted(running.items()))), force=True)

    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            futs = {pool.submit(run_child, ctx, j): j for j in jobs}
            for fut in concurrent.futures.as_completed(futs):
                j = futs[fut]
                try:
                    r = fut.result()
                except Exception as e:  # pragma: no cover - defensive
                    r = {"id": j["id"], "role": j["role"], "status": "error", "error": repr(e), "text": "", "parsed": None}
                results[j["id"]] = r
                done_count[0] += 1
                if not ctx.args.dry_run:
                    log("stage %s: %d/%d done" % (name, done_count[0], len(jobs)), force=True)
    finally:
        stop_hb.set()
    return [results[j["id"]] for j in jobs]


# --------------------------------------------------------------------------- executor file ownership


def _norm(p: str) -> str:
    p = p.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def _expand(cwd: str, pattern: str) -> List[str]:
    pat = _norm(pattern)
    if any(c in pat for c in "*?["):
        hits = glob.glob(os.path.join(cwd, pat), recursive=True)
        return sorted(_norm(os.path.relpath(h, cwd)) for h in hits if os.path.isfile(h))
    full = os.path.join(cwd, pat)
    if os.path.isdir(full):
        out = []
        for root, dirs, files in os.walk(full):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules")]
            for f in files:
                out.append(_norm(os.path.relpath(os.path.join(root, f), cwd)))
        return sorted(out)
    return [pat]


def _overlap(cwd: str, a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
        return True
    if fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
        return True
    return bool(set(_expand(cwd, a)) & set(_expand(cwd, b)))


def check_disjoint(cwd: str, executors: List[Dict[str, Any]]) -> List[str]:
    errs = []
    if len(executors) > 1:
        for t in executors:
            if not t.get("files"):
                errs.append("executor %s has no `files`: with several executors every one must declare the "
                            "files it owns (never two writers on the same files)" % t["id"])
    for i in range(len(executors)):
        for j in range(i + 1, len(executors)):
            for fa in executors[i].get("files") or []:
                for fb in executors[j].get("files") or []:
                    if _overlap(cwd, fa, fb):
                        errs.append("executors %s and %s both claim %s / %s" % (executors[i]["id"], executors[j]["id"], fa, fb))
    return errs


def snapshot(cwd: str, patterns: List[str]) -> Dict[str, Optional[str]]:
    snap = {}  # type: Dict[str, Optional[str]]
    for p in patterns:
        for rel in _expand(cwd, p):
            full = os.path.join(cwd, rel)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    snap[rel] = fh.read()
            except OSError:
                snap[rel] = None
    return snap


def diff_snapshots(cwd: str, before: Dict[str, Optional[str]], patterns: List[str]) -> Tuple[str, List[str]]:
    after = snapshot(cwd, patterns)
    chunks, changed = [], []
    for rel in sorted(set(before) | set(after)):
        a, b = before.get(rel), after.get(rel)
        if a == b:
            continue
        changed.append(rel)
        chunks.extend(difflib.unified_diff((a or "").splitlines(True), (b or "").splitlines(True),
                                           "a/" + rel if a is not None else "/dev/null",
                                           "b/" + rel if b is not None else "/dev/null"))
    return "".join(chunks), changed


def git(cwd: str, *args: str) -> Tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", cwd] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        return p.returncode, p.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def git_changed(cwd: str) -> Optional[set]:
    rc, out = git(cwd, "status", "--porcelain", "--untracked-files=all", "--", ".")
    if rc != 0:
        return None
    rc2, top = git(cwd, "rev-parse", "--show-prefix")
    prefix = top.strip() if rc2 == 0 else ""
    paths = set()
    for line in out.splitlines():
        if len(line) < 4:
            continue
        p = line[3:]
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        p = p.strip().strip('"')
        if prefix and p.startswith(prefix):
            p = p[len(prefix):]
        paths.add(_norm(p))
    return paths


def owned_by(path: str, patterns: List[str]) -> bool:
    for pat in patterns:
        q = _norm(pat)
        if path == q or path.startswith(q + "/") or fnmatch.fnmatch(path, q):
            return True
    return False


# Build/test byproducts: never treated as scope violations, never reverted.
ARTIFACT_RE = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.tox|node_modules)(/|$)|\.py[co]$")
TREE_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".venv",
                  "venv"}


def _read_bytes(path: str, limit: int) -> Tuple[bool, Optional[bytes]]:
    """(saved, content). content None = absent. saved False = too big to keep."""
    try:
        if os.path.getsize(path) > limit:
            return False, None
        with open(path, "rb") as fh:
            return True, fh.read()
    except OSError:
        return True, None


def git_paths_diff(cwd: str, paths: List[str]) -> str:
    """Unified diff of `paths` against HEAD (untracked new files diffed against empty)."""
    chunks = []
    for rel in paths:
        rc, out = git(cwd, "diff", "HEAD", "--", rel)
        if rc == 0 and out.strip():
            chunks.append(out if out.endswith("\n") else out + "\n")
            continue
        rc2, _ = git(cwd, "ls-files", "--error-unmatch", "--", rel)
        full = os.path.join(cwd, rel)
        if rc2 != 0 and os.path.isfile(full):
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    b = fh.read()
            except OSError:
                continue
            chunks.extend(difflib.unified_diff([], b.splitlines(True), "/dev/null", "b/" + rel))
    return "".join(chunks)


class ScopeGuard(object):
    """Detect and revert edits made OUTSIDE the allowed file sets during one write stage.

    git work tree: `git status` before/after plus a content snapshot of paths that were already
    dirty; a clean path is restored from HEAD, a new untracked one is deleted. Otherwise a
    bounded tree manifest (size + mtime) with a content cache of small files. Gitignored files
    and build artifacts (ARTIFACT_RE) are not watched.
    """

    MAX_FILE = 4 * 1024 * 1024
    CACHE_BUDGET = 96 * 1024 * 1024
    MAX_FILES = 60000

    def __init__(self, cwd: str, exclude: Optional[List[str]] = None):
        self.cwd = cwd
        # Paths (absolute) that are never watched: ultracode's own run dir (child logs, worktrees,
        # patches) when --out-dir lies inside --cwd.
        self.exclude = []  # type: List[str]
        for x in exclude or []:
            rel = _norm(os.path.relpath(os.path.realpath(x), os.path.realpath(cwd)))
            if rel and rel != "." and not rel.startswith(".."):
                self.exclude.append(rel)
        d = git_changed(cwd)
        self.dirty_before = {p for p in d if not self.skip(p)} if d is not None else None
        self.mode = "git" if self.dirty_before is not None else "tree"
        self.saved = {}  # type: Dict[str, Optional[bytes]]
        self.unsaved = set()  # type: set
        self.manifest = {}  # type: Dict[str, Tuple[int, int]]
        if self.mode == "git":
            for rel in self.dirty_before:
                ok, data = _read_bytes(os.path.join(cwd, rel), self.MAX_FILE)
                if ok:
                    self.saved[rel] = data
                else:
                    self.unsaved.add(rel)
        else:
            self.manifest = self._manifest()
            budget = self.CACHE_BUDGET
            for rel, (size, _) in self.manifest.items():
                if size <= self.MAX_FILE and size <= budget:
                    ok, data = _read_bytes(os.path.join(cwd, rel), self.MAX_FILE)
                    if ok and data is not None:
                        self.saved[rel] = data
                        budget -= len(data)
                        continue
                self.unsaved.add(rel)

    @staticmethod
    def skip_rel(rel: str, exclude: List[str], cwd: str) -> bool:
        """`rel` (relative to cwd) is a build artifact or lies under one of the absolute `exclude` dirs."""
        if ARTIFACT_RE.search(rel):
            return True
        for x in exclude:
            xr = _norm(os.path.relpath(os.path.realpath(x), os.path.realpath(cwd)))
            if xr and xr != "." and not xr.startswith("..") and (rel == xr or rel.startswith(xr + "/")):
                return True
        return False

    def skip(self, rel: str) -> bool:
        """Not watched: build artifacts and the excluded run dir."""
        if ARTIFACT_RE.search(rel):
            return True
        return any(rel == x or rel.startswith(x + "/") for x in self.exclude)

    def _manifest(self) -> Dict[str, Tuple[int, int]]:
        out = {}  # type: Dict[str, Tuple[int, int]]
        for root, dirs, files in os.walk(self.cwd):
            rroot = _norm(os.path.relpath(root, self.cwd))
            dirs[:] = [d for d in dirs if d not in TREE_SKIP_DIRS
                       and not self.skip(_norm(os.path.join(rroot, d)) if rroot not in (".", "") else d)]
            for f in files:
                full = os.path.join(root, f)
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                out[_norm(os.path.relpath(full, self.cwd))] = (st.st_size, st.st_mtime_ns)
                if len(out) >= self.MAX_FILES:
                    return out
        return out

    def changed(self) -> set:
        if self.mode == "git":
            after = git_changed(self.cwd) or set()
            out = set(after - self.dirty_before)
            for rel in self.dirty_before:
                if rel in self.unsaved:
                    continue
                ok, data = _read_bytes(os.path.join(self.cwd, rel), self.MAX_FILE)
                if not ok or data != self.saved.get(rel):
                    out.add(rel)
        else:
            m2 = self._manifest()
            out = {p for p in set(self.manifest) | set(m2) if self.manifest.get(p) != m2.get(p)}
        return {p for p in out if not self.skip(p)}

    def restore(self, rel: str) -> Optional[str]:
        """Put `rel` back to its state when the guard was created. Returns an error or None."""
        full = os.path.join(self.cwd, rel)
        try:
            if rel in self.saved:
                return _write_back(full, self.saved[rel])
            if rel in self.unsaved:
                return "too large to snapshot; not reverted"
            if self.mode == "git":
                try:
                    p = subprocess.run(["git", "-C", self.cwd, "show", "HEAD:./" + rel], stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, timeout=60)
                except (OSError, subprocess.TimeoutExpired) as e:
                    return "git show failed: %s" % e
                return _write_back(full, p.stdout if p.returncode == 0 else None)
            if rel not in self.manifest:
                return _write_back(full, None)  # created during the stage
            return "no snapshot; not reverted"
        except OSError as e:
            return str(e)


def _write_back(full: str, data: Optional[bytes]) -> Optional[str]:
    if data is None:
        if os.path.isfile(full) or os.path.islink(full):
            os.remove(full)
        return None
    os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)
    return None


def run_tests(cmd: str, cwd: str, timeout: int, cap: int, log_path: str) -> Dict[str, Any]:
    env = dict(os.environ)
    env["ULTRACODE_CHILD"] = "1"
    t0 = time.time()
    log("gate: running tests: %s" % cmd, force=True)
    timed_out = False
    try:
        proc = subprocess.Popen(cmd, shell=True, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=env, start_new_session=True)
    except OSError as e:
        return {"command": cmd, "rc": None, "passed": False, "timed_out": False, "duration_sec": 0.0,
                "output_tail": "could not start: %s" % e, "log": None}
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            time.sleep(2)
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, _ = proc.communicate()
    text = (out or b"").decode("utf-8", errors="replace")
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    raw = text.encode("utf-8")
    tail = raw[-cap:].decode("utf-8", errors="ignore") if len(raw) > cap else text
    res = {"command": cmd, "rc": proc.returncode, "passed": (proc.returncode == 0 and not timed_out),
           "timed_out": timed_out, "duration_sec": round(time.time() - t0, 1), "output_tail": tail, "log": log_path}
    log("gate: tests %s (rc=%s, %.1fs)%s" % ("PASSED" if res["passed"] else "FAILED", proc.returncode,
                                             res["duration_sec"], " TIMEOUT" if timed_out else ""), force=True)
    return res


# --------------------------------------------------------------------------- spec + settings


def load_spec(args) -> Dict[str, Any]:
    spec = {}  # type: Dict[str, Any]
    if args.spec:
        if args.spec == "-":
            raw = sys.stdin.read()
        else:
            with open(args.spec, "r", encoding="utf-8") as fh:
                raw = fh.read()
        spec = json.loads(raw)
        if isinstance(spec, list):
            spec = {"tasks": spec}
        if not isinstance(spec, dict):
            raise ValueError("spec must be a JSON object or a list of tasks")
    tasks = list(spec.get("tasks") or [])
    for t in args.task or []:
        tasks.append({"role": "scout", "task": t})
    for t in args.executor_task or []:
        files, _, text = t.partition("::")
        tasks.append({"role": "executor", "files": [f for f in files.split(",") if f.strip()], "task": text})
    spec["tasks"] = tasks
    if args.goal:
        spec["goal"] = args.goal
    return spec


def normalize_tasks(spec: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    out, errs, seen = [], [], set()
    counters = {}  # type: Dict[str, int]
    for i, t in enumerate(spec.get("tasks") or []):
        if isinstance(t, str):
            t = {"role": "scout", "task": t}
        if not isinstance(t, dict):
            errs.append("task #%d is not an object" % (i + 1))
            continue
        role = str(t.get("role") or "scout").lower()
        role = {"worker": "scout", "reader": "scout", "scouts": "scout", "writer": "executor", "exec": "executor",
                "executors": "executor"}.get(role, role)
        if role not in ("scout", "executor"):
            errs.append("task #%d: role must be scout or executor (verifier/synthesizer are stages)" % (i + 1))
            continue
        text = t.get("task") or t.get("prompt") or t.get("message")
        if not text or not str(text).strip():
            errs.append("task #%d has no `task` text" % (i + 1))
            continue
        counters[role] = counters.get(role, 0) + 1
        tid = str(t.get("id") or "%s-%d" % (role, counters[role]))
        tid = "".join(c if c.isalnum() or c in "-_." else "-" for c in tid)[:40]
        if tid in seen:
            tid = "%s-%d" % (tid, i + 1)
        seen.add(tid)
        files = t.get("files") or []
        if isinstance(files, str):
            files = [files]
        out.append({"id": tid, "role": role, "task": str(text).strip(), "files": [str(f) for f in files],
                    "model": t.get("model"), "effort": t.get("effort"), "verify": t.get("verify", True)})
    return out, errs


def cli_layer(args) -> Dict[str, Any]:
    roles = {}  # type: Dict[str, Dict[str, Any]]

    def put(role, field, val):
        if val:
            roles.setdefault(role, {})[field] = val

    put("workers", "model", args.worker_model)
    put("workers", "effort", args.worker_effort)
    put("scout", "model", args.scout_model)
    put("scout", "effort", args.scout_effort)
    put("executor", "model", args.executor_model)
    put("executor", "effort", args.executor_effort)
    put("skeptic", "model", args.skeptic_model)
    put("skeptic", "effort", args.skeptic_effort)
    put("verifier", "model", args.verify_model)
    put("verifier", "effort", args.verify_effort)
    put("synthesizer", "model", args.synth_model)
    put("synthesizer", "effort", args.synth_effort)
    layer, msgs = core.parse_kv_tokens(args.set or [])
    for msg in msgs:
        if msg.startswith("ignored"):
            log("--set " + msg, force=True)
    # explicit flags win over --set
    for r, v in roles.items():
        layer["roles"].setdefault(r, {}).update(v)
    for key in ("width", "votes", "concurrency", "timeout", "output_cap", "max_verify_items", "gate_retries"):
        val = getattr(args, key, None)
        if val is not None:
            layer[key] = val
    if args.verify_mode:
        layer["verify_mode"] = args.verify_mode
    if args.no_gate:
        layer["gate"] = "off"
    elif args.gate:
        layer["gate"] = args.gate
    if args.test_cmd is not None:
        layer["test_command"] = args.test_cmd
    if args.serial_verify:
        layer["verify_serial"] = True
    return layer


# --------------------------------------------------------------------------- main pipeline


def scout_items(r: Dict[str, Any], task: Dict[str, Any]) -> List[Dict[str, Any]]:
    p = r.get("parsed")
    items = []
    if isinstance(p, dict) and isinstance(p.get("findings"), list) and p["findings"]:
        for n, f in enumerate(p["findings"], 1):
            if not isinstance(f, dict):
                continue
            ev = f.get("evidence") or []
            ev_txt = "\n".join("  - %s:%s  \"%s\"" % (e.get("file"), e.get("line"), one_line(str(e.get("quote")), 200))
                               for e in ev if isinstance(e, dict))
            items.append({"id": "%s.f%d" % (task["id"], n), "source": task["id"], "kind": "finding",
                          "claim": f.get("claim", ""), "text": "CLAIM: %s\nEVIDENCE:\n%s\n(scout task: %s)" % (
                              f.get("claim", ""), ev_txt or "  (none given)", one_line(task["task"], 300))})
    elif r.get("text"):
        items.append({"id": "%s.r" % task["id"], "source": task["id"], "kind": "report", "claim": one_line(r["text"], 200),
                      "text": "SCOUT TASK: %s\nREPORT:\n%s" % (task["task"], r["text"])})
    return items


def vote_of(v: Dict[str, Any], require_inspection: bool = True) -> Dict[str, Any]:
    """One skeptic's ballot. refuted=None means abstain (not counted).

    A skeptic that ran no command / tool call never looked at the code, so its verdict is an
    abstention even if it sounds confident (this is how a hallucinated "file is empty" used
    to kill a correct change).
    """
    p = v.get("parsed")
    if v.get("status") == "dry-run":
        return {"refuted": None, "reason": "dry-run", "voter": v["id"], "commands": 0}
    ok = v.get("status") == "ok" and isinstance(p, dict) and isinstance(p.get("refuted"), bool)
    if ok and str(p.get("reason", "")).strip().upper().startswith("ABSTAIN"):
        return {"refuted": None, "reason": one_line(str(p.get("reason")), 300), "voter": v["id"], "commands": v.get("commands", 0)}
    if ok and require_inspection and not v.get("commands"):
        return {"refuted": None, "voter": v["id"], "commands": 0,
                "reason": one_line("ABSTAIN (auto): ran 0 inspection commands, verdict discarded; it said: refuted=%s %s" % (
                    p["refuted"], str(p.get("reason", ""))), 300)}
    if ok:
        return {"refuted": p["refuted"], "confidence": p.get("confidence"), "reason": one_line(str(p.get("reason", "")), 500),
                "voter": v["id"], "commands": v.get("commands", 0)}
    why = v.get("error") or (str(p.get("reason"))[:300] if isinstance(p, dict) and p.get("reason") else "no parsable verdict")
    return {"refuted": None, "reason": one_line(why, 300), "voter": v["id"], "commands": v.get("commands", 0)}


def quorum_for(votes_requested: int) -> int:
    """Valid votes needed for a verdict: a strict majority of the skeptics requested."""
    return votes_requested // 2 + 1 if votes_requested > 0 else 1


def tally(item: Dict[str, Any], votes: List[Dict[str, Any]], votes_requested: Optional[int] = None,
          require_inspection: bool = True) -> Dict[str, Any]:
    cast = [vote_of(v, require_inspection) for v in votes]
    valid = [c for c in cast if c["refuted"] is not None]
    nref = sum(1 for c in valid if c["refuted"])
    need = quorum_for(votes_requested if votes_requested is not None else len(votes))
    if len(valid) < need:
        verdict = "unverified"
    elif nref * 2 > len(valid):
        verdict = "killed"
    elif nref * 2 == len(valid):
        verdict = "contested"
    else:
        verdict = "confirmed"
    out = {"item_id": item["id"], "source": item["source"], "kind": item["kind"], "claim": item.get("claim", ""),
           "verdict": verdict, "refuted_votes": nref, "valid_votes": len(valid), "quorum": need, "votes": cast}
    if verdict == "unverified" and votes:
        out["note"] = "no quorum: %d valid vote(s), %d needed (abstentions / 0-command skeptics do not count)" % (len(valid), need)
    return out


def main(argv: Optional[List[str]] = None) -> int:
    global _QUIET
    ap = argparse.ArgumentParser(description="ultracode orchestrator for Codex (fan-out, adversarial verify, synthesize).",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("spec", nargs="?", help="JSON spec file, or - for stdin")
    ap.add_argument("--show-roles", action="store_true", help="print effective model+effort per role and exit")
    ap.add_argument("--detect", action="store_true", help="print parent model/effort detection as JSON and exit")
    ap.add_argument("--json", action="store_true", help="print the result JSON on stdout (default: markdown)")
    ap.add_argument("--goal", help="overall goal (overrides spec.goal)")
    ap.add_argument("--task", action="append", help="add a scout task (repeatable)")
    ap.add_argument("--executor-task", action="append", metavar="FILES::TASK",
                    help="add an executor task owning comma-separated FILES (repeatable)")
    ap.add_argument("--cwd", help="working root for children (default: spec.cwd or current dir)")
    ap.add_argument("--width", type=int, help="max tasks per run (default 8, cap 16)")
    ap.add_argument("--votes", type=int, help="skeptics per checked item (default 3, cap 5, 0 = no skeptic checks)")
    ap.add_argument("--concurrency", type=int, help="max parallel children (default 4, cap 6)")
    ap.add_argument("--timeout", type=int, help="per-child timeout seconds (default 900)")
    ap.add_argument("--output-cap", type=int, help="bytes of each child answer passed on (default 6000)")
    ap.add_argument("--max-verify-items", type=int, help="max findings/changes sent to skeptics (default 12)")
    for role, flag in (("worker", "worker"), ("scout", "scout"), ("skeptic", "skeptic"), ("executor", "executor"),
                       ("verify", "verify"), ("synth", "synth")):
        ap.add_argument("--%s-model" % flag, dest="%s_model" % role, help="model for %s (default inherit)" % role)
        ap.add_argument("--%s-effort" % flag, dest="%s_effort" % role, help="effort for %s (default inherit)" % role)
    ap.add_argument("--verifier-model", dest="verify_model", help=argparse.SUPPRESS)
    ap.add_argument("--verifier-effort", dest="verify_effort", help=argparse.SUPPRESS)
    ap.add_argument("--set", action="append", metavar="K=V",
                    help="e.g. scout.model=gpt-6-luna verifier.effort=high workers=low verify_mode=fix gate=review")
    ap.add_argument("--verify-mode", choices=core.VERIFY_MODES,
                    help="executor changes: fix = big-model verifier reviews + fixes each executor's files (default); "
                         "vote = skeptic majority vote, no fixes; off = no review")
    ap.add_argument("--serial-verify", action="store_true",
                    help="run verifiers one at a time (exact attribution of every out-of-set edit)")
    ap.add_argument("--test-cmd", metavar="CMD",
                    help="final-gate test command (shell); 'none' disables tests; default: config / detect")
    ap.add_argument("--gate", choices=core.GATE_MODES,
                    help="final gate: auto (tests if available, else read-only review; default), tests, review, off")
    ap.add_argument("--no-gate", action="store_true", help="same as --gate off")
    ap.add_argument("--gate-retries", type=int, help="extra verifier fix rounds when gate tests fail (default 1, cap 2)")
    ap.add_argument("--parent-model", help="explicit parent model (skips detection for model)")
    ap.add_argument("--parent-effort", help="explicit parent effort (skips detection for effort)")
    ap.add_argument("--allow-write", action="store_true", help="executors run -s workspace-write (else dry-run diff proposals)")
    ap.add_argument("--worktree", action="store_true", help="run each executor in its own git worktree, then apply its patch")
    ap.add_argument("--no-apply", action="store_true", help="with --worktree: keep patches, do not apply them")
    ap.add_argument("--no-verify", action="store_true", help="no skeptic checks and no verifier review/fix")
    ap.add_argument("--no-revote", action="store_true",
                    help="do not re-run abstaining / 0-command skeptics once (default: one re-vote round)")
    ap.add_argument("--no-synth", action="store_true")
    ap.add_argument("--no-schema", action="store_true", help="do not pass --output-schema to children")
    ap.add_argument("--lean", action="store_true", help="children use --ignore-user-config (no MCP/plugins/notify)")
    ap.add_argument("--child-arg", action="append", help="extra raw arg for every codex exec child (repeatable)")
    ap.add_argument("--isolate-home", action="store_true",
                    help="children run with a throw-away CODEX_HOME (copied config, symlinked auth), so Codex's own "
                         "writes (project trust entries, sqlite state, model cache) do not land in ~/.codex; "
                         "also ULTRACODE_ISOLATE_HOME=1")
    ap.add_argument("--codex-bin", help="codex binary (default: $CODEX_BIN, PATH, ~/.local/bin/codex)")
    ap.add_argument("--out-dir", help="run directory (default: $TMPDIR/ultracode-runs/<ts>)")
    ap.add_argument("--dry-run", action="store_true", help="print the child commands, run nothing")
    ap.add_argument("-v", "--verbose", action="count", default=1, help="more progress (default shows child commands)")
    ap.add_argument("-q", "--quiet", action="store_true", help="only stage-level progress")
    args = ap.parse_args(argv)
    if args.quiet:
        args.verbose = 0
        _QUIET = True

    try:
        spec = load_spec(args)
    except (OSError, ValueError) as e:
        log("spec error: %s" % e, force=True)
        return EXIT_SPEC
    cwd = os.path.abspath(args.cwd or spec.get("cwd") or os.getcwd())
    session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    extra = []  # type: List[Tuple[str, Dict[str, Any]]]
    spec_layer = {k: spec[k] for k in ("roles", "width", "votes", "concurrency", "timeout", "timeout_sec", "output_cap",
                                        "max_verify_items", "gate_retries", "gateRetries", "verify_mode", "verifyMode",
                                        "gate", "test_command", "testCommand", "test_cmd", "verify_serial") if k in spec}
    if spec_layer:
        extra.append(("spec", spec_layer))
    extra.append(("cli", cli_layer(args)))
    settings, warns, _state = core.build_settings(cwd, session, extra)
    parent = core.detect_parent(explicit_model=args.parent_model, explicit_effort=args.parent_effort)
    catalog = core.load_catalog()
    roles = core.resolve_roles(settings, parent, catalog)
    caps = settings["caps"]

    if args.detect:
        print(json.dumps(parent, indent=2))
        return EXIT_OK
    if args.show_roles:
        if args.json:
            print(json.dumps({"parent": parent, "roles": roles, "caps": caps, "options": settings["options"],
                              "gate": core.gate_plan(settings, cwd), "warnings": warns,
                              "config_files": [p for p, _ in core.config_layers(cwd)]}, indent=2))
        else:
            print(core.roles_table(roles, parent, settings, cwd))
            files = [p for p, _ in core.config_layers(cwd)]
            print("config files: %s" % (", ".join(files) if files else "(none - all roles inherit)"))
            for w in warns:
                print("warning: %s" % w)
            print("native spawn_agent args (omit a key = inherit):")
            for r in core.CHILD_ROLES:
                print("  %-11s %s" % (r, json.dumps(roles[r]["native_spawn_args"])))
            print("  %-11s %s" % ("gate_", json.dumps(roles["synthesizer"].get("gate_native_spawn_args", {}))))
        return EXIT_OK

    tasks, errs = normalize_tasks(spec)
    if not tasks and not errs:
        errs.append("no tasks: pass a spec with `tasks`, or --task / --executor-task")
    if len(tasks) > caps["width"]:
        errs.append("%d tasks exceed width=%d (cap %d): raise --width or split into rounds" % (
            len(tasks), caps["width"], core.HARD_CAPS["width"][1]))
    scouts = [t for t in tasks if t["role"] == "scout"]
    executors = [t for t in tasks if t["role"] == "executor"]
    can_write = bool(args.allow_write or spec.get("allow_write"))
    errs.extend(check_disjoint(cwd, executors))
    if args.worktree and executors:
        rc, _ = git(cwd, "rev-parse", "--is-inside-work-tree")
        if rc != 0:
            errs.append("--worktree needs a git repository at %s" % cwd)
    if errs:
        for e in errs:
            log("spec error: %s" % e, force=True)
        return EXIT_SPEC

    codex_bin = args.codex_bin or os.environ.get("CODEX_BIN") or shutil.which("codex") or os.path.expanduser("~/.local/bin/codex")
    if not args.dry_run and not os.path.exists(codex_bin):
        log("codex binary not found (%s); use --codex-bin" % codex_bin, force=True)
        return EXIT_SPEC
    out_dir = args.out_dir or os.path.join(os.environ.get("ULTRACODE_RUNS_DIR") or os.path.join(tempfile.gettempdir(), "ultracode-runs"),
                                           "%s-%d" % (time.strftime("%Y%m%d-%H%M%S"), os.getpid()))
    os.makedirs(out_dir, exist_ok=True)
    ctx = Ctx(args, out_dir, codex_bin)
    if (args.isolate_home or os.environ.get("ULTRACODE_ISOLATE_HOME") in ("1", "true", "yes")) and not args.dry_run:
        iso_home, iso_notes = make_isolated_home()
        ctx.child_env["CODEX_HOME"] = iso_home
        log("children use an isolated CODEX_HOME: %s (removed after the run)" % iso_home, force=True)
        for n in iso_notes:
            log("warning: %s" % n, force=True)

        def _cleanup_home(h=iso_home):
            try:
                log(isolated_home_report(h), force=True)
            except Exception:  # pragma: no cover - best effort
                pass
            shutil.rmtree(h, ignore_errors=True)

        atexit.register(_cleanup_home)
    started = time.time()
    goal = str(spec.get("goal") or "")
    context = str(spec.get("context") or "")
    words = max(80, caps["output_cap"] // 7)

    log("parent model=%s [%s] effort=%s [%s]" % (parent["model"] or "(default)", parent["model_method"],
                                                parent["effort"] or "(default)", parent["effort_method"]), force=True)
    for r in core.CHILD_ROLES:
        rr = roles[r]
        log("role %-11s model=%s%s effort=%s%s" % (r, rr["model"] or "(default)", "*" if rr["inherited_model"] else "",
                                                  rr["effort"] or "(default)", "*" if rr["inherited_effort"] else ""), force=True)
        for w in rr["warnings"]:
            log("warning [%s]: %s" % (r, w), force=True)
    for w in warns:
        log("warning: %s" % w, force=True)
    _gp = core.gate_plan(settings, cwd, can_write)
    log("verify_mode=%s%s; final gate: %s%s" % (
        vmode_for_log(args, spec, settings), " (serial)" if settings["options"].get("verify_serial") else "",
        ("tests `%s`" % _gp["test_command"]) if _gp["kind"] == "tests" else _gp["kind"],
        (" [%s]" % _gp["note"]) if _gp.get("note") else ""), force=True)
    if os.environ.get("CODEX_SANDBOX") and not args.dry_run:
        log("note: running inside a Codex sandbox (CODEX_SANDBOX=%s); codex exec children usually need this "
            "command to run escalated/unsandboxed" % os.environ.get("CODEX_SANDBOX"), force=True)
    log("run dir: %s" % out_dir, force=True)

    def job_for(t, role, prompt, schema, sandbox, jcwd=None):
        rr = roles[role]
        model = t.get("model") or rr["model"]
        eff = core.normalize_effort(t.get("effort")) if t.get("effort") else rr["effort"]
        if eff == "inherit":
            eff = parent["effort"]
        eff, note = core.clamp_effort(eff, model or parent["model"], catalog)
        if note:
            log("warning [%s]: %s" % (t["id"], note), force=True)
        return {"id": t["id"], "role": role, "prompt": prompt, "schema": schema, "sandbox": sandbox,
                "model": model, "effort": eff, "cwd": jcwd or cwd, "timeout": caps["timeout_sec"], "cap": caps["output_cap"]}

    # ---- stage 1: scouts (read-only)
    scout_jobs = [job_for(t, "scout", scout_prompt(goal, context, t, words), SCOUT_SCHEMA, "read-only") for t in scouts]
    scout_res = run_stage(ctx, "scout", scout_jobs, caps["concurrency"])
    by_id = {r["id"]: r for r in scout_res}

    votes = 0 if args.no_verify or spec.get("verify") is False else caps["votes"]
    vmode = "off" if args.no_verify or spec.get("verify") is False else settings["options"]["verify_mode"]
    verification = []  # type: List[Dict[str, Any]]
    budget = [caps["max_verify_items"]]

    def verify_stage(name, items):
        """V read-only skeptics per item (skeptic role: big model), prompted to REFUTE; majority of valid votes kills."""
        if votes <= 0 or not items or ABORT.is_set():
            return []
        take, rest = items[: budget[0]], items[budget[0]:]
        budget[0] -= len(take)
        out = []
        vjobs = []
        for it in take:
            for k in range(1, votes + 1):
                vjobs.append(job_for({"id": "%s.v%d" % (it["id"], k)}, "skeptic",
                                     skeptic_prompt(goal, it, k, votes), VERDICT_SCHEMA, "read-only"))
        if args.dry_run:
            log("dry-run: would verify %d item(s) x %d skeptic(s)" % (len(take), votes), force=True)
        vres = run_stage(ctx, name, vjobs, caps["concurrency"])
        vmap = {}  # type: Dict[str, List[Dict[str, Any]]]
        for v in vres:
            vmap.setdefault(v["id"].rsplit(".v", 1)[0], []).append(v)
        # One re-vote round: every abstaining / 0-command / failed skeptic gets ONE fresh replacement,
        # so a single flaky child does not leave an item below quorum.
        if not args.dry_run and not args.no_revote and not ABORT.is_set():
            item_by_id = {it["id"]: it for it in take}
            rjobs = []
            for iid, vs in vmap.items():
                for v in vs:
                    if vote_of(v)["refuted"] is None and v.get("status") != "dry-run":
                        k = int(re.sub(r"\D", "", v["id"].rsplit(".v", 1)[1]) or 1)
                        rjobs.append(job_for({"id": "%s.v%dr" % (iid, k)}, "skeptic",
                                             skeptic_prompt(goal, item_by_id[iid], k, votes, retry=True), VERDICT_SCHEMA, "read-only"))
            if rjobs:
                log("%s: re-voting %d abstained/invalid skeptic(s)" % (name, len(rjobs)), force=True)
                for v in run_stage(ctx, name + "-revote", rjobs, caps["concurrency"]):
                    vmap.setdefault(v["id"].rsplit(".v", 1)[0], []).append(v)
        for it in take:
            out.append(tally(it, vmap.get(it["id"], []), votes_requested=votes))
        for it in rest:
            out.append({"item_id": it["id"], "source": it["source"], "kind": it["kind"], "claim": it.get("claim", ""),
                        "verdict": "unverified", "refuted_votes": 0, "valid_votes": 0, "votes": [],
                        "note": "over max_verify_items cap"})
        log("%s: %d item(s): %d killed, %d confirmed, %d contested, %d unverified" % (
            name, len(out), sum(1 for v in out if v["verdict"] == "killed"),
            sum(1 for v in out if v["verdict"] == "confirmed"), sum(1 for v in out if v["verdict"] == "contested"),
            sum(1 for v in out if v["verdict"] == "unverified")), force=True)
        return out

    # ---- stage 2: verify scout findings BEFORE anything edits the tree
    s_items = []  # type: List[Dict[str, Any]]
    for t in scouts:
        r = by_id.get(t["id"]) or {}
        if r.get("status") == "ok" and t.get("verify") is not False:
            s_items.extend(scout_items(r, t))
    verification.extend(verify_stage("verify-scouts", s_items))
    verdict_of = {v["item_id"]: v for v in verification}

    def enforce_exec_scope(guard, changed_paths, results):
        """In-place executors: revert every path changed outside ALL executors' declared files.

        Same machinery as the verifier stage (ScopeGuard snapshot / git). Attribution uses each
        child's file_change events; a single executor gets every unattributed path. An edit
        inside ANOTHER executor's set is flagged but not reverted (it is mixed with the owner's
        change; that owner's verifier reviews the file). When some executor declared no files
        its scope is unknown, so nothing is reverted (warning only).
        """
        tmap = {t["id"]: t for t in executors}
        if any(not t["files"] for t in executors):
            if len(executors) > 1:
                union = [f for t in executors for f in t["files"]]
                new = sorted(p for p in changed_paths if union and not owned_by(p, union))
                if new:
                    log("warning: files changed outside the declared files (an executor without `files` makes the "
                        "scope unknown; not reverted): %s" % ", ".join(new), force=True)
                    for r in results:
                        r["scope_violations"] = r.get("scope_violations", []) + ["(unattributed, not reverted) " + p for p in new]
            return
        union = [f for t in executors for f in t["files"]]
        for path in sorted(changed_paths):
            editors = [r for r in results if path in (r.get("file_changes") or [])]
            if owned_by(path, union):
                for r in editors:
                    if not owned_by(path, tmap[r["id"]]["files"]):
                        owner = [t["id"] for t in executors if owned_by(path, t["files"])]
                        r["scope_violations"] = r.get("scope_violations", []) + [
                            "%s (owned by %s; not reverted, its verifier reviews it)" % (path, ", ".join(owner))]
                        log("executor: %s edited %s owned by %s (flagged)" % (r["id"], path, ", ".join(owner)), force=True)
                continue
            err = guard.restore(path)
            tag = "reverted" if not err else "NOT reverted (%s)" % err
            targets = editors or (results if len(results) == 1 else [])
            for r in (targets or results):
                r["scope_violations"] = r.get("scope_violations", []) + [
                    "%s%s (outside its files; %s)" % ("" if targets else "(unattributed) ", path, tag)]
                if not err:
                    r.setdefault("reverted", []).append(path)
            log("executor: edit outside every executor's file set: %s -> %s" % (path, tag), force=True)

    # ---- stage 3: executors (disjoint files)
    exec_res = []  # type: List[Dict[str, Any]]
    exec_meta = {}  # type: Dict[str, Dict[str, Any]]
    if executors and not ABORT.is_set():
        digest_parts = []
        refuted_lines = []  # killed claims + skeptics' reasons: the refutation often names the real bug
        for t in scouts:
            r = by_id.get(t["id"]) or {}
            p = r.get("parsed")
            if isinstance(p, dict) and isinstance(p.get("findings"), list):
                lines = ["[%s] %s" % (t["id"], p.get("summary", ""))]
                for n, f in enumerate(p["findings"], 1):
                    if not isinstance(f, dict):
                        continue
                    v = verdict_of.get("%s.f%d" % (t["id"], n))
                    if v and v["verdict"] == "killed":
                        # refuted by a majority of skeptics: never hand the claim over as true, but pass the
                        # reasons on (e.g. refuting "is_even() is correct" shows is_even IS buggy)
                        why = "; ".join(c["reason"] for c in v["votes"] if c.get("refuted"))
                        refuted_lines.append("  - [REFUTED %d/%d] claim: %s\n    skeptics: %s" % (
                            v["refuted_votes"], v["valid_votes"], one_line(f.get("claim", ""), 200), one_line(why, 600)))
                        continue
                    ev = "; ".join("%s:%s" % (e.get("file"), e.get("line")) for e in (f.get("evidence") or []) if isinstance(e, dict))
                    lines.append("  - [%s] %s (%s)" % ((v["verdict"] if v else "unverified").upper(), f.get("claim", ""), ev))
                digest_parts.append("\n".join(lines))
            elif r.get("text"):
                v = verdict_of.get("%s.r" % t["id"])
                if v and v["verdict"] == "killed":
                    why = "; ".join(c["reason"] for c in v["votes"] if c.get("refuted"))
                    refuted_lines.append("  - [REFUTED] report of %s\n    skeptics: %s" % (t["id"], one_line(why, 600)))
                    continue
                digest_parts.append("[%s] (%s) %s" % (t["id"], (v["verdict"] if v else "unverified").upper(), r["text"]))
        if refuted_lines:
            digest_parts.append("REFUTED CLAIMS (rejected by skeptics: do NOT act on the claim itself; the skeptics' "
                                "reasons may point at the real issue - verify in the code before relying on them):\n"
                                + "\n".join(refuted_lines))
        digest, _ = cap_text("\n\n".join(digest_parts), 12000)
        exec_jobs = []
        # In-place executors: watch the whole tree (minus the run dir) so edits outside every
        # executor's declared files can be reverted, exactly like the verifier stage.
        eguard = ScopeGuard(cwd, exclude=[out_dir]) if (can_write and not args.worktree and not args.dry_run) else None
        for t in executors:
            sandbox = "workspace-write" if can_write else "read-only"
            jcwd = cwd
            meta = {"files": t["files"], "worktree": None}
            if can_write and args.worktree and not args.dry_run:
                rc, top = git(cwd, "rev-parse", "--show-toplevel")
                root = top.strip()
                wt = os.path.join(out_dir, "worktrees", t["id"])
                rc, out = git(root, "worktree", "add", "--detach", wt, "HEAD")
                if rc != 0:
                    log("worktree add failed for %s: %s" % (t["id"], one_line(out, 200)), force=True)
                    return EXIT_FAIL
                jcwd = os.path.join(wt, os.path.relpath(cwd, root))
                meta.update({"worktree": wt, "root": root})
            meta["before"] = snapshot(jcwd, t["files"]) if (can_write and t["files"]) else {}
            meta["cwd"] = jcwd
            exec_meta[t["id"]] = meta
            exec_jobs.append(job_for(t, "executor", executor_prompt(goal, context, t, digest, can_write, words),
                                     EXECUTOR_SCHEMA, sandbox, jcwd))
        if can_write and args.worktree:
            dirty = [p for p in (git_changed(cwd) or set()) if not ScopeGuard.skip_rel(p, [out_dir], cwd)]
            if dirty:
                log("warning: worktrees start from HEAD; uncommitted changes in %s are not visible to executors" % cwd, force=True)
        exec_res = run_stage(ctx, "executor", exec_jobs, caps["concurrency"])
        # collect diffs + scope checks
        eguard_changed = eguard.changed() if eguard is not None else None
        for r in exec_res:
            meta = exec_meta[r["id"]]
            t = next(x for x in executors if x["id"] == r["id"])
            r["files"] = t["files"]
            diff, changed = "", []
            if meta.get("worktree"):
                wt = meta["worktree"]
                git(wt, "add", "-A")
                _, full_diff = git(wt, "diff", "--cached", "--binary", "HEAD")
                _, names = git(wt, "diff", "--cached", "--name-only", "HEAD")
                rel_prefix = os.path.relpath(cwd, meta["root"])
                changed, root_of = [], {}
                for n in names.splitlines():
                    n = _norm(n)
                    if not n:
                        continue
                    root_n = n
                    if rel_prefix not in (".", "") and n.startswith(rel_prefix + "/"):
                        n = n[len(rel_prefix) + 1:]
                    if ARTIFACT_RE.search(n):
                        continue  # build/test byproducts are never applied
                    changed.append(n)
                    root_of[n] = root_n
                viol = [c for c in changed if t["files"] and not owned_by(c, t["files"])]
                keep = [c for c in changed if c not in viol]
                diff = full_diff
                if viol or len(keep) != len(names.splitlines()):
                    # Out-of-set paths are DROPPED (same rule as the verifier: edits outside the
                    # set never land); the in-set part of the change is still applied.
                    diff = git(wt, "diff", "--cached", "--binary", "HEAD", "--", *[root_of[c] for c in keep])[1] if keep else ""
                    if viol:
                        fpath = os.path.join(out_dir, "%s.full.patch" % r["id"])
                        with open(fpath, "w", encoding="utf-8") as fh:
                            fh.write(full_diff)
                        r["full_patch"] = fpath
                patch = os.path.join(out_dir, "%s.patch" % r["id"])
                with open(patch, "w", encoding="utf-8") as fh:
                    fh.write(diff)
                r["patch"] = patch
                r["scope_violations"] = ["%s (outside its files; dropped from the patch)" % c for c in viol]
                r["dropped_paths"] = viol
                changed = keep
                if diff.strip() and not args.no_apply:
                    rc, out = git(meta["root"], "apply", "--whitespace=nowarn", patch)
                    if rc != 0:
                        rc, out = git(meta["root"], "apply", "--3way", patch)
                    r["applied"] = rc == 0
                    if rc != 0:
                        r["change_lost"] = "patch did not apply: %s" % one_line(out, 200)
                    log("%s patch %s%s" % (r["id"], "applied" if rc == 0 else "NOT applied: " + one_line(out, 160),
                                           (" WITHOUT out-of-set path(s) %s (dropped)" % ", ".join(viol)) if viol else ""),
                        force=True)
                elif viol and not args.no_apply:
                    r["applied"] = False
                    r["change_lost"] = "every changed path is outside its files (%s); nothing applied" % ", ".join(viol)
                    log("%s patch NOT applied: every changed path is outside its files (%s)" % (r["id"], ", ".join(viol)),
                        force=True)
                if not diff.strip():
                    changed = []
                git(meta["root"], "worktree", "remove", "--force", wt)
            elif can_write:
                if t["files"]:
                    diff, changed = diff_snapshots(meta["cwd"], meta["before"], t["files"])
                elif eguard_changed is not None and len(executors) == 1:
                    # single executor without declared files: its change set is what changed on disk
                    changed = sorted(eguard_changed)
                    diff = git_paths_diff(cwd, changed) if eguard.mode == "git" else ""
                r.setdefault("scope_violations", [])
            else:
                p = r.get("parsed")
                diff = (p or {}).get("diff", "") if isinstance(p, dict) else ""
                r["scope_violations"] = []
                r["proposal"] = True  # dry-run: the diff is NOT applied, the workspace keeps the original code
                if diff.strip():
                    chk = os.path.join(out_dir, "%s.check.diff" % r["id"])
                    with open(chk, "w", encoding="utf-8") as fh:
                        fh.write(diff if diff.endswith("\n") else diff + "\n")
                    rc, out = git(cwd, "apply", "--check", "--whitespace=nowarn", chk)
                    r["diff_check"] = "git apply --check: ok" if rc == 0 else "git apply --check FAILED: " + one_line(out, 200)
            r["files_changed"] = changed
            if diff:
                dpath = os.path.join(out_dir, "%s.diff" % r["id"])
                with open(dpath, "w", encoding="utf-8") as fh:
                    fh.write(diff)
                r["diff_path"] = dpath
            r["_diff"] = diff
        if eguard_changed:
            enforce_exec_scope(eguard, eguard_changed, exec_res)
            for r in exec_res:
                if r.get("reverted") and not r.get("files_changed") and r.get("status") == "ok":
                    r["change_lost"] = "every edit was outside its files and was reverted (%s)" % ", ".join(r["reverted"])
        lost = [r for r in exec_res if r.get("change_lost")]
        for r in lost:
            log("executor %s: change NOT applied - %s" % (r["id"], r["change_lost"]), force=True)

    # ---- stage 4: review executor changes (after ALL executors finished: a verifier never runs
    # concurrently with the executor whose files it reviews)
    task_by_id = {t["id"]: t for t in executors}
    reviews = []  # type: List[Dict[str, Any]]

    def allowed_for(r):
        t = task_by_id[r["id"]]
        return list(t["files"] or r.get("files_changed") or [])

    def current_change(r):
        """Diff of this executor's set against the state before the executor ran (incl. verifier fixes)."""
        t = task_by_id[r["id"]]
        meta = exec_meta.get(r["id"]) or {}
        if r.get("proposal") or not can_write:
            return r.get("_diff", "")
        if t["files"] and meta.get("before") is not None:
            return diff_snapshots(cwd, meta["before"], t["files"])[0]
        if r.get("files_changed"):
            return git_paths_diff(cwd, r["files_changed"])
        return r.get("_diff", "")

    def review_target(r):
        """(ok, write, why-not). Write access only for an applied change with a known file set."""
        t = task_by_id[r["id"]]
        if r.get("status") != "ok" and not (args.dry_run and r.get("status") == "dry-run"):
            return False, False, "executor status %s" % r.get("status")
        if t.get("verify") is False:
            return False, False, "verify disabled for this task"
        if r.get("change_lost"):
            return False, False, "executor change not applied (%s)" % r["change_lost"]
        if exec_meta.get(r["id"], {}).get("worktree") and not r.get("applied"):
            return False, False, "worktree patch not applied"
        write = bool(can_write and not r.get("proposal") and allowed_for(r))
        return True, write, ""

    def verifier_stage(name, targets, round_no, failure_text=""):
        """One verifier per executor: review + advise + fix, writes limited to that executor's set."""
        if not targets or ABORT.is_set():
            return []
        entries = []
        for r in targets:
            ok, write, _ = review_target(r)
            if not ok:
                continue
            allowed = allowed_for(r)
            p = r.get("parsed") if isinstance(r.get("parsed"), dict) else {}
            dtext, _ = cap_text(current_change(r), 14000)
            scope = ", ".join(r.get("scope_violations") or []) or "none"
            if r.get("proposal"):
                scope += "; " + (r.get("diff_check") or "git apply --check: not run")
            jid = "%s.verify%s" % (r["id"], "" if round_no == 1 else str(round_no))
            entries.append({"r": r, "id": jid, "allowed": allowed, "write": write,
                            "args": (goal, context, task_by_id[r["id"]], p.get("summary") or one_line(r.get("text", ""), 400),
                                     dtext, allowed, write, scope, round_no, failure_text)})
        if not entries:
            return []

        def make_job(e, retry=False):
            prompt = verifier_prompt(*(e["args"] + (retry, words)))
            return job_for({"id": e["id"] + ("r" if retry else "")}, "verifier", prompt, VERIFIER_SCHEMA,
                           "workspace-write" if e["write"] else "read-only")

        def run_batch(batch, retry=False):
            for e in batch:
                e["vbefore"] = snapshot(cwd, e["allowed"]) if (e["write"] and not args.dry_run) else {}
            guard = ScopeGuard(cwd, exclude=[out_dir]) if (any(e["write"] for e in batch) and not args.dry_run) else None
            res = run_stage(ctx, name + ("-retry" if retry else ""), [make_job(e, retry) for e in batch],
                            1 if len(batch) == 1 else caps["concurrency"])
            for e, x in zip(batch, res):
                e["res"] = x
                e["violations"], e["reverted"], e["notes"] = [], [], []
            if guard is not None:
                enforce_scope(guard, batch)
            for e in batch:
                if e["write"] and not args.dry_run:
                    e["fix_diff"], e["fix_files"] = diff_snapshots(cwd, e["vbefore"], e["allowed"])
                else:
                    e["fix_diff"], e["fix_files"] = "", []

        def enforce_scope(guard, batch):
            writers = [e for e in batch if e["write"]]
            single = len(batch) == 1
            for path in sorted(guard.changed()):
                if any(owned_by(path, e["allowed"]) for e in writers):
                    continue
                editors = [e for e in batch if path in (e["res"].get("file_changes") or [])]
                if not editors and single:
                    editors = batch
                err = guard.restore(path)
                tag = "reverted" if not err else "NOT reverted (%s)" % err
                for e in (editors or writers):
                    e["violations"].append("%s%s (outside its files; %s)" % ("" if editors else "(unattributed) ", path, tag))
                    if not err:
                        e["reverted"].append(path)
                log("%s: edit outside every verifier's file set: %s -> %s" % (name, path, tag), force=True)
            for e in batch:
                for path in sorted(set(e["res"].get("file_changes") or [])):
                    if owned_by(path, e["allowed"]) or ARTIFACT_RE.search(path):
                        continue
                    owners = [o for o in writers if o is not e and owned_by(path, o["allowed"])]
                    if not owners:
                        if not any(path in v for v in e["violations"]):
                            e["violations"].append("%s (reported edit outside its files; not seen on disk or not watched)" % path)
                        continue
                    err = guard.restore(path)  # back to the pre-verifier state (drops the owner's fixes too)
                    for o in owners:
                        o["notes"].append("fixes to %s discarded: verifier %s also edited it (re-run with --serial-verify)"
                                          % (path, e["id"]))
                    e["violations"].append("%s (owned by %s; %s)" % (
                        path, ", ".join(o["r"]["id"] for o in owners),
                        "reverted to the pre-verifier state" if not err else "NOT reverted: " + err))
                    if not err:
                        e["reverted"].append(path)
                    log("%s: %s edited %s owned by another executor -> %s" % (
                        name, e["id"], path, "reverted" if not err else "NOT reverted: " + err), force=True)

        serial = settings["options"].get("verify_serial")
        if serial:
            for e in entries:
                run_batch([e])
        else:
            run_batch(entries)
        if not args.dry_run and not args.no_revote and not ABORT.is_set():
            again = [e for e in entries if e["res"].get("status") == "ok" and not e["res"].get("commands")
                     and not e["fix_files"]]
            if again:
                log("%s: re-running %d verifier(s) that inspected nothing" % (name, len(again)), force=True)
                if serial:
                    for e in again:
                        run_batch([e], retry=True)
                else:
                    run_batch(again, retry=True)

        out = []
        for e in entries:
            x, r = e["res"], e["r"]
            p = x.get("parsed") if isinstance(x.get("parsed"), dict) else None
            claimed = p.get("verdict") if p else None
            if x.get("status") == "dry-run":
                verdict = "dry-run"
            elif x.get("status") != "ok":
                verdict = "error"
            elif claimed not in ("ok", "fixed", "needs_work", "reject"):
                verdict = "unparsable"
            elif not x.get("commands") and not e["fix_files"]:
                verdict = "unreviewed"
            else:
                verdict = claimed
            notes = list(e["notes"])
            if verdict == "ok" and e["fix_files"]:
                verdict = "fixed"
                notes.append("verifier said ok but changed %s; counted as fixed" % ", ".join(e["fix_files"]))
            pd = (p or {}).get("proposed_diff") or ""
            if verdict == "fixed" and e["write"] and not e["fix_files"]:
                # a claimed fix that is not on disk is not a fix (it may also have been reverted as out of scope)
                verdict = "needs_work"
                notes.append("verifier claimed 'fixed' but nothing changed in its files on disk; downgraded to needs_work")
            elif verdict == "fixed" and not e["write"] and not pd.strip():
                verdict = "needs_work"
                notes.append("read-only verifier claimed 'fixed' but returned no proposed_diff; downgraded to needs_work")
            if verdict == "unreviewed":
                notes.append("ran 0 inspection commands; its verdict (%s) was discarded" % claimed)
            rv = {"id": x["id"], "executor": r["id"], "round": round_no, "model": x.get("model"), "effort": x.get("effort"),
                  "status": x.get("status"), "error": x.get("error"), "verdict": verdict, "claimed_verdict": claimed,
                  "write": e["write"], "allowed_files": e["allowed"], "commands": x.get("commands", 0),
                  "issues": (p or {}).get("issues") or [], "fixes_summary": (p or {}).get("fixes_summary", ""),
                  "advice_executor": (p or {}).get("advice_executor", ""),
                  "advice_orchestrator": (p or {}).get("advice_orchestrator", ""),
                  "tests_run": (p or {}).get("tests_run") or [], "fix_files": e["fix_files"],
                  "violations": e["violations"], "reverted": e["reverted"], "notes": notes,
                  "duration_sec": x.get("duration_sec", 0)}
            if e["fix_diff"]:
                dpath = os.path.join(out_dir, "%s.fix.diff" % x["id"])
                with open(dpath, "w", encoding="utf-8") as fh:
                    fh.write(e["fix_diff"])
                rv["fix_diff_path"] = dpath
                rv["fix_diff"] = cap_text(e["fix_diff"], caps["output_cap"])[0]
            if not e["write"] and pd.strip():
                chk = os.path.join(out_dir, "%s.proposed.diff" % x["id"])
                with open(chk, "w", encoding="utf-8") as fh:
                    fh.write(pd if pd.endswith("\n") else pd + "\n")
                rc, o = git(cwd, "apply", "--check", "--whitespace=nowarn", chk)
                rv["proposed_diff_path"] = chk
                rv["proposed_diff_check"] = "git apply --check: ok" if rc == 0 else "git apply --check FAILED: " + one_line(o, 200)
                r["_proposed_fix"] = pd
            r.setdefault("reviews", []).append(rv)
            out.append(rv)
        log("%s: %d review(s): %s" % (name, len(out), ", ".join("%s=%s%s" % (
            v["executor"], v["verdict"], (" (%d fix file(s))" % len(v["fix_files"])) if v["fix_files"] else "") for v in out)),
            force=True)
        return out

    if vmode == "fix":
        reviews.extend(verifier_stage("verify-fix", exec_res, 1))
    elif vmode == "vote":
        e_items = []  # type: List[Dict[str, Any]]
        for r in exec_res:
            t = task_by_id[r["id"]]
            if r.get("status") != "ok" or t.get("verify") is False:
                continue
            p = r.get("parsed") if isinstance(r.get("parsed"), dict) else {}
            dtext, _ = cap_text(r.get("_diff", ""), 14000)
            if r.get("proposal"):
                state_note = ("STATE: PROPOSED diff only (dry-run, read-only executor). It is NOT applied: the files in the\n"
                              "workspace still contain the ORIGINAL code. Judge whether APPLYING this diff to the current files\n"
                              "would correctly and completely do the task. 'The fix is not in the file' is NOT a valid refutation.\n"
                              "%s\n" % (r.get("diff_check") or "git apply --check: not run"))
                kind = "proposed code change (not applied)"
            else:
                state_note = "STATE: the change below IS applied in the workspace; inspect the current files.\n"
                kind = "code change"
            e_items.append({"id": "%s.change" % r["id"], "source": r["id"], "kind": kind,
                            "claim": one_line(p.get("summary") or r.get("text", ""), 200),
                            "text": "%sTASK: %s\nALLOWED FILES: %s\nEXECUTOR SUMMARY: %s\nSCOPE VIOLATIONS: %s\nDIFF:\n%s" % (
                                state_note, t["task"], ", ".join(t["files"]) or "(not declared)", p.get("summary") or r.get("text", ""),
                                ", ".join(r.get("scope_violations") or []) or "none", dtext or "(no diff captured)")})
        verification.extend(verify_stage("verify-changes", e_items))

    # ---- stage 5: final gate (a fixer approving its own fixes is not independent)
    plan = core.gate_plan(settings, cwd, can_write)
    lost = [r for r in exec_res if r.get("change_lost")]
    gate = {"kind": plan["kind"], "mode": plan["mode"], "status": "skipped", "test_command": plan.get("test_command"),
            "test_source": plan.get("test_source"), "note": plan.get("note"), "test_runs": [], "fix_rounds": 0,
            "review": None}  # type: Dict[str, Any]
    if args.dry_run:
        has_change = [r for r in exec_res if r.get("status") == "dry-run"]
    else:
        has_change = [r for r in exec_res if r.get("status") == "ok" and (current_change(r).strip() or r.get("_proposed_fix"))]
    if plan["kind"] == "off":
        gate["status"] = "off"
    elif not executors:
        gate["status"] = "skipped"
        gate["note"] = "no executor tasks"
    elif ABORT.is_set():
        gate["note"] = "run aborted"
    elif not has_change:
        gate["note"] = "no changes to check"
        if lost:
            gate["note"] += "; executor change(s) NOT applied: %s" % ", ".join(r["id"] for r in lost)
    elif args.dry_run:
        gate["status"] = "dry-run"
        log("dry-run: final gate would %s" % (("run tests: %s" % plan["test_command"]) if plan["kind"] == "tests"
                                             else "run a read-only review (synthesizer role)"), force=True)
    elif plan["kind"] == "tests":
        tr = run_tests(plan["test_command"], cwd, caps["timeout_sec"], caps["output_cap"],
                       os.path.join(out_dir, "gate-tests-1.log"))
        gate["test_runs"].append(tr)
        while not tr["passed"] and gate["fix_rounds"] < caps["gate_retries"] and vmode == "fix" and not ABORT.is_set():
            targets = [r for r in exec_res if review_target(r)[1]]
            if not targets:
                gate["note"] = (gate.get("note") or "") + "; no writable verifier target for a fix round"
                break
            gate["fix_rounds"] += 1
            log("gate: tests failed -> verifier fix round %d/%d" % (gate["fix_rounds"], caps["gate_retries"]), force=True)
            reviews.extend(verifier_stage("gate-fix-%d" % gate["fix_rounds"], targets, gate["fix_rounds"] + 1,
                                          failure_text=tr["output_tail"]))
            tr = run_tests(plan["test_command"], cwd, caps["timeout_sec"], caps["output_cap"],
                           os.path.join(out_dir, "gate-tests-%d.log" % (gate["fix_rounds"] + 1)))
            gate["test_runs"].append(tr)
        gate["status"] = "passed" if tr["passed"] else "failed"
    else:
        parts = []
        for r in has_change:
            t = task_by_id[r["id"]]
            rv = (r.get("reviews") or [None])[-1]
            parts.append("### executor %s\nTASK: %s\nFILES: %s\nVERIFIER: %s" % (
                r["id"], t["task"], ", ".join(allowed_for(r)) or "(not declared)",
                ("%s; issues: %s; fixes: %s" % (rv["verdict"], "; ".join(one_line(str(i.get("description")), 160)
                                                                      for i in rv["issues"] if isinstance(i, dict)) or "none",
                                                rv["fixes_summary"] or "none")) if rv else "none"))
            if r.get("_proposed_fix"):
                parts.append("COMBINED PROPOSED DIFF (verifier-corrected; replaces the executor's diff):\n%s" % r["_proposed_fix"])
            else:
                parts.append("COMBINED DIFF (executor + verifier fixes):\n%s" % current_change(r))
        sections, _ = cap_text("\n\n".join(parts), 40000)
        applied = bool(can_write and not any(r.get("proposal") for r in has_change))

        def gate_job(retry=False):
            j = job_for({"id": "gate-review" + ("-r" if retry else ""),
                         "model": roles["synthesizer"].get("gate_model"),
                         "effort": roles["synthesizer"].get("gate_effort")}, "synthesizer",
                        ("NOTE: the previous gate reviewer ran no command; you MUST inspect the code first.\n" if retry else "")
                        + gate_review_prompt(goal, sections, applied), GATE_SCHEMA, "read-only")
            return j

        g = run_stage(ctx, "gate-review", [gate_job()], 1)[0]
        if g.get("status") == "ok" and not g.get("commands") and not args.no_revote and not ABORT.is_set():
            g = run_stage(ctx, "gate-review-retry", [gate_job(True)], 1)[0]
        p = g.get("parsed") if isinstance(g.get("parsed"), dict) else None
        verdict = (p or {}).get("verdict")
        gate["review"] = {"id": g["id"], "model": g.get("model"), "effort": g.get("effort"), "status": g.get("status"),
                          "error": g.get("error"), "commands": g.get("commands", 0), "verdict": verdict,
                          "summary": (p or {}).get("summary", ""), "issues": (p or {}).get("issues") or []}
        if g.get("status") != "ok":
            gate["status"] = "error"
        elif verdict not in ("pass", "concerns", "fail"):
            gate["status"] = "unverified"
            gate["note"] = "gate reviewer gave no parsable verdict"
        elif not g.get("commands"):
            gate["status"] = "unverified"
            gate["note"] = "gate reviewer ran no inspection command; verdict %s discarded" % verdict
        else:
            gate["status"] = {"pass": "passed", "concerns": "concerns", "fail": "failed"}[verdict]
    if gate["status"] not in ("off", "skipped", "dry-run"):
        log("final gate: %s%s" % (gate["status"].upper(), (" (%s)" % gate["note"]) if gate.get("note") else ""), force=True)

    # ---- stage 6: synthesis
    synthesis = {"status": "skipped", "text": ""}  # type: Dict[str, Any]
    if not args.no_synth and spec.get("synthesize", True) is not False and not ABORT.is_set() and (scout_res or exec_res):
        verdict_of = {}
        for v in verification:
            verdict_of[v["item_id"]] = v
        parts = []
        for t in scouts:
            r = by_id.get(t["id"]) or {}
            parts.append("### scout %s (%s)\nTASK: %s" % (t["id"], r.get("status"), t["task"]))
            p = r.get("parsed")
            if isinstance(p, dict) and isinstance(p.get("findings"), list):
                parts.append("SUMMARY: %s" % p.get("summary", ""))
                for n, f in enumerate(p["findings"], 1):
                    if not isinstance(f, dict):
                        continue
                    v = verdict_of.get("%s.f%d" % (t["id"], n))
                    tag = (v["verdict"].upper() + " %d/%d refuted" % (v["refuted_votes"], v["valid_votes"])) if v else "UNVERIFIED (not sent to skeptics)"
                    if v and v["verdict"] == "unverified":
                        tag = "UNVERIFIED (%s)" % (v.get("note") or "no valid votes")
                    reason = ""
                    if v and v["verdict"] in ("killed", "contested"):
                        reason = " | skeptic: " + "; ".join(c["reason"] for c in v["votes"] if c.get("refuted"))[:400]
                    ev = "; ".join("%s:%s" % (e.get("file"), e.get("line")) for e in (f.get("evidence") or []) if isinstance(e, dict))
                    parts.append("- [%s] %s (%s)%s" % (tag, f.get("claim", ""), ev, reason))
                if p.get("open_questions"):
                    parts.append("OPEN QUESTIONS: %s" % "; ".join(str(q) for q in p["open_questions"]))
            else:
                v = verdict_of.get("%s.r" % t["id"])
                parts.append("[%s]\n%s" % (v["verdict"].upper() if v else "UNVERIFIED", r.get("text") or r.get("error") or ""))
        for r in exec_res:
            v = verdict_of.get("%s.change" % r["id"])
            p = r.get("parsed") if isinstance(r.get("parsed"), dict) else {}
            rv = r.get("reviews") or []
            tag = ("VOTE " + v["verdict"].upper()) if v else (("REVIEWED: " + rv[-1]["verdict"].upper()) if rv else "UNREVIEWED")
            parts.append("### executor %s (%s) [%s]%s%s%s\nFILES CHANGED: %s\nSUMMARY: %s\nTESTS: %s%s" % (
                r["id"], r.get("status"), tag,
                (" PROPOSAL ONLY - diff not applied (%s)" % (r.get("diff_check") or "unchecked")) if r.get("proposal") else "",
                (" CHANGE NOT APPLIED - %s" % r["change_lost"]) if r.get("change_lost") else "",
                (" SCOPE VIOLATIONS: %s" % "; ".join(r["scope_violations"])) if r.get("scope_violations") else "",
                ", ".join(r.get("files_changed") or []) or "none", p.get("summary") or r.get("text") or r.get("error") or "",
                ", ".join(p.get("tests_run") or []) or "none",
                ("\nSKEPTICS: " + "; ".join(c["reason"] for c in v["votes"] if c.get("refuted"))[:600]) if v and v["refuted_votes"] else ""))
            for x in rv:
                parts.append("VERIFIER %s (round %d, %s/%s): %s | issues: %s | fixes: %s | fix files: %s%s | advice to "
                             "executor: %s | advice to orchestrator: %s%s" % (
                                 x["id"], x["round"], x.get("model") or "default", x.get("effort") or "default",
                                 x["verdict"].upper(),
                                 "; ".join("[%s] %s:%s %s%s" % (i.get("severity"), i.get("file"), i.get("line"),
                                                               one_line(str(i.get("description")), 200),
                                                               " (fixed)" if i.get("fixed") else "")
                                           for i in x["issues"] if isinstance(i, dict)) or "none",
                                 x["fixes_summary"] or "none", ", ".join(x["fix_files"]) or "none",
                                 (" (proposed fix diff: %s)" % x.get("proposed_diff_check")) if x.get("proposed_diff_check") else "",
                                 x["advice_executor"] or "-", x["advice_orchestrator"] or "-",
                                 (" | SCOPE VIOLATIONS: " + "; ".join(x["violations"])) if x["violations"] else ""))
        parts.append("### FINAL GATE: %s\n%s" % (gate["status"].upper(), gate_summary(gate)))
        sections, _ = cap_text("\n".join(parts), 40000)
        sjob = job_for({"id": "synth"}, "synthesizer", synth_prompt(goal, sections), None, "read-only")
        sjob["cap"] = max(caps["output_cap"] * 3, 12000)
        sres = run_stage(ctx, "synthesize", [sjob], 1)
        synthesis = {"status": sres[0].get("status"), "text": sres[0].get("text", ""), "error": sres[0].get("error")}

    # ---- outputs
    for r in exec_res:
        r.pop("_diff", None)
        r.pop("_proposed_fix", None)
    all_children = scout_res + exec_res
    result = {
        "version": 2,
        "goal": goal,
        "cwd": cwd,
        "out_dir": out_dir,
        "duration_sec": round(time.time() - started, 1),
        "aborted": ABORT.is_set(),
        "dry_run": bool(args.dry_run),
        "parent": parent,
        "roles": {k: {kk: vv for kk, vv in v.items() if kk != "warnings"} for k, v in roles.items()},
        "caps": caps,
        "options": settings["options"],
        "verify_mode": vmode,
        "warnings": warns + [w for r in core.CHILD_ROLES for w in roles[r]["warnings"]],
        "tasks": [{k: v for k, v in r.items() if (k != "cmd" or args.dry_run) and k != "reviews"} for r in all_children],
        "verification": verification,
        "reviews": reviews,
        "gate": gate,
        "synthesis": synthesis,
    }
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    md = render_markdown(result, roles, parent, settings)
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as fh:
        fh.write(md)
    log("wrote %s and %s" % (os.path.join(out_dir, "result.json"), os.path.join(out_dir, "report.md")), force=True)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        out, trunc = cap_text(md, 30000)
        print(out)
        if trunc:
            print("\n(full report: %s)" % os.path.join(out_dir, "report.md"))
    if ABORT.is_set():
        return EXIT_SANDBOX
    if all_children and all(r.get("status") not in ("ok", "dry-run") for r in all_children):
        return EXIT_FAIL
    if gate["status"] == "failed":
        return EXIT_GATE
    if lost:
        log("exit %d: executor change(s) not applied: %s" % (EXIT_FAIL, "; ".join(
            "%s (%s)" % (r["id"], r["change_lost"]) for r in lost)), force=True)
        return EXIT_FAIL
    return EXIT_OK


def vmode_for_log(args, spec: Dict[str, Any], settings: Dict[str, Any]) -> str:
    return "off" if args.no_verify or spec.get("verify") is False else settings["options"]["verify_mode"]


def gate_summary(gate: Dict[str, Any]) -> str:
    L = []
    if gate["kind"] == "tests" and gate["test_runs"]:
        L.append("tests: `%s` (%s); %d run(s), %d verifier fix round(s)" % (
            gate["test_command"], gate.get("test_source") or "", len(gate["test_runs"]), gate["fix_rounds"]))
        for n, tr in enumerate(gate["test_runs"], 1):
            L.append("- run %d: %s rc=%s %.1fs%s (log: %s)" % (n, "PASSED" if tr["passed"] else "FAILED", tr["rc"],
                                                               tr["duration_sec"], " TIMEOUT" if tr["timed_out"] else "",
                                                               tr.get("log")))
        last = gate["test_runs"][-1]
        if not last["passed"]:
            L.append("last test output (tail):\n```\n%s\n```" % cap_text(last["output_tail"], 3000)[0].strip())
    elif gate.get("review"):
        g = gate["review"]
        L.append("read-only review by %s (%s/%s, %d command(s)): verdict %s" % (
            g["id"], g.get("model") or "default", g.get("effort") or "default", g.get("commands", 0), g.get("verdict")))
        if g.get("summary"):
            L.append(g["summary"].strip())
        for i in g.get("issues") or []:
            if isinstance(i, dict):
                L.append("- [%s] %s:%s %s" % (i.get("severity"), i.get("file"), i.get("line"), one_line(str(i.get("description")), 300)))
    if gate.get("note") and gate.get("note") != gate.get("test_source"):
        L.append("note: %s" % gate["note"])
    return "\n".join(L) or "(nothing ran)"


def render_markdown(result: Dict[str, Any], roles: Dict[str, Any], parent: Dict[str, Any], settings: Dict[str, Any]) -> str:
    L = ["# ultracode run", ""]
    if result["goal"]:
        L += ["**Goal:** %s" % result["goal"], ""]
    if result["aborted"]:
        L += ["> **ABORTED:** codex exec children could not start (sandbox). Re-run escalated, or use native spawn_agent.", ""]
    L += ["```", core.roles_table(roles, parent, settings, result.get("cwd")), "```", ""]
    gate = result.get("gate") or {}
    if gate:
        L += ["**Final gate: %s**" % str(gate.get("status", "skipped")).upper(), ""]
    lost = [r for r in result["tasks"] if r.get("change_lost")]
    if lost:
        L += ["**Executor change(s) NOT applied:** %s" % "; ".join("%s (%s)" % (r["id"], r["change_lost"]) for r in lost), ""]
    if result["synthesis"].get("text"):
        L += ["## Synthesis", "", result["synthesis"]["text"].strip(), ""]
    L += ["## Tasks", "", "| id | role | status | model/effort | time | note |", "|---|---|---|---|---|---|"]
    for r in result["tasks"]:
        note = r.get("error") or ""
        if r.get("files_changed"):
            note = "changed: " + ", ".join(r["files_changed"])
        if r.get("scope_violations"):
            note += " SCOPE VIOLATION: " + ", ".join(r["scope_violations"])
        if r.get("change_lost"):
            note += " NOT APPLIED: " + r["change_lost"]
        L.append("| %s | %s | %s | %s/%s | %ss | %s |" % (r["id"], r["role"], r.get("status"), r.get("model") or "default",
                                                     r.get("effort") or "default", r.get("duration_sec", 0), one_line(note, 160)))
    L.append("")
    if result["verification"]:
        L += ["## Skeptic checks (majority of valid votes kills; quorum = majority of skeptics requested)", "",
              "| item | verdict | refuted/valid (quorum) | abstained | claim |", "|---|---|---|---|---|"]
        for v in result["verification"]:
            nabs = sum(1 for c in v.get("votes", []) if c.get("refuted") is None)
            L.append("| %s | %s | %d/%d (%s) | %d | %s |" % (v["item_id"], v["verdict"].upper(), v["refuted_votes"], v["valid_votes"],
                                                          v.get("quorum", "-"), nabs, one_line(v.get("claim", ""), 140).replace("|", "/")))
        killed = [v for v in result["verification"] if v["verdict"] in ("killed", "contested")]
        if killed:
            L += ["", "### Refutations"]
            for v in killed:
                for c in v["votes"]:
                    if c.get("refuted"):
                        L.append("- %s: %s" % (v["item_id"], one_line(c.get("reason", ""), 300)))
        L.append("")
    if result.get("reviews"):
        L += ["## Verifier reviews (review + advise + fix; writes limited to the executor's files)", "",
              "| verifier | executor | round | verdict | issues | fixed files | violations |", "|---|---|---|---|---|---|---|"]
        for x in result["reviews"]:
            L.append("| %s | %s | %d | %s | %d | %s | %s |" % (
                x["id"], x["executor"], x["round"], x["verdict"].upper(), len(x["issues"]),
                ", ".join(x["fix_files"]) or "-", one_line("; ".join(x["violations"]), 160).replace("|", "/") or "-"))
        for x in result["reviews"]:
            L += ["", "### %s (%s/%s)" % (x["id"], x.get("model") or "default", x.get("effort") or "default")]
            for i in x["issues"]:
                if isinstance(i, dict):
                    L.append("- [%s]%s %s:%s %s" % (i.get("severity"), " fixed" if i.get("fixed") else "", i.get("file"),
                                                   i.get("line"), one_line(str(i.get("description")), 300)))
            if x["fixes_summary"]:
                L.append("- fixes: %s" % one_line(x["fixes_summary"], 400))
            if x["advice_executor"]:
                L.append("- advice (executor): %s" % one_line(x["advice_executor"], 400))
            if x["advice_orchestrator"]:
                L.append("- advice (orchestrator): %s" % one_line(x["advice_orchestrator"], 400))
            for n in x.get("notes") or []:
                L.append("- note: %s" % n)
            if x.get("proposed_diff_check"):
                L.append("- proposed fix diff: %s (%s)" % (x.get("proposed_diff_path"), x["proposed_diff_check"]))
            if x.get("fix_diff"):
                L += ["", "```diff", x["fix_diff"].rstrip(), "```"]
        L.append("")
    if gate and gate.get("status") not in (None,):
        L += ["## Final gate: %s" % str(gate["status"]).upper(), "", gate_summary(gate), ""]
    if not result["synthesis"].get("text"):
        L += ["## Child answers", ""]
        for r in result["tasks"]:
            L += ["### %s (%s)" % (r["id"], r["role"]), "", (r.get("text") or r.get("error") or "(no output)").strip(), ""]
    L += ["Run dir: `%s`" % result["out_dir"]]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())

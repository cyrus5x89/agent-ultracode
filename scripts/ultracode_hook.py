#!/usr/bin/env python3
"""ultracode Codex hook (UserPromptSubmit + optional PreToolUse on spawn_agent). Python 3.9.

UserPromptSubmit
  * `ultracode on|off|status|roles [k=v ...]` (also `$ultracode ...`) toggles the standing
    mode for this session, stores per-role overrides, and BLOCKS the prompt so no model turn
    is spent. The block reason shows the effective model+effort per role.
    (A real `/ultracode` slash command is impossible: the Codex TUI rejects unknown slash
    commands before hooks run.)
  * Otherwise, if standing mode is on or the prompt contains the word "ultracode", inject a
    short additionalContext telling the model to follow the `ultracode` skill, explicitly
    authorising sub-agent fan-out, with the resolved role table.

PreToolUse (matcher ".*spawn_agent|Agent"; the tool arrives as "collaborationspawn_agent"), only while ultracode is active for the turn
  * Fills a missing `reasoning_effort` / `model` from the role named by the task_name prefix
    (scout_*, skeptic_*/check_*, exec_*, verify_*/review_*, synth_*, gate_* = synthesizer role moved off the
    scout model; a prefix only matches as a whole word: `readme_reviewer` has no role); unknown role -> the
    parent's CURRENT effort, so
    effort stays inherited even on models whose catalog multi_agent effort differs.
  * Clamps an unsupported effort to the target model's supported levels and drops an
    unknown model. Never denies.

State: ~/.local/state/ultracode/<session_id>.json (outside ~/.codex).
Children started by ultracode.py carry ULTRACODE_CHILD=1 and are ignored.
"""

import json
import os
import re
import shlex
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import uc_core as core  # noqa: E402

TOGGLE = re.compile(r"^\s*[/$]?ultracode\s+(on|off|status|roles)\b(.*)$", re.I | re.S)
KEYWORD = re.compile(r"(?<![\w/.-])\$?ultracode(?!\.\w|[\w-])", re.I)
ROLE_PREFIX = [
    ("scout", "scout"), ("reader", "scout"), ("read", "scout"),
    ("skeptic", "skeptic"), ("check", "skeptic"),
    ("executor", "executor"), ("exec", "executor"), ("impl", "executor"), ("writer", "executor"),
    ("verifier", "verifier"), ("verify", "verifier"), ("reviewer", "verifier"), ("review", "verifier"),
    ("synthesizer", "synthesizer"), ("synth", "synthesizer"), ("gate", "gate"),
]
# A prefix only counts as a whole word: `scout_a`, `verify-2`, `exec3`, `gate` match;
# `readme_reviewer`, `reading_x`, `checkout_fix` do not (they get no role -> inherit).
ROLE_RE = [(re.compile(r"^%s(?:$|[_\-.:\s\d])" % re.escape(p)), r) for p, r in ROLE_PREFIX]
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ultracode.py")


def emit(obj):
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def resolve(payload, state_override=None):
    sid = payload.get("session_id")
    cwd = payload.get("cwd") or os.getcwd()
    settings, warns, state = core.build_settings(cwd, sid)
    if state_override is not None:
        settings = core.empty_settings()
        for path, data in core.config_layers(cwd):
            warns.extend(core.merge_layer(settings, data, core.layer_label(path)))
        ov = state_override.get("overrides")
        if isinstance(ov, dict):
            warns.extend(core.merge_layer(settings, ov, "session toggle"))
    parent = core.detect_parent(
        thread_id=sid,
        rollout_path=payload.get("transcript_path"),
        explicit_model=payload.get("model"),  # the hook payload carries the active model
    )
    if payload.get("model"):
        parent["model_method"] = "hook payload"
        parent["model_live"] = True
    catalog = core.load_catalog()
    roles = core.resolve_roles(settings, parent, catalog)
    return settings, warns, parent, catalog, roles


def compact_roles(roles):
    parts = []
    for r in core.CHILD_ROLES:
        x = roles[r]
        parts.append("%s %s%s/%s%s" % (
            r, x["model"] or "default", "*" if x["inherited_model"] else "",
            x["effort"] or "default", "*" if x["inherited_effort"] else ""))
    return "; ".join(parts)


def on_prompt(payload):
    prompt = payload.get("prompt") or ""
    sid = payload.get("session_id") or "default"
    m = TOGGLE.match(prompt)
    tokens = []
    if m:
        try:
            tokens = shlex.split(m.group(2))  # allows test_cmd="pytest -q"
        except ValueError:
            tokens = m.group(2).split()
    is_toggle = bool(m) and all(("=" in t) or t.lower() == "reset" for t in tokens)
    state = core.load_state(sid)

    if is_toggle:
        cmd = m.group(1).lower()
        layer, msgs = core.parse_kv_tokens(tokens)
        ov = state.get("overrides") if isinstance(state.get("overrides"), dict) else {}
        if layer.pop("reset", False):
            ov = {}
        for k, v in layer.items():
            if k == "roles":
                for rname, rc in v.items():
                    ov.setdefault("roles", {}).setdefault(rname, {}).update(rc)
            else:
                ov[k] = v
        state["overrides"] = ov
        if cmd == "on":
            state["enabled"] = True
        elif cmd == "off":
            state["enabled"] = False
        try:
            core.save_state(sid, state)
        except OSError as e:
            msgs.append("could not save state: %s" % e)
        settings, warns, parent, _, roles = resolve(payload, state)
        c = settings["caps"]
        o = settings.get("options") or {}
        head = ("ultracode: %s (width=%d, votes=%d, concurrency=%d, verify_mode=%s, gate=%s; effort is inherited "
                "unless a role overrides it)" % ("ON" if state.get("enabled") else "OFF", c["width"], c["votes"],
                                                 c["concurrency"], o.get("verify_mode"), o.get("gate")))
        body = core.roles_table(roles, parent, settings, payload.get("cwd") or os.getcwd())
        extra = ("\nchanges: " + ", ".join(msgs)) if msgs else ""
        extra += "".join("\nwarning: %s" % w for w in warns)
        emit({"decision": "block", "reason": head + "\n" + body + extra})
        return

    active = bool(state.get("enabled")) or bool(KEYWORD.search(prompt))
    if not active:
        if state.get("turn_active") or state.get("turn_active_at"):
            # A new non-ultracode prompt ends any keyword-only turn (matters when payloads lack turn_id).
            state.pop("turn_active", None)
            state.pop("turn_active_at", None)
            try:
                core.save_state(sid, state)
            except OSError:
                pass
        return
    how = "standing mode" if state.get("enabled") else "keyword in this prompt"
    state["turn_active"] = payload.get("turn_id") or KEYWORD_TURN_NO_ID
    state["turn_active_at"] = int(time.time())
    if payload.get("model"):
        state["parent_model"] = payload.get("model")  # lets ultracode.py see the live model
    try:
        core.save_state(sid, state)
    except OSError:
        pass
    settings, warns, parent, _, roles = resolve(payload)
    c = settings["caps"]
    o = settings.get("options") or {}
    gate = core.gate_plan(settings, payload.get("cwd") or os.getcwd())
    gate_txt = ("tests `%s`" % gate["test_command"]) if gate["kind"] == "tests" else (
        "off" if gate["kind"] == "off" else "read-only review of the combined diff (synthesizer role, prefix gate_)")
    vm = o.get("verify_mode") or "fix"
    if vm == "fix":
        verify_txt = ("AFTER each executor finishes, one verifier (big model, verify_ prefix) reviews exactly that "
                      "executor's files, fixes bugs in place (only those files; revert anything else it touched) and "
                      "reports issues / fixes (diff) / advice")
    elif vm == "vote":
        verify_txt = "verify_mode=vote: %d skeptics vote on each executor change (majority kills, no fixes)" % c["votes"]
    else:
        verify_txt = "verify_mode=off: executor changes are not reviewed"
    if gate["kind"] == "tests" and vm == "fix":
        gate_txt += " (on failure at most %d more verifier fix round(s), then report the failure honestly)" % c["gate_retries"]
    native = "; ".join("%s %s" % (r, json.dumps(roles[r]["native_spawn_args"])) for r in core.CHILD_ROLES)
    native += "; gate %s" % json.dumps(role_spawn_args(roles, "gate"))
    ctx = (
        "ULTRACODE is ON (%s). The user explicitly instructs multi-agent orchestration: for any substantive task, "
        "load and follow the `ultracode` skill (SKILL.md). This instruction explicitly authorises spawning sub-agents "
        "(collaboration.spawn_agent) per that skill. Trivial one-line questions: just answer.\n"
        "You are the ORCHESTRATOR: plan, split work, write the spec, route by role, spot-check evidence, decide. "
        "Flow: scouts read and report (read-only, file:line + quote) -> skeptics cross-check the scout findings "
        "(read-only, big model) -> you decide -> executors write (one per disjoint file set) -> %s -> final gate: %s "
        "-> synthesize. "
        "Avoid heavy edits yourself.\n"
        "Roles (model/effort, *=inherited from this session): %s.\n"
        "spawn_agent args per role (task_name prefix scout_/skeptic_/exec_/verify_/synth_ or gate_): %s.\n"
        "Caps: width=%d tasks, votes=%d skeptics per scout finding, concurrency=%d (spawn in waves, wait_agent "
        "between waves).\n"
        "End with the `ultracode ledger` from the skill (real spawned agent ids, votes, verifier fixes, gate result); "
        "if nothing was spawned, say so - "
        "never claim adversarial review without agent ids. "
        "Effort is orthogonal: never raise or lower your own effort for ultracode. "
        "Deterministic engine (needs an unsandboxed/escalated command): python3 %s --show-roles"
        % (how, verify_txt, gate_txt, compact_roles(roles), native, c["width"], c["votes"], c["concurrency"], SCRIPT)
    )
    emit({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": ctx}})


def role_of(task_name):
    t = (task_name or "").lower().lstrip("/")
    t = t.split("/")[-1]
    for rx, role in ROLE_RE:
        if rx.match(t):
            return role
    return None


def role_spawn_args(roles, role):
    """The spawn args for a task_name role; `gate` = the synthesizer role on a non-scout model."""
    if role == "gate":
        syn = roles["synthesizer"]
        return syn.get("gate_native_spawn_args", syn["native_spawn_args"])
    return roles[role]["native_spawn_args"]


KEYWORD_TURN_NO_ID = "(no turn_id)"
KEYWORD_TURN_TTL = int(os.environ.get("ULTRACODE_KEYWORD_TURN_TTL") or 3600)


def keyword_turn_active(state, payload):
    """True while the prompt that contained the keyword is still the current turn.

    With turn ids on both sides they must match. If Codex omits turn_id (in the prompt or in the
    PreToolUse payload), fall back to: the keyword prompt was the LAST prompt of this session
    (any later non-keyword prompt clears it in on_prompt) and it is younger than the TTL.
    """
    stored = state.get("turn_active")
    if not stored:
        return False
    tid = payload.get("turn_id")
    if tid and stored != KEYWORD_TURN_NO_ID:
        return stored == tid
    at = state.get("turn_active_at")
    try:
        return at is not None and (time.time() - int(at)) <= KEYWORD_TURN_TTL
    except (TypeError, ValueError):
        return False


def on_spawn(payload):
    name = str(payload.get("tool_name") or "")
    if "spawn_agent" not in name and name != "Agent":
        return
    sid = payload.get("session_id") or "default"
    state = core.load_state(sid)
    if not (state.get("enabled") or keyword_turn_active(state, payload)):
        return
    ti = payload.get("tool_input")
    if not isinstance(ti, dict):
        return
    settings, warns, parent, catalog, roles = resolve(payload)
    new = dict(ti)
    notes = []
    role = role_of(ti.get("task_name"))
    want = role_spawn_args(roles, role) if role else {}
    if role and not new.get("model") and want.get("model"):
        new["model"] = want["model"]
        notes.append("model=%s (%s role)" % (want["model"], role))
    if new.get("model") and catalog and new["model"] not in catalog:
        notes.append("dropped unknown model %s (inherit)" % new.pop("model"))
    target = new.get("model") or parent.get("model")
    eff = core.normalize_effort(new.get("reasoning_effort")) if new.get("reasoning_effort") else None
    if eff == "inherit":
        eff = None
    if not eff:
        eff = want.get("reasoning_effort") if role else None
        if not eff and parent.get("effort_live"):
            eff = parent.get("effort")
        if eff:
            notes.append("reasoning_effort=%s (%s)" % (eff, ("%s role" % role) if role else "inherited from parent"))
    if eff:
        clamped, note = core.clamp_effort(eff, target, catalog)
        if note:
            notes.append(note)
        eff = clamped
        new["reasoning_effort"] = eff
    if new != ti:
        emit({
            "systemMessage": "ultracode spawn: " + "; ".join(notes),
            "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow", "updatedInput": new},
        })


def main():
    if os.environ.get("ULTRACODE_CHILD"):
        return 0
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        return 0
    if os.environ.get("ULTRACODE_HOOK_DEBUG"):
        try:
            os.makedirs(core.state_dir(), exist_ok=True)
            with open(os.path.join(core.state_dir(), "hook-debug.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(raw.strip() + "\n")
        except OSError:
            pass
    ev = payload.get("hook_event_name")
    try:
        if ev == "UserPromptSubmit":
            on_prompt(payload)
        elif ev == "PreToolUse":
            on_spawn(payload)
    except Exception as e:  # never break the user's turn because of this hook
        sys.stderr.write("ultracode hook error: %r\n" % (e,))
    return 0


if __name__ == "__main__":
    sys.exit(main())

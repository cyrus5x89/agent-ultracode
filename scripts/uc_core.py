"""ultracode core: parent model/effort detection, role tiers, config layers, session state.

Python 3.9 compatible (no match statements, no X|Y type unions). Standard library only.

Design rule: ultracode is ORTHOGONAL to effort. Every role defaults to "inherit", which
resolves to the parent Codex session's CURRENT model + reasoning effort (as recorded by
Codex for the latest turn). Nothing here ever forces max or low; per-role overrides are
opt-in via config files, the `ultracode on k=v` toggle, the JSON spec or CLI flags.
"""

import glob
import json
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

# --------------------------------------------------------------------------- constants

EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
EFFORT_ALIASES = {
    "off": "none",
    "min": "minimal",
    "lo": "low",
    "med": "medium",
    "mid": "medium",
    "hi": "high",
    "x-high": "xhigh",
    "extra-high": "xhigh",
    "extrahigh": "xhigh",
    "maximum": "max",
}

# Roles that run as child agents, plus the orchestrator (= the parent session itself).
#   scout        small/cheap reader (preset: the Luna line), READ-ONLY, structured file:line reports
#   skeptic      read-only cross-check of SCOUT FINDINGS before the orchestrator decides (big model,
#                never the scout model); also the voters of the optional verify_mode=vote
#   executor     strongest model, writes, one per disjoint file set
#   verifier     big-model reviewer + advisor + FIXER of one executor's change, write access limited
#                to that executor's file set (out-of-set edits are reverted)
#   synthesizer  final answer; also the read-only final-gate reviewer when no test command exists
ROLES = ["orchestrator", "scout", "skeptic", "executor", "verifier", "synthesizer"]
CHILD_ROLES = ["scout", "skeptic", "executor", "verifier", "synthesizer"]
ROLE_ALIASES = {
    "scout": ["scout"],
    "scouts": ["scout"],
    "reader": ["scout"],
    "readers": ["scout"],
    "worker": ["scout", "executor"],
    "workers": ["scout", "executor"],
    "skeptic": ["skeptic"],
    "skeptics": ["skeptic"],
    "checker": ["skeptic"],
    "check": ["skeptic"],
    "executor": ["executor"],
    "executors": ["executor"],
    "exec": ["executor"],
    "writer": ["executor"],
    "verifier": ["verifier"],
    "verifiers": ["verifier"],
    "verify": ["verifier"],
    "reviewer": ["verifier"],
    "fixer": ["verifier"],
    "synthesizer": ["synthesizer"],
    "synth": ["synthesizer"],
    "synthesis": ["synthesizer"],
    "all": ["scout", "skeptic", "executor", "verifier", "synthesizer"],
}

DEFAULT_CAPS = {
    "width": 8,  # max tasks per run (fan-out width)
    "votes": 3,  # skeptics per checked item (0 disables skeptic checks)
    "concurrency": 4,  # max child codex processes at once
    "timeout_sec": 900,  # per child (and per test-command run)
    "output_cap": 6000,  # bytes of each child's final answer passed on
    "max_verify_items": 12,  # max findings/changes sent to skeptics
    "gate_retries": 1,  # extra verifier fix rounds when the final-gate tests fail
}
HARD_CAPS = {
    "width": (1, 16),
    "votes": (0, 5),
    "concurrency": (1, 6),
    "timeout_sec": (30, 3600),
    "output_cap": (1000, 64000),
    "max_verify_items": (1, 40),
    "gate_retries": (0, 2),
}

# Non-numeric options (verify mode, final gate, test command).
VERIFY_MODES = ["fix", "vote", "off"]
GATE_MODES = ["auto", "tests", "review", "off"]
DEFAULT_OPTIONS = {
    "verify_mode": "fix",  # fix = big-model review + advise + fix per executor; vote = old skeptic majority vote
    "gate": "auto",  # auto = tests if a test command is configured/detected (and writes are on), else read-only review
    "test_command": None,  # None = detect; "none" = never run tests
    "verify_serial": False,  # run verifiers one at a time (exact attribution of every out-of-set edit)
}
OPTION_ALIASES = {
    "verify_mode": ["verify_mode", "verifyMode", "verifier_mode", "verifierMode"],
    "gate": ["gate", "final_gate", "finalGate"],
    "test_command": ["test_command", "testCommand", "test_cmd", "testCmd"],
    "verify_serial": ["verify_serial", "verifySerial", "serial_verify"],
}

# Used ONLY when a big-model role (skeptic / verifier / gate review) would otherwise run the
# configured scout model and neither the executor nor this session offers another model.
BIG_MODEL_FALLBACK = ["gpt-6.1-sol"]

ROLE_SANDBOX = {
    "scout": "read-only",
    "skeptic": "read-only",
    "executor": "workspace-write",
    "verifier": "workspace-write",  # limited to its executor's file set (enforced by ultracode.py)
    "synthesizer": "read-only",
}


def codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def skill_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def normalize_effort(value: Any) -> Optional[str]:
    """Return a canonical effort name, "inherit", or None if invalid."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s:
        return None
    if s in ("inherit", "parent", "default", "auto"):
        return "inherit"
    s = EFFORT_ALIASES.get(s, s)
    if s in EFFORT_ORDER:
        return s
    return None


def clamp_int(value: Any, key: str, fallback: int) -> int:
    lo, hi = HARD_CAPS[key]
    try:
        n = int(str(value).lower().replace("k", "000")) if isinstance(value, str) else int(value)
    except (TypeError, ValueError):
        return fallback
    return max(lo, min(hi, n))


# --------------------------------------------------------------------------- tiny TOML scan

_TOML_KV = re.compile(r"^\s*([A-Za-z0-9_.\-\"']+)\s*=\s*(.+?)\s*$")
_TOML_HDR = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")


def _toml_value(raw: str) -> Optional[str]:
    raw = raw.strip()
    if not raw:
        return None
    if raw[0] in "\"'":
        q = raw[0]
        end = raw.find(q, 1)
        if end > 0:
            return raw[1:end]
        return None
    raw = raw.split("#", 1)[0].strip()
    return raw or None


def toml_get(path: str, table: str, key: str) -> Optional[str]:
    """Return the scalar `key` in `[table]` ("" = top level) of a TOML file, or None.

    Deliberately tiny: handles `key = "string"` / bare scalars, which is all we need for
    model / model_reasoning_effort / profile. Python 3.9 has no tomllib.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    current = ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _TOML_HDR.match(stripped)
        if m and not _TOML_KV.match(stripped):
            current = m.group(1).replace('"', "").replace("'", "").strip()
            continue
        if current != table:
            continue
        kv = _TOML_KV.match(stripped)
        if kv and kv.group(1).strip("\"'") == key:
            return _toml_value(kv.group(2))
    return None


# --------------------------------------------------------------------------- model catalog


def load_catalog() -> Dict[str, Dict[str, Any]]:
    """slug -> {efforts, default, multi_agent_effort, visibility} from models_cache.json."""
    path = os.path.join(codex_home(), "models_cache.json")
    out = {}  # type: Dict[str, Dict[str, Any]]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return out
    models = data.get("models") if isinstance(data, dict) else data
    if not isinstance(models, list):
        return out
    for m in models:
        if not isinstance(m, dict) or not m.get("slug"):
            continue
        efforts = []
        for lvl in m.get("supported_reasoning_levels") or []:
            e = lvl.get("effort") if isinstance(lvl, dict) else lvl
            e = normalize_effort(e)
            if e and e != "inherit":
                efforts.append(e)
        out[str(m["slug"])] = {
            "efforts": efforts,
            "default": m.get("default_reasoning_level"),
            "multi_agent_effort": m.get("multi_agent_reasoning_effort"),
            "visibility": m.get("visibility"),
        }
    return out


def clamp_effort(effort: Optional[str], model: Optional[str], catalog: Dict[str, Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    """Clamp `effort` to the nearest level `model` supports. Returns (effort, note)."""
    if not effort or effort == "inherit":
        return effort, None
    info = catalog.get(model or "") if model else None
    supported = (info or {}).get("efforts") or []
    if not supported or effort in supported:
        return effort, None
    idx = EFFORT_ORDER.index(effort)
    best = sorted(supported, key=lambda s: (abs(EFFORT_ORDER.index(s) - idx), EFFORT_ORDER.index(s)))[0]
    return best, "effort %s is not supported by %s; clamped to %s" % (effort, model, best)


# --------------------------------------------------------------------------- parent detection


def _state_db_paths() -> List[str]:
    paths = glob.glob(os.path.join(codex_home(), "state_*.sqlite"))

    def num(p):
        m = re.search(r"state_(\d+)\.sqlite$", p)
        return int(m.group(1)) if m else -1

    return sorted(paths, key=num, reverse=True)


def _sqlite_thread(thread_id: str) -> Optional[Dict[str, Any]]:
    for db in _state_db_paths():
        for flags in ("mode=ro", "mode=ro&immutable=1"):
            try:
                conn = sqlite3.connect("file:%s?%s" % (quote(db), flags), uri=True, timeout=2)
            except sqlite3.Error:
                continue
            try:
                cur = conn.execute(
                    "select model, reasoning_effort, rollout_path from threads where id = ?", (thread_id,)
                )
                row = cur.fetchone()
                if row is None:
                    return None
                return {"model": row[0], "effort": row[1], "rollout_path": row[2], "db": db}
            except sqlite3.Error:
                continue
            finally:
                conn.close()
    return None


def _find_rollout(thread_id: str) -> Optional[str]:
    base = os.path.join(codex_home(), "sessions")
    hits = glob.glob(os.path.join(base, "*", "*", "*", "rollout-*-%s.jsonl" % thread_id))
    if not hits:
        hits = glob.glob(os.path.join(base, "**", "rollout-*%s*.jsonl" % thread_id), recursive=True)
    if not hits:
        return None
    return max(hits, key=lambda p: os.path.getmtime(p))


def _turn_context_from_line(line: bytes) -> Optional[Dict[str, Any]]:
    if b'"turn_context"' not in line:
        return None
    try:
        obj = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "turn_context":
        return None
    p = obj.get("payload") or {}
    settings = ((p.get("collaboration_mode") or {}).get("settings")) or {}
    return {
        "model": p.get("model") or settings.get("model"),
        "effort": p.get("effort") or settings.get("reasoning_effort"),
        "turn_id": p.get("turn_id"),
    }


def last_turn_context(rollout_path: str, max_back_bytes: int = 64 * 1024 * 1024) -> Optional[Dict[str, Any]]:
    """Return {model, effort, turn_id} from the LAST turn_context record of a rollout file."""
    try:
        size = os.path.getsize(rollout_path)
        fh = open(rollout_path, "rb")
    except OSError:
        return None
    try:
        chunk = 1024 * 1024
        pos = size
        buf = b""
        scanned = 0
        while pos > 0 and scanned < max_back_bytes:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
            scanned += step
            lines = buf.split(b"\n")
            # The first piece may be a partial line unless we reached the start of the file.
            complete = lines if pos == 0 else lines[1:]
            for line in reversed(complete):
                tc = _turn_context_from_line(line)
                if tc:
                    return tc
            buf = lines[0] if pos > 0 else b""
        return None
    finally:
        fh.close()


def detect_parent(
    thread_id: Optional[str] = None,
    rollout_path: Optional[str] = None,
    explicit_model: Optional[str] = None,
    explicit_effort: Optional[str] = None,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    """Detect the parent Codex session's CURRENT model and reasoning effort.

    Order (first hit wins, separately for model and effort):
      0. explicit (--parent-model/--parent-effort or ULTRACODE_PARENT_MODEL/_EFFORT)
      1. rollout turn_context of $CODEX_THREAD_ID (path from state_N.sqlite or glob) -
         the latest turn, so it follows /model changes made mid-session
      2. state_N.sqlite threads.model / threads.reasoning_effort
      3. active profile: $ULTRACODE_PROFILE / $CODEX_PROFILE -> $CODEX_HOME/<p>.config.toml
         or [profiles.<p>] in config.toml
      4. $CODEX_HOME/config.toml top-level model / model_reasoning_effort
      5. nothing -> children get no -m / -c model_reasoning_effort (Codex default)
    """
    res = {
        "model": None,
        "effort": None,
        "model_method": "none (child uses Codex default)",
        "effort_method": "none (no -c model_reasoning_effort; child uses Codex default)",
        "thread_id": None,
        "rollout_path": None,
        "notes": [],
        # live = read from THIS session's own records (explicit, rollout, state db, hook payload).
        # A config/profile fallback may differ from what the session really runs (e.g. `-c`
        # overrides in an --ephemeral session), so native spawns then omit the value and let
        # Codex inherit it itself.
        "model_live": False,
        "effort_live": False,
    }  # type: Dict[str, Any]

    def take(model, effort, method, live=False):
        if model and not res["model"]:
            res["model"] = str(model)
            res["model_method"] = method
            res["model_live"] = live
        e = normalize_effort(effort)
        if e and e != "inherit" and not res["effort"]:
            res["effort"] = e
            res["effort_method"] = method
            res["effort_live"] = live

    take(explicit_model or os.environ.get("ULTRACODE_PARENT_MODEL"),
         explicit_effort or os.environ.get("ULTRACODE_PARENT_EFFORT"), "explicit", True)

    tid = thread_id or os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_SESSION_ID")
    res["thread_id"] = tid
    if tid and not thread_id:
        st = load_state(tid)
        if st.get("parent_model"):
            take(st.get("parent_model"), None, "hook payload (session state)", True)
    row = None
    if tid:
        row = _sqlite_thread(tid)
        rp = rollout_path or (row or {}).get("rollout_path") or _find_rollout(tid)
        if rp and os.path.exists(rp):
            res["rollout_path"] = rp
            tc = last_turn_context(rp)
            if tc:
                take(tc.get("model"), tc.get("effort"), "rollout turn_context", True)
            else:
                res["notes"].append("rollout has no turn_context yet")
        elif not row:
            res["notes"].append("thread %s not found (ephemeral session?)" % tid)
        if row:
            take(row.get("model"), row.get("effort"), "%s threads row" % os.path.basename(row["db"]), True)
    elif rollout_path and os.path.exists(rollout_path):
        tc = last_turn_context(rollout_path)
        if tc:
            take(tc.get("model"), tc.get("effort"), "transcript turn_context", True)
    else:
        res["notes"].append("no CODEX_THREAD_ID in env (not running inside a Codex shell?)")

    cfg = os.path.join(codex_home(), "config.toml")
    prof = profile or os.environ.get("ULTRACODE_PROFILE") or os.environ.get("CODEX_PROFILE")
    if prof:
        pfile = os.path.join(codex_home(), "%s.config.toml" % prof)
        take(toml_get(pfile, "", "model"), toml_get(pfile, "", "model_reasoning_effort"), "profile file %s" % pfile)
        tbl = "profiles.%s" % prof
        take(toml_get(cfg, tbl, "model"), toml_get(cfg, tbl, "model_reasoning_effort"), "config.toml [%s]" % tbl)
    take(toml_get(cfg, "", "model"), toml_get(cfg, "", "model_reasoning_effort"), "config.toml top-level")
    return res


# --------------------------------------------------------------------------- config layers


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def project_config_path(cwd: str) -> Optional[str]:
    d = os.path.abspath(cwd)
    home = os.path.expanduser("~")
    while True:
        p = os.path.join(d, ".codex", "ultracode.json")
        if os.path.isfile(p) and os.path.abspath(os.path.join(d, ".codex")) != os.path.abspath(codex_home()):
            return p
        if os.path.isdir(os.path.join(d, ".git")) or d == home:
            return None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def config_layers(cwd: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Config files, lowest precedence first. The CODE default is inherit-everything."""
    layers = []  # type: List[Tuple[str, Dict[str, Any]]]
    cands = [
        os.path.join(skill_dir(), "ultracode.json"),
        os.path.join(codex_home(), "ultracode.json"),
        project_config_path(cwd),
        os.environ.get("ULTRACODE_CONFIG"),
    ]
    seen = set()
    for p in cands:
        if not p:
            continue
        ap = os.path.abspath(os.path.expanduser(p))
        if ap in seen or not os.path.isfile(ap):
            continue
        seen.add(ap)
        data = _read_json(ap)
        if data is not None:
            layers.append((ap, data))
    return layers


def empty_settings() -> Dict[str, Any]:
    return {"caps": dict(DEFAULT_CAPS), "roles": {r: {} for r in CHILD_ROLES}, "options": dict(DEFAULT_OPTIONS),
            "sources": {}}


def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def normalize_option(key: str, value: Any) -> Tuple[Any, Optional[str]]:
    """Validate one option value. Returns (value, error)."""
    if key == "verify_mode":
        v = str(value).strip().lower()
        v = {"review": "fix", "fixer": "fix", "verifier": "fix", "voting": "vote", "votes": "vote", "skeptics": "vote",
             "none": "off", "false": "off"}.get(v, v)
        return (v, None) if v in VERIFY_MODES else (None, "verify_mode must be %s" % "|".join(VERIFY_MODES))
    if key == "gate":
        v = str(value).strip().lower()
        v = {"test": "tests", "none": "off", "false": "off", "read-only": "review", "readonly": "review"}.get(v, v)
        return (v, None) if v in GATE_MODES else (None, "gate must be %s" % "|".join(GATE_MODES))
    if key == "test_command":
        if value is None:
            return None, None
        v = str(value).strip()
        if v.lower() in ("", "none", "off", "false", "no"):
            return "none", None
        if v.lower() in ("auto", "detect"):
            return None, None
        return v, None
    if key == "verify_serial":
        return _bool(value), None
    return value, None


def merge_layer(settings: Dict[str, Any], layer: Dict[str, Any], source: str) -> List[str]:
    """Merge a config/spec/state layer into settings. Returns warnings."""
    warns = []  # type: List[str]
    caps = layer.get("caps") if isinstance(layer.get("caps"), dict) else layer
    for key in DEFAULT_CAPS:
        alias = {"timeout_sec": ["timeout", "timeout_sec"], "output_cap": ["output_cap", "cap"],
                 "gate_retries": ["gate_retries", "gateRetries"]}.get(key, [key])
        for a in alias:
            if a in caps and caps[a] is not None:
                settings["caps"][key] = clamp_int(caps[a], key, settings["caps"][key])
                settings["sources"]["cap." + key] = source
    opts = layer.get("options") if isinstance(layer.get("options"), dict) else layer
    for key, aliases in OPTION_ALIASES.items():
        for a in aliases:
            if a in opts and (opts[a] is not None or key == "test_command"):
                val, err = normalize_option(key, opts[a])
                if err:
                    warns.append("%s: %s=%r ignored (%s)" % (source, a, opts[a], err))
                    continue
                settings.setdefault("options", dict(DEFAULT_OPTIONS))[key] = val
                settings["sources"]["opt." + key] = source
    roles = layer.get("roles")
    if isinstance(roles, dict):
        for name, rc in roles.items():
            if name.startswith("_"):
                continue
            targets = ROLE_ALIASES.get(str(name).lower())
            if not targets:
                if str(name).lower() != "orchestrator":
                    warns.append("%s: unknown role %r ignored" % (source, name))
                else:
                    warns.append("%s: orchestrator is the current session; set its model with /model" % source)
                continue
            if isinstance(rc, str):
                rc = parse_role_value(rc)
            if not isinstance(rc, dict):
                continue
            for t in targets:
                for field in ("model", "effort"):
                    if field in rc and rc[field] not in (None, ""):
                        val = rc[field]
                        if field == "effort":
                            e = normalize_effort(val)
                            if not e:
                                warns.append("%s: %s.effort=%r is not a valid effort; ignored" % (source, t, val))
                                continue
                            val = e
                        settings["roles"][t][field] = val
                        settings["sources"]["%s.%s" % (t, field)] = source
    return warns


def parse_role_value(v: str) -> Dict[str, Any]:
    """`low` -> effort; `gpt-6.1-sol` -> model; `gpt-6.1-sol@high` / `gpt-6.1-sol:high` -> both."""
    v = v.strip()
    for sep in ("@", ":"):
        if sep in v:
            m, e = v.split(sep, 1)
            out = {}
            if m.strip():
                out["model"] = m.strip()
            if e.strip():
                out["effort"] = e.strip()
            return out
    if normalize_effort(v):
        return {"effort": v}
    return {"model": v}


def parse_kv_tokens(tokens: List[str]) -> Tuple[Dict[str, Any], List[str]]:
    """Parse `width=4 votes=3 scout.model=x verifier.effort=high workers=low verify_mode=fix
    gate=auto test_cmd="pytest -q" gate_retries=1 reset` into a layer."""
    layer = {"roles": {}}  # type: Dict[str, Any]
    msgs = []  # type: List[str]
    for tok in tokens:
        tok = tok.strip()
        if not tok:
            continue
        if tok == "reset":
            layer["reset"] = True
            msgs.append("reset overrides")
            continue
        if "=" not in tok:
            msgs.append("ignored %r (expected key=value)" % tok)
            continue
        key, val = tok.split("=", 1)
        key = key.strip().lower()
        val = val.strip()
        if "." in key:
            rname, field = key.split(".", 1)
            if field in ("thinking", "reasoning", "reasoning_effort"):
                field = "effort"
            if field not in ("model", "effort") or rname not in ROLE_ALIASES:
                msgs.append("ignored %r" % tok)
                continue
            if field == "effort" and not normalize_effort(val):
                msgs.append("ignored %r (effort must be inherit|%s)" % (tok, "|".join(EFFORT_ORDER)))
                continue
            layer["roles"].setdefault(rname, {})[field] = val
            msgs.append("%s.%s=%s" % (rname, field, val))
        elif key in ROLE_ALIASES:
            layer["roles"].setdefault(key, {}).update(parse_role_value(val))
            msgs.append("%s=%s" % (key, val))
        elif key in ("width", "votes", "concurrency", "timeout", "timeout_sec", "cap", "output_cap", "max_verify_items",
                     "gate_retries"):
            layer[key] = val
            msgs.append("%s=%s" % (key, val))
        elif any(key in [a.lower() for a in al] for al in OPTION_ALIASES.values()):
            opt = [k for k, al in OPTION_ALIASES.items() if key in [a.lower() for a in al]][0]
            nv, err = normalize_option(opt, val)
            if err:
                msgs.append("ignored %r (%s)" % (tok, err))
                continue
            layer[opt] = val
            msgs.append("%s=%s" % (opt, val))
        else:
            msgs.append("ignored %r (unknown key)" % tok)
    return layer, msgs


# --------------------------------------------------------------------------- session state


def state_dir() -> str:
    d = os.environ.get("ULTRACODE_STATE_DIR")
    if not d:
        base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
        d = os.path.join(base, "ultracode")
    return d


def _state_path(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.\-]", "_", session_id or "default")
    return os.path.join(state_dir(), safe + ".json")


def load_state(session_id: Optional[str]) -> Dict[str, Any]:
    if not session_id:
        return {}
    return _read_json(_state_path(session_id)) or {}


def save_state(session_id: str, state: Dict[str, Any]) -> str:
    path = _state_path(session_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state["updated_at"] = int(time.time())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)
    return path


# --------------------------------------------------------------------------- role resolution


def _model_candidates(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    s = str(v).strip()
    return [x.strip() for x in s.split(",") if x.strip()] if s else []


def resolve_roles(settings: Dict[str, Any], parent: Dict[str, Any], catalog: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Effective model + effort per role. Unconfigured -> inherit the parent session.

    Model fallbacks (only when the role has NO `model` key at all; an explicit `inherit`
    keeps the parent model):
      verifier  -> the executor's model if configured, else inherit
      skeptic   -> the verifier's model if configured, else the executor's, else inherit
      synthesizer -> the orchestrator (= parent session)
    The verifier and the skeptic are BIG-model roles: they never run the scout model. If one
    resolves to the configured scout model (and that is not the parent's model), it is moved
    to the executor's model (or the parent's) with a warning.
    Effort is NEVER borrowed between roles: an unconfigured effort is always the parent's, so
    a cheap `workers=low` / `scout=low` fan-out cannot weaken checking or fixing.
    """
    roles = settings["roles"]
    src = settings["sources"]
    pm, pe = parent.get("model"), parent.get("effort")
    out = {}  # type: Dict[str, Dict[str, Any]]
    out["orchestrator"] = {
        "role": "orchestrator",
        "model": pm,
        "effort": pe,
        "model_source": "this session (%s)" % parent.get("model_method"),
        "effort_source": "this session (%s)" % parent.get("effort_method"),
        "inherited_model": True,
        "inherited_effort": True,
        "sandbox": "(session)",
        "warnings": [],
    }

    def configured(role, field):
        v = roles.get(role, {}).get(field)
        if v is None:
            return None
        if field == "effort" and normalize_effort(v) == "inherit":
            return None
        if field == "model" and str(v).strip().lower() in ("inherit", "parent", "default", ""):
            return None
        return v

    def first_available(role):
        for cand in _model_candidates(configured(role, "model")):
            if not catalog or cand in catalog:
                return cand
        return None

    tier_fallback = {"verifier": ["executor"], "skeptic": ["verifier", "executor"]}

    for role in CHILD_ROLES:
        warns = []  # type: List[str]
        mv = configured(role, "model")
        msrc = src.get("%s.model" % role)
        if mv is None and "model" not in roles.get(role, {}):
            for base in tier_fallback.get(role, []):
                if configured(base, "model") is not None:
                    mv = configured(base, "model")
                    msrc = "%s tier (%s)" % (base, src.get("%s.model" % base))
                    break
        ev = configured(role, "effort")
        esrc = src.get("%s.effort" % role)

        model = None
        inherited_model = True
        for cand in _model_candidates(mv):
            if catalog and cand not in catalog:
                warns.append("model %s not in Codex model catalog; skipped" % cand)
                continue
            model = cand
            inherited_model = False
            break
        if model is None:
            if mv is not None:
                warns.append("no configured model available; falling back to parent model")
            model = pm
            msrc = "inherit parent" if pm else "inherit (Codex default)"
            if role == "synthesizer":
                msrc = "orchestrator (inherit parent)"

        effort = normalize_effort(ev) if ev is not None else None
        inherited_effort = effort is None
        if inherited_effort:
            effort = pe
            esrc = "inherit parent" if pe else "inherit (Codex default)"
            if role == "synthesizer":
                esrc = "orchestrator (inherit parent)"
        out[role] = {
            "role": role,
            "model": model,
            "effort": effort,
            "model_source": msrc or "config",
            "effort_source": esrc or "config",
            "inherited_model": inherited_model,
            "inherited_effort": inherited_effort,
            "sandbox": ROLE_SANDBOX[role],
            "warnings": warns,
        }

    # Big-model roles never run the scout model - whether it was configured for them, borrowed
    # from a tier, or INHERITED because this session itself runs the scout model.
    scout = out["scout"]
    scout_model = scout["model"] if not scout["inherited_model"] else None

    def big_model_for(role):
        """(model, source, inherited) of a non-scout big model, or None if there is none."""
        ex = out["executor"]
        if not ex["inherited_model"] and ex["model"] and ex["model"] != scout_model:
            return ex["model"], "executor tier (scout model refused)", False
        if pm and pm != scout_model:
            return pm, "inherit parent (scout model refused)", True
        for cand in BIG_MODEL_FALLBACK:
            if cand != scout_model and (not catalog or cand in catalog):
                return cand, "built-in big-model fallback (scout model refused)", False
        return None

    for role in ("skeptic", "verifier"):
        r = out[role]
        if scout_model and r["model"] == scout_model:
            alt = big_model_for(role)
            if alt is None:
                r["warnings"].append("%s is the scout model and no other model is available; the %s (a big-model role) "
                                     "STILL runs it - configure %s.model" % (scout_model, role, role))
                continue
            r["model"], r["model_source"], r["inherited_model"] = alt
            r["warnings"].append("%s is the scout model; the %s is a big-model role and never runs it -> %s" % (
                scout_model, role, r["model"] or "Codex default"))

    # Final-gate reviewer (read-only review when no tests run): the synthesizer role, but never on
    # the scout model either (it is the independent check of the verifiers' fixes).
    syn = out["synthesizer"]
    gate_model, gate_src, gate_inh = syn["model"], "synthesizer (%s)" % syn["model_source"], syn["inherited_model"]
    if scout_model and syn["model"] == scout_model:
        v = out["verifier"]
        if v["model"] and v["model"] != scout_model:
            gate_model, gate_src, gate_inh = v["model"], "verifier tier (scout model refused)", v["inherited_model"]
        else:
            alt = big_model_for("synthesizer")
            if alt:
                gate_model, gate_src, gate_inh = alt
        if gate_model != scout_model:
            syn["warnings"].append("%s is the scout model; the final-gate review runs on %s instead" % (scout_model, gate_model))
    syn["gate_model"], syn["gate_model_source"], syn["gate_inherited_model"] = gate_model, gate_src, gate_inh

    for role in CHILD_ROLES:
        r = out[role]
        # Clamp against the model the child will actually run (configured, parent, or config default).
        r["requested_effort"] = r["effort"]
        eff, note = clamp_effort(r["effort"], r["model"] or pm, catalog)
        r["effort"] = eff
        if note:
            r["warnings"].append(note)
        native = {}  # type: Dict[str, Any]
        if r["model"] and (not r["inherited_model"]) and r["model"] != pm:
            native["model"] = r["model"]
        if eff and (not r["inherited_effort"] or parent.get("effort_live")):
            # Explicit even when inherited (if read live from this session): some catalog models
            # (e.g. gpt-6.1-sol) carry a multi_agent_reasoning_effort that could otherwise
            # replace the parent's effort. A config-file fallback is omitted: Codex then
            # inherits the session's real effort natively.
            native["reasoning_effort"] = eff
        r["native_spawn_args"] = native
    gate_native = dict(syn["native_spawn_args"])
    gate_native.pop("model", None)
    if syn["gate_model"] and syn["gate_model"] != pm:
        gate_native["model"] = syn["gate_model"]
    # the gate's effort is clamped against the GATE model (not the synthesizer's), so a scout
    # model's lower ceiling never caps the independent check
    syn["gate_effort"] = clamp_effort(syn.get("requested_effort"), syn["gate_model"] or pm, catalog)[0]
    if "reasoning_effort" in gate_native and syn["gate_effort"]:
        gate_native["reasoning_effort"] = syn["gate_effort"]
    syn["gate_native_spawn_args"] = gate_native
    return out


# --------------------------------------------------------------------------- final gate


def _read_text(path: str, limit: int = 256 * 1024) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def detect_test_command(cwd: str) -> Tuple[Optional[str], str]:
    """Return (command, reason) for an OBVIOUSLY present test setup, else (None, reason).

    Deliberately conservative: only pytest (pytest.ini / conftest.py / [tool.pytest...] /
    [tool:pytest] / tox.ini [pytest]) with pytest installed, and npm test (package.json with a
    real `scripts.test`) with npm on PATH. Anything else: configure `test_command`.
    """
    import shutil

    d = os.path.abspath(cwd)
    py_markers = []
    for f in ("pytest.ini", "conftest.py"):
        if os.path.isfile(os.path.join(d, f)):
            py_markers.append(f)
    if "[tool.pytest" in _read_text(os.path.join(d, "pyproject.toml")):
        py_markers.append("pyproject.toml [tool.pytest]")
    if "[tool:pytest]" in _read_text(os.path.join(d, "setup.cfg")):
        py_markers.append("setup.cfg [tool:pytest]")
    if "[pytest]" in _read_text(os.path.join(d, "tox.ini")):
        py_markers.append("tox.ini [pytest]")
    if py_markers:
        if shutil.which("pytest"):
            return "pytest -q", "detected: %s + pytest on PATH" % py_markers[0]
        py = shutil.which("python3") or shutil.which("python")
        if py:
            try:
                import subprocess

                ok = subprocess.run([py, "-c", "import pytest"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    timeout=5).returncode == 0
            except (OSError, Exception):
                ok = False
            if ok:
                return "%s -m pytest -q" % os.path.basename(py), "detected: %s + importable pytest" % py_markers[0]
        return None, "found %s but pytest is not installed" % py_markers[0]
    pkg = os.path.join(d, "package.json")
    if os.path.isfile(pkg):
        data = _read_json(pkg) or {}
        test = ((data.get("scripts") or {}) if isinstance(data.get("scripts"), dict) else {}).get("test")
        if test and "no test specified" not in str(test):
            if shutil.which("npm"):
                return "npm test --silent", "detected: package.json scripts.test + npm on PATH"
            return None, "package.json has scripts.test but npm is not on PATH"
    return None, "no obvious test setup (configure test_command / --test-cmd)"


def gate_plan(settings: Dict[str, Any], cwd: Optional[str], can_write: Optional[bool] = None) -> Dict[str, Any]:
    """What the final gate will do. can_write=None -> describe both cases (status output)."""
    opts = settings.get("options") or DEFAULT_OPTIONS
    mode = opts.get("gate") or "auto"
    tc = opts.get("test_command")
    src = settings["sources"].get("opt.test_command")
    plan = {"mode": mode, "test_command": None, "test_source": None, "kind": "off", "note": ""}  # type: Dict[str, Any]
    if mode == "off":
        plan["note"] = "final gate disabled"
        return plan
    if tc == "none":
        cmd, why = None, "test command disabled (%s)" % src
    elif tc:
        cmd, why = tc, "configured (%s)" % src
    elif cwd:
        cmd, why = detect_test_command(cwd)
    else:
        cmd, why = None, "not detected"
    plan["test_source"] = why
    if mode == "review" or not cmd:
        plan["kind"] = "review"
        if mode == "tests" and not cmd:
            plan["note"] = "gate=tests but no test command (%s); falling back to read-only review" % why
        else:
            plan["note"] = why if not cmd else "gate=review (tests not run)"
        return plan
    plan["test_command"] = cmd
    if can_write is False:
        plan["kind"] = "review"
        plan["note"] = "proposal mode (no --allow-write): diffs are not applied, so tests cannot check them; read-only review"
    else:
        plan["kind"] = "tests"
        plan["note"] = why
    return plan


def build_settings(cwd: str, session_id: Optional[str], extra_layers: Optional[List[Tuple[str, Dict[str, Any]]]] = None) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
    """Merge code defaults < config files < session state < extra layers (spec, CLI)."""
    settings = empty_settings()
    warns = []  # type: List[str]
    for path, data in config_layers(cwd):
        warns.extend(merge_layer(settings, data, layer_label(path)))
    state = load_state(session_id)
    ov = state.get("overrides")
    if isinstance(ov, dict):
        warns.extend(merge_layer(settings, ov, "session toggle"))
    for name, layer in extra_layers or []:
        warns.extend(merge_layer(settings, layer, name))
    return settings, warns, state


def layer_label(path: str) -> str:
    """Short, human label for a config file layer."""
    ap = os.path.abspath(path)
    if ap == os.path.abspath(os.path.join(skill_dir(), "ultracode.json")):
        return "skill ultracode.json"
    if ap == os.path.abspath(os.path.join(codex_home(), "ultracode.json")):
        return "~/.codex/ultracode.json" if codex_home() == os.path.expanduser("~/.codex") else "$CODEX_HOME/ultracode.json"
    env = os.environ.get("ULTRACODE_CONFIG")
    if env and ap == os.path.abspath(os.path.expanduser(env)):
        return "$ULTRACODE_CONFIG"
    return "project .codex/ultracode.json"


def short_src(src: str) -> str:
    return src or ""


def roles_table(resolved: Dict[str, Dict[str, Any]], parent: Dict[str, Any], settings: Dict[str, Any],
                cwd: Optional[str] = None) -> str:
    opts = settings.get("options") or DEFAULT_OPTIONS
    vmode = opts.get("verify_mode") or "fix"
    lines = []
    lines.append(
        "parent: model=%s (%s) | effort=%s (%s)"
        % (parent.get("model") or "?", parent.get("model_method"), parent.get("effort") or "?", parent.get("effort_method"))
    )
    lines.append("%-12s %-16s %-8s %-17s %s" % ("role", "model", "effort", "sandbox", "source"))
    for role in ROLES:
        r = resolved[role]
        m = (r.get("model") or "(default)") + ("*" if r.get("inherited_model") and role != "orchestrator" else "")
        e = (r.get("effort") or "(default)") + ("*" if r.get("inherited_effort") and role != "orchestrator" else "")
        ms, es = short_src(r["model_source"]), short_src(r["effort_source"])
        srcs = ms if ms == es else "model: %s; effort: %s" % (ms, es)
        sb = r.get("sandbox")
        if role == "verifier":
            sb = {"fix": "write: exec's set", "vote": "(unused: vote)", "off": "(off)"}.get(vmode, sb)
        lines.append("%-12s %-16s %-8s %-17s %s" % (role, m, e, sb, srcs))
    lines.append("* = inherited from the parent session")
    lines.append("flow: scout (read) -> skeptic check of scout findings (read-only) -> orchestrator decides -> "
                 "executor (write, disjoint sets) -> verifier -> final gate -> synthesizer")
    if vmode == "fix":
        lines.append("verifier mode: fix - after each executor finishes, one verifier reviews exactly that executor's "
                     "file set, fixes bugs in place (edits outside the set are reverted), reports issues / fixes (diff) "
                     "/ advice%s" % (" [serial]" if opts.get("verify_serial") else ""))
    elif vmode == "vote":
        lines.append("verifier mode: vote - executor changes get %d skeptic votes (majority of valid votes kills); "
                     "no fixes" % settings["caps"]["votes"])
    else:
        lines.append("verifier mode: off - executor changes are not reviewed")
    g = gate_plan(settings, cwd)
    synth = resolved["synthesizer"]
    review = "read-only review of the combined diff by the synthesizer role (%s/%s%s)" % (
        synth.get("gate_model", synth.get("model")) or "default", synth.get("gate_effort", synth.get("effort")) or "default",
        "; scout model refused" if synth.get("gate_model", synth.get("model")) != synth.get("model") else "")
    if g["kind"] == "off":
        lines.append("final gate: off")
    elif g["kind"] == "tests":
        lines.append("final gate: tests `%s` (%s); on failure up to %d verifier fix round(s), then re-test; "
                     "proposal mode (no --allow-write) -> %s" % (
                         g["test_command"], g["note"], settings["caps"]["gate_retries"], review))
    else:
        lines.append("final gate: %s [%s]" % (review, g["note"]))
    if parent.get("effort") and not parent.get("effort_live"):
        lines.append("note: parent effort came from %s, not from this session's own records (ephemeral session or "
                     "no CODEX_THREAD_ID). Native spawns omit it so Codex inherits the real value; for the script "
                     "engine pass --parent-effort if the session runs a different effort." % parent.get("effort_method"))
    c = settings["caps"]
    lines.append(
        "caps: width=%d votes=%d concurrency=%d timeout=%ds output_cap=%dB max_verify_items=%d gate_retries=%d"
        % (c["width"], c["votes"], c["concurrency"], c["timeout_sec"], c["output_cap"], c["max_verify_items"],
           c["gate_retries"])
    )
    for role in CHILD_ROLES:
        for w in resolved[role]["warnings"]:
            lines.append("warning [%s]: %s" % (role, w))
    return "\n".join(lines)

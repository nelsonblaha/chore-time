"""Pure reducer and validation helpers for long-running, multi-session chores.

The HTTP server owns authorization and appending events.  This module is kept
side-effect free so a project can always be reconstructed after a restart and
the same transition rules can be exercised without Home Assistant.
"""

from __future__ import annotations

PHASES = ("session_start", "work", "session_stop", "job_finish")


class TransitionError(ValueError):
    """A requested transition does not match the authoritative project state."""


def definition(chore):
    value = (chore or {}).get("stepped")
    return value if isinstance(value, dict) and value.get("steps") else None


def validate_definition(value):
    """Return a normalized definition, raising for ambiguous/sunsafe shapes."""
    if not isinstance(value, dict):
        raise ValueError("stepped must be an object")
    version = str(value.get("version") or "").strip()
    if not version:
        raise ValueError("stepped.version is required")
    steps, seen = [], set()
    for raw in value.get("steps") or []:
        step = dict(raw)
        sid = str(step.get("id") or "").strip()
        phase = step.get("phase")
        actor = step.get("actor", "kid")
        scope = step.get("scope", "session")
        if not sid or sid in seen:
            raise ValueError("step ids must be nonempty and unique")
        if phase not in PHASES:
            raise ValueError("invalid step phase: %s" % phase)
        if actor not in ("kid", "adult"):
            raise ValueError("invalid step actor: %s" % actor)
        if scope not in ("session", "project"):
            raise ValueError("invalid step scope: %s" % scope)
        if phase in ("session_stop", "job_finish") and step.get("control"):
            raise ValueError("only a work step may be a control")
        step.update(id=sid, actor=actor, scope=scope)
        seen.add(sid)
        steps.append(step)
    if not steps or not any(x["phase"] == "work" for x in steps):
        raise ValueError("a stepped job needs a work step")
    out = dict(value, version=version, steps=steps)
    out["min_team"] = max(1, int(out.get("min_team") or 1))
    out["contexts"] = list(dict.fromkeys(str(x) for x in out.get("contexts") or []))
    return out


def _blank_project(event, config):
    return {
        "instance": event["instance"],
        "chore": event["chore"],
        "definition_version": event["definition_version"],
        "created_at": event["ts"],
        "created_by": event.get("kid") or event.get("by"),
        "revision": 0,
        "status": "held",
        "completed_once": [],
        "sessions": [],
        "active_session": None,
        "finishing": False,
        "completed_at": None,
        "abandoned_at": None,
        "config": config,
    }


def _project_event(event):
    return str(event.get("type") or "").startswith("project_")


def derive(chores, events):
    """Reduce project ledger events into public authoritative state.

    Definitions are selected by the version snapshotted in ``project_start``.
    A missing version is surfaced as ``definition_missing`` rather than silently
    running a changed sequence.
    """
    by_chore = {c["id"]: c for c in chores if definition(c)}
    projects = {}
    for event in events:
        if not _project_event(event):
            continue
        typ = event["type"]
        if typ == "project_start":
            chore = by_chore.get(event.get("chore")) or {}
            raw = definition(chore)
            cfg = validate_definition(raw) if raw and str(raw.get("version")) == str(event.get("definition_version")) else None
            projects[event["instance"]] = _blank_project(event, cfg)
            if cfg is None:
                projects[event["instance"]]["status"] = "definition_missing"
            continue
        project = projects.get(event.get("instance"))
        if not project:
            continue
        project["revision"] += 1
        if typ == "project_session_start":
            session = {
                "id": event["session"], "team": list(event["team"]), "started_at": event["ts"],
                "completed": [], "stop_mode": None, "stop_reason": None, "awards": None,
                "closed_at": None,
            }
            project["sessions"].append(session)
            project["active_session"] = session
            project["status"] = "active"
        elif typ == "project_step":
            session = project.get("active_session")
            if event.get("scope") == "project":
                if event["step"] not in project["completed_once"]:
                    project["completed_once"].append(event["step"])
            elif session and event["step"] not in session["completed"]:
                session["completed"].append(event["step"])
        elif typ == "project_stop":
            session = project.get("active_session")
            if session:
                session["stop_mode"] = event.get("mode", "pause")
                session["stop_reason"] = event.get("reason")
                project["status"] = "stopping"
        elif typ == "project_session_award":
            session = project.get("active_session")
            if session:
                session["awards"] = dict(event.get("awards") or {})
                session["awarded_by"] = event.get("by")
        elif typ == "project_session_close":
            session = project.get("active_session")
            if session:
                session["closed_at"] = event["ts"]
                session["closed_by"] = event.get("by")
                project["finishing"] = event.get("mode") == "finish"
            project["active_session"] = None
            project["status"] = "finishing" if project["finishing"] else "held"
        elif typ == "project_complete":
            project["active_session"] = None
            project["finishing"] = False
            project["completed_at"] = event["ts"]
            project["completed_by"] = event.get("by")
            project["status"] = "complete"
        elif typ == "project_abandon":
            project["active_session"] = None
            project["finishing"] = False
            project["abandoned_at"] = event["ts"]
            project["abandoned_by"] = event.get("by")
            project["status"] = "abandoned"
        elif typ == "project_migrate":
            # Migrations are explicit ledger facts; a current matching config is
            # still required before the instance may proceed.
            project["definition_version"] = str(event["definition_version"])
            chore = by_chore.get(project["chore"]) or {}
            raw = definition(chore)
            project["config"] = validate_definition(raw) if raw and str(raw.get("version")) == project["definition_version"] else None
            project["status"] = "held" if project["config"] else "definition_missing"
    return list(projects.values())


def current(project):
    """Describe the single legal next action for a reduced project."""
    if project["status"] in ("complete", "abandoned", "definition_missing"):
        return {"kind": project["status"]}
    cfg = project["config"]
    session = project.get("active_session")
    if not session:
        if project.get("finishing"):
            for step in cfg["steps"]:
                if step["phase"] == "job_finish" and step["id"] not in project["completed_once"]:
                    return {"kind": "step", "phase": "job_finish", "step": step}
            return {"kind": "complete"}
        return {"kind": "start", "min_team": cfg["min_team"]}

    done_session = set(session["completed"])
    done_project = set(project["completed_once"])
    phases = ("session_stop",) if session.get("stop_mode") else ("session_start", "work")
    for phase in phases:
        for step in cfg["steps"]:
            if step["phase"] != phase:
                continue
            done = done_project if step["scope"] == "project" else done_session
            if step["id"] in done:
                continue
            if phase == "work" and step.get("control"):
                return {"kind": "work", "phase": phase, "step": step}
            return {"kind": "step", "phase": phase, "step": step}
    if session.get("stop_mode"):
        return {"kind": "award", "mode": session["stop_mode"], "team": list(session["team"])}
    # A definition without a control work step can be stopped after its work
    # steps are complete.
    return {"kind": "work", "phase": "work", "step": None}


def public_project(project):
    """Compact state for dashboards; omit the duplicated private config object."""
    out = {k: v for k, v in project.items() if k != "config"}
    out["next"] = current(project)
    return out


def find_open(projects, chore):
    rows = [p for p in projects if p["chore"] == chore and p["status"] not in ("complete", "abandoned")]
    return rows[-1] if rows else None


def require_revision(project, supplied):
    try:
        supplied = int(supplied)
    except (TypeError, ValueError):
        raise TransitionError("This screen is out of date. Reloading the current step.")
    if supplied != project["revision"]:
        raise TransitionError("This step changed on another screen. Reloading the current step.")


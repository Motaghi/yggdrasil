"""Model resolution: work package beats PID beats programme beats default (A1.11)."""

from __future__ import annotations

from . import config, state, tracker, util


def base_models() -> tuple[dict, str]:
    """The model map before programme, PID and package overrides.

    Packaged defaults are the lowest layer: the core and preset `models`, or, with `launcher_cli: codex`,
    the `codex_models` map in their place. Explicit `models` settings in the user config or profile then
    apply on top, key by key, so they are never discarded by the Codex defaults.
    """
    packaged = {}
    explicit = []
    for label, values in config.file_layers():
        kind = label.split(":", 1)[0]
        if not isinstance(values.get("models"), dict):
            continue
        if kind in ("core", "preset"):
            packaged = dict(values["models"])
        else:
            explicit.append((label, values["models"]))
    merged = dict(packaged)
    if config.get("launcher_cli") == "codex" and isinstance(config.get("codex_models"), dict):
        merged = dict(config.get("codex_models"))
    for _, values in explicit:
        merged.update(values)
    return merged, "default"  # A1.11: file layers all report as `default`


def resolve(node_id: str, package: str | None = None) -> dict:
    return resolve_node(state.require_node(node_id, state.all_nodes()), package)


def core_models() -> dict:
    """The packaged core `models` map (the programme reviewer's model, D1)."""
    values = dict(config.file_layers())["core"].get("models")
    return dict(values) if isinstance(values, dict) else {}


def resolve_node(node: dict, package: str | None = None) -> dict:
    """Resolve the models of a node record, which may not be saved yet (`ygg node add`, D1)."""
    node_id = node["id"]
    merged, source = base_models()
    programme = tracker.programme(node["programme"]) if node.get("programme") else None
    if programme and isinstance(programme.get("models"), dict) and programme["models"]:
        merged.update(programme["models"])
        source = "programme"
    if node.get("project") and tracker.read_document(node["project"], "pid"):
        from .commands import approved_pid, pid_document
        approved = approved_pid(node["project"])
        # After approval only the user-approved copy counts; before it, the draft PID is shown.
        pid_models = (approved.get("guarded") or {}).get("models") if approved is not None else \
            pid_document(node["project"]).header.get("models")
        if isinstance(pid_models, dict) and pid_models:
            merged.update(pid_models)
            source = "pid"
    if package:
        util.check_record_id(package)
        issue = tracker.read_issue(node["project"], package) if node.get("project") else None
        if issue is not None and isinstance(issue.get("models"), dict) and issue.get("models"):
            merged.update(issue.get("models"))
            source = "package"
    model, effort = merged.get("model"), merged.get("effort")
    pair_model, pair_effort = merged.get("pair_model"), merged.get("pair_effort")
    if node["role"] == "reviewer" and not node.get("project"):
        # The programme reviewer (morning go) always runs on Claude with the core model (D1).
        core = core_models()
        model, effort = core.get("model"), core.get("effort")
    elif node["role"] == "reviewer":
        model, effort = pair_model, pair_effort
    pools = (config.profile() or {}).get("pools") or {}
    if node["role"] == "l1":
        # With a profile `pools` mapping, L1 uses its l1_* entries when set; everything the conductor
        # starts uses the launched_* entries. Without one, no provider is named.
        main_pool_value = pools.get("l1_claude") or pools.get("launched_claude")
        pair_pool_value = pools.get("l1_codex") or pools.get("launched_codex")
    else:
        main_pool_value = pools.get("launched_claude")
        pair_pool_value = pools.get("launched_codex")
    is_codex = str(model or "").startswith("gpt")
    return {"node": node_id, "model": model, "effort": effort, "pair_model": pair_model,
            "pair_effort": pair_effort, "pool": (pair_pool_value if is_codex else main_pool_value) if pools else None,
            "pair_pool": pair_pool_value if pools else None, "source": source}


# The roles v1.1.0 refused on Codex (D1); since v1.1.1 all four run on Codex (CB-D1).
BUILDER_ROLES = ("pm", "tl", "worker", "su")


def is_codex(model) -> bool:
    return str(model or "").startswith("gpt")



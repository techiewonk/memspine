"""TI-5b: the tuning commands of the step mode (mixed into ``StepSession``).

    config [prefix] | config diff       the effective engine config, with default and source of each value
    set <key>=<value> | unset <key>     a live override, validated by the config schema
    params <stage-id|name>              the config keys that affect one stage (docs/v2/data/stage_params.json)
    prompt list | show <id> | edit <id> <file> | reset <id>
    rerun window <window_id> [--commit] extraction + envelope stages again with the current params and prompts
    rerun ask <qid|text>                a read again, diffed against its previous run
    sweep <key> <v1,v2,...> ask <targets>   the reads once per value (read-side keys only)
    export arm <name>                   the current arm + overrides as ``evals/arms/<name>.json``

Every set, unset, prompt change, rerun, sweep and export is logged to ``tuning.jsonl`` (and copied into
the saved bundle). The module touches the engine only through the running ``MemspineSystem``.
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["TuneMixin", "flatten", "get_path", "set_path"]

#: first segments whose change needs a rebuilt engine (models, stores, wiring decided at start)
REBUILD_PREFIXES = (
    "embedding",
    "storage",
    "vector",
    "cache",
    "graph",
    "llm",
    "memories",
    "namespaces",
    "profile",
    "data_profile",
    "template",
    "observability",
    "event_log",
    "workers",
    "read.rerank",
    "read.rerank_settings",
    "read.hybrid",
    "read.static_embedding_prefilter",
    "read.lexical_provider",
    "read.lexical_analyzer",
)
#: prompts that are code constants, not registry files: replaced in the model call instead
CODE_PROMPTS = ("structured", "structured.window", "structured.repair")
READER_PROMPT = "reader.qa"
STAGE_PARAMS = Path(__file__).resolve().parents[3] / "docs" / "v2" / "data" / "stage_params.json"


# ------------------------------------------------------------------------------------------ dict paths
def flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    """Nested dict -> dotted leaf keys (an empty dict and every list is a leaf)."""
    out: dict[str, Any] = {}
    if isinstance(d, Mapping) and d:
        for k, v in d.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    else:
        out[prefix] = d
    return out


def get_path(d: Any, key: str, default: Any = None) -> Any:
    cur = d
    for part in key.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        elif not isinstance(cur, Mapping) and hasattr(cur, part):
            cur = getattr(cur, part)
        else:
            return default
    return cur


def set_path(d: dict[str, Any], key: str, value: Any) -> None:
    cur = d
    parts = key.split(".")
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = cur[part] = {}
        cur = nxt
    cur[parts[-1]] = value


def del_path(d: dict[str, Any], key: str) -> None:
    parts = key.split(".")
    chain = [d]
    for part in parts[:-1]:
        nxt = chain[-1].get(part)
        if not isinstance(nxt, dict):
            return
        chain.append(nxt)
    chain[-1].pop(parts[-1], None)
    for i in range(len(parts) - 2, -1, -1):  # drop dicts the removal emptied
        if not chain[i + 1]:
            chain[i].pop(parts[i], None)


def _short(value: Any, n: int = 80) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3] + "..."


def parse_value(raw: str) -> Any:
    """``12`` -> 12, ``true`` -> True, ``null`` -> None, ``[1,2]`` / ``{"a":1}`` -> json, else the string."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


# ----------------------------------------------------------------------------------------------- mixin
class TuneMixin:
    TUNE_COMMANDS = ("config", "set", "unset", "params", "prompt", "rerun", "sweep", "export")

    # ---- state
    def _init_tuning(self, cfg: Mapping[str, Any]) -> None:
        self.arm_cfg_orig: dict[str, Any] = copy.deepcopy(
            dict(getattr(self, "_arm_cfg_orig", None) or cfg)
        )
        self.base_cfg: dict[str, Any] = copy.deepcopy(dict(cfg))
        self.overrides: dict[str, Any] = {}
        self.prompt_overrides: dict[str, dict[str, str]] = {}
        self.tuning_log: list[dict[str, Any]] = []
        self._base_dump: dict[str, Any] | None = None
        self._prompt_hook_on = False
        self.arms_dir: Path | None = None
        self.stage_params_path: Path | None = None
        self._shipped: dict[str, dict[str, Any]] = {}

    def _compose_cfg(self) -> dict[str, Any]:
        cfg = copy.deepcopy(self.base_cfg)
        for key, val in self.overrides.items():
            set_path(cfg, key, copy.deepcopy(val))
        return cfg

    def _log_tuning(
        self,
        command: str,
        key: str | None = None,
        old: Any = None,
        new: Any = None,
        effect: str = "",
    ) -> None:
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "command": command,
            "key": key,
            "old": old,
            "new": new,
            "effect": effect,
        }
        self.tuning_log.append(row)
        self.d_mem.mkdir(parents=True, exist_ok=True)
        with (self.d_mem / "tuning.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str, ensure_ascii=False) + "\n")

    # ---- the effective config
    def _engine_cfg(self) -> Any:
        return self.system._engine._resolved.config

    def _effective(self) -> dict[str, Any]:
        return self._engine_cfg().model_dump(mode="json")

    def _defaults(self) -> dict[str, Any]:
        from memspine.config.schema import MemspineConfig

        return MemspineConfig().model_dump(mode="json")

    def _capture_base(self) -> None:
        if self._base_dump is None and self.system is not None:
            self._base_dump = self._effective()

    def _stage_params(self) -> dict[str, Any]:
        path = self.stage_params_path or STAGE_PARAMS
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("stages", data) if isinstance(data, dict) else {}

    def _param_info(self, key: str) -> dict[str, Any] | None:
        for st in self._stage_params().values():
            for k in st.get("keys", []):
                if k.get("key") == key:
                    return k
        return None

    def _needs_rebuild(self, key: str) -> bool:
        info = self._param_info(key)
        if info is not None and "rebuild" in info:
            return bool(info["rebuild"]) and str(info.get("rebuild_scope", "")) != "read"
        return any(key == p or key.startswith(p + ".") for p in REBUILD_PREFIXES)

    def _source(self, key: str, value: Any, default: Any) -> str:
        if key in self.overrides or any(key.startswith(o + ".") for o in self.overrides):
            return "override"
        if key in flatten(self.base_cfg) or get_path(self.base_cfg, key, _MISSING) is not _MISSING:
            return "arm"
        return "default" if value == default else "template"

    async def cmd_config(self, arg: str) -> None:
        diff_only = arg.strip().lower().startswith("diff")
        prefix = "" if diff_only else arg.strip()
        eff, dflt = flatten(self._effective()), flatten(self._defaults())
        rows = []
        for key in sorted(eff):
            if prefix and not (
                key == prefix or key.startswith(prefix.rstrip(".") + ".") or key.startswith(prefix)
            ):
                continue
            value, default = eff[key], dflt.get(key, None)
            if diff_only and value == default:
                continue
            rows.append((key, value, default, self._source(key, value, default)))
        self.head(
            f"config{' diff' if diff_only else ''}{(' ' + prefix) if prefix else ''}: {len(rows)} value(s)"
        )
        limit = 400 if self.wide else 120
        for key, value, default, src in rows[:limit]:
            tail = "" if value == default else f"   (default {_short(default, 40)})"
            self.w(f"  {key} = {_short(value, 60)}{tail}  [{src}]")
        if len(rows) > limit:
            self.w(f"  ... {len(rows) - limit} more (config <prefix> narrows; --wide prints all)")
        if self.overrides:
            self.w(
                f"  overrides: {', '.join(f'{k}={_short(v, 30)}' for k, v in self.overrides.items())}"
            )

    # ---- set / unset
    def _validated(self, key: str, value: Any) -> tuple[Any, str | None]:
        """The config with ``key=value`` parsed by the schema; ``(None, error)`` when it does not fit."""
        from memspine.config.schema import MemspineConfig

        dump = self._effective()
        parent = get_path(dump, key.rsplit(".", 1)[0]) if "." in key else dump
        if not isinstance(parent, dict):
            return None, f"no such config section {key.rsplit('.', 1)[0]!r}"
        if key.rsplit(".", 1)[-1] not in parent and not self._free_dict(key):
            near = [k for k in flatten(dump) if k.startswith(key.rsplit(".", 1)[0] + ".")][:6]
            return None, f"unknown key {key!r}" + (
                f" (siblings: {', '.join(near)})" if near else ""
            )
        set_path(dump, key, value)
        try:
            new = MemspineConfig.model_validate(dump)
        except Exception as exc:
            return None, _short(str(exc).replace("\n", " "), 240)
        return new, None

    @staticmethod
    def _free_dict(key: str) -> bool:
        """A key inside an open dict of the schema (``read.scoring.*``, ``memories.*.policies.*``)."""
        return (
            key.startswith(("read.scoring.", "read.assembly.", "read.compression.", "prompts."))
            or ".policies." in key
        )

    async def cmd_set(self, arg: str) -> None:
        key, sep, raw = arg.partition("=")
        key, raw = key.strip(), raw.strip()
        if not sep or not key:
            self.w(
                "usage: set <dotted.key>=<value>   e.g. set read.rrf_k=30 | set read.hybrid=false"
            )
            return
        await self._set_value(key, parse_value(raw), command="set")

    async def _set_value(self, key: str, value: Any, command: str, quiet: bool = False) -> bool:
        self._capture_base()
        old = get_path(self._effective(), key)
        new_cfg, err = self._validated(key, value)
        if err:
            self.w(f"set refused: {err}")
            return False
        self.overrides[key] = value
        self.cfg = self._compose_cfg()
        rebuild = self._needs_rebuild(key)
        if rebuild:
            effect = "needs a rebuild: run `reset` to rebuild the engine with this override (nothing written is kept)"
        else:
            self._apply_live(key, new_cfg)
            effect = "applied to the running engine"
        self._log_tuning(command, key, old, value, effect)
        if not quiet:
            self.head(f"{command} {key}")
            self.w(f"  {key}: {_short(old, 50)} -> {_short(value, 50)}   {effect}")
            if not rebuild:
                self.w(
                    "  (read per call keys take effect on the next `ask`; a key a stage bound at start needs `reset`)"
                )
        return True

    def _apply_live(self, key: str, validated: Any) -> None:
        eng = self.system._engine
        eng._resolved.config = _replace(
            eng._resolved.config, key.split("."), get_path(validated, key)
        )
        if key.startswith("read.") and key.split(".")[1] in ("scoring", "assembly", "compression"):
            with contextlib.suppress(Exception):
                from memspine.api import engine as eng_mod

                cfg = eng._resolved.config
                if key.split(".")[1] == "scoring":
                    eng._scoring = eng_mod.ScoringPolicy.bind(cfg.read.scoring)
                elif key.split(".")[1] == "assembly":
                    eng._assembly = eng_mod.AssemblyPolicy.bind(cfg.read.assembly)
                else:
                    eng._assembly_compression = eng_mod.CompressionPolicy.bind(
                        eng_mod._as_options_dict(cfg.read.compression)
                    )
        sys_cfg = {k: v for k, v in self.cfg.items() if k != "template"}
        with contextlib.suppress(Exception):
            self.system.config = copy.deepcopy(sys_cfg)

    async def cmd_unset(self, arg: str) -> None:
        key = arg.strip()
        if key not in self.overrides:
            self.w(f"no override {key!r} (overrides: {', '.join(self.overrides) or 'none'})")
            return
        old = self.overrides.pop(key)
        self.cfg = self._compose_cfg()
        base = get_path(self._base_dump or {}, key)
        effect = "restored"
        if self._needs_rebuild(key):
            effect = "needs a rebuild: run `reset`"
        else:
            new_cfg, err = self._validated(key, base)
            if err is None:
                self._apply_live(key, new_cfg)
        self._log_tuning("unset", key, old, base, effect)
        self.head(f"unset {key}")
        self.w(f"  {key}: {_short(old, 50)} -> {_short(base, 50)}   {effect}")

    # ---- params
    async def cmd_params(self, arg: str) -> None:
        arg = arg.strip()
        if not arg:
            self.w(
                "usage: params <stage-id|stage-name|key-prefix>   e.g. params B7 | params fusion | params read.rrf"
            )
            return
        eff = flatten(self._effective())
        stages = self._stage_params()
        low = arg.lower()
        hit = {sid: st for sid, st in stages.items() if sid.lower() == low} or {
            sid: st for sid, st in stages.items() if low in str(st.get("name", "")).lower()
        }
        if hit:
            for sid, st in hit.items():
                self.head(f"params {sid}: {st.get('name')}")
                keys = [
                    k for k in st.get("keys", []) if not str(k.get("key", "")).startswith("cli:")
                ]
                for k in keys:
                    key = k["key"]
                    rng = k.get("choices") or k.get("range")
                    self.w(
                        f"  {key} = {_short(eff.get(key, '(not in the effective config)'), 40)}  default {_short(k.get('default'), 30)}"
                        f"  type {k.get('type')}"
                        + (
                            f"  {'choices' if k.get('choices') else 'range'} {_short(rng, 40)}"
                            if rng
                            else ""
                        )
                        + (
                            "  [needs rebuild]"
                            if k.get("rebuild") and k.get("rebuild_scope") != "read"
                            else ""
                        )
                    )
                    if k.get("description"):
                        self.w(f"      {_short(k['description'], 160)}")
                if not keys:
                    self.w("  (no config keys listed for this stage)")
                for pr in st.get("prompts", []):
                    self.w(
                        f"  prompt {pr.get('prompt_id')} (role {pr.get('role')}, v{pr.get('version')}): `prompt show {pr.get('prompt_id')}`"
                    )
                for md in st.get("models", []):
                    self.w(
                        f"  model role {md.get('role')} <- {md.get('source')}"
                        + (f" (default {md['default']})" if md.get("default") else "")
                    )
            return
        # fallback: prefix / substring match on the config schema
        dflt = flatten(self._defaults())
        keys = sorted(k for k in dflt if k.startswith(arg) or low in k.lower())
        self.head(
            f"params {arg}: {len(keys)} key(s) from the config schema"
            + ("" if stages else " (docs/v2/data/stage_params.json not found)")
        )
        for key in keys[:80]:
            info = self._schema_info(key)
            self.w(
                f"  {key} = {_short(eff.get(key), 40)}  default {_short(dflt[key], 30)}"
                + (f"  {info}" if info else "")
                + ("  [needs rebuild]" if self._needs_rebuild(key) else "")
            )
        if len(keys) > 80:
            self.w(f"  ... {len(keys) - 80} more (narrow the prefix)")
        if not keys:
            self.w("  nothing matches")

    def _schema_info(self, key: str) -> str:
        from pydantic import BaseModel

        from memspine.config.schema import MemspineConfig

        model: Any = MemspineConfig
        field = None
        for part in key.split("."):
            fields = getattr(model, "model_fields", None)
            if not fields or part not in fields:
                return ""
            field = fields[part]
            ann = field.annotation
            model = ann if isinstance(ann, type) and issubclass(ann, BaseModel) else None
            if model is None:
                break
        if field is None:
            return ""
        bits = []
        meta = [m for m in field.metadata]
        lo = next(
            (
                getattr(m, "ge", None) or getattr(m, "gt", None)
                for m in meta
                if getattr(m, "ge", None) is not None or getattr(m, "gt", None) is not None
            ),
            None,
        )
        hi = next(
            (
                getattr(m, "le", None) or getattr(m, "lt", None)
                for m in meta
                if getattr(m, "le", None) is not None or getattr(m, "lt", None) is not None
            ),
            None,
        )
        if lo is not None or hi is not None:
            bits.append(f"range [{lo}, {hi}]")
        args = getattr(field.annotation, "__args__", None)
        if args and all(isinstance(a, str) for a in args):
            bits.append(f"choices {list(args)}")
        elif field.annotation is not None:
            bits.append(f"type {getattr(field.annotation, '__name__', str(field.annotation))[:30]}")
        if field.description:
            bits.append(_short(field.description, 100))
        return "  ".join(bits)

    # ---- prompts
    def _registry(self) -> Any:
        return self.system._engine._prompts

    def _code_prompt_text(self, pid: str) -> str:
        from memspine.prompts import structured as S

        if pid == "structured":
            return S.STRUCTURED_SYSTEM
        if pid == "structured.window":
            return S.window_messages(["facts", "edges", "episode"], "[1] Speaker: text")[0][
                "content"
            ]
        # structured.repair: the final user message, with {n} and {problems}
        return (
            "{n} of your items failed validation. Problems, in the same order as the items:\n{problems}\n"
            "Reply with ONE JSON array of exactly {n} corrected items, in the same order. Fix only what the problems "
            "name; do not add items, do not drop items, and keep every citation to a numbered line that exists. If an "
            "item cannot be fixed from the lines, return it unchanged."
        )

    def _reader_prompt(self) -> str:
        from .. import readers

        return str(getattr(readers, "DEFAULT_QA_PROMPT", ""))

    def _prompt_ids(self) -> list[tuple[str, str, str]]:
        rows = [(p.id, p.role, f"v{p.version}") for p in self._registry().list()]
        rows += [(pid, "code", "constant") for pid in CODE_PROMPTS]
        rows.append((READER_PROMPT, "reader", "harness"))
        return rows

    async def cmd_prompt(self, arg: str) -> None:
        sub, _, rest = arg.strip().partition(" ")
        sub, rest = sub.lower(), rest.strip()
        if sub in ("", "list"):
            self.head("prompts (role, version, state)")
            for pid, role, ver in self._prompt_ids():
                state = "edited" if pid in self.prompt_overrides else "shipped"
                self.w(f"  {pid:<28} {role:<16} {ver:<9} {state}")
        elif sub == "show":
            await self._prompt_show(rest)
        elif sub == "edit":
            pid, _, path = rest.partition(" ")
            await self._prompt_edit(pid.strip(), path.strip())
        elif sub == "reset":
            await self._prompt_reset(rest)
        else:
            self.w("usage: prompt list | show <id> | edit <id> <file> | reset <id>")

    def _known_prompt(self, pid: str) -> bool:
        return (
            pid in CODE_PROMPTS
            or pid == READER_PROMPT
            or any(p.id == pid for p in self._registry().list())
        )

    async def _prompt_show(self, pid: str) -> None:
        if not self._known_prompt(pid):
            self.w(f"unknown prompt {pid!r}; try `prompt list`")
            return
        edited = self.prompt_overrides.get(pid)
        self.head(f"prompt {pid}" + (" (edited this session)" if edited else ""))
        if pid in CODE_PROMPTS:
            self.w(
                "  system (code constant; `prompt edit` replaces the whole text in the model call):"
                if pid != "structured.repair"
                else "  user message (placeholders {n} and {problems}):"
            )
            self.w(self._indent((edited or {}).get("system") or self._code_prompt_text(pid)))
        elif pid == READER_PROMPT:
            self.w(self._indent((edited or {}).get("body") or self._reader_prompt()))
        else:
            p = self._registry().get(pid)
            self.w(
                f"  role={p.role} version={p.version} format={p.format.value} output_model={p.output_model}"
            )
            if p.system:
                self.w("  system:")
                self.w(self._indent(p.system))
            self.w("  body:")
            self.w(self._indent(p.body))
        seen = [
            r for r in self.sink.rows if r["event"] == "write.extract.io" and r.get("prompt_text")
        ]
        if pid in ("structured.window", "window_extract", "structured") and seen:
            self.w("  last rendered (from write.extract.io):")
            self.w(self._indent(str(seen[-1]["prompt_text"]), 1200 if self.wide else 400))

    @staticmethod
    def _indent(text: str, limit: int = 100000) -> str:
        text = text if len(text) <= limit else text[:limit] + " ..."
        return "\n".join("    | " + ln for ln in str(text).splitlines())

    async def _prompt_edit(self, pid: str, path: str) -> None:
        if not pid or not path or not Path(path).exists():
            self.w(f"usage: prompt edit <id> <file>   (file not found: {path!r})")
            return
        if not self._known_prompt(pid):
            self.w(f"unknown prompt {pid!r}; try `prompt list`")
            return
        text = Path(path).read_text(encoding="utf-8")
        old = self.prompt_overrides.get(pid)
        if pid in CODE_PROMPTS:
            self.prompt_overrides[pid] = {"system": text.rstrip("\n")}
        elif pid == READER_PROMPT:
            self.prompt_overrides[pid] = {"body": text.rstrip("\n")}
        else:
            self.prompt_overrides[pid] = self._parse_registry_edit(text)
        self._apply_prompt_overrides_live()
        self._log_tuning(
            "prompt edit",
            pid,
            "(edited)" if old else "(shipped)",
            f"{len(text)} chars from {Path(path).name}",
            "applied to the running engine; `rerun window` shows its effect",
        )
        self.head(f"prompt edit {pid}")
        self.w(
            f"  {len(text)} chars from {path}; used by the next model call of this role (`prompt reset {pid}` undoes it)"
        )

    @staticmethod
    def _parse_registry_edit(text: str) -> dict[str, str]:
        """A registry prompt file: plain text = the body, or the shipped format (``system: ...`` front matter,
        ``---``, body)."""
        if "\n---" in text and re.match(r"\s*(id|system|version|role):", text):
            import yaml

            head, _, body = text.partition("\n---")
            meta = yaml.safe_load(head) or {}
            out = {"body": body.lstrip("\n").rstrip("\n")}
            if meta.get("system"):
                out["system"] = str(meta["system"]).strip()
            return out
        return {"body": text.rstrip("\n")}

    async def _prompt_reset(self, pid: str) -> None:
        if pid not in self.prompt_overrides:
            self.w(f"no edit of {pid!r} (edited: {', '.join(self.prompt_overrides) or 'none'})")
            return
        self.prompt_overrides.pop(pid)
        if pid in self._shipped:
            if pid not in CODE_PROMPTS and pid != READER_PROMPT:
                p = self._registry().get(pid)
                for k, v in self._shipped.pop(pid).items():
                    object.__setattr__(p, k, v)
            else:
                self._shipped.pop(pid)
        self._apply_prompt_overrides_live()
        self._log_tuning("prompt reset", pid, "(edited)", "(shipped)", "restored")
        self.head(f"prompt reset {pid}")
        self.w("  restored the shipped text")

    def _apply_prompt_overrides_live(self) -> None:
        """Registry prompts are changed in place (the writers hold the same objects); the code constants and
        the reader prompt are replaced in the call by the hook / the reader."""
        reg = getattr(self.system._engine, "_prompts", None) if self.system is not None else None
        for pid, ed in self.prompt_overrides.items():
            if pid in CODE_PROMPTS or pid == READER_PROMPT or reg is None:
                continue
            p = reg.get(pid)
            self._shipped.setdefault(
                pid, {"body": p.body, "system": p.system, "version": p.version}
            )
            object.__setattr__(p, "body", ed.get("body", p.body))
            if "system" in ed:
                object.__setattr__(p, "system", ed["system"])
            object.__setattr__(p, "version", self._shipped[pid]["version"] + 1)

    def _install_prompt_hook(self) -> None:
        """Wrap ``litellm.acompletion`` once: a call whose messages are a code-constant prompt gets the edited text."""
        import litellm

        from memspine.prompts import structured as S

        inner = litellm.acompletion
        owner = self

        async def hooked(**kwargs: Any) -> Any:
            ov = owner.prompt_overrides
            if ov:
                msgs = kwargs.get("messages") or []
                new = owner._edit_messages(S, msgs, ov)
                if new is not None:
                    kwargs = {**kwargs, "messages": new}
            return await inner(**kwargs)

        litellm.acompletion = hooked

        def restore() -> None:
            litellm.acompletion = inner

        self._stack.callback(restore)
        self._prompt_hook_on = True

    @staticmethod
    def _edit_messages(
        S: Any, msgs: list[Mapping[str, Any]], ov: Mapping[str, Mapping[str, str]]
    ) -> list[dict[str, Any]] | None:
        if not msgs:
            return None
        out = [dict(m) for m in msgs]
        first = str(out[0].get("content", ""))
        changed = False
        if first.startswith(S._WINDOW_HEAD[:80]) and "structured.window" in ov:
            out[0]["content"], changed = ov["structured.window"]["system"], True
        elif first.startswith(S.STRUCTURED_SYSTEM[:80]) and "structured" in ov:
            out[0]["content"], changed = ov["structured"]["system"], True
        last = str(out[-1].get("content", ""))
        m = re.match(
            r"(\d+) of your items failed validation\. Problems, in the same order as the items:\n(.*)\nReply with ONE JSON array of exactly",
            last,
            re.S,
        )
        if m and "structured.repair" in ov:
            out[-1]["content"] = (
                ov["structured.repair"]["system"]
                .replace("{n}", m.group(1))
                .replace("{problems}", m.group(2))
            )
            changed = True
        return out if changed else None

    # ---- the commands that need a window or a question
    def _window_rows(self, rows: list[dict[str, Any]], window_id: str) -> list[dict[str, Any]]:
        return [r for r in rows if r.get("window_id") == window_id]

    def _window_view(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Per envelope (by occurrence id): text, the status at each stage, and the integration verdict."""
        view: dict[str, dict[str, Any]] = {}
        for r in rows:
            if r["event"] != "write.envelope.trace":
                continue
            v = view.setdefault(
                str(r.get("occ")), {"text": None, "stages": {}, "integrate": None, "final": None}
            )
            v["text"] = r.get("text") or v["text"]
            v["stages"][str(r.get("stage"))] = r.get("status") + (
                f"({r['reason']})" if r.get("reason") else ""
            )
            integ = r.get("integrate")
            if isinstance(integ, dict) and any(integ.values()):
                v["integrate"] = f"{integ.get('op')}/{integ.get('rung')}/{integ.get('outcome')}"
            v["final"] = r.get("status")
        return view

    async def cmd_rerun(self, arg: str) -> None:
        sub, _, rest = arg.strip().partition(" ")
        if sub.lower() == "window":
            commit = "--commit" in rest
            await self._rerun_window(rest.replace("--commit", "").strip(), commit)
        elif sub.lower() == "ask":
            await self._rerun_ask(rest.strip())
        else:
            self.w("usage: rerun window <window_id> [--commit] | rerun ask <qid|text>")

    async def _rerun_window(self, window_id: str, commit: bool) -> None:
        from types import SimpleNamespace

        from memspine.observability import tap
        from memspine.workers import windows as W

        members = next(
            (
                r
                for r in self.sink.rows
                if r["event"] == "write.window.members"
                and r.get("window_id") == window_id
                and r.get("member_record_ids")
            ),
            None,
        )
        if members is None:
            ids = sorted(
                {
                    r.get("window_id")
                    for r in self.sink.rows
                    if r["event"] == "write.window.members" and r.get("window_id")
                }
            )
            self.w(
                f"no window {window_id!r} (windows so far: {', '.join(map(str, ids)) or 'none'})"
            )
            return
        pipe = getattr(self.system._engine, "_windows", None)
        if pipe is None:
            self.w("this arm has no window write (write.window_extract is off)")
            return
        before = self._window_view(self._window_rows(self.sink.rows, window_id))
        recs = {str(r.record_id): r for r in await self._records()}
        ids = [str(x) for x in members["member_record_ids"]]
        records = [recs[i] for i in ids if i in recs]
        if not records:
            self.w("the window's records are gone from the store")
            return
        scratch = type(self.sink)()
        job = W._Job(
            namespace=self.item,
            window=SimpleNamespace(
                session=members.get("session"),
                turns=[SimpleNamespace(record_id=i) for i in ids],
                reason="rerun",
                tokens=0,
            ),
        )
        saved_cache = pipe._cache
        pipe._cache = None  # the extraction cache is keyed by content, not by the edited prompt
        try:
            with (
                self.system._scope(phase="rerun", window_id=window_id),
                tap.capture_only(scratch),  # the rerun trace goes to the scratch sink only
                tap.request("write", rerun=True),
            ):
                prepared = await pipe._prepare_records(self.item, job, records, strict=False)
                if commit and prepared is not None:
                    await pipe._commit_job(self.item, pipe._store(self.item), prepared)
        finally:
            pipe._cache = saved_cache
        after = self._window_view(scratch.rows)
        self.head(
            f"rerun window {window_id}: {len(records)} turn(s)"
            + ("  (--commit: deposited)" if commit else "  (scratch: nothing deposited)")
        )
        if prepared is None:
            self.w("  the window produced nothing (skipped or cancelled)")
        n_add, n_del, n_chg = self._print_window_diff(before, after)
        io = [r for r in scratch.rows if r["event"] == "write.extract.io"]
        for r in io[:1]:
            self.w(
                f"  extract.io now: model={r.get('model')} tokens={r.get('tokens')} sections={r.get('sections')}"
            )
            self.w(f"    prompt: {_short(r.get('prompt_text'), 200)}")
        if commit:
            self.w(
                "  warning: --commit deposited a second copy of this window's items; `reset` starts clean"
            )
        self._log_tuning(
            "rerun window",
            window_id,
            f"{len(before)} envelope(s)",
            f"{len(after)} envelope(s)",
            f"added {n_add}, removed {n_del}, changed {n_chg}" + ("; committed" if commit else ""),
        )

    @staticmethod
    def _stage_delta(vb: Mapping[str, Any], va: Mapping[str, Any]) -> dict[str, tuple[Any, Any]]:
        """Stages both runs went through whose status differs (a scratch run never reaches ``deposit``)."""
        common = set(vb["stages"]) & set(va["stages"])
        return {
            st: (vb["stages"][st], va["stages"][st])
            for st in sorted(common)
            if vb["stages"][st] != va["stages"][st]
        }

    def _print_window_diff(
        self, before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
    ) -> tuple[int, int, int]:
        def by_text(view: dict[str, dict[str, Any]]) -> dict[str, tuple[str, dict[str, Any]]]:
            return {" ".join(str(v["text"] or k).lower().split()): (k, v) for k, v in view.items()}

        b, a = by_text(before), by_text(after)
        added = [t for t in a if t not in b]
        removed = [t for t in b if t not in a]
        changed = []
        for t in a:
            if t in b:
                (_, vb), (_, va) = b[t], a[t]
                if self._stage_delta(vb, va) or vb["integrate"] != va["integrate"]:
                    changed.append(t)
        self.w(
            f"  items before {len(before)}, after {len(after)}: +{len(added)} added, -{len(removed)} removed, ~{len(changed)} changed, "
            f"={len(a) - len(added) - len(changed)} same"
        )
        for t in added:
            self.w(f"  + {_short(a[t][1]['text'], 100)}   stages {_short(a[t][1]['stages'], 80)}")
        for t in removed:
            self.w(f"  - {_short(b[t][1]['text'], 100)}")
        for t in changed:
            (_, vb), (_, va) = b[t], a[t]
            self.w(f"  ~ {_short(va['text'], 80)}")
            for stage, (x, y) in self._stage_delta(vb, va).items():
                self.w(f"      stage {stage}: {x} -> {y}")
            if vb["integrate"] != va["integrate"]:
                self.w(f"      integration verdict: {vb['integrate']} -> {va['integrate']}")
        if changed or added or removed:
            self.w(
                "  (integration is judged against the live store, which already holds this window's first run)"
            )
        return len(added), len(removed), len(changed)

    async def _rerun_ask(self, arg: str) -> None:
        if not arg:
            self.w("usage: rerun ask <qid|text>")
            return
        qid, q = self._find_question(arg)
        prev = self._last_read.get(qid)
        if prev is None:
            self.w(f"{qid} was not asked before: running it now (nothing to compare)")
        self._mute = True
        try:
            now = await self._ask_one(qid, q)
        finally:
            self._mute = False
        self.head(f"rerun ask {qid}: {q['question']}")
        if prev is not None:
            effect = self._print_read_diff(prev, now)
        else:
            effect = f"first run: gold {now['found']}/{now['total']}, lost_at {now['lost']}"
            self.w("  " + self._brief_line(now))
        self._log_tuning("rerun ask", qid, None, None, effect)

    def _ranks(self, res: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
        from .assemble import rank_of

        fx = res.get("fx") or {}
        out: dict[str, dict[str, Any]] = {}
        for g in res.get("gold_turns") or []:
            out[g] = {
                name: rank_of(fx.get(name), g)
                for name in ("vector", "lexical", "fused", "pool", "rerank_scores", "final")
            }
            out[g]["context"] = g in (res.get("got") or [])
        return out

    def _print_read_diff(self, prev: Mapping[str, Any], now: Mapping[str, Any]) -> str:
        rp, rn = self._ranks(prev), self._ranks(now)
        self.w(f"  {'':22}{'before':>22}{'after':>22}")
        changed = 0
        for g in now.get("gold_turns") or []:
            for name in ("vector", "lexical", "fused", "pool", "rerank_scores", "final", "context"):
                b, a = rp.get(g, {}).get(name), rn.get(g, {}).get(name)
                mark = "" if a == b else "   <== changed"
                changed += a != b
                self.w(f"  gold {g} {name:<14}{_short(b, 20):>22}{_short(a, 20):>22}{mark}")
        for label, key in (
            ("gold found", None),
            ("first losing stage", "lost"),
            ("answer correct", "correct"),
            ("tokens", "tokens"),
        ):
            b = f"{prev['found']}/{prev['total']}" if key is None else prev.get(key)
            a = f"{now['found']}/{now['total']}" if key is None else now.get(key)
            self.w(
                f"  {label:<22}{_short(b, 20):>22}{_short(a, 20):>22}"
                + ("" if a == b else "   <== changed")
            )
        bl, al = prev.get("context", "").splitlines(), now.get("context", "").splitlines()
        gone, new = [x for x in bl if x not in al], [x for x in al if x not in bl]
        self.w(f"  packed context: -{len(gone)} line(s), +{len(new)} line(s)")
        for x in gone[:6]:
            self.w("    - " + _short(x, 150))
        for x in new[:6]:
            self.w("    + " + _short(x, 150))
        if prev.get("answer") != now.get("answer"):
            self.w(f"  answer: {_short(prev.get('answer'), 80)} -> {_short(now.get('answer'), 80)}")
        return (
            f"gold {prev['found']}/{prev['total']} -> {now['found']}/{now['total']}; lost_at {prev['lost']} -> {now['lost']}; "
            f"{changed} rank/context change(s); context -{len(gone)}/+{len(new)} lines"
        )

    # ---- sweep
    async def _sweep(self, arg: str) -> None:
        head, sep, targets_arg = arg.partition(" ask ")
        parts = head.split(None, 1)
        if not sep or len(parts) != 2:
            self.w("usage: sweep <key> <v1,v2,...> ask <q1,q2 | all | cat:n>")
            return
        key, raw_values = parts[0], parts[1]
        values = [parse_value(v.strip()) for v in raw_values.split(",") if v.strip()]
        if not key.startswith("read."):
            self.w(
                f"sweep refused: {key!r} is not a read-side key (read.*). A write-side key changes what is stored: "
                f"use `set {key}=<value>` then `rerun window <window_id>` to see its effect on one window."
            )
            return
        if self._needs_rebuild(key):
            self.w(f"sweep refused: {key!r} needs a rebuilt engine (`set` it and `reset`).")
            return
        targets = self._targets(targets_arg.strip())
        if not targets:
            return
        self._capture_base()
        had = key in self.overrides
        old_override = self.overrides.get(key)
        table = []
        for value in values:
            if not await self._set_value(key, value, command="sweep", quiet=True):
                return
            self._mute = True
            try:
                results = [await self._ask_one(qid, q) for qid, q in targets]
            finally:
                self._mute = False
            table.append((value, results))
        # restore
        if had:
            await self._set_value(key, old_override, command="sweep restore", quiet=True)
        else:
            self.overrides.pop(key, None)
            self.cfg = self._compose_cfg()
            new_cfg, err = self._validated(key, get_path(self._base_dump or {}, key))
            if err is None:
                self._apply_live(key, new_cfg)
        self.head(
            f"sweep {key} over {len(targets)} question(s) (no deposit changes; the key is restored)"
        )
        rows = [(key, "gold found", "first losing stage", "correct")]
        for value, results in table:
            tot = sum(r["total"] for r in results)
            found = sum(r["found"] for r in results)
            lost: dict[str, int] = {}
            for r in results:
                if r["lost"] != "-":
                    lost[r["lost"]] = lost.get(r["lost"], 0) + 1
            ok = sum(1 for r in results if str(r["correct"]).endswith("yes"))
            judged = sum(1 for r in results if r["correct"] != "-")
            rows.append(
                (
                    _short(value, 20),
                    f"{found}/{tot}",
                    ", ".join(f"{k}:{v}" for k, v in lost.items()) or "-",
                    f"{ok}/{judged}",
                )
            )
            self._log_tuning(
                "sweep",
                key,
                None,
                value,
                f"gold {found}/{tot}; lost_at {lost or '-'}; correct {ok}/{judged}",
            )
        widths = [max(len(r[i]) for r in rows) for i in range(4)]
        for k, row in enumerate(rows):
            self.w("  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(row)))
            if k == 0:
                self.w("  " + "  ".join("-" * w for w in widths))
        per_q = [
            (qid, [r["found"] for _v, rs in table for r in rs if r["qid"] == qid])
            for qid, _q in targets
        ]
        for qid, founds in per_q:
            self.w(f"  {qid}: gold found per value: {founds}")

    # ---- export
    async def _export_arm(self, name: str) -> None:
        from ..rehearsal import validate_engine_config

        name = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", name or ""):
            self.w("usage: export arm <name>   (letters, digits, - _ .)")
            return
        arms = self.arms_dir or (Path(__file__).resolve().parents[2] / "arms")
        arms.mkdir(parents=True, exist_ok=True)
        base = copy.deepcopy(self.arm_cfg_orig)
        cfg = copy.deepcopy(base)
        for key, val in self.overrides.items():
            set_path(cfg, key, copy.deepcopy(val))
        files = []
        pdir = arms / f"{name}.prompts"
        notes = []
        for pid, ed in self.prompt_overrides.items():
            if pid in CODE_PROMPTS or pid == READER_PROMPT:
                pdir.mkdir(exist_ok=True)
                f = pdir / f"{pid}.txt"
                f.write_text(ed.get("system") or ed.get("body") or "", encoding="utf-8")
                files.append(f)
                notes.append(
                    f"{pid}: a {'code constant' if pid != READER_PROMPT else 'harness prompt'}, saved as a file only "
                    f"(an arm json cannot carry it)"
                )
            else:
                set_path(
                    cfg,
                    f"prompts.overrides.{pid}",
                    {k: v for k, v in ed.items() if k in ("body", "system")},
                )
                pdir.mkdir(exist_ok=True)
                f = pdir / f"{pid}.txt"
                f.write_text(ed.get("body", ""), encoding="utf-8")
                files.append(f)
        try:
            validate_engine_config(copy.deepcopy(cfg))
        except Exception as exc:
            self.w(
                f"export refused: the arm does not validate: {_short(str(exc).replace(chr(10), ' '), 300)}"
            )
            return
        target = arms / f"{name}.json"
        target.write_text(json.dumps(cfg, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        self.head(f"export arm {name}")
        self.w(f"  wrote {target}" + "".join(f"\n  wrote {f}" for f in files))
        fb, fc = flatten(base), flatten(cfg)
        diffs = [k for k in sorted(set(fb) | set(fc)) if fb.get(k, _MISSING) != fc.get(k, _MISSING)]
        self.w(f"  diff vs the base arm ({self.arm_name}): {len(diffs)} value(s)")
        for k in diffs[:40]:
            self.w(
                f"    {k}: {_short(fb.get(k, '(absent)'), 40)} -> {_short(fc.get(k, '(absent)'), 40)}"
            )
        for n in notes:
            self.w(f"  note: {n}")
        if self.mode in ("offline", "replay"):
            self.w(
                "  note: the arm is the original one (offline swaps of embedder and reranker are not exported)"
            )
        self._log_tuning("export arm", name, None, str(target), f"{len(diffs)} diff value(s)")

    # ---- dispatch
    async def tune_command(self, cmd: str, arg: str) -> None:
        if cmd == "config":
            await self.cmd_config(arg)
        elif cmd == "set":
            await self.cmd_set(arg)
        elif cmd == "unset":
            await self.cmd_unset(arg)
        elif cmd == "params":
            await self.cmd_params(arg)
        elif cmd == "prompt":
            await self.cmd_prompt(arg)
        elif cmd == "rerun":
            await self.cmd_rerun(arg)
        elif cmd == "sweep":
            await self._sweep(arg)
        elif cmd == "export":
            sub, _, rest = arg.partition(" ")
            if sub.lower() == "arm":
                await self._export_arm(rest)
            else:
                self.w("usage: export arm <name>")


_MISSING = object()


def _replace(obj: Any, parts: list[str], leaf: Any) -> Any:
    """``obj`` with the value at ``parts`` replaced (pydantic models are copied, dicts rebuilt)."""
    if not parts:
        return leaf
    head, rest = parts[0], parts[1:]
    if isinstance(obj, dict):
        out = dict(obj)
        out[head] = _replace(out.get(head), rest, leaf)
        return out
    if obj is None:
        return _replace({}, parts, leaf)
    return obj.model_copy(update={head: _replace(getattr(obj, head, None), rest, leaf)})

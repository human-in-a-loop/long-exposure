"""Shared parsing for config boolean flags.

YAML makes it easy to write a boolean that is not one. `enabled: "false"`,
`enabled: 'no'` and `enabled: off` all look like switches being turned off,
but a bare `bool()` reads the first two as **true** — so a run the operator
believed was uncapped gets killed by the spend limit, or a flow they believed
was fixed starts being planned by the researcher. The value is quoted by
accident far more often than deliberately (a templating layer, a `sed`, an
editor helpfully quoting a value it thinks is a string).

`truthy` uses the same spelling set as `exploration._env_flag`, so a value
means the same thing whether it arrives from config.yaml, the score, or an
env override. Anything unrecognised warns once and falls back to the
caller's default, because silently choosing either way is worse than saying
so.
"""

from __future__ import annotations

FALSE_WORDS = ("0", "false", "no", "off", "none", "null", "")
TRUE_WORDS = ("1", "true", "yes", "on")

_WARNED: set[str] = set()


def _warn(message: str) -> None:
    if message in _WARNED:
        return
    _WARNED.add(message)
    print(f"[config] {message}", flush=True)


def _reset_warnings() -> None:
    """Test hook: clear the warn-once memo."""
    _WARNED.clear()


def truthy(value, default: bool = False, *, name: str = "flag") -> bool:
    """Interpret `value` as a config boolean.

    - a real bool is returned as-is;
    - an int/float is truthy when non-zero;
    - a string is matched case-insensitively against FALSE_WORDS/TRUE_WORDS;
    - None returns `default`;
    - anything else warns once and returns `default`.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in FALSE_WORDS:
            return False
        if text in TRUE_WORDS:
            return True
        _warn(
            f"{name}: {value!r} is not a recognised boolean; using "
            f"{default!r}. Use true/false."
        )
        return default
    _warn(
        f"{name}: {type(value).__name__} is not a boolean; using {default!r}."
    )
    return default

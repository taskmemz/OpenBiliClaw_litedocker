"""Credential form descriptors, derived from the write gate.

``GET /api/sources/credentials`` ships a ``form`` per platform so the three
settings surfaces can render every registered platform without knowing anything about any
of them (invariant I4). Before this, each surface carried its own idea of which
platforms take a paste box, and the desktop page additionally hardcoded one
sentence about 小红书 — the last per-platform display branch in the settings
region.

Everything here is **derived from** :data:`~openbiliclaw.api.source_auth.write.CREDENTIAL_SPECS`,
never restated. A descriptor that advertised different required keys than the
gate enforces would be D6's drift rebuilt one layer up: the form would tell a
user to include a cookie the validator does not want, or omit one it rejects
for. The single place both read from is the platform's ``CredentialSpec``.

**Actions are capabilities, not decoration.** ``verify`` has a route for every
platform and ``open_login_window`` needs a login page. Stored credentials are
write-only: masked status may be displayed, but a settings read must not export
the original value, so ``copy`` is deliberately not advertised.
``clear`` — which the spec's field table listed as an example — is deliberately
absent: nothing in the API can erase a stored credential, and shipping the
button first and the endpoint later is how a UI starts lying.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from openbiliclaw.api.models import CredentialFormSpec, FormAction
from openbiliclaw.api.source_auth.write import CREDENTIAL_SPECS

if TYPE_CHECKING:
    from openbiliclaw.api.source_auth.write import CredentialSpec
    from openbiliclaw.config import Config

#: Kinds that put a writable credential box in front of the user. Everything
#: else is read-only or absent, and the distinction is load-bearing: the backend
#: stores no 小红书/知乎 cookie, so a paste box on those platforms would accept
#: input that goes nowhere. Mirrored by ``WRITABLE_FORM_KINDS`` in
#: ``web/shared/source-status.js`` — the two must agree, which is what
#: ``test_form_kind_matches_actual_write_capability`` checks.
WRITABLE_FORM_KINDS = frozenset({"cookie_textarea", "token_input"})


def _resolve_env_var(spec: CredentialSpec, cfg: Config) -> str | None:
    """Name of the env var this platform honours, or ``None``.

    Read through the config rather than hardcoded so a user who renamed
    ``cookie_env`` sees the name they actually set — the whole point of
    surfacing it is to answer "where is this value coming from?".
    """
    if not spec.env_var_path:
        return None
    node: object = cfg
    for part in spec.env_var_path.split("."):
        node = getattr(node, part, None)
        if node is None:
            return spec.env_var_default or None
    name = str(node).strip()
    return name or spec.env_var_default or None


def _actions(spec: CredentialSpec) -> list[FormAction]:
    """Buttons this platform can actually back."""
    actions = [
        # Every platform has POST /api/sources/{slug}/verify, including YouTube,
        # whose honest answer is "needs no login". A platform missing from this
        # list would be a source the user cannot ask about at all.
        FormAction(action="verify", label="测试连接"),
    ]
    if spec.login_url:
        actions.append(FormAction(action="open_login_window", label="去登录", url=spec.login_url))
    return actions


def build_credential_form(slug: str, *, cfg: Config) -> CredentialFormSpec:
    """Form descriptor for *slug*, derived from its write gate."""
    spec = CREDENTIAL_SPECS.get(slug)
    if spec is None:
        return CredentialFormSpec(label=slug)

    # The gate stores its structural rule in whichever of the two fields fits;
    # the descriptor carries both the names and which rule applies, so no
    # surface has to guess that 抖音's three names are alternatives.
    if spec.required_keys:
        keys, mode = list(spec.required_keys), "all"
    elif spec.any_of_keys:
        keys, mode = list(spec.any_of_keys), "any"
    else:
        keys, mode = [], "all"

    return CredentialFormSpec(
        kind=spec.form_kind,
        label=spec.form_label or slug,
        placeholder=spec.form_placeholder,
        env_var=_resolve_env_var(spec, cfg),
        required_keys=keys,
        required_keys_mode=mode,  # type: ignore[arg-type]
        actions=_actions(spec),
        help_text=spec.help_text,
    )


def credential_summary(
    form: CredentialFormSpec, *, label: str, available: bool, detail: str
) -> str:
    """One-line summary for a credential row.

    *label* names the stored value (``"Cookie"``, ``"xsec_token"``), not the
    platform, because that is what the row expands to show.

    Computed here rather than in each frontend because the only interesting
    case is platform knowledge: on an ``extension_only`` platform a stored value
    is *not* proof of login (小红书 keeps content tokens whose presence says
    nothing about the account), and saying "凭据已保存" there would contradict
    the access badge sitting right above it. That contradiction used to be
    patched with a ``key === "xiaohongshu"`` branch on the desktop page only,
    so the extension never got the correction.
    """
    if not available:
        return detail or "当前没有可展示 Cookie"
    name = label or "Cookie"
    if form.kind == "extension_only":
        return f"{name} 已保存（不代表账号登录；原值不回传）"
    return f"{name} 已保存（原值不回传）"

"""Construct a *simulation* of a strict two-Deployment Helm candidate.

Does not render, apply, or authorize a Helm release. All non-target Helm
manifests are copied byte-semantically from the recorded release. Used only
with the real three-way strategic patch calculator for safety analysis.
"""
from copy import deepcopy

TARGETS = ("api-gateway", "auth-service")
GATEWAY_ORIGIN_ENV = "GATEWAY_CORS_ALLOWED_ORIGINS"
AUTH_ORIGIN_ENV = "AUTH_ALLOWED_PUBLIC_ORIGINS"


class ScopeViolation(ValueError):
    pass


def _deployment(resources, name):
    matches = [key for key in resources if key[1:] == ("Deployment", name)]
    if len(matches) != 1 or matches[0][0] != "apps/v1":
        raise ScopeViolation("EXPECTED_DEPLOYMENT_MISSING_OR_AMBIGUOUS")
    return matches[0]


def _container(manifest, name):
    try:
        containers = manifest["spec"]["template"]["spec"]["containers"]
    except (KeyError, TypeError) as exc:
        raise ScopeViolation("DEPLOYMENT_CONTAINER_STRUCTURE_INVALID") from exc
    if len(containers) != 1 or containers[0].get("name") != name:
        raise ScopeViolation("TARGET_CONTAINER_IDENTITY_INVALID")
    return containers[0]


def _env(container, key):
    env = container.get("env")
    if not isinstance(env, list):
        raise ScopeViolation("TARGET_ENV_STRUCTURE_INVALID")
    found = [item for item in env if item.get("name") == key]
    if len(found) > 1:
        raise ScopeViolation("DUPLICATE_TARGET_ENV")
    return env, found[0] if found else None


def simulate(old, rendered, release_spec):
    """Return a filtered, in-memory proposed manifest and exact field contract.

    Old and newly rendered manifest identities must match. The deployment
    image and CORS values are copied from independently rendered chart output
    only when they match the pinned release spec.
    """
    if set(old) != set(rendered):
        raise ScopeViolation("MANAGED_RESOURCE_SET_CHANGED")
    if set(release_spec["images"]) != set(TARGETS):
        raise ScopeViolation("RELEASE_SPEC_SERVICE_SCOPE_INVALID")
    candidate = deepcopy(old)
    origins = ",".join(release_spec["origins"])
    for name in TARGETS:
        key = _deployment(old, name)
        source = _container(rendered[key], name)
        target = _container(candidate[key], name)
        expected_image = release_spec["images"][name]["image"]
        if source.get("image") != expected_image:
            raise ScopeViolation("CHART_RENDER_IMAGE_NOT_PINNED")
        target["image"] = expected_image

        env_name = GATEWAY_ORIGIN_ENV if name == "api-gateway" else AUTH_ORIGIN_ENV
        source_env, source_item = _env(source, env_name)
        if source_item != {"name": env_name, "value": origins}:
            raise ScopeViolation("CHART_RENDER_ORIGIN_ENV_MISMATCH")
        target_env, old_item = _env(target, env_name)
        if old_item:
            old_item.clear()
            old_item.update(source_item)
        else:
            target_env.append(deepcopy(source_item))

        # Ensure no other env entry is touched, including valueFrom secrets.
        if len(source_env) == 0:
            raise ScopeViolation("CHART_RENDER_NO_ENV")

    for key in old:
        if key[1] != "Deployment" or key[2] not in TARGETS:
            if candidate[key] != old[key]:
                raise ScopeViolation("NON_TARGET_RESOURCE_MUTATION")
    return candidate

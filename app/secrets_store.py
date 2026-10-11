"""Authenticated encryption at rest. Keys are provisioned outside the repository.

Decryption creates short-lived Python strings; Python/Playwright cannot guarantee
memory zeroization. Never write the decrypted value back to a persistent store.
"""
import json
import os
import re

PREFIX = 'enc:v1:'
MASK = '[encrypted]'

# Named once so the API error, the health probe and the dashboard banner cannot
# drift apart and send an operator looking for a variable that does not exist.
KEY_ENV_VAR = 'TF_VARIABLE_ENCRYPTION_KEYS'
GENERATE_KEY_COMMAND = (
    'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
)
NOT_CONFIGURED = (
    f'Encryption is not configured on this server, so encrypted values cannot be saved. '
    f'Set {KEY_ENV_VAR} in the service environment and restart the service. '
    f'Generate a key with: {GENERATE_KEY_COMMAND}'
)
INVALID_KEYRING = (
    f'{KEY_ENV_VAR} is set but is not a usable keyring. It must be one or more comma-separated '
    f'Fernet keys (44-character url-safe base64), newest first, with no quotes or line breaks. '
    f'Generate a key with: {GENERATE_KEY_COMMAND}'
)


def _keys():
    return [k.strip() for k in os.environ.get(KEY_ENV_VAR, '').split(',') if k.strip()]


def cipher():
    from cryptography.fernet import Fernet, MultiFernet
    keys = _keys()
    if not keys:
        raise ValueError(NOT_CONFIGURED)
    try:
        return MultiFernet([Fernet(k.encode()) for k in keys])
    except Exception:
        raise ValueError(INVALID_KEYRING) from None


def status():
    """Non-secret readiness report for the health probe and the dashboard.

    Never returns key material: only whether the keyring loads, and how many
    keys it holds so a rotation can be confirmed from the outside.
    """
    keys = _keys()
    report = {
        'variable': KEY_ENV_VAR,
        'configured': bool(keys),
        'ready': False,
        'keys': len(keys),
        'generate_command': GENERATE_KEY_COMMAND,
        'reason': None,
    }
    try:
        cipher()
    except ValueError as exc:
        report['reason'] = str(exc)
    except Exception:  # cryptography missing or unimportable on this instance
        report['reason'] = (
            f'{KEY_ENV_VAR} could not be loaded on this server; the cryptography package is '
            f'unavailable. Reinstall requirements.txt, then restart the service.'
        )
    else:
        report['ready'] = True
    return report


def encrypt(value):
    return PREFIX + cipher().encrypt(str(value).encode()).decode()


def decrypt(value):
    if not isinstance(value, str) or not value.startswith(PREFIX):
        return value
    try:
        return cipher().decrypt(value[len(PREFIX):].encode()).decode()
    except ValueError:
        raise
    except Exception:
        raise ValueError('Encrypted value cannot be decrypted with the configured keyring') from None


def protected(value):
    return isinstance(value, str) and (value.startswith(PREFIX) or value.startswith('env:TF_SECRET_'))


def validate_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][\w.-]{0,99}', name):
        raise ValueError('Use a variable/parameter name of 1–100 letters, digits, underscores, dots or hyphens')
    return name


def protect_overrides(project_ids, raw, secret_keys=()):
    from app.db import SessionLocal, Variable
    if not isinstance(raw, dict) or len(raw) > 100 or len(json.dumps(raw, allow_nan=False)) > 65536:
        raise ValueError('Overrides must be an object of at most 100 small values')
    with SessionLocal() as db:
        secret_names = {v.name for v in db.query(Variable).filter(Variable.project_id.in_(project_ids)) if v.is_secret or protected(v.value)}
    secret_names.update(secret_keys)
    result = {}
    for key, value in raw.items():
        validate_name(key)
        if not isinstance(value, (str, int, float, bool)) or len(str(value)) > 4096:
            raise ValueError('Override values must be small scalars')
        if isinstance(value, str) and value.startswith(PREFIX):
            raise ValueError('Ciphertexts cannot be submitted through override fields; keep masked values unchanged')
        result[key] = encrypt(value) if key in secret_names else str(value)
    return result


def public_map(values):
    return {k: MASK if protected(v) else v for k, v in (values or {}).items()}


def reveal_map(values):
    from app.plan_runtime import resolve_values
    output = {}
    for key, value in (values or {}).items():
        value = SecretValue(value) if isinstance(value, str) and value.startswith(PREFIX) else value
        if isinstance(value, str) and value.startswith('env:TF_SECRET_'):
            value = {'env': value[4:]}
        output[key] = value
    return resolve_values(output)


def scrub(value, sensitive):
    """Redact known values recursively, before logging/broadcasting/persisting."""
    if isinstance(value, str):
        for secret in sorted({str(s) for s in sensitive if s is not None and str(s)}, key=len, reverse=True):
            value = value.replace(secret, '[redacted]')
        return value
    if isinstance(value, dict):
        return {k: scrub(v, sensitive) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, sensitive) for v in value]
    return value


class SecretValue:
    """Keeps ciphertext between steps; interpolation decrypts only on demand."""
    def __init__(self, ciphertext):
        decrypt(ciphertext)  # fail closed before a browser is started
        self.ciphertext = ciphertext
    def __str__(self):
        return decrypt(self.ciphertext)
    def __repr__(self):
        return '[encrypted]'


def sensitive_values(db, project_ids):
    from app.db import Variable
    return [SecretValue(v.value) if str(v.value).startswith(PREFIX) else reveal_map({'v': v.value})['v']
            for v in db.query(Variable).filter(Variable.project_id.in_(project_ids)) if v.is_secret or protected(v.value)]


def scrub_entry(entry, sensitive):
    fixed = {k: v for k, v in entry.items() if k in {'order', 'status', 'action', 'percent', 'screenshot'}}
    return {**scrub({k: v for k, v in entry.items() if k not in fixed}, sensitive), **fixed}


def snapshot_inputs(project_ids, defaults=None, overrides=None):
    """Immutable queue snapshot, retaining ciphertext rather than decrypted values."""
    from app.db import SessionLocal, Variable
    values = {}
    names = set()
    with SessionLocal() as db:
        for project_id in project_ids:
            for variable in db.query(Variable).filter_by(project_id=project_id):
                values[variable.name] = variable.value
                if variable.is_secret or protected(variable.value):
                    names.add(variable.name)
    values.update(defaults or {})
    values.update(overrides or {})
    for name in names:
        if name in values and not protected(values[name]):
            values[name] = encrypt(values[name])
    return values


def freeze_inputs(project_ids, defaults=None, overrides=None):
    base = snapshot_inputs(project_ids)
    selected = {**(defaults or {}), **(overrides or {})}
    sealed = snapshot_inputs(project_ids, selected)
    return base, {k: sealed[k] for k in selected}


def plan_secret_names(plan):
    plan = plan or {}
    return {k for values in [plan.get('variables', {}), *plan.get('dataset', [])]
            for k, v in values.items() if isinstance(v, dict) and 'env' in v}

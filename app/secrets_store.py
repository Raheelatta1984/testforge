"""Authenticated encryption at rest. Keys are provisioned outside the repository.

Decryption creates short-lived Python strings; Python/Playwright cannot guarantee
memory zeroization. Never write the decrypted value back to a persistent store.
"""
import hashlib
import json
import os
import re
import threading
import time

PREFIX = 'enc:v1:'
MASK = '[encrypted]'

# Named once so the API error, the health probe and the dashboard banner cannot
# drift apart and send an operator looking for a variable that does not exist.
KEY_ENV_VAR = 'TF_VARIABLE_ENCRYPTION_KEYS'
MANAGED_ENV_VAR = 'TF_MANAGED_ENCRYPTION_KEY'
PURPOSE = 'variables'
GENERATE_KEY_COMMAND = (
    'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
)
NOT_CONFIGURED = (
    f'Encryption is unavailable: {KEY_ENV_VAR} is empty and the application keyring is switched '
    f'off with {MANAGED_ENV_VAR}=0. Set one of the two. Generate an environment key with: '
    f'{GENERATE_KEY_COMMAND}'
)
INVALID_KEYRING = (
    f'{KEY_ENV_VAR} is set but is not a usable keyring. It must be one or more comma-separated '
    f'Fernet keys (44-character url-safe base64), newest first, with no quotes or line breaks. '
    f'Generate a key with: {GENERATE_KEY_COMMAND}'
)
# What the application-owned keyring does and does not buy you. Returned by the
# status probe and printed in the dashboard, because a key stored beside its own
# ciphertext must never be presented as if it were an external secret.
MANAGED_NOTE = (
    'The application generated and stores this key in its own database. It keeps plaintext out of '
    'the library files published to GitHub, which is where these values would otherwise leak. It '
    'does not protect against someone who already has the database, because the key is in there '
    f'too. For that, set {KEY_ENV_VAR} in the service environment: it takes precedence, and keys '
    'already in the database stay available for decrypting existing values.'
)

_LOCK = threading.RLock()
# Building a MultiFernet costs a key derivation per key, and the managed keys
# cost a query, so both are cached against the material they were built from.
_CACHE = {'fingerprint': None, 'cipher': None}
_MANAGED_CACHE = {'keys': None, 'at': 0.0}
MANAGED_CACHE_SECONDS = 30


def managed_enabled():
    return os.environ.get(MANAGED_ENV_VAR, '1').strip() not in {'0', 'false', 'no'}


def _env_keys():
    return [k.strip() for k in os.environ.get(KEY_ENV_VAR, '').split(',') if k.strip()]


def reset_cache():
    """Drop the cached keyring. Called after rotation and by the tests."""
    with _LOCK:
        _CACHE.update(fingerprint=None, cipher=None)
        _MANAGED_CACHE.update(keys=None, at=0.0)


def _read_managed_keys():
    from app.db import ManagedKey, SessionLocal
    with SessionLocal() as db:
        rows = db.query(ManagedKey).filter_by(purpose=PURPOSE).order_by(
            ManagedKey.sequence.desc(), ManagedKey.created_at.desc()).all()
        return [row.material for row in rows]


def _create_managed_key(note='initial'):
    """Mint one key. Safe to lose a race: an extra key only widens the keyring."""
    from cryptography.fernet import Fernet
    from app.db import ManagedKey, SessionLocal
    material = Fernet.generate_key().decode()
    with SessionLocal() as db:
        highest = db.query(ManagedKey).filter_by(purpose=PURPOSE).order_by(
            ManagedKey.sequence.desc()).first()
        db.add(ManagedKey(purpose=PURPOSE, sequence=(highest.sequence if highest else 0) + 1,
                          material=material, note=note))
        db.commit()
    return material


def managed_keys(create=False):
    """Application-owned keys, newest first. Creates the first one on demand."""
    with _LOCK:
        fresh = _MANAGED_CACHE['keys'] is not None and (time.monotonic() - _MANAGED_CACHE['at']) < MANAGED_CACHE_SECONDS
        keys = _MANAGED_CACHE['keys'] if fresh else _read_managed_keys()
        if not keys and create and managed_enabled():
            _create_managed_key()
            keys = _read_managed_keys()
        _MANAGED_CACHE.update(keys=keys, at=time.monotonic())
        return list(keys)


def keyring(create=False):
    """Every key that may decrypt, newest first. The first one encrypts.

    The environment keeps precedence so an operator who provisions a key owns
    new writes immediately, while values already sealed by the application key
    keep decrypting instead of becoming unreadable the moment the variable is set.
    """
    env = _env_keys()
    if env and not managed_enabled():
        return env
    return env + [k for k in managed_keys(create=create and not env) if k not in env]


def cipher(create=True):
    from cryptography.fernet import Fernet, MultiFernet
    keys = keyring(create=create)
    if not keys:
        raise ValueError(NOT_CONFIGURED)
    fingerprint = hashlib.sha256('\x00'.join(keys).encode()).hexdigest()
    with _LOCK:
        if _CACHE['fingerprint'] == fingerprint and _CACHE['cipher'] is not None:
            return _CACHE['cipher']
    try:
        built = MultiFernet([Fernet(k.encode()) for k in keys])
    except Exception:
        # Only the environment can contain text this process did not generate.
        raise ValueError(INVALID_KEYRING if _env_keys() else
                         'The stored application keyring is unreadable; it was not written by this '
                         'application or the database row was altered.') from None
    with _LOCK:
        _CACHE.update(fingerprint=fingerprint, cipher=built)
    return built


def status():
    """Non-secret readiness report for the health probe and the dashboard.

    Never returns key material: only where the keyring comes from, whether it
    loads, and how many keys it holds so a rotation can be confirmed from
    outside the process.
    """
    env = _env_keys()
    managed = managed_enabled()
    report = {
        'variable': KEY_ENV_VAR,
        'managed_variable': MANAGED_ENV_VAR,
        'source': 'environment' if env else ('app-managed' if managed else 'unavailable'),
        'managed_available': managed,
        'env_keys': len(env),
        'managed_keys': 0,
        'configured': bool(env) or managed,
        'ready': False,
        'keys': 0,
        'generate_command': GENERATE_KEY_COMMAND,
        'note': MANAGED_NOTE if not env and managed else None,
        'reason': None,
    }
    try:
        # create=True is what makes the deployment work with no setup at all:
        # the first status check after boot mints the key if there is none.
        keys = keyring(create=True)
        report['keys'] = len(keys)
        report['managed_keys'] = len(managed_keys()) if managed else 0
        if not keys:
            report['reason'] = NOT_CONFIGURED
            return report
        cipher(create=False)
    except ValueError as exc:
        report['reason'] = str(exc)
    except Exception:  # cryptography missing, or the database is unreachable
        report['reason'] = (
            'The encryption keyring could not be loaded on this server. Check /api/diagnostics for '
            'the database status, and that requirements.txt is installed.'
        )
    else:
        report['ready'] = True
    return report


def rotate(note='rotation'):
    """Mint a new application key and re-seal every stored value with it.

    This is the decrypt-and-immediately-re-encrypt path: each value is opened
    with the keyring that can still read it and sealed again with the new key
    before the next one is touched. Superseded keys are kept, so queued run
    snapshots and any value this pass did not reach still decrypt.
    """
    from app import library_store
    if _env_keys():
        raise ValueError(
            f'{KEY_ENV_VAR} is set, so this deployment uses operator-provisioned keys. Rotate by '
            f'putting the new key first in that variable ({KEY_ENV_VAR}=new,old) and restarting.')
    if not managed_enabled():
        raise ValueError(f'The application keyring is switched off with {MANAGED_ENV_VAR}=0.')
    with _LOCK:
        keyring(create=True)          # make sure there is something to supersede
        _create_managed_key(note)
        reset_cache()
    return library_store.reseal_library()


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

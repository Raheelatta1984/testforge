"""Cached PDFs from existing evidence, release attachments, and hourly expiry.

No new worker, browser, capture, image encoder or scheduler. Called after a run
and by the existing maintenance loop. Remote publishing is explicitly opt-in.
"""
import json
import os
from pathlib import Path
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from app import github_api, library_store
from app.execution_speed import speed_profile
from app.config import ARTIFACTS, logger
from app.db import Batch, Recording, Run, SessionLocal

TTL = 48 * 3600
MAX_BYTES = 64 * 1024 * 1024
TAG = 'testforge-ephemeral-reports'
LOCK = threading.RLock()
PUBLICATION_LOCK = threading.RLock()
_last_sweep = 0.0


def root():
    path = Path(ARTIFACTS) / 'pdf-reports'
    if path.resolve().is_relative_to(library_store.library_dir().resolve()):
        raise ValueError('TF_ARTIFACTS must be outside the repository library; PDF binaries cannot be committed')
    path.mkdir(parents=True, exist_ok=True)
    return path


def stem(kind, identity):
    if kind not in {'run', 'batch'}:
        raise ValueError('Unknown report kind')
    try:
        library_store.require_id(identity, 'report id')
    except library_store.LibraryError as exc:
        raise ValueError(str(exc)) from exc
    return kind + '-' + identity


def metadata(kind, identity):
    path = root() / (stem(kind, identity) + '.json')
    return json.loads(path.read_text()) if path.is_file() else None


def _utc_iso(value):
    """A stored UTC datetime as ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (None stays None)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _seconds_between(start, end):
    if not start or not end:
        return None
    begin, finish = (datetime.fromisoformat(str(v).replace('Z', '+00:00')) for v in (start, end))
    return round(max(0.0, (finish - begin).total_seconds()), 3)


def snapshot(kind, identity):
    with SessionLocal() as db:
        owner = db.get(Run if kind == 'run' else Batch, identity)
        if not owner:
            raise ValueError('Execution not found')
        if owner.status in {'queued', 'running', 'investigating'}:
            raise ValueError('Execution is not complete yet')
        when = owner.finished_at or owner.created_at
        completed = when.replace(tzinfo=timezone.utc).timestamp()
        rows = [owner] if kind == 'run' else db.query(Run).filter_by(batch_id=identity).order_by(Run.sequence, Run.created_at).all()
        # Batches carry one speed in their options; runs created before the per-run
        # column existed fall back to it, and runs with neither stay "not recorded".
        batch_speed = (owner.options or {}).get('execution_speed') if kind == 'batch' else None
        runs = []
        for run in rows:
            recording = db.get(Recording, run.recording_id)
            started_iso = _utc_iso(run.started_at)
            finished_iso = _utc_iso(run.finished_at)
            runs.append({'id': run.id, 'name': recording.name if recording else run.recording_id,
                         'status': run.status, 'steps': run.execution_log or [],
                         'execution_speed': run.execution_speed if run.execution_speed is not None else batch_speed,
                         'started_at': started_iso, 'finished_at': finished_iso,
                         'duration_seconds': _seconds_between(started_iso, finished_iso)})
        started_when = owner.started_at or owner.created_at or when
        started = started_when.replace(tzinfo=timezone.utc).timestamp()
        speeds = sorted({run['execution_speed'] for run in runs if run['execution_speed'] is not None})
        return {'kind': kind, 'id': identity, 'status': owner.status, 'completed_at': completed,
                'started_at': started, 'expires_at': completed + TTL, 'runs': runs,
                'started_iso': _utc_iso(started_when), 'completed_iso': _utc_iso(when),
                'execution_speeds': speeds,
                'scenario': ((owner.options or {}).get('scenario') or {}) if kind == 'batch' else {}}


def _stamp_text(value):
    """'2026-10-13T14:02:11.123Z' -> '2026-10-13 14:02:11.123 UTC' for printing."""
    if not value:
        return 'not recorded'
    return str(value).replace('T', ' ').replace('Z', ' UTC')


def _clock_text(value):
    """Time of day only, for table cells where the date is already in the header."""
    if not value:
        return 'n/a'
    return str(value).split('T')[-1].rstrip('Z')


def _duration_text(seconds):
    return 'n/a' if seconds is None else f'{float(seconds):.3f} s'


def _ms_text(value):
    return 'n/a' if value is None else f'{float(value):.1f} ms'


def _speed_text(factor):
    return 'n/a' if factor is None else f'{float(factor):g}x'


def _speeds_summary(speeds):
    if not speeds:
        return 'not recorded (run started before speed was captured)'
    return ', '.join(f"{float(value):g}x ({speed_profile(value)['label']})" for value in speeds)


def _fit(value, font, size, width):
    from reportlab.pdfbase.pdfmetrics import stringWidth
    value = ' '.join(str(value).split())
    if stringWidth(value, font, size) <= width:
        return value
    while value and stringWidth(value + '...', font, size) > width:
        value = value[:-1]
    return value + '...'


def draw(path, data):
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from textwrap import wrap
    c = canvas.Canvas(str(path), pagesize=(595, 842), pageCompression=0)
    c.setTitle('TestForge ' + data['kind'] + ' execution report')
    y = 794
    def text(value, size=10):
        nonlocal y
        c.setFont('Helvetica', size)
        for line in wrap(str(value), 94) or ['']:
            if y < 45:
                c.showPage(); y = 794; c.setFont('Helvetica', size)
            c.drawString(36, y, line); y -= size + 5
    def table(headers, widths, rows, size=8):
        """Fixed-column table; the header row repeats after every page break."""
        nonlocal y
        xs = [36]
        for width in widths[:-1]:
            xs.append(xs[-1] + width)
        def line(cells, font):
            c.setFont(font, size)
            for x, width, cell in zip(xs, widths, cells):
                c.drawString(x, y, _fit(cell, font, size, width - 6))
        def new_page_if_needed(space):
            nonlocal y
            if y < space:
                c.showPage(); y = 794
                line(headers, 'Helvetica-Bold'); y -= size + 5
        new_page_if_needed(90)
        line(headers, 'Helvetica-Bold'); y -= size + 5
        for cells in rows:
            new_page_if_needed(60)
            line(cells, 'Helvetica'); y -= size + 5
        y -= 8
    text('TestForge execution report', 19)
    text(data['kind'].upper() + ' ' + data['id'])
    text('Outcome: ' + data['status'])
    text('Started: ' + _stamp_text(data.get('started_iso')))
    text('Finished: ' + _stamp_text(data.get('completed_iso')))
    text('Duration: ' + _duration_text(_seconds_between(data.get('started_iso'), data.get('completed_iso'))))
    text('Execution speed: ' + _speeds_summary(data.get('execution_speeds') or []))
    if data['scenario']:
        text('Scenario: ' + data['scenario'].get('scenario_name', ''))
        text('Plan: ' + data['scenario'].get('plan_name', ''))
    text('PDF expires 48 hours after completion. Screenshots are reused JPEG evidence, not recaptured.')
    text('Sensitive values are redacted from logs; password inputs are masked. Review images before sharing.')
    text('All times are UTC. A step starts when its action begins and finishes when the action returns; screenshot encoding is not counted.')
    # Timing summary: one row per recording, then one row per step, so runs at
    # different speeds can be compared side by side.
    total_steps = sum(len(run['steps']) for run in data['runs'])
    total_failed = sum(1 for run in data['runs'] for step in run['steps'] if step.get('status') == 'failed')
    action_ms = sum(float(step.get('duration_ms') or 0) for run in data['runs'] for step in run['steps'])
    text('Timing summary', 14)
    text(f"{len(data['runs'])} recording(s), {total_steps} step(s), {total_failed} failed, "
         f"{action_ms / 1000:.3f} s total action time.", 9)
    table(['Recording', 'Status', 'Steps', 'Speed', 'Started (UTC)', 'Duration'],
          [170, 62, 44, 50, 125, 72],
          [[run['name'], run['status'], len(run['steps']), _speed_text(run.get('execution_speed')),
            _stamp_text(run.get('started_at')), _duration_text(run.get('duration_seconds'))]
           for run in data['runs']])
    step_rows = []
    for run in data['runs']:
        for number, step in enumerate(run['steps'], 1):
            step_rows.append([step.get('order', number), run['name'], step.get('action', ''), step.get('status', ''),
                              _stamp_text(step.get('started_at')), _clock_text(step.get('finished_at')),
                              _ms_text(step.get('duration_ms'))])
    if step_rows:
        text('Step timing', 11)
        table(['#', 'Recording', 'Action', 'Status', 'Started (UTC)', 'Finished', 'Duration'],
              [30, 118, 80, 50, 125, 62, 58], step_rows, size=7)
    image_bytes = 0
    step_count = 0
    for run in data['runs']:
        if not run['steps']:
            text(run['name'] + ' | ' + run['status'])
            text('No executed steps (cancelled, skipped or browser startup failure).')
        for step in run['steps']:
            step_count += 1
            if step_count > 2000:
                raise ValueError('PDF exceeds 2,000 evidence steps')
            c.showPage(); y = 794
            text(run['name'], 14)
            text(f"Recording started {_stamp_text(run.get('started_at'))} | finished {_stamp_text(run.get('finished_at'))} | "
                 f"duration {_duration_text(run.get('duration_seconds'))} | speed {_speed_text(run.get('execution_speed'))}", 8)
            text(f"Step {step.get('order', step_count)} | {step.get('action', '')} | {step.get('status', '')}")
            text(f"Step started {_stamp_text(step.get('started_at'))} | finished {_clock_text(step.get('finished_at'))} | "
                 f"duration {_ms_text(step.get('duration_ms'))}", 9)
            for field in ('label', 'stage', 'value', 'selector', 'result', 'error', 'error_type', 'excerpt', 'evidence_error', 'diagnostic', 'symbology'):
                value = step.get(field)
                if value is not None:
                    text(field + ': ' + (json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value))[:2000], 9)
            result = step.get('result') if isinstance(step.get('result'), dict) else {}
            for field in ('expected', 'actual'):
                value = result.get(field, step.get(field))
                if value is not None:
                    text(field + ': ' + str(value)[:2000], 9)
            directory = (Path(ARTIFACTS) / 'runs' / run['id']).resolve()
            name = Path(str(step.get('screenshot') or '')).name
            image = directory / name
            if name and image.suffix in {'.jpg', '.png'} and image.is_file() and not image.is_symlink() and image.stat().st_size <= 512 * 1024:
                image_bytes += image.stat().st_size
                if image_bytes > MAX_BYTES - 8 * 1024 * 1024:
                    raise ValueError('Evidence exceeds PDF size budget')
                width, height = ImageReader(str(image)).getSize()
                scaled_height = min(330, 523 * height / max(1, width))
                if y < scaled_height + 50:
                    c.showPage(); y = 794
                # Passing the filename preserves original JPEG/DCT bytes; no re-encoding.
                c.drawImage(str(image), 36, y - scaled_height, width=523, height=scaled_height, preserveAspectRatio=True)
                y -= scaled_height + 10
            else:
                text('Screenshot unavailable; see evidence_error or run logs.', 9)
            text('Run ' + run['id'], 8)
    c.save()
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('PDF exceeds 64 MiB budget')
    return step_count


def remote_enabled():
    return os.environ.get('TF_REPORTS_GITHUB', '0') == '1'


class ReleaseStore:
    def __init__(self):
        self.client = github_api.GitHubClient()
        if not self.client.slug or not self.client.token:
            raise ValueError('Configure TF_GITHUB_REPO and TF_GITHUB_TOKEN for report attachments')
        self.base = '/repos/' + self.client.slug

    def release(self, create=False):
        try:
            return self.client.request('GET', self.base + '/releases/tags/' + TAG)[1]
        except github_api.GitHubAPIError as exc:
            if getattr(exc, 'status', None) != 404:
                raise
            if not create:
                return None
            return self.client.request('POST', self.base + '/releases', {
                'tag_name': TAG, 'target_commitish': self.client.branch, 'name': 'TestForge reports (48-hour retention)',
                'body': 'Temporary execution PDFs. Summaries remain in library/reports. Cleanup requires the application to be running.',
                'prerelease': True})[1]

    def assets(self, release):
        result = []
        for page in range(1, 101):
            rows = self.client.request('GET', self.base + f"/releases/{release['id']}/assets?per_page=100&page={page}")[1]
            result.extend(rows)
            if len(rows) < 100:
                return result
        raise ValueError('Report release exceeds 10,000 assets; operator cleanup required')

    def upload(self, path, name):
        release = self.release(create=True)
        existing = next((a for a in self.assets(release) if a['name'] == name), None)
        if existing:
            return existing
        url = release['upload_url'].split('{')[0]
        parsed = urllib.parse.urlsplit(url)
        expected = f"/repos/{self.client.slug}/releases/{release['id']}/assets"
        if parsed.scheme != 'https' or parsed.netloc != 'uploads.github.com' or parsed.path != expected:
            raise ValueError('Untrusted GitHub upload URL')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError('Upload redirect refused')
        with path.open('rb') as stream:
            request = urllib.request.Request(url + '?name=' + urllib.parse.quote(name), data=stream, method='POST', headers={
                'Authorization': 'Bearer ' + self.client.token, 'Content-Type': 'application/pdf',
                'Content-Length': str(path.stat().st_size), 'Accept': 'application/vnd.github+json'})
            with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
                return json.loads(response.read(1024 * 1024))

    def delete(self, asset_id):
        self.client.request('DELETE', self.base + '/releases/assets/' + str(int(asset_id)))


# How many execution reports the repository keeps. One file per execution would
# otherwise grow the repo without bound on a free plan; the oldest are dropped.
def retained():
    try:
        return max(1, int(os.environ.get('TF_MAX_REPO_REPORTS', '200')))
    except ValueError:
        return 200


def repository_dir():
    return library_store.library_dir() / 'reports'


def repository_path(kind, identity):
    return f"{github_api.library_prefix()}/reports/{stem(kind, identity)}.json"


def summary(meta):
    # Outcomes, counts and timings only. No step payloads, selectors, input values,
    # screenshots or PDF binaries are ever committed; `recordings` carries names
    # that are already in the library tree plus per-recording tallies.
    keys = ('kind', 'id', 'status', 'completed_at', 'expires_at', 'started_at', 'steps', 'duration_seconds',
            'execution_speeds', 'recordings', 'scenario', 'url', 'asset_id', 'remote_status')
    return {k: meta[k] for k in keys if k in meta}


def _iso(epoch):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch))


def _prune(directory):
    """Keep the newest reports only, so the repository stays bounded."""
    entries = []
    for path in directory.glob('*.json'):
        try:
            entries.append((json.loads(path.read_text()).get('completed_at') or 0, path))
        except (ValueError, OSError):
            path.unlink(missing_ok=True)
    for _when, path in sorted(entries, reverse=True)[retained():]:
        path.unlink(missing_ok=True)


def _write_index(directory):
    """A readable run history for anyone browsing the repository on GitHub."""
    rows = []
    for path in directory.glob('*.json'):
        try:
            rows.append(json.loads(path.read_text()))
        except (ValueError, OSError):
            continue
    rows.sort(key=lambda row: row.get('completed_at') or 0, reverse=True)
    lines = [
        '# Execution reports', '',
        'Written by TestForge when an execution finishes, and committed with the rest of the',
        'library. Outcomes, counts and timings only: no step payloads, selectors, input values,',
        'screenshots or PDF binaries are committed. The PDF is generated on demand by the',
        'instance and expires 48 hours after the execution; `TF_REPORTS_GITHUB=1` additionally',
        'uploads it as a release asset. Start times and step durations are in the PDF; the',
        'speed columns record the replay speed factor (1x = normal) so runs can be compared.', '',
        f'Newest first, newest {retained()} retained.', '',
        '| Finished (UTC) | Started (UTC) | Speed | Kind | Id | Outcome | Steps | Recordings | PDF asset |',
        '| --- | --- | --- | --- | --- | --- | --- | --- | --- |',
    ]
    for row in rows:
        names = ', '.join(str(r.get('name', '')).replace('|', '\\|') for r in (row.get('recordings') or [])) or '—'
        asset = f"[download]({row['url']})" if row.get('url') else (
            'not uploaded' if row.get('remote_status') == 'disabled' else str(row.get('remote_status') or '—'))
        speeds = ', '.join(f'{float(v):g}x' for v in (row.get('execution_speeds') or [])) or '—'
        lines.append(f"| {_iso(row.get('completed_at') or 0)} | {row.get('started_at') or '—'} | {speeds} | "
                     f"{row.get('kind', '')} | {row.get('id', '')} | {row.get('status', '')} | {row.get('steps', 0)} | "
                     f"{names[:120]} | {asset} |")
    (directory / 'index.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def write_summary(meta, commit=True):
    """Write the report into the library tree; optionally push it right away.

    `commit=False` is for the runs inside a batch: their files are written now
    and the batch's own publication pushes all of them in one commit, instead of
    one commit and one push per recording.
    """
    with library_store.LOCK:
        directory = repository_dir()
        library_store._dump(directory / (stem(meta['kind'], meta['id']) + '.json'), summary(meta))
        _prune(directory)
        _write_index(directory)
        if not commit:
            return False
        return library_store._finish(
            f"Publish execution report {stem(meta['kind'], meta['id'])} ({meta.get('status', 'complete')})")


def publish(meta, path, commit=True):
    """Two separate publications, deliberately kept apart.

    The report itself is part of the library and is always committed to the
    repository - that needs no extra configuration beyond the publishing this
    deployment already does. Uploading the PDF *binary* as a release asset is a
    different decision (repository visibility, storage quota) and stays behind
    TF_REPORTS_GITHUB.
    """
    if remote_enabled() and meta.get('remote_status') != 'published':
        try:
            name = f"tf-{int(meta['completed_at'])}-{stem(meta['kind'], meta['id'])}.pdf"
            asset = ReleaseStore().upload(path, name)
            meta.update(asset_id=asset['id'], url=asset['browser_download_url'], remote_status='published')
            meta.pop('publish_error', None)
        except Exception as exc:
            # Exceptions from network transports may include credentials or URLs.
            meta.update(remote_status='pending', publish_error=type(exc).__name__)
    meta['summary_published'] = write_summary(meta, commit=commit)


def generate(kind, identity):
    key = stem(kind, identity)
    with LOCK:
        meta = metadata(kind, identity)
        path = root() / (key + '.pdf')
        if meta and (time.time() >= meta['expires_at'] or meta.get('local_status') == 'expired'):
            raise ValueError('PDF expired after 48 hours')
        if meta and path.is_file():
            return path, meta
        if not meta or not path.is_file():
            data = snapshot(kind, identity)
            if time.time() >= data['expires_at']:
                raise ValueError('PDF expired after 48 hours')
            temporary = root() / (key + '.tmp')
            try:
                steps = draw(temporary, data)
                budget = max(MAX_BYTES, int(os.environ.get('TF_REPORTS_MAX_LOCAL_BYTES', 256 * 1024 * 1024)))
                existing = list(root().glob('*.pdf'))
                if len(existing) >= 500 or sum(p.stat().st_size for p in existing) + temporary.stat().st_size > budget:
                    raise ValueError('Local PDF cache capacity reached; retained reports are not deleted before expiry')
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            meta = {k: data[k] for k in ('kind', 'id', 'status', 'completed_at', 'expires_at')}
            meta.update(steps=steps, remote_status='pending' if remote_enabled() else 'disabled')
            # Tallies for the committed report. Names already exist in the
            # library tree, so this adds outcomes, not new exposure.
            meta['recordings'] = [{
                'id': run['id'], 'name': run['name'], 'status': run['status'],
                'steps': len(run['steps']),
                'failed': sum(1 for step in run['steps'] if step.get('status') == 'failed'),
                'started_at': run.get('started_at'), 'duration_seconds': run.get('duration_seconds'),
                'execution_speed': run.get('execution_speed'),
            } for run in data['runs']]
            # When it began and at which speed factor(s): the 1x/2x/3x comparison key.
            meta['started_at'] = data.get('started_iso')
            meta['execution_speeds'] = list(data.get('execution_speeds') or [])
            if data.get('scenario'):
                meta['scenario'] = {k: data['scenario'][k] for k in ('scenario_name', 'plan_name')
                                    if k in data['scenario']}
            meta['duration_seconds'] = max(0, round(data['completed_at'] - data.get('started_at', data['completed_at']), 3))
        # Network work is outside the render lock and never blocks cached downloads.
        library_store._dump(root() / (key + '.json'), meta)
        return path, meta


def finalize(kind, identity, commit=True):
    try:
        children = []
        if kind == 'batch':
            with SessionLocal() as db:
                children = [r.id for r in db.query(Run).filter_by(batch_id=identity).order_by(Run.sequence)]
            # Write each recording's report, but do not push 40 times: the
            # batch's own publication below carries all of them in one commit.
            for run_id in children:
                finalize('run', run_id, commit=False)
        path, meta = generate(kind, identity)
        with PUBLICATION_LOCK:
            meta = metadata(kind, identity) or meta
            if time.time() < meta['expires_at'] and path.is_file():
                publish(meta, path, commit=commit)
                library_store._dump(path.with_suffix('.json'), meta)
        if commit and children and meta.get('summary_published'):
            # That one push carried the child reports too; record it on each so
            # they are not reported as still pending.
            for run_id in children:
                child = metadata('run', run_id)
                if child and not child.get('summary_published'):
                    child['summary_published'] = True
                    library_store._dump(root() / (stem('run', run_id) + '.json'), child)
    except Exception as exc:
        logger.warning('PDF NOT READY %s %s (%s)', kind, identity, type(exc).__name__)


def sweep(now=None, force=False):
    """Oldest first; restart-safe remote discovery; hourly at most, including retries."""
    global _last_sweep
    now = time.time() if now is None else now
    with PUBLICATION_LOCK:
        if not force and now - _last_sweep < 3600:
            return
        _last_sweep = now
        metas = []
        for path in root().glob('*.json'):
            try:
                metas.append((json.loads(path.read_text()), path))
            except (ValueError, OSError):
                continue
        metas.sort(key=lambda pair: pair[0]['completed_at'])
        store = None
        remote_errors = []
        if remote_enabled():
            try:
                store = ReleaseStore()
                release = store.release()
                assets = store.assets(release) if release else []
                expired = []
                for asset in assets:
                    match = re.fullmatch(r'tf-(\d+)-(run|batch)-([\w-]+)\.pdf', asset['name'])
                    if match and int(match[1]) + TTL <= now:
                        expired.append((int(match[1]), asset, match[2], match[3]))
                for _, asset, kind, identity in sorted(expired, key=lambda x: x[0]):
                    store.delete(asset['id'])
                    # A restarted/replaced instance can still expire a Git summary.
                    old = library_store.library_dir() / 'reports' / (stem(kind, identity) + '.json')
                    if old.is_file():
                        data = json.loads(old.read_text()); data.update(remote_status='expired', url=None)
                        write_summary(data)
            except Exception as exc:
                remote_errors.append(type(exc).__name__)
        for meta, path in metas:
            pdf = path.with_suffix('.pdf')
            if meta['expires_at'] <= now:
                pdf.unlink(missing_ok=True)
                meta['local_status'] = 'expired'
                if remote_enabled() and not remote_errors:
                    changed = meta.get('remote_status') != 'expired' or not meta.get('summary_published')
                    meta.update(remote_status='expired', url=None)
                    if changed:
                        meta['summary_published'] = write_summary(meta)
            elif remote_enabled() and not remote_errors and (meta.get('remote_status') != 'published' or not meta.get('summary_published')) and pdf.is_file():
                publish(meta, pdf)
                if meta.get('remote_status') == 'pending':
                    remote_errors.append(meta.get('publish_error', 'Publication failed'))
            if meta['expires_at'] + 14 * 86400 < now:
                path.unlink(missing_ok=True)
            else:
                library_store._dump(path, meta)
        library_store._dump(root() / 'maintenance-status.tmp', {'checked_at': now, 'remote_errors': remote_errors})

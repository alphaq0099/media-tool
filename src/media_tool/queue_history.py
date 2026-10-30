"""Queue ordering and conservative history removal; never touches media files."""
from datetime import datetime
import json

COMPLETE = ('succeeded', 'completed with skips')


def move_waiting(store, job_id, direction):
    if direction not in (-1, 1):
        raise ValueError('Choose up or down.')
    store.db.execute('BEGIN IMMEDIATE')
    with store.db:
        active = {r[0] for r in store.db.execute('SELECT job_id FROM job_runs WHERE ended IS NULL')}
        waiting = [j['id'] for j in store.visible_jobs() if j['state'] == 'queued' and j['id'] not in active]
        if job_id not in waiting:
            raise ValueError('Only waiting entries can be moved.')
        index = waiting.index(job_id)
        target = index + direction
        if not 0 <= target < len(waiting):
            return False
        other = waiting[target]
        positions = {r['job_id']:r['position'] for r in store.db.execute(
            'SELECT * FROM queue_order WHERE job_id IN (?,?)', (job_id, other))}
        for key, position in ((job_id, positions[other]), (other, positions[job_id])):
            store.db.execute('UPDATE queue_order SET position=? WHERE job_id=?', (position, key))
    return True


def clear_completed(store, preview=False, protected_jobs=()):
    """Remove whole completed groups. Preserve standalone undo and recovery evidence."""
    store.db.execute('BEGIN IMMEDIATE')
    with store.db:
        jobs = {j['id']:j for j in store.jobs()}
        links = {}
        for row in store.db.execute('SELECT * FROM workflow_links'):
            links.setdefault(row['parent_job'], set()).add(row['child_job'])
        undo = {r[key] for r in store.db.execute('SELECT * FROM rename_undos')
                for key in ('original_job', 'undo_job')}
        busy=set(protected_jobs) | {r[0] for r in store.db.execute('SELECT job_id FROM job_runs WHERE ended IS NULL')}
        groups, protected = [], 0
        for root in store.visible_jobs():
            if root['state'] not in COMPLETE:
                continue
            group = {root['id'], *links.get(root['id'], ())}
            safe = not group & (undo | busy) and all(jobs[key]['state'] in COMPLETE for key in group)
            # Standalone rename/move history is the only evidence available for undo.
            if root['adapter'] in ('rename', 'flatten'):
                safe = False
            for key in group:
                for item in store.items(key):
                    if item['state'] not in ('succeeded', 'skipped'):
                        safe = False
                    # A skipped/unfinished temp or pending original cleanup needs review.
                    cleanup = json.loads(item['original_cleanup']) if item.get('original_cleanup') else {}
                    if ((item['state'] == 'skipped' and item['temp']) or cleanup.get('state') in ('pending','partial')
                            or any(v.get('state')=='pending' for v in cleanup.get('files',{}).values())):
                        safe = False
            if safe:
                groups.append(group)
            else:
                protected += 1
        if not preview:
            for group in groups:
                for root in group:
                    store.db.execute('DELETE FROM workflow_links WHERE parent_job=? OR child_job=?', (root, root))
                for key in group:
                    for table in ('events', 'folder_cleanup', 'job_runs', 'queue_order', 'items'):
                        store.db.execute(f'DELETE FROM {table} WHERE job_id=?', (key,))
                    store.db.execute('DELETE FROM jobs WHERE id=?', (key,))
        return len(groups), protected


def recover_runs(store):
    with store.db:
        for run in store.db.execute('SELECT * FROM job_runs WHERE ended IS NULL').fetchall():
            ids = [run['job_id'], *[r[0] for r in store.db.execute(
                'SELECT child_job FROM workflow_links WHERE parent_job=?', (run['job_id'],))]]
            end = run['started']
            for key in ids:
                value = store.db.execute('SELECT MAX(time) FROM events WHERE job_id=?', (key,)).fetchone()[0]
                if value and value > end:
                    end = value
            seconds = max(0, (datetime.fromisoformat(end)-datetime.fromisoformat(run['started'])).total_seconds())
            store.db.execute('UPDATE job_runs SET ended=?,seconds=? WHERE id=?', (end, seconds, run['id']))


def duration(seconds):
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{hours}:{minutes:02}:{seconds:02}' if hours else f'{minutes}:{seconds:02}'

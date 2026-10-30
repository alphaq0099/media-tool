"""Thread-local, transient activity events; no media data or new database schema."""
from contextvars import ContextVar

listener = ContextVar('media_activity_listener', default=None)


def publish(job_id, item_id, state, message='', **values):
    callback = listener.get()
    if callback:
        callback(dict(job_id=job_id, item_id=item_id, state=state, message=message, **values))


def media(item, processed, duration, message):
    publish(item['job_id'], item['id'], 'media', message,
            processed=max(0, processed), duration=duration)

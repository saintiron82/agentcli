"""Shared sync/async conversation leases; local files are not distributed locks."""
import asyncio
import hashlib
import os
import threading
import time
import weakref
from contextlib import asynccontextmanager
from pathlib import Path
from .filesystem import safe_workspace_path

_guard = threading.Lock()
_locks = weakref.WeakValueDictionary()


class ConversationLease:
    def __init__(self, store, conversation_id, factory=None):
        self.store = store  # Keep memory identity alive while the lease exists.
        db = getattr(store, "_coordination_path", None)
        self.key = (db or id(store), conversation_id)
        with _guard:
            self.lock = _locks.get(self.key)
            if self.lock is None:
                self.lock = threading.Lock()
                _locks[self.key] = self.lock
        self.db, self.conversation_id = db, conversation_id
        self.factory, self.external, self.file = factory, None, None
        self.acquired = False

    def try_acquire(self, cancel=None):
        try:
            if not self.lock.acquire(blocking=False):
                return None
            self.acquired = True
            if self.db:
                db = Path(self.db)
                folder = safe_workspace_path(db.parent, db.parent / (db.name + ".agentcli-locks"))
                folder.mkdir(mode=0o700, exist_ok=True)
                name = hashlib.sha256(self.conversation_id.encode()).hexdigest() + ".lock"
                path = safe_workspace_path(folder, folder / name)
                fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
                self.file = os.fdopen(fd, "r+b", buffering=0)
                if os.fstat(fd).st_size == 0:
                    self.file.write(b"\0")
                try:
                    self.file.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno not in (11, 13, 35, 36):
                        raise
                    self.release()
                    return None
            if self.factory:
                # The host supplies a thread-independent distributed lease.
                self.external = self.factory(self.store, self.conversation_id)
                self.external.acquire()
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Conversation lease acquisition cancelled")
            return self
        except BaseException:
            self.release()
            raise

    def acquire(self, cancel=None):
        while True:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Conversation lease acquisition cancelled")
            if self.try_acquire(cancel) is not None:
                return self
            time.sleep(0.05)

    def release(self):
        try:
            if self.external is not None:
                external, self.external = self.external, None
                external.release()
        finally:
            if self.file is not None:
                self.file.close()  # Closing releases the OS lock; do not unlink.
                self.file = None
            if self.acquired:
                self.acquired = False
                self.lock.release()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()


@asynccontextmanager
async def async_lease(lease):
    cancel = threading.Event()
    while True:
        # A single nonblocking attempt per worker avoids exhausting the executor
        # with queued lock waiters while the active turn needs to persist.
        task = asyncio.create_task(asyncio.to_thread(lease.try_acquire, cancel))
        try:
            acquired = await asyncio.shield(task)
        except BaseException:
            cancel.set()
            def reclaim(done):
                try:
                    if done.result() is not None:
                        lease.release()
                except BaseException:
                    pass
            task.add_done_callback(reclaim)
            raise
        if acquired is not None:
            break
        await asyncio.sleep(0.01)
    try:
        yield lease
    finally:
        lease.release()

"""Test-harness-only compatibility shim for gltest's direct-mode runner on
Windows. It does not touch the contract or the GenVM SDK; it works around a
platform-specific gap in the mocked WASI layer.

`gltest.direct.loader._inject_message_to_fd0` writes the encoded message to a
temporary file, `dup2`s that file descriptor onto fd 0, and then unlinks the
temporary path while a duplicated handle (fd 0) is still open on it. POSIX
permits deleting an open file; Windows does not, and raises PermissionError.
Swallowing that specific error is safe: the file's content was already fully
duplicated onto fd 0 before the unlink is attempted, so a failed unlink only
leaves a harmless leftover temp file on disk.
"""

import os

_original_unlink = os.unlink


def _safe_unlink(path, *args, **kwargs):
    try:
        _original_unlink(path, *args, **kwargs)
    except PermissionError:
        pass


os.unlink = _safe_unlink

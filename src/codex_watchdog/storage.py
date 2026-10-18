"""Durable JSON writes shared by the daemon and registry."""
import json
import os
from pathlib import Path
import tempfile


def atomic_json(path, value):
    path = Path(path)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent,
                                         prefix=path.name+'.', delete=False) as stream:
            temp = Path(stream.name)
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if temp is not None and temp.exists():
            temp.unlink()

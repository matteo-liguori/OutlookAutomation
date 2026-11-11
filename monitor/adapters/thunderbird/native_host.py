"""Thunderbird transport: Thunderbird Native Messaging to a user-only socket.

No mailbox credentials, network listeners, eval, or shell commands are exposed.
"""
import json
import os
from pathlib import Path
import queue
import select
import shutil
import socket
import struct
import sys
import tempfile
import threading
import time
import uuid

from monitor.paths import data_root


def runtime_root():
    return data_root() / 'thunderbird'

MAX_BYTES = 900_000


def read_frame(stream):
    header = stream.read(4)
    if not header:
        raise EOFError
    if len(header) != 4:
        raise ValueError('Incomplete frame')
    size = struct.unpack('=I', header)[0]
    if not 0 < size <= MAX_BYTES:
        raise ValueError('Invalid frame length')
    chunks = bytearray()
    while len(chunks) < size:
        part = stream.read(size - len(chunks))
        if not part:
            raise EOFError
        chunks.extend(part)
    return json.loads(chunks)


def write_frame(stream, value):
    body = json.dumps(value, separators=(',', ':')).encode()
    if len(body) > MAX_BYTES:
        raise ValueError('Response exceeds transport size limit')
    pending=memoryview(struct.pack('=I', len(body)) + body)
    while pending:
        written=stream.write(pending)
        if not written:raise OSError('Native frame write failed')
        pending=pending[written:]
    stream.flush()


class NativeInput:
    """Cancellable raw pipe reads; never hold Python's buffered-stdin lock."""
    def __init__(self,fd,stopped):self.fd,self.stopped=fd,stopped

    def read(self,size):
        data=bytearray()
        while len(data)<size and not self.stopped.is_set():
            if not select.select([self.fd],[],[],0.2)[0]:continue
            part=os.read(self.fd,size-len(data))
            if not part:break
            data.extend(part)
        return bytes(data)


def serve():
    os.umask(0o077)
    folder = Path(tempfile.mkdtemp(prefix='oa-tb-', dir='/private/tmp'))
    path = folder / 'bridge.sock'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path)); listener.listen(2); listener.settimeout(0.5)
    responses = queue.Queue()
    stopped = threading.Event()
    native_input=NativeInput(sys.stdin.fileno(),stopped)
    native_output=os.fdopen(os.dup(sys.stdout.fileno()),'wb',buffering=0)

    def receive():
        try:
            while True:
                responses.put(read_frame(native_input))
        except (EOFError, ValueError, OSError):
            stopped.set()

    reader=threading.Thread(target=receive,name='native-input')
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = root / 'runtime.json'
    pending = root / ('runtime-' + uuid.uuid4().hex + '.json')
    pending.write_text(json.dumps(dict(pid=os.getpid(), socket=str(path))))
    pending.replace(receipt)
    try:
        reader.start()
        while not stopped.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            with connection:
                connection.settimeout(60)
                channel = connection.makefile('rwb')
                try:
                    request = read_frame(channel)
                    request_id = uuid.uuid4().hex
                    write_frame(native_output, dict(id=request_id, request=request))
                    deadline=time.monotonic()+55
                    while True:
                        if stopped.is_set():raise EOFError
                        remaining=deadline-time.monotonic()
                        if remaining<=0:raise queue.Empty
                        try:response = responses.get(timeout=min(remaining,0.5))
                        except queue.Empty:continue
                        if response.get('id') == request_id:
                            break
                    write_frame(channel, response)
                except (EOFError, OSError, ValueError, queue.Empty) as error:
                    if isinstance(error,(BrokenPipeError,EOFError)):stopped.set()
                    # A lost response must never trigger an automatic write retry.
                    try:
                        write_frame(channel, dict(status='blocked', code='bridge-response-uncertain', mutation_may_have_occurred=True))
                    except (OSError, ValueError):
                        pass
                finally:
                    channel.close()
    finally:
        stopped.set()
        if reader.ident is not None:reader.join(timeout=2)
        native_output.close()
        listener.close()
        shutil.rmtree(folder)
        try:
            if json.loads(receipt.read_text())['pid'] == os.getpid():
                receipt.unlink()
        except (FileNotFoundError, ValueError, KeyError):
            pass


def request(value):
    state = json.loads((runtime_root() / 'runtime.json').read_text())
    path = Path(state['socket'])
    if path.parent.stat().st_uid != os.getuid() or path.parent.stat().st_mode & 0o077:
        raise ValueError('Bridge socket directory is not private')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(60); connection.connect(str(path))
        with connection.makefile('rwb') as channel:
            write_frame(channel, value)
            return read_frame(channel)


if __name__ == '__main__':
    serve()

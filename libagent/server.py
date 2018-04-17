"""UNIX-domain socket server for ssh-agent implementation."""
import contextlib
import logging
import os
import socket
import subprocess
import threading

from . import util

log = logging.getLogger(__name__)


def remove_file(path, remove=os.remove, exists=os.path.exists):
    """Remove file, and raise OSError if still exists."""
    try:
        remove(path)
    except OSError:
        if exists(path):
            raise


@contextlib.contextmanager
def unix_domain_socket_server(sock_path):
    """
    Create UNIX-domain socket on specified path.

    Listen on it, and delete it after the generated context is over.
    """
    log.debug('serving on %s', sock_path)
    remove_file(sock_path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    try:
        yield server
    finally:
        remove_file(sock_path)


class FDServer(object):
    def __init__(self, fd):
        self.fd = fd
        self.sock = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)

    def accept(self):
        return self, None

    def recv(self, n):
        return self.sock.recv(n)

    def sendall(self, data):
        return self.sock.sendall(data)

    def close(self):
        pass

    def settimeout(self, _):
        pass

    def getsockname(self):
        return '<fd: {}>'.format(self.fd)


@contextlib.contextmanager
def unix_domain_socket_server_from_fd(fd):
    yield FDServer(fd)


def handle_connection(conn, handler, mutex):
    """
    Handle a single connection using the specified protocol handler in a loop.

    Since this function may be called concurrently from server_thread,
    the specified mutex is used to synchronize the device handling.

    Exit when EOFError is raised.
    All other exceptions are logged as warnings.
    """
    try:
        log.debug('welcome agent')
        with contextlib.closing(conn):
            while True:
                msg = util.read_frame(conn)
                with mutex:
                    reply = handler.handle(msg=msg)
                util.send(conn, reply)
    except EOFError:
        log.debug('goodbye agent')
    except Exception as e:  # pylint: disable=broad-except
        log.warning('error: %s', e, exc_info=True)


def retry(func, exception_type, quit_event):
    """
    Run the function, retrying when the specified exception_type occurs.

    Poll quit_event on each iteration, to be responsive to an external
    exit request.
    """
    while True:
        if quit_event.is_set():
            raise StopIteration
        try:
            return func()
        except exception_type:
            pass


def server_thread(sock, handle_conn, quit_event):
    """Run a server on the specified socket."""
    log.debug('server thread started')

    def accept_connection():
        conn, _ = sock.accept()
        conn.settimeout(None)
        return conn

    while True:
        log.debug('waiting for connection on %s', sock.getsockname())
        try:
            conn = retry(accept_connection, socket.timeout, quit_event)
        except StopIteration:
            log.debug('server stopped')
            break
        # Handle connections from SSH concurrently.
        threading.Thread(target=handle_conn,
                         kwargs=dict(conn=conn)).start()
    log.debug('server thread stopped')


@contextlib.contextmanager
def spawn(func, kwargs):
    """Spawn a thread, and join it after the context is over."""
    t = threading.Thread(target=func, kwargs=kwargs)
    t.start()
    yield
    t.join()


def run_process(command, environ):
    """
    Run the specified process and wait until it finishes.

    Use environ dict for environment variables.
    """
    log.info('running %r with %r', command, environ)
    env = dict(os.environ)
    env.update(environ)
    try:
        p = subprocess.Popen(args=command, env=env)
    except OSError as e:
        raise OSError('cannot run %r: %s' % (command, e))
    log.debug('subprocess %d is running', p.pid)
    ret = p.wait()
    log.debug('subprocess %d exited: %d', p.pid, ret)
    return ret

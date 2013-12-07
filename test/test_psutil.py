#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Copyright (c) 2009, Giampaolo Rodola'. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""
psutil test suite. Run it with:
$ make test

If you're on Python < 2.7 it is recommended to install unittest2 module
from: https://pypi.python.org/pypi/unittest2
"""

from __future__ import division

import atexit
import datetime
import errno
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import traceback
import types
import warnings
from socket import AF_INET, SOCK_STREAM, SOCK_DGRAM
try:
    import ast  # python >= 2.6
except ImportError:
    ast = None
try:
    import unittest2 as unittest  # pyhon < 2.7 + unittest2 installed
except ImportError:
    import unittest

import psutil
from psutil._compat import PY3, callable, long, wraps



# ===================================================================
# --- Constants
# ===================================================================

# conf for retry_before_failing() decorator
NO_RETRIES = 10
# bytes tolerance for OS memory related tests
TOLERANCE = 500 * 1024  # 500KB

AF_INET6 = getattr(socket, "AF_INET6")
AF_UNIX = getattr(socket, "AF_UNIX", None)
PYTHON = os.path.realpath(sys.executable)
DEVNULL = open(os.devnull, 'r+')
TESTFN = os.path.join(os.getcwd(), "$testfile")
TESTFN_UNICODE = TESTFN + "ƒőő"
TESTFILE_PREFIX = 'psutil-test-suite-'
if not PY3:
    try:
        TESTFN_UNICODE = unicode(TESTFN_UNICODE, sys.getfilesystemencoding())
    except UnicodeDecodeError:
        TESTFN_UNICODE = TESTFN + "???"

EXAMPLES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                               '..', 'examples'))

POSIX = os.name == 'posix'
LINUX = sys.platform.startswith("linux")
WINDOWS = sys.platform.startswith("win32")
OSX = sys.platform.startswith("darwin")
BSD = sys.platform.startswith("freebsd")
SUNOS = sys.platform.startswith("sunos")
VALID_PROC_STATUSES = [getattr(psutil, x) for x in dir(psutil)
                       if x.startswith('STATUS_')]

# ===================================================================
# --- Utility functions
# ===================================================================

_subprocesses_started = set()

def get_test_subprocess(cmd=None, stdout=DEVNULL, stderr=DEVNULL, stdin=DEVNULL,
                        wait=False):
    """Return a subprocess.Popen object to use in tests.
    By default stdout and stderr are redirected to /dev/null and the
    python interpreter is used as test process.
    If 'wait' is True attemps to make sure the process is in a
    reasonably initialized state.
    """
    if cmd is None:
        pyline = ""
        if wait:
            pyline += "open(r'%s', 'w'); " % TESTFN
        pyline += "import time; time.sleep(2);"
        cmd_ = [PYTHON, "-c", pyline]
    else:
        cmd_ = cmd
    sproc = subprocess.Popen(cmd_, stdout=stdout, stderr=stderr, stdin=stdin)
    if wait:
        if cmd is None:
            stop_at = time.time() + 3
            while stop_at > time.time():
                if os.path.exists(TESTFN):
                    break
                time.sleep(0.001)
            else:
                warn("couldn't make sure test file was actually created")
        else:
            wait_for_pid(sproc.pid)
    _subprocesses_started.add(psutil.Process(sproc.pid))
    return sproc


_testfiles = []

def pyrun(src):
    """Run python code 'src' in a separate interpreter.
    Return interpreter subprocess.
    """
    if PY3:
        src = bytes(src, 'ascii')
    # could have used NamedTemporaryFile(delete=False) but it's
    # >= 2.6 only
    fd, path = tempfile.mkstemp(prefix=TESTFILE_PREFIX)
    _testfiles.append(path)
    f = open(path, 'wb')
    try:
        f.write(src)
        f.flush()
        subp = get_test_subprocess([PYTHON, f.name], stdout=None, stderr=None)
        wait_for_pid(subp.pid)
        return subp
    finally:
        f.close()


def warn(msg):
    """Raise a warning msg."""
    warnings.warn(msg, UserWarning)


def register_warning(msg):
    """Register a warning which will be printed on interpreter exit."""
    atexit.register(lambda: warn(msg))


def sh(cmdline, stdout=subprocess.PIPE, stderr=subprocess.PIPE):
    """run cmd in a subprocess and return its output.
    raises RuntimeError on error.
    """
    p = subprocess.Popen(cmdline, shell=True, stdout=stdout, stderr=stderr)
    stdout, stderr = p.communicate()
    if p.returncode != 0:
        raise RuntimeError(stderr)
    if stderr:
        warn(stderr)
    if PY3:
        stdout = str(stdout, sys.stdout.encoding)
    return stdout.strip()


def which(program):
    """Same as UNIX which command. Return None on command not found."""
    def is_exe(fpath):
        return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

    fpath, fname = os.path.split(program)
    if fpath:
        if is_exe(program):
            return program
    else:
        for path in os.environ["PATH"].split(os.pathsep):
            exe_file = os.path.join(path, program)
            if is_exe(exe_file):
                return exe_file
    return None


if POSIX:
    def get_kernel_version():
        """Return a tuple such as (2, 6, 36)."""
        s = ""
        uname = os.uname()[2]
        for c in uname:
            if c.isdigit() or c == '.':
                s += c
            else:
                break
        if not s:
            raise ValueError("can't parse %r" % uname)
        minor = 0
        micro = 0
        nums = s.split('.')
        major = int(nums[0])
        if len(nums) >= 2:
            minor = int(nums[1])
        if len(nums) >= 3:
            micro = int(nums[2])
        return (major, minor, micro)


def wait_for_pid(pid, timeout=1):
    """Wait for pid to show up in the process list then return.
    Used in the test suite to give time the sub process to initialize.
    """
    raise_at = time.time() + timeout
    while 1:
        if pid in psutil.get_pid_list():
            # give it one more iteration to allow full initialization
            time.sleep(0.01)
            return
        time.sleep(0.0001)
        if time.time() >= raise_at:
            raise RuntimeError("Timed out")


def reap_children(search_all=False):
    """Kill any subprocess started by this test suite and ensure that
    no zombies stick around to hog resources and create problems when
    looking for refleaks.
    """
    procs = _subprocesses_started.copy()
    if search_all:
        this_process = psutil.Process(os.getpid())
        for p in this_process.get_children(recursive=True):
            procs.add(p)
    for p in procs:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=3)
    for p in alive:
        warn("couldn't terminate process %s" % p)
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=3)
    if alive:
        warn("couldn't not kill processes %s" % str(alive))


def check_ip_address(addr, family):
    """Attempts to check IP address's validity."""
    if not addr:
        return
    if family in (AF_INET, AF_INET6):
        assert isinstance(addr, tuple)
        ip, port = addr
        assert isinstance(port, int), port
        if family == AF_INET:
            ip = list(map(int, ip.split('.')))
            assert len(ip) == 4, ip
            for num in ip:
                assert 0 <= num <= 255, ip
        assert 0 <= port <= 65535, port
    elif family == AF_UNIX:
        assert isinstance(addr, (str, None))
    else:
        raise ValueError("unknown family %r", family)


def check_connection(conn):
    """Check validity of a connection namedtuple."""
    valid_conn_states = [getattr(psutil, x) for x in dir(psutil) if
                         x.startswith('CONN_')]

    assert conn.type in (SOCK_STREAM, SOCK_DGRAM), repr(conn.type)
    assert conn.family in (AF_INET, AF_INET6, AF_UNIX), repr(conn.family)
    assert conn.status in valid_conn_states, conn.status
    check_ip_address(conn.laddr, conn.family)
    check_ip_address(conn.raddr, conn.family)

    if conn.family in (AF_INET, AF_INET6):
        # actually try to bind the local socket; ignore IPv6
        # sockets as their address might be represented as
        # an IPv4-mapped-address (e.g. "::127.0.0.1")
        # and that's rejected by bind()
        if conn.family == AF_INET:
            s = socket.socket(conn.family, conn.type)
            s.bind((conn.laddr[0], 0))
            s.close()
    elif conn.family == AF_UNIX:
        assert not conn.raddr, repr(conn.raddr)
        assert conn.status == psutil.CONN_NONE, conn.status

    if getattr(conn, 'fd', -1) != -1:
        assert conn.fd > 0, conn
        if hasattr(socket, 'fromfd') and not WINDOWS:
            dupsock = None
            try:
                try:
                    dupsock = socket.fromfd(conn.fd, conn.family, conn.type)
                except (socket.error, OSError):
                    err = sys.exc_info()[1]
                    if err.args[0] != errno.EBADF:
                        raise
                else:
                    # python >= 2.5
                    if hasattr(dupsock, "family"):
                        assert dupsock.family == conn.family
                        assert dupsock.type == conn.type
            finally:
                if dupsock is not None:
                    dupsock.close()


def safe_remove(file):
    "Convenience function for removing temporary test files"
    try:
        os.remove(file)
    except OSError:
        err = sys.exc_info()[1]
        if err.errno != errno.ENOENT:
            raise


def safe_rmdir(dir):
    "Convenience function for removing temporary test directories"
    try:
        os.rmdir(dir)
    except OSError:
        err = sys.exc_info()[1]
        if err.errno != errno.ENOENT:
            raise


def call_until(fun, expr, timeout=1):
    """Keep calling function for timeout secs and exit if eval()
    expression is True.
    """
    stop_at = time.time() + timeout
    while time.time() < stop_at:
        ret = fun()
        if eval(expr):
            return ret
        time.sleep(0.001)
    raise RuntimeError('timed out (ret=%r)' % ret)


def retry_before_failing(ntimes=None):
    """Decorator which runs a test function and retries N times before
    actually failing.
    """
    def decorator(fun):
        @wraps(fun)
        def wrapper(*args, **kwargs):
            for x in range(ntimes or NO_RETRIES):
                try:
                    return fun(*args, **kwargs)
                except AssertionError:
                    pass
            raise
        return wrapper
    return decorator


def skip_on_access_denied(only_if=None):
    """Decorator to Ignore AccessDenied exceptions."""
    def decorator(fun):
        @wraps(fun)
        def wrapper(*args, **kwargs):
            try:
                return fun(*args, **kwargs)
            except psutil.AccessDenied:
                if only_if is not None:
                    if not only_if:
                        raise
                msg = "%r was skipped because it raised AccessDenied" \
                      % fun.__name__
                self = args[0]
                if hasattr(self, 'skip'):  # python >= 2.7
                    self.skip(msg)
                else:
                    register_warning(msg)
        return wrapper
    return decorator


def skip_on_not_implemented(only_if=None):
    """Decorator to Ignore NotImplementedError exceptions."""
    def decorator(fun):
        @wraps(fun)
        def wrapper(*args, **kwargs):
            try:
                return fun(*args, **kwargs)
            except NotImplementedError:
                if only_if is not None:
                    if not only_if:
                        raise
                msg = "%r was skipped because it raised NotImplementedError" \
                      % fun.__name__
                self = args[0]
                if hasattr(self, 'skip'):  # python >= 2.7
                    self.skip(msg)
                else:
                    register_warning(msg)
        return wrapper
    return decorator


def supports_ipv6():
    """Return True if IPv6 is supported on this platform."""
    if not socket.has_ipv6 or not hasattr(socket, "AF_INET6"):
        return False
    sock = None
    try:
        try:
            sock = socket.socket(AF_INET6, SOCK_STREAM)
            sock.bind(("::1", 0))
        except (socket.error, socket.gaierror):
            return False
        else:
            return True
    finally:
        if sock is not None:
            sock.close()


class ThreadTask(threading.Thread):
    """A thread object used for running process thread tests."""

    def __init__(self):
        threading.Thread.__init__(self)
        self._running = False
        self._interval = None
        self._flag = threading.Event()

    def __repr__(self):
        name = self.__class__.__name__
        return '<%s running=%s at %#x>' % (name, self._running, id(self))

    def start(self, interval=0.001):
        """Start thread and keep it running until an explicit
        stop() request. Polls for shutdown every 'timeout' seconds.
        """
        if self._running:
            raise ValueError("already started")
        self._interval = interval
        threading.Thread.start(self)
        self._flag.wait()

    def run(self):
        self._running = True
        self._flag.set()
        while self._running:
            time.sleep(self._interval)

    def stop(self):
        """Stop thread execution and and waits until it is stopped."""
        if not self._running:
            raise ValueError("already stopped")
        self._running = False
        self.join()


# ===================================================================
# --- Support for python < 2.7 in case unittest2 is not installed
# ===================================================================

if not hasattr(unittest, 'skip'):
    register_warning("unittest2 module is not installed; a serie of pretty "
                     "darn ugly workarounds will be used")

    class SkipTest(Exception):
        pass

    class TestCase(unittest.TestCase):

        def _safe_repr(self, obj):
            MAX_LENGTH = 80
            try:
                result = repr(obj)
            except Exception:
                result = object.__repr__(obj)
            if len(result) < MAX_LENGTH:
                return result
            return result[:MAX_LENGTH] + ' [truncated]...'

        def _fail_w_msg(self, a, b, middle, msg):
            self.fail(msg or '%s %s %s' % (self._safe_repr(a), middle,
                                           self._safe_repr(b)))

        def skip(self, msg):
            raise SkipTest(msg)

        def assertIn(self, a, b, msg=None):
            if a not in b:
                self._fail_w_msg(a, b, 'not found in', msg)

        def assertNotIn(self, a, b, msg=None):
            if a in b:
                self._fail_w_msg(a, b, 'found in', msg)

        def assertGreater(self, a, b, msg=None):
            if not a > b:
                self._fail_w_msg(a, b, 'not greater than', msg)

        def assertGreaterEqual(self, a, b, msg=None):
            if not a >= b:
                self._fail_w_msg(a, b, 'not greater than or equal to', msg)

        def assertLess(self, a, b, msg=None):
            if not a < b:
                self._fail_w_msg(a, b, 'not less than', msg)

        def assertLessEqual(self, a, b, msg=None):
            if not a <= b:
                self._fail_w_msg(a, b, 'not less or equal to', msg)

        def assertIsInstance(self, a, b, msg=None):
            if not isinstance(a, b):
                self.fail(msg or '%s is not an instance of %r'
                          % (self._safe_repr(a), b))

        def assertAlmostEqual(self, a, b, msg=None, delta=None):
            if delta is not None:
                if abs(a - b) <= delta:
                    return
                self.fail(msg or '%s != %s within %s delta'
                          % (self._safe_repr(a), self._safe_repr(b),
                             self._safe_repr(delta)))
            else:
                self.assertEqual(a, b, msg=msg)

    def skipIf(condition, reason):
        def decorator(fun):
            @wraps(fun)
            def wrapper(*args, **kwargs):
                self = args[0]
                if condition:
                    sys.stdout.write("skipped-")
                    sys.stdout.flush()
                    if warn:
                        objname = "%s.%s" % (self.__class__.__name__,
                                             fun.__name__)
                        msg = "%s was skipped" % objname
                        if reason:
                            msg += "; reason: " + repr(reason)
                        register_warning(msg)
                    return
                else:
                    return fun(*args, **kwargs)
            return wrapper
        return decorator

    def skipUnless(condition, reason):
        if not condition:
            return unittest.skipIf(True, reason)
        return unittest.skipIf(False, reason)

    unittest.TestCase = TestCase
    unittest.skipIf = skipIf
    unittest.skipUnless = skipUnless
    del TestCase, skipIf, skipUnless


# python 2.4
if not hasattr(subprocess.Popen, 'terminate'):
    subprocess.Popen.terminate = \
        lambda self: psutil.Process(self.pid).terminate()


# ===================================================================
# --- System-related API tests
# ===================================================================

class TestSystemAPIs(unittest.TestCase):
    """Tests for system-related APIs."""

    def setUp(self):
        safe_remove(TESTFN)
        safe_rmdir(TESTFN_UNICODE)

    def tearDown(self):
        reap_children()

    def test_process_iter(self):
        self.assertIn(os.getpid(), [x.pid for x in psutil.process_iter()])
        sproc = get_test_subprocess()
        self.assertIn(sproc.pid, [x.pid for x in psutil.process_iter()])
        p = psutil.Process(sproc.pid)
        p.kill()
        p.wait()
        self.assertNotIn(sproc.pid, [x.pid for x in psutil.process_iter()])

    def test_wait_procs(self):
        l = []
        callback = lambda p: l.append(p.pid)

        sproc1 = get_test_subprocess()
        sproc2 = get_test_subprocess()
        sproc3 = get_test_subprocess()
        procs = [psutil.Process(x.pid) for x in (sproc1, sproc2, sproc3)]
        self.assertRaises(ValueError, psutil.wait_procs, procs, timeout=-1)
        t = time.time()
        gone, alive = psutil.wait_procs(procs, timeout=0.01, callback=callback)

        self.assertLess(time.time() - t, 0.5)
        self.assertEqual(gone, [])
        self.assertEqual(len(alive), 3)
        self.assertEqual(l, [])
        for p in alive:
            self.assertFalse(hasattr(p, 'retcode'))

        sproc3.terminate()
        gone, alive = psutil.wait_procs(procs, timeout=0.03, callback=callback)
        self.assertEqual(len(gone), 1)
        self.assertEqual(len(alive), 2)
        self.assertIn(sproc3.pid, [x.pid for x in gone])
        if POSIX:
            self.assertEqual(gone.pop().retcode, signal.SIGTERM)
        else:
            self.assertEqual(gone.pop().retcode, 1)
        self.assertEqual(l, [sproc3.pid])
        for p in alive:
            self.assertFalse(hasattr(p, 'retcode'))

        sproc1.terminate()
        sproc2.terminate()
        gone, alive = psutil.wait_procs(procs, timeout=0.03, callback=callback)
        self.assertEqual(len(gone), 3)
        self.assertEqual(len(alive), 0)
        self.assertEqual(set(l), set([sproc1.pid, sproc2.pid, sproc3.pid]))
        for p in gone:
            self.assertTrue(hasattr(p, 'retcode'))

    def test_wait_procs_no_timeout(self):
        sproc1 = get_test_subprocess()
        sproc2 = get_test_subprocess()
        sproc3 = get_test_subprocess()
        procs = [psutil.Process(x.pid) for x in (sproc1, sproc2, sproc3)]
        for p in procs:
            p.terminate()
        gone, alive = psutil.wait_procs(procs)

    def test_get_boot_time(self):
        bt = psutil.get_boot_time()
        self.assertIsInstance(bt, float)
        self.assertGreater(bt, 0)
        self.assertLess(bt, time.time())

    @unittest.skipUnless(POSIX, 'posix only')
    def test_PAGESIZE(self):
        # pagesize is used internally to perform different calculations
        # and it's determined by using SC_PAGE_SIZE; make sure
        # getpagesize() returns the same value.
        import resource
        self.assertEqual(os.sysconf("SC_PAGE_SIZE"), resource.getpagesize())

    def test_deprecated_apis(self):
        s = socket.socket()
        s.bind(('localhost', 0))
        s.listen(1)
        warnings.filterwarnings("error")
        p = psutil.Process(os.getpid())
        try:
            self.assertRaises(DeprecationWarning, getattr, psutil, 'NUM_CPUS')
            self.assertRaises(DeprecationWarning, getattr, psutil, 'BOOT_TIME')
            self.assertRaises(DeprecationWarning, getattr, psutil,
                              'TOTAL_PHYMEM')
            self.assertRaises(DeprecationWarning, psutil.virtmem_usage)
            self.assertRaises(DeprecationWarning, psutil.used_phymem)
            self.assertRaises(DeprecationWarning, psutil.avail_phymem)
            self.assertRaises(DeprecationWarning, psutil.total_virtmem)
            self.assertRaises(DeprecationWarning, psutil.used_virtmem)
            self.assertRaises(DeprecationWarning, psutil.avail_virtmem)
            self.assertRaises(DeprecationWarning, psutil.phymem_usage)
            self.assertRaises(DeprecationWarning, psutil.get_process_list)
            self.assertRaises(DeprecationWarning, psutil.network_io_counters)
            if LINUX:
                self.assertRaises(DeprecationWarning, psutil.phymem_buffers)
                self.assertRaises(DeprecationWarning, psutil.cached_phymem)
            try:
                p.nice
            except DeprecationWarning:
                pass
            else:
                self.fail("p.nice didn't raise DeprecationWarning")
            ret = call_until(p.get_connections, "len(ret) != 0", timeout=1)
            self.assertRaises(DeprecationWarning,
                              getattr, ret[0], 'local_address')
            self.assertRaises(DeprecationWarning,
                              getattr, ret[0], 'remote_address')
        finally:
            s.close()
            warnings.resetwarnings()

        # check value against new APIs
        warnings.filterwarnings("ignore")
        try:
            self.assertEqual(psutil.NUM_CPUS, psutil.cpu_count())
            self.assertEqual(psutil.BOOT_TIME, psutil.get_boot_time())
            self.assertEqual(psutil.TOTAL_PHYMEM, psutil.virtual_memory().total)
        finally:
            warnings.resetwarnings()

    def test_deprecated_apis_retval(self):
        warnings.filterwarnings("ignore")
        p = psutil.Process(os.getpid())
        try:
            self.assertEqual(psutil.total_virtmem(), psutil.swap_memory().total)
            self.assertEqual(p.nice, p.get_nice())
        finally:
            warnings.resetwarnings()

    def test_virtual_memory(self):
        mem = psutil.virtual_memory()
        assert mem.total > 0, mem
        assert mem.available > 0, mem
        assert 0 <= mem.percent <= 100, mem
        assert mem.used > 0, mem
        assert mem.free >= 0, mem
        for name in mem._fields:
            value = getattr(mem, name)
            if name != 'percent':
                self.assertIsInstance(value, (int, long))
            if name != 'total':
                if not value >= 0:
                    self.fail("%r < 0 (%s)" % (name, value))
                if value > mem.total:
                    self.fail("%r > total (total=%s, %s=%s)"
                              % (name, mem.total, name, value))

    def test_swap_memory(self):
        mem = psutil.swap_memory()
        assert mem.total >= 0, mem
        assert mem.used >= 0, mem
        if mem.total > 0:
            # likely a system with no swap partition
            assert mem.free > 0, mem
        else:
            assert mem.free == 0, mem
        assert 0 <= mem.percent <= 100, mem
        assert mem.sin >= 0, mem
        assert mem.sout >= 0, mem

    def test_pid_exists(self):
        sproc = get_test_subprocess(wait=True)
        assert psutil.pid_exists(sproc.pid)
        p = psutil.Process(sproc.pid)
        p.kill()
        p.wait()
        self.assertFalse(psutil.pid_exists(sproc.pid))
        self.assertFalse(psutil.pid_exists(-1))

    def test_pid_exists_2(self):
        reap_children()
        pids = psutil.get_pid_list()
        for pid in pids:
            try:
                assert psutil.pid_exists(pid)
            except AssertionError:
                # in case the process disappeared in meantime fail only
                # if it is no longer in get_pid_list()
                time.sleep(.1)
                if pid in psutil.get_pid_list():
                    self.fail(pid)
        pids = range(max(pids) + 5000, max(pids) + 6000)
        for pid in pids:
            self.assertFalse(psutil.pid_exists(pid))

    def test_get_pid_list(self):
        plist = [x.pid for x in psutil.process_iter()]
        pidlist = psutil.get_pid_list()
        self.assertEqual(plist.sort(), pidlist.sort())
        # make sure every pid is unique
        self.assertEqual(len(pidlist), len(set(pidlist)))

    def test_test(self):
        # test for psutil.test() function
        stdout = sys.stdout
        sys.stdout = DEVNULL
        try:
            psutil.test()
        finally:
            sys.stdout = stdout

    def test_cpu_count(self):
        self.assertEqual(psutil.cpu_count(), len(psutil.cpu_times(percpu=True)))
        self.assertGreaterEqual(psutil.cpu_count(), 1)

    def test_sys_cpu_times(self):
        total = 0
        times = psutil.cpu_times()
        sum(times)
        for cp_time in times:
            self.assertIsInstance(cp_time, float)
            self.assertGreaterEqual(cp_time, 0.0)
            total += cp_time
        self.assertEqual(total, sum(times))
        str(times)
        if not WINDOWS:
            # CPU times are always supposed to increase over time or
            # remain the same but never go backwards, see:
            # https://code.google.com/p/psutil/issues/detail?id=392
            last = psutil.cpu_times()
            for x in range(100):
                new = psutil.cpu_times()
                for field in new._fields:
                    new_t = getattr(new, field)
                    last_t = getattr(last, field)
                    self.assertGreaterEqual(new_t, last_t,
                                            msg="%s %s" % (new_t, last_t))
                last = new

    def test_sys_cpu_times2(self):
        t1 = sum(psutil.cpu_times())
        time.sleep(0.1)
        t2 = sum(psutil.cpu_times())
        difference = t2 - t1
        if not difference >= 0.05:
            self.fail("difference %s" % difference)

    def test_sys_per_cpu_times(self):
        for times in psutil.cpu_times(percpu=True):
            total = 0
            sum(times)
            for cp_time in times:
                self.assertIsInstance(cp_time, float)
                self.assertGreaterEqual(cp_time, 0.0)
                total += cp_time
            self.assertEqual(total, sum(times))
            str(times)
        self.assertEqual(len(psutil.cpu_times(percpu=True)[0]),
                         len(psutil.cpu_times(percpu=False)))
        if not WINDOWS:
            # CPU times are always supposed to increase over time or
            # remain the same but never go backwards, see:
            # https://code.google.com/p/psutil/issues/detail?id=392
            last = psutil.cpu_times(percpu=True)
            for x in range(100):
                new = psutil.cpu_times(percpu=True)
                for index in range(len(new)):
                    newcpu = new[index]
                    lastcpu = last[index]
                    for field in newcpu._fields:
                        new_t = getattr(newcpu, field)
                        last_t = getattr(lastcpu, field)
                        self.assertGreaterEqual(new_t, last_t,
                                                msg="%s %s" % (lastcpu, newcpu))
                last = new

    def test_sys_per_cpu_times2(self):
        tot1 = psutil.cpu_times(percpu=True)
        stop_at = time.time() + 0.1
        while 1:
            if time.time() >= stop_at:
                break
        tot2 = psutil.cpu_times(percpu=True)
        for t1, t2 in zip(tot1, tot2):
            t1, t2 = sum(t1), sum(t2)
            difference = t2 - t1
            if difference >= 0.05:
                return
        self.fail()

    def _test_cpu_percent(self, percent):
        self.assertIsInstance(percent, float)
        self.assertGreaterEqual(percent, 0.0)
        self.assertLessEqual(percent, 100.0)

    def test_sys_cpu_percent(self):
        psutil.cpu_percent(interval=0.001)
        for x in range(1000):
            self._test_cpu_percent(psutil.cpu_percent(interval=None))

    def test_sys_per_cpu_percent(self):
        self.assertEqual(len(psutil.cpu_percent(interval=0.001, percpu=True)),
                         psutil.cpu_count())
        for x in range(1000):
            percents = psutil.cpu_percent(interval=None, percpu=True)
            for percent in percents:
                self._test_cpu_percent(percent)

    def test_sys_cpu_times_percent(self):
        psutil.cpu_times_percent(interval=0.001)
        for x in range(1000):
            cpu = psutil.cpu_times_percent(interval=None)
            for percent in cpu:
                self._test_cpu_percent(percent)
            self._test_cpu_percent(sum(cpu))

    def test_sys_per_cpu_times_percent(self):
        self.assertEqual(len(psutil.cpu_times_percent(interval=0.001,
                                                      percpu=True)),
                         psutil.cpu_count())
        for x in range(1000):
            cpus = psutil.cpu_times_percent(interval=None, percpu=True)
            for cpu in cpus:
                for percent in cpu:
                    self._test_cpu_percent(percent)
                self._test_cpu_percent(sum(cpu))

    @unittest.skipIf(POSIX and not hasattr(os, 'statvfs'),
                     "os.statvfs() function not available on this platform")
    def test_disk_usage(self):
        usage = psutil.disk_usage(os.getcwd())
        assert usage.total > 0, usage
        assert usage.used > 0, usage
        assert usage.free > 0, usage
        assert usage.total > usage.used, usage
        assert usage.total > usage.free, usage
        assert 0 <= usage.percent <= 100, usage.percent
        if hasattr(shutil, 'disk_usage'):
            # py >= 3.3, see: http://bugs.python.org/issue12442
            shutil_usage = shutil.disk_usage(os.getcwd())
            tolerance = 5 * 1024 * 1024  # 5MB
            self.assertEqual(usage.total, shutil_usage.total)
            self.assertAlmostEqual(usage.free, shutil_usage.free,
                                   delta=tolerance)
            self.assertAlmostEqual(usage.used, shutil_usage.used,
                                   delta=tolerance)

        # if path does not exist OSError ENOENT is expected across
        # all platforms
        fname = tempfile.mktemp()
        try:
            psutil.disk_usage(fname)
        except OSError:
            err = sys.exc_info()[1]
            if err.args[0] != errno.ENOENT:
                raise
        else:
            self.fail("OSError not raised")

    @unittest.skipIf(POSIX and not hasattr(os, 'statvfs'),
                     "os.statvfs() function not available on this platform")
    def test_disk_usage_unicode(self):
        # see: https://code.google.com/p/psutil/issues/detail?id=416
        os.mkdir(TESTFN_UNICODE)
        psutil.disk_usage(TESTFN_UNICODE)

    @unittest.skipIf(POSIX and not hasattr(os, 'statvfs'),
                     "os.statvfs() function not available on this platform")
    def test_disk_partitions(self):
        # all = False
        for disk in psutil.disk_partitions(all=False):
            if WINDOWS and 'cdrom' in disk.opts:
                continue
            if not POSIX:
                assert os.path.exists(disk.device), disk
            else:
                # we cannot make any assumption about this, see:
                # http://goo.gl/p9c43
                disk.device
            if SUNOS:
                # on solaris apparently mount points can also be files
                assert os.path.exists(disk.mountpoint), disk
            else:
                assert os.path.isdir(disk.mountpoint), disk
            assert disk.fstype, disk
            self.assertIsInstance(disk.opts, str)

        # all = True
        for disk in psutil.disk_partitions(all=True):
            if not WINDOWS:
                try:
                    os.stat(disk.mountpoint)
                except OSError:
                    # http://mail.python.org/pipermail/python-dev/2012-June/120787.html
                    err = sys.exc_info()[1]
                    if err.errno not in (errno.EPERM, errno.EACCES):
                        raise
                else:
                    if SUNOS:
                        # on solaris apparently mount points can also be files
                        assert os.path.exists(disk.mountpoint), disk
                    else:
                        assert os.path.isdir(disk.mountpoint), disk
            self.assertIsInstance(disk.fstype, str)
            self.assertIsInstance(disk.opts, str)

        def find_mount_point(path):
            path = os.path.abspath(path)
            while not os.path.ismount(path):
                path = os.path.dirname(path)
            return path

        mount = find_mount_point(__file__)
        mounts = [x.mountpoint for x in psutil.disk_partitions(all=True)]
        self.assertIn(mount, mounts)
        psutil.disk_usage(mount)

    def test_net_io_counters(self):
        def check_ntuple(nt):
            self.assertEqual(nt[0], nt.bytes_sent)
            self.assertEqual(nt[1], nt.bytes_recv)
            self.assertEqual(nt[2], nt.packets_sent)
            self.assertEqual(nt[3], nt.packets_recv)
            self.assertEqual(nt[4], nt.errin)
            self.assertEqual(nt[5], nt.errout)
            self.assertEqual(nt[6], nt.dropin)
            self.assertEqual(nt[7], nt.dropout)
            assert nt.bytes_sent >= 0, nt
            assert nt.bytes_recv >= 0, nt
            assert nt.packets_sent >= 0, nt
            assert nt.packets_recv >= 0, nt
            assert nt.errin >= 0, nt
            assert nt.errout >= 0, nt
            assert nt.dropin >= 0, nt
            assert nt.dropout >= 0, nt

        ret = psutil.net_io_counters(pernic=False)
        check_ntuple(ret)
        ret = psutil.net_io_counters(pernic=True)
        assert ret != []
        for key in ret:
            assert key
            check_ntuple(ret[key])

    def test_disk_io_counters(self):
        def check_ntuple(nt):
            self.assertEqual(nt[0], nt.read_count)
            self.assertEqual(nt[1], nt.write_count)
            self.assertEqual(nt[2], nt.read_bytes)
            self.assertEqual(nt[3], nt.write_bytes)
            self.assertEqual(nt[4], nt.read_time)
            self.assertEqual(nt[5], nt.write_time)
            assert nt.read_count >= 0, nt
            assert nt.write_count >= 0, nt
            assert nt.read_bytes >= 0, nt
            assert nt.write_bytes >= 0, nt
            assert nt.read_time >= 0, nt
            assert nt.write_time >= 0, nt

        ret = psutil.disk_io_counters(perdisk=False)
        check_ntuple(ret)
        ret = psutil.disk_io_counters(perdisk=True)
        # make sure there are no duplicates
        self.assertEqual(len(ret), len(set(ret)))
        for key in ret:
            assert key, key
            check_ntuple(ret[key])
            if LINUX and key[-1].isdigit():
                # if 'sda1' is listed 'sda' shouldn't, see:
                # http://code.google.com/p/psutil/issues/detail?id=338
                while key[-1].isdigit():
                    key = key[:-1]
                self.assertNotIn(key, ret.keys())

    def test_get_users(self):
        users = psutil.get_users()
        assert users
        for user in users:
            assert user.name, user
            user.terminal
            user.host
            assert user.started > 0.0, user
            datetime.datetime.fromtimestamp(user.started)


# ===================================================================
# --- psutil.Process class tests
# ===================================================================

class TestProcess(unittest.TestCase):
    """Tests for psutil.Process class."""

    def setUp(self):
        safe_remove(TESTFN)

    def tearDown(self):
        reap_children()

    def test_no_pid(self):
        self.assertEqual(psutil.Process().pid, os.getpid())

    def test_kill(self):
        sproc = get_test_subprocess(wait=True)
        test_pid = sproc.pid
        p = psutil.Process(test_pid)
        name = p.name
        p.kill()
        p.wait()
        self.assertFalse(psutil.pid_exists(test_pid) and name == PYTHON)

    def test_terminate(self):
        sproc = get_test_subprocess(wait=True)
        test_pid = sproc.pid
        p = psutil.Process(test_pid)
        name = p.name
        p.terminate()
        p.wait()
        self.assertFalse(psutil.pid_exists(test_pid) and name == PYTHON)

    def test_send_signal(self):
        if POSIX:
            sig = signal.SIGKILL
        else:
            sig = signal.SIGTERM
        sproc = get_test_subprocess()
        test_pid = sproc.pid
        p = psutil.Process(test_pid)
        name = p.name
        p.send_signal(sig)
        p.wait()
        self.assertFalse(psutil.pid_exists(test_pid) and name == PYTHON)

    def test_wait(self):
        # check exit code signal
        sproc = get_test_subprocess()
        p = psutil.Process(sproc.pid)
        p.kill()
        code = p.wait()
        if os.name == 'posix':
            self.assertEqual(code, signal.SIGKILL)
        else:
            self.assertEqual(code, 0)
        self.assertFalse(p.is_running())

        sproc = get_test_subprocess()
        p = psutil.Process(sproc.pid)
        p.terminate()
        code = p.wait()
        if os.name == 'posix':
            self.assertEqual(code, signal.SIGTERM)
        else:
            self.assertEqual(code, 0)
        self.assertFalse(p.is_running())

        # check sys.exit() code
        code = "import time, sys; time.sleep(0.01); sys.exit(5);"
        sproc = get_test_subprocess([PYTHON, "-c", code])
        p = psutil.Process(sproc.pid)
        self.assertEqual(p.wait(), 5)
        self.assertFalse(p.is_running())

        # Test wait() issued twice.
        # It is not supposed to raise NSP when the process is gone.
        # On UNIX this should return None, on Windows it should keep
        # returning the exit code.
        sproc = get_test_subprocess([PYTHON, "-c", code])
        p = psutil.Process(sproc.pid)
        self.assertEqual(p.wait(), 5)
        self.assertIn(p.wait(), (5, None))

        # test timeout
        sproc = get_test_subprocess()
        p = psutil.Process(sproc.pid)
        p.name
        self.assertRaises(psutil.TimeoutExpired, p.wait, 0.01)

        # timeout < 0 not allowed
        self.assertRaises(ValueError, p.wait, -1)

    @unittest.skipUnless(POSIX, '')  # XXX why is this skipped on Windows?
    def test_wait_non_children(self):
        # test wait() against processes which are not our children
        code = "import sys;"
        code += "from subprocess import Popen, PIPE;"
        code += "cmd = ['%s', '-c', 'import time; time.sleep(2)'];" % PYTHON
        code += "sp = Popen(cmd, stdout=PIPE);"
        code += "sys.stdout.write(str(sp.pid));"
        sproc = get_test_subprocess([PYTHON, "-c", code], stdout=subprocess.PIPE)

        grandson_pid = int(sproc.stdout.read())
        grandson_proc = psutil.Process(grandson_pid)
        try:
            self.assertRaises(psutil.TimeoutExpired, grandson_proc.wait, 0.01)
            grandson_proc.kill()
            ret = grandson_proc.wait()
            self.assertEqual(ret, None)
        finally:
            if grandson_proc.is_running():
                grandson_proc.kill()
                grandson_proc.wait()

    def test_wait_timeout_0(self):
        sproc = get_test_subprocess()
        p = psutil.Process(sproc.pid)
        self.assertRaises(psutil.TimeoutExpired, p.wait, 0)
        p.kill()
        stop_at = time.time() + 2
        while 1:
            try:
                code = p.wait(0)
            except psutil.TimeoutExpired:
                if time.time() >= stop_at:
                    raise
            else:
                break
        if os.name == 'posix':
            self.assertEqual(code, signal.SIGKILL)
        else:
            self.assertEqual(code, 0)
        self.assertFalse(p.is_running())

    def test_cpu_percent(self):
        p = psutil.Process(os.getpid())
        p.get_cpu_percent(interval=0.001)
        p.get_cpu_percent(interval=0.001)
        for x in range(100):
            percent = p.get_cpu_percent(interval=None)
            self.assertIsInstance(percent, float)
            self.assertGreaterEqual(percent, 0.0)
            if os.name != 'posix':
                self.assertLessEqual(percent, 100.0)
            else:
                self.assertGreaterEqual(percent, 0.0)

    def test_cpu_times(self):
        times = psutil.Process(os.getpid()).get_cpu_times()
        assert (times.user > 0.0) or (times.system > 0.0), times
        # make sure returned values can be pretty printed with strftime
        time.strftime("%H:%M:%S", time.localtime(times.user))
        time.strftime("%H:%M:%S", time.localtime(times.system))

    # Test Process.cpu_times() against os.times()
    # os.times() is broken on Python 2.6
    # http://bugs.python.org/issue1040026
    # XXX fails on OSX: not sure if it's for os.times(). We should
    # try this with Python 2.7 and re-enable the test.

    @unittest.skipUnless(sys.version_info > (2, 6, 1) and not OSX,
                         'os.times() is not reliable on this Python version')
    def test_cpu_times2(self):
        user_time, kernel_time = psutil.Process(os.getpid()).get_cpu_times()
        utime, ktime = os.times()[:2]

        # Use os.times()[:2] as base values to compare our results
        # using a tolerance  of +/- 0.1 seconds.
        # It will fail if the difference between the values is > 0.1s.
        if (max([user_time, utime]) - min([user_time, utime])) > 0.1:
            self.fail("expected: %s, found: %s" % (utime, user_time))

        if (max([kernel_time, ktime]) - min([kernel_time, ktime])) > 0.1:
            self.fail("expected: %s, found: %s" % (ktime, kernel_time))

    def test_create_time(self):
        sproc = get_test_subprocess(wait=True)
        now = time.time()
        p = psutil.Process(sproc.pid)
        create_time = p.create_time

        # Use time.time() as base value to compare our result using a
        # tolerance of +/- 1 second.
        # It will fail if the difference between the values is > 2s.
        difference = abs(create_time - now)
        if difference > 2:
            self.fail("expected: %s, found: %s, difference: %s"
                      % (now, create_time, difference))

        # make sure returned value can be pretty printed with strftime
        time.strftime("%Y %m %d %H:%M:%S", time.localtime(p.create_time))

    @unittest.skipIf(WINDOWS, 'windows only')
    def test_terminal(self):
        terminal = psutil.Process(os.getpid()).terminal
        if sys.stdin.isatty():
            self.assertEqual(terminal, sh('tty'))
        else:
            assert terminal, repr(terminal)

    @unittest.skipIf(not hasattr(psutil.Process, 'get_io_counters'),
                     'not available on this platform')
    @skip_on_not_implemented(only_if=LINUX)
    def test_get_io_counters(self):
        p = psutil.Process(os.getpid())
        # test reads
        io1 = p.get_io_counters()
        f = open(PYTHON, 'rb')
        f.read()
        f.close()
        io2 = p.get_io_counters()
        if not BSD:
            assert io2.read_count > io1.read_count, (io1, io2)
            self.assertEqual(io2.write_count, io1.write_count)
        assert io2.read_bytes >= io1.read_bytes, (io1, io2)
        assert io2.write_bytes >= io1.write_bytes, (io1, io2)
        # test writes
        io1 = p.get_io_counters()
        f = tempfile.TemporaryFile(prefix=TESTFILE_PREFIX)
        if PY3:
            f.write(bytes("x" * 1000000, 'ascii'))
        else:
            f.write("x" * 1000000)
        f.close()
        io2 = p.get_io_counters()
        assert io2.write_count >= io1.write_count, (io1, io2)
        assert io2.write_bytes >= io1.write_bytes, (io1, io2)
        assert io2.read_count >= io1.read_count, (io1, io2)
        assert io2.read_bytes >= io1.read_bytes, (io1, io2)

    # Linux and Windows Vista+
    @unittest.skipUnless(hasattr(psutil.Process, 'get_ionice'),
                         'Linux and Windows Vista only')
    def test_get_set_ionice(self):
        if LINUX:
            from psutil import (IOPRIO_CLASS_NONE, IOPRIO_CLASS_RT,
                                IOPRIO_CLASS_BE, IOPRIO_CLASS_IDLE)
            self.assertEqual(IOPRIO_CLASS_NONE, 0)
            self.assertEqual(IOPRIO_CLASS_RT, 1)
            self.assertEqual(IOPRIO_CLASS_BE, 2)
            self.assertEqual(IOPRIO_CLASS_IDLE, 3)
            p = psutil.Process(os.getpid())
            try:
                p.set_ionice(2)
                ioclass, value = p.get_ionice()
                self.assertEqual(ioclass, 2)
                self.assertEqual(value, 4)
                #
                p.set_ionice(3)
                ioclass, value = p.get_ionice()
                self.assertEqual(ioclass, 3)
                self.assertEqual(value, 0)
                #
                p.set_ionice(2, 0)
                ioclass, value = p.get_ionice()
                self.assertEqual(ioclass, 2)
                self.assertEqual(value, 0)
                p.set_ionice(2, 7)
                ioclass, value = p.get_ionice()
                self.assertEqual(ioclass, 2)
                self.assertEqual(value, 7)
                self.assertRaises(ValueError, p.set_ionice, 2, 10)
            finally:
                p.set_ionice(IOPRIO_CLASS_NONE)
        else:
            p = psutil.Process(os.getpid())
            original = p.get_ionice()
            try:
                value = 0  # very low
                if original == value:
                    value = 1  # low
                p.set_ionice(value)
                self.assertEqual(p.get_ionice(), value)
            finally:
                p.set_ionice(original)
            #
            self.assertRaises(ValueError, p.set_ionice, 3)
            self.assertRaises(TypeError, p.set_ionice, 2, 1)

    @unittest.skipUnless(hasattr(psutil.Process, 'get_rlimit'),
                         "only available on Linux >= 2.6.36")
    def test_get_rlimit(self):
        import resource
        p = psutil.Process(os.getpid())
        names = [x for x in dir(psutil) if x.startswith('RLIMIT_')]
        for name in names:
            value = getattr(psutil, name)
            if name in dir(resource):
                self.assertEqual(value, getattr(resource, name))
                self.assertEqual(p.get_rlimit(value), resource.getrlimit(value))
            else:
                ret = p.get_rlimit(value)
                self.assertEqual(len(ret), 2)
                self.assertGreaterEqual(ret[0], -1)
                self.assertGreaterEqual(ret[1], -1)

    @unittest.skipUnless(hasattr(psutil.Process, 'set_rlimit'),
                         "only available on Linux >= 2.6.36")
    def test_set_rlimit(self):
        sproc = get_test_subprocess()
        p = psutil.Process(sproc.pid)
        p.set_rlimit(psutil.RLIMIT_NOFILE, (5, 5))
        self.assertEqual(p.get_rlimit(psutil.RLIMIT_NOFILE), (5, 5))

    def test_get_num_threads(self):
        # on certain platforms such as Linux we might test for exact
        # thread number, since we always have with 1 thread per process,
        # but this does not apply across all platforms (OSX, Windows)
        p = psutil.Process(os.getpid())
        step1 = p.get_num_threads()

        thread = ThreadTask()
        thread.start()
        try:
            step2 = p.get_num_threads()
            self.assertEqual(step2, step1 + 1)
            thread.stop()
        finally:
            if thread._running:
                thread.stop()

    @unittest.skipUnless(WINDOWS, 'Windows only')
    def test_get_num_handles(self):
        # a better test is done later into test/_windows.py
        p = psutil.Process(os.getpid())
        self.assertGreater(p.get_num_handles(), 0)

    def test_get_threads(self):
        p = psutil.Process(os.getpid())
        step1 = p.get_threads()

        thread = ThreadTask()
        thread.start()

        try:
            step2 = p.get_threads()
            self.assertEqual(len(step2), len(step1) + 1)
            # on Linux, first thread id is supposed to be this process
            if LINUX:
                self.assertEqual(step2[0].id, os.getpid())
            athread = step2[0]
            # test named tuple
            self.assertEqual(athread.id, athread[0])
            self.assertEqual(athread.user_time, athread[1])
            self.assertEqual(athread.system_time, athread[2])
            # test num threads
            thread.stop()
        finally:
            if thread._running:
                thread.stop()

    def test_get_memory_info(self):
        p = psutil.Process(os.getpid())

        # step 1 - get a base value to compare our results
        rss1, vms1 = p.get_memory_info()
        percent1 = p.get_memory_percent()
        self.assertGreater(rss1, 0)
        self.assertGreater(vms1, 0)

        # step 2 - allocate some memory
        memarr = [None] * 1500000

        rss2, vms2 = p.get_memory_info()
        percent2 = p.get_memory_percent()
        # make sure that the memory usage bumped up
        self.assertGreater(rss2, rss1)
        self.assertGreaterEqual(vms2, vms1)  # vms might be equal
        self.assertGreater(percent2, percent1)
        del memarr

    # def test_get_ext_memory_info(self):
    # # tested later in fetch all test suite

    def test_get_memory_maps(self):
        p = psutil.Process(os.getpid())
        maps = p.get_memory_maps()
        paths = [x for x in maps]
        self.assertEqual(len(paths), len(set(paths)))
        ext_maps = p.get_memory_maps(grouped=False)

        for nt in maps:
            if not nt.path.startswith('['):
                assert os.path.isabs(nt.path), nt.path
                if POSIX:
                    assert os.path.exists(nt.path), nt.path
                else:
                    # XXX - On Windows we have this strange behavior with
                    # 64 bit dlls: they are visible via explorer but cannot
                    # be accessed via os.stat() (wtf?).
                    if '64' not in os.path.basename(nt.path):
                        assert os.path.exists(nt.path), nt.path
        for nt in ext_maps:
            for fname in nt._fields:
                value = getattr(nt, fname)
                if fname == 'path':
                    continue
                elif fname in ('addr', 'perms'):
                    assert value, value
                else:
                    self.assertIsInstance(value, (int, long))
                    assert value >= 0, value

    def test_get_memory_percent(self):
        p = psutil.Process(os.getpid())
        self.assertGreater(p.get_memory_percent(), 0.0)

    def test_pid(self):
        sproc = get_test_subprocess()
        self.assertEqual(psutil.Process(sproc.pid).pid, sproc.pid)

    def test_is_running(self):
        sproc = get_test_subprocess(wait=True)
        p = psutil.Process(sproc.pid)
        assert p.is_running()
        assert p.is_running()
        p.kill()
        p.wait()
        assert not p.is_running()
        assert not p.is_running()

    def test_exe(self):
        sproc = get_test_subprocess(wait=True)
        exe = psutil.Process(sproc.pid).exe
        try:
            self.assertEqual(exe, PYTHON)
        except AssertionError:
            if WINDOWS and len(exe) == len(PYTHON):
                # on Windows we don't care about case sensitivity
                self.assertEqual(exe.lower(), PYTHON.lower())
            else:
                # certain platforms such as BSD are more accurate returning:
                # "/usr/local/bin/python2.7"
                # ...instead of:
                # "/usr/local/bin/python"
                # We do not want to consider this difference in accuracy
                # an error.
                ver = "%s.%s" % (sys.version_info[0], sys.version_info[1])
                self.assertEqual(exe.replace(ver, ''), PYTHON.replace(ver, ''))

    def test_cmdline(self):
        cmdline = [PYTHON, "-c", "import time; time.sleep(2)"]
        sproc = get_test_subprocess(cmdline, wait=True)
        self.assertEqual(' '.join(psutil.Process(sproc.pid).cmdline),
                         ' '.join(cmdline))

    def test_name(self):
        sproc = get_test_subprocess(PYTHON, wait=True)
        name = psutil.Process(sproc.pid).name.lower()
        pyexe = os.path.basename(os.path.realpath(sys.executable)).lower()
        assert pyexe.startswith(name), (pyexe, name)

    @unittest.skipUnless(POSIX, 'posix only')
    def test_uids(self):
        p = psutil.Process(os.getpid())
        real, effective, saved = p.uids
        # os.getuid() refers to "real" uid
        self.assertEqual(real, os.getuid())
        # os.geteuid() refers to "effective" uid
        self.assertEqual(effective, os.geteuid())
        # no such thing as os.getsuid() ("saved" uid), but starting
        # from python 2.7 we have os.getresuid()[2]
        if hasattr(os, "getresuid"):
            self.assertEqual(saved, os.getresuid()[2])

    @unittest.skipUnless(POSIX, 'posix only')
    def test_gids(self):
        p = psutil.Process(os.getpid())
        real, effective, saved = p.gids
        # os.getuid() refers to "real" uid
        self.assertEqual(real, os.getgid())
        # os.geteuid() refers to "effective" uid
        self.assertEqual(effective, os.getegid())
        # no such thing as os.getsuid() ("saved" uid), but starting
        # from python 2.7 we have os.getresgid()[2]
        if hasattr(os, "getresuid"):
            self.assertEqual(saved, os.getresgid()[2])

    def test_nice(self):
        p = psutil.Process(os.getpid())
        self.assertRaises(TypeError, p.set_nice, "str")
        if os.name == 'nt':
            try:
                self.assertEqual(p.get_nice(), psutil.NORMAL_PRIORITY_CLASS)
                p.set_nice(psutil.HIGH_PRIORITY_CLASS)
                self.assertEqual(p.get_nice(), psutil.HIGH_PRIORITY_CLASS)
                p.set_nice(psutil.NORMAL_PRIORITY_CLASS)
                self.assertEqual(p.get_nice(), psutil.NORMAL_PRIORITY_CLASS)
            finally:
                p.set_nice(psutil.NORMAL_PRIORITY_CLASS)
        else:
            try:
                try:
                    first_nice = p.get_nice()
                    p.set_nice(1)
                    self.assertEqual(p.get_nice(), 1)
                    # going back to previous nice value raises AccessDenied on OSX
                    if not OSX:
                        p.set_nice(0)
                        self.assertEqual(p.get_nice(), 0)
                except psutil.AccessDenied:
                    pass
            finally:
                try:
                    p.set_nice(first_nice)
                except psutil.AccessDenied:
                    pass

    def test_status(self):
        p = psutil.Process(os.getpid())
        self.assertEqual(p.status, psutil.STATUS_RUNNING)

    def test_username(self):
        sproc = get_test_subprocess()
        p = psutil.Process(sproc.pid)
        if POSIX:
            import pwd
            self.assertEqual(p.username, pwd.getpwuid(os.getuid()).pw_name)
        elif WINDOWS and 'USERNAME' in os.environ:
            expected_username = os.environ['USERNAME']
            expected_domain = os.environ['USERDOMAIN']
            domain, username = p.username.split('\\')
            self.assertEqual(domain, expected_domain)
            self.assertEqual(username, expected_username)
        else:
            p.username

    @unittest.skipUnless(hasattr(psutil.Process, "getcwd"),
                         'not available on this platform')
    def test_getcwd(self):
        sproc = get_test_subprocess(wait=True)
        p = psutil.Process(sproc.pid)
        self.assertEqual(p.getcwd(), os.getcwd())

    @unittest.skipIf(not hasattr(psutil.Process, "getcwd"),
                     'not available on this platform')
    def test_getcwd_2(self):
        cmd = [PYTHON, "-c", "import os, time; os.chdir('..'); time.sleep(2)"]
        sproc = get_test_subprocess(cmd, wait=True)
        p = psutil.Process(sproc.pid)
        call_until(p.getcwd, "ret == os.path.dirname(os.getcwd())", timeout=1)

    @unittest.skipIf(not hasattr(psutil.Process, "get_cpu_affinity"),
                     'not available on this platform')
    def test_cpu_affinity(self):
        p = psutil.Process(os.getpid())
        initial = p.get_cpu_affinity()
        all_cpus = list(range(len(psutil.cpu_percent(percpu=True))))
        #
        for n in all_cpus:
            p.set_cpu_affinity([n])
            self.assertEqual(p.get_cpu_affinity(), [n])
        #
        p.set_cpu_affinity(all_cpus)
        self.assertEqual(p.get_cpu_affinity(), all_cpus)
        #
        self.assertRaises(TypeError, p.set_cpu_affinity, 1)
        p.set_cpu_affinity(initial)
        invalid_cpu = [len(psutil.cpu_times(percpu=True)) + 10]
        self.assertRaises(ValueError, p.set_cpu_affinity, invalid_cpu)
        self.assertRaises(ValueError, p.set_cpu_affinity, range(10000, 11000))

    def test_get_open_files(self):
        # current process
        p = psutil.Process(os.getpid())
        files = p.get_open_files()
        self.assertFalse(TESTFN in files)
        f = open(TESTFN, 'w')
        call_until(p.get_open_files, "len(ret) != %i" % len(files))
        filenames = [x.path for x in p.get_open_files()]
        self.assertIn(TESTFN, filenames)
        f.close()
        for file in filenames:
            assert os.path.isfile(file), file

        # another process
        cmdline = "import time; f = open(r'%s', 'r'); time.sleep(2);" % TESTFN
        sproc = get_test_subprocess([PYTHON, "-c", cmdline], wait=True)
        p = psutil.Process(sproc.pid)

        for x in range(100):
            filenames = [x.path for x in p.get_open_files()]
            if TESTFN in filenames:
                break
            time.sleep(.01)
        else:
            self.assertIn(TESTFN, filenames)
        for file in filenames:
            assert os.path.isfile(file), file

    def test_get_open_files2(self):
        # test fd and path fields
        fileobj = open(TESTFN, 'w')
        p = psutil.Process(os.getpid())
        for path, fd in p.get_open_files():
            if path == fileobj.name or fd == fileobj.fileno():
                break
        else:
            self.fail("no file found; files=%s" % repr(p.get_open_files()))
        self.assertEqual(path, fileobj.name)
        if WINDOWS:
            self.assertEqual(fd, -1)
        else:
            self.assertEqual(fd, fileobj.fileno())
        # test positions
        ntuple = p.get_open_files()[0]
        self.assertEqual(ntuple[0], ntuple.path)
        self.assertEqual(ntuple[1], ntuple.fd)
        # test file is gone
        fileobj.close()
        self.assertTrue(fileobj.name not in p.get_open_files())

    def test_connection_constants(self):
        ints = []
        strs = []
        for name in dir(psutil):
            if name.startswith('CONN_'):
                num = getattr(psutil, name)
                str_ = str(num)
                assert str_.isupper(), str_
                assert str_ not in strs, str_
                assert num not in ints, num
                ints.append(num)
                strs.append(str_)
        if SUNOS:
            psutil.CONN_IDLE
            psutil.CONN_BOUND
        if WINDOWS:
            psutil.CONN_DELETE_TCB

    def test_get_connections(self):
        arg = "import socket, time;" \
              "s = socket.socket();" \
              "s.bind(('127.0.0.1', 0));" \
              "s.listen(1);" \
              "conn, addr = s.accept();" \
              "time.sleep(2);"
        sproc = get_test_subprocess([PYTHON, "-c", arg])
        p = psutil.Process(sproc.pid)
        cons = call_until(p.get_connections, "len(ret) != 0", timeout=1)
        self.assertEqual(len(cons), 1)
        con = cons[0]
        check_connection(con)
        self.assertEqual(con.family, AF_INET)
        self.assertEqual(con.type, SOCK_STREAM)
        self.assertEqual(con.status, psutil.CONN_LISTEN, con.status)
        self.assertEqual(con.laddr[0], '127.0.0.1')
        self.assertEqual(con.raddr, ())
        # test positions
        self.assertEqual(con[0], con.fd)
        self.assertEqual(con[1], con.family)
        self.assertEqual(con[2], con.type)
        self.assertEqual(con[3], con.laddr)
        self.assertEqual(con[4], con.raddr)
        self.assertEqual(con[5], con.status)
        # test kind arg
        self.assertRaises(ValueError, p.get_connections, 'foo')

    @unittest.skipUnless(supports_ipv6(), 'IPv6 is not supported')
    def test_get_connections_ipv6(self):
        s = socket.socket(AF_INET6, SOCK_STREAM)
        s.bind(('::1', 0))
        s.listen(1)
        cons = psutil.Process(os.getpid()).get_connections()
        s.close()
        self.assertEqual(len(cons), 1)
        self.assertEqual(cons[0].laddr[0], '::1')

    @unittest.skipUnless(hasattr(socket, 'AF_UNIX'), 'AF_UNIX is not supported')
    def test_get_connections_unix(self):
        def check(type):
            safe_remove(TESTFN)
            sock = socket.socket(AF_UNIX, type)
            try:
                sock.bind(TESTFN)
                conn = psutil.Process(os.getpid()).get_connections(kind='unix')[0]
                check_connection(conn)
                if conn.fd != -1:  # != sunos and windows
                    self.assertEqual(conn.fd, sock.fileno())
                self.assertEqual(conn.family, AF_UNIX)
                self.assertEqual(conn.type, type)
                self.assertEqual(conn.laddr, TESTFN)
            finally:
                sock.close()

        check(SOCK_STREAM)
        check(SOCK_DGRAM)

    @unittest.skipUnless(hasattr(socket, "fromfd"),
                         'socket.fromfd() is not availble')
    @unittest.skipIf(WINDOWS or SUNOS,
                     'connection fd available on this platform')
    def test_connection_fromfd(self):
        sock = socket.socket()
        sock.bind(('localhost', 0))
        sock.listen(1)
        p = psutil.Process(os.getpid())
        for conn in p.get_connections():
            if conn.fd == sock.fileno():
                break
        else:
            sock.close()
            self.fail("couldn't find socket fd")
        dupsock = socket.fromfd(conn.fd, conn.family, conn.type)
        try:
            self.assertEqual(dupsock.getsockname(), conn.laddr)
            self.assertNotEqual(sock.fileno(), dupsock.fileno())
        finally:
            sock.close()
            dupsock.close()

    def test_get_connections_all(self):
        tcp_template = textwrap.dedent("""
            import socket
            s = socket.socket($family, socket.SOCK_STREAM)
            s.bind(('$addr', 0))
            s.listen(1)
            conn, addr = s.accept()
        """)

        udp_template = textwrap.dedent("""
            import socket, time
            s = socket.socket($family, socket.SOCK_DGRAM)
            s.bind(('$addr', 0))
            time.sleep(2)
        """)

        from string import Template
        tcp4_template = Template(tcp_template).substitute(
            family=int(AF_INET), addr="127.0.0.1")
        udp4_template = Template(udp_template).substitute(
            family=int(AF_INET), addr="127.0.0.1")
        tcp6_template = Template(tcp_template).substitute(
            family=int(AF_INET6), addr="::1")
        udp6_template = Template(udp_template).substitute(
            family=int(AF_INET6), addr="::1")

        # launch various subprocess instantiating a socket of various
        # families and types to enrich psutil results
        tcp4_proc = pyrun(tcp4_template)
        udp4_proc = pyrun(udp4_template)
        if supports_ipv6():
            tcp6_proc = pyrun(tcp6_template)
            udp6_proc = pyrun(udp6_template)
        else:
            tcp6_proc = None
            udp6_proc = None

        # check matches against subprocesses just created
        all_kinds = ("all", "inet", "inet4", "inet6", "tcp", "tcp4", "tcp6",
                     "udp", "udp4", "udp6")

        def check_conn(proc, conn, family, type, laddr, raddr, status, kinds):
            self.assertEqual(conn.family, family)
            self.assertEqual(conn.type, type)
            self.assertIn(conn.laddr[0], laddr)
            self.assertEqual(conn.raddr, raddr)
            self.assertEqual(conn.status, status)
            for kind in all_kinds:
                cons = proc.get_connections(kind=kind)
                if kind in kinds:
                    assert cons != [], cons
                else:
                    self.assertEqual(cons, [], cons)

        for p in psutil.Process(os.getpid()).get_children():
            for conn in p.get_connections():
                # TCP v4
                if p.pid == tcp4_proc.pid:
                    check_conn(p, conn, AF_INET, SOCK_STREAM, "127.0.0.1", (),
                               psutil.CONN_LISTEN,
                               ("all", "inet", "inet4", "tcp", "tcp4"))
                # UDP v4
                elif p.pid == udp4_proc.pid:
                    check_conn(p, conn, AF_INET, SOCK_DGRAM, "127.0.0.1", (),
                               psutil.CONN_NONE,
                               ("all", "inet", "inet4", "udp", "udp4"))
                # TCP v6
                elif p.pid == getattr(tcp6_proc, "pid", None):
                    check_conn(p, conn, AF_INET6, SOCK_STREAM, ("::", "::1"),
                               (), psutil.CONN_LISTEN,
                               ("all", "inet", "inet6", "tcp", "tcp6"))
                # UDP v6
                elif p.pid == getattr(udp6_proc, "pid", None):
                    check_conn(p, conn, AF_INET6, SOCK_DGRAM, ("::", "::1"),
                               (), psutil.CONN_NONE,
                               ("all", "inet", "inet6", "udp", "udp6"))

    @unittest.skipUnless(POSIX, 'posix only')
    def test_get_num_fds(self):
        p = psutil.Process(os.getpid())
        start = p.get_num_fds()
        file = open(TESTFN, 'w')
        self.assertEqual(p.get_num_fds(), start + 1)
        sock = socket.socket()
        self.assertEqual(p.get_num_fds(), start + 2)
        file.close()
        sock.close()
        self.assertEqual(p.get_num_fds(), start)

    @skip_on_not_implemented(only_if=LINUX)
    def test_get_num_ctx_switches(self):
        p = psutil.Process(os.getpid())
        before = sum(p.get_num_ctx_switches())
        for x in range(500000):
            after = sum(p.get_num_ctx_switches())
            if after > before:
                return
        self.fail("num ctx switches still the same after 50.000 iterations")

    def test_parent_ppid(self):
        this_parent = os.getpid()
        sproc = get_test_subprocess()
        p = psutil.Process(sproc.pid)
        self.assertEqual(p.ppid, this_parent)
        self.assertEqual(p.parent.pid, this_parent)
        # no other process is supposed to have us as parent
        for p in psutil.process_iter():
            if p.pid == sproc.pid:
                continue
            self.assertTrue(p.ppid != this_parent)

    def test_get_children(self):
        p = psutil.Process(os.getpid())
        self.assertEqual(p.get_children(), [])
        self.assertEqual(p.get_children(recursive=True), [])
        sproc = get_test_subprocess()
        children1 = p.get_children()
        children2 = p.get_children(recursive=True)
        for children in (children1, children2):
            self.assertEqual(len(children), 1)
            self.assertEqual(children[0].pid, sproc.pid)
            self.assertEqual(children[0].ppid, os.getpid())

    def test_get_children_recursive(self):
        # here we create a subprocess which creates another one as in:
        # A (parent) -> B (child) -> C (grandchild)
        s = "import subprocess, os, sys, time;"
        s += "PYTHON = os.path.realpath(sys.executable);"
        s += "cmd = [PYTHON, '-c', 'import time; time.sleep(2);'];"
        s += "subprocess.Popen(cmd);"
        s += "time.sleep(2);"
        get_test_subprocess(cmd=[PYTHON, "-c", s])
        p = psutil.Process(os.getpid())
        self.assertEqual(len(p.get_children(recursive=False)), 1)
        # give the grandchild some time to start
        stop_at = time.time() + 1.5
        while time.time() < stop_at:
            children = p.get_children(recursive=True)
            if len(children) > 1:
                break
        self.assertEqual(len(children), 2)
        self.assertEqual(children[0].ppid, os.getpid())
        self.assertEqual(children[1].ppid, children[0].pid)

    def test_get_children_duplicates(self):
        # find the process which has the highest number of children
        from psutil._compat import defaultdict
        table = defaultdict(int)
        for p in psutil.process_iter():
            try:
                table[p.ppid] += 1
            except psutil.Error:
                pass
        # this is the one, now let's make sure there are no duplicates
        pid = sorted(table.items(), key=lambda x: x[1])[-1][0]
        p = psutil.Process(pid)
        try:
            c = p.get_children(recursive=True)
        except psutil.AccessDenied:  # windows
            pass
        else:
            self.assertEqual(len(c), len(set(c)))

    def test_suspend_resume(self):
        sproc = get_test_subprocess(wait=True)
        p = psutil.Process(sproc.pid)
        p.suspend()
        for x in range(100):
            if p.status == psutil.STATUS_STOPPED:
                break
            time.sleep(0.01)
        p.resume()
        self.assertNotEqual(p.status, psutil.STATUS_STOPPED)

    def test_invalid_pid(self):
        self.assertRaises(TypeError, psutil.Process, "1")
        self.assertRaises(ValueError, psutil.Process, -1)

    def test_as_dict(self):
        p = psutil.Process(os.getpid())
        d = p.as_dict()
        try:
            import json
        except ImportError:
            pass
        else:
            json.loads(json.dumps(d))
        #
        d = p.as_dict(attrs=['exe', 'name'])
        self.assertEqual(sorted(d.keys()), ['exe', 'name'])
        #
        p = psutil.Process(min(psutil.get_pid_list()))
        d = p.as_dict(attrs=['get_connections'], ad_value='foo')
        if not isinstance(d['connections'], list):
            self.assertEqual(d['connections'], 'foo')

    def test_halfway_terminated_process(self):
        # Test that NoSuchProcess exception gets raised in case the
        # process dies after we create the Process object.
        # Example:
        #  >>> proc = Process(1234)
        # >>> time.sleep(2)  # time-consuming task, process dies in meantime
        #  >>> proc.name
        # Refers to Issue #15
        sproc = get_test_subprocess()
        p = psutil.Process(sproc.pid)
        p.kill()
        p.wait()

        for name in dir(p):
            if name.startswith('_')\
                or name in ('pid', 'send_signal', 'is_running', 'set_ionice',
                            'wait', 'set_cpu_affinity', 'create_time', 'set_nice',
                            'nice'):
                continue
            try:
                # if name == 'get_rlimit'
                args = ()
                meth = getattr(p, name)
                if callable(meth):
                    if name == 'get_rlimit':
                        args = (psutil.RLIMIT_NOFILE,)
                    elif name == 'set_rlimit':
                        args = (psutil.RLIMIT_NOFILE, (5, 5))
                    meth(*args)
            except psutil.NoSuchProcess:
                pass
            except NotImplementedError:
                pass
            else:
                self.fail("NoSuchProcess exception not raised for %r" % name)

        # other methods
        try:
            if os.name == 'posix':
                p.set_nice(1)
            else:
                p.set_nice(psutil.NORMAL_PRIORITY_CLASS)
        except psutil.NoSuchProcess:
            pass
        else:
            self.fail("exception not raised")
        if hasattr(p, 'set_ionice'):
            self.assertRaises(psutil.NoSuchProcess, p.set_ionice, 2)
        self.assertRaises(psutil.NoSuchProcess, p.send_signal, signal.SIGTERM)
        self.assertRaises(psutil.NoSuchProcess, p.set_nice, 0)
        self.assertFalse(p.is_running())
        if hasattr(p, "set_cpu_affinity"):
            self.assertRaises(psutil.NoSuchProcess, p.set_cpu_affinity, [0])

    @unittest.skipUnless(POSIX, 'posix only')
    def test_zombie_process(self):
        # Note: in this test we'll be creating two sub processes.
        # Both of them are supposed to be freed / killed by
        # reap_children() as they are attributable to 'us'
        # (os.getpid()) via get_children(recursive=True).
        src = textwrap.dedent("""\
        import os, sys, time, socket
        child_pid = os.fork()
        if child_pid > 0:
            time.sleep(3000)
        else:
            # this is the zombie process
            s = socket.socket(socket.AF_UNIX)
            s.connect('%s')
            if sys.version_info < (3, ):
                pid = str(os.getpid())
            else:
                pid = bytes(str(os.getpid()), 'ascii')
            s.sendall(pid)
            s.close()
        """ % TESTFN)
        sock = None
        try:
            sock = socket.socket(socket.AF_UNIX)
            sock.settimeout(2)
            sock.bind(TESTFN)
            sock.listen(1)
            pyrun(src)
            conn, _ = sock.accept()
            zpid = int(conn.recv(1024))
            zproc = psutil.Process(zpid)
            # Make sure we can re-instantiate the process after its
            # status changed to zombie and at least be able to
            # query its status.
            # XXX should we also assume ppid should be querable?
            call_until(lambda: zproc.status, "ret == psutil.STATUS_ZOMBIE")
            self.assertTrue(psutil.pid_exists(zpid))
            zproc = psutil.Process(zpid)
            descendants = [x.pid for x in psutil.Process(
                           os.getpid()).get_children(recursive=True)]
            self.assertIn(zpid, descendants)
        finally:
            if sock is not None:
                sock.close()
            reap_children(search_all=True)

    def test__str__(self):
        sproc = get_test_subprocess()
        p = psutil.Process(sproc.pid)
        self.assertIn(str(sproc.pid), str(p))
        # python shows up as 'Python' in cmdline on OS X so test fails on OS X
        if not OSX:
            self.assertIn(os.path.basename(PYTHON), str(p))
        sproc = get_test_subprocess()
        p = psutil.Process(sproc.pid)
        p.kill()
        p.wait()
        self.assertIn(str(sproc.pid), str(p))
        self.assertIn("terminated", str(p))

    def test__eq__(self):
        self.assertTrue(psutil.Process() == psutil.Process())

    def test__hash__(self):
        s = set([psutil.Process(), psutil.Process()])
        self.assertEqual(len(s), 1)

    @unittest.skipIf(LINUX, 'PID 0 not available on Linux')
    def test_pid_0(self):
        # Process(0) is supposed to work on all platforms except Linux
        p = psutil.Process(0)
        self.assertTrue(p.name)

        if os.name == 'posix':
            try:
                self.assertEqual(p.uids.real, 0)
                self.assertEqual(p.gids.real, 0)
            except psutil.AccessDenied:
                pass

        self.assertIn(p.ppid, (0, 1))
        #self.assertEqual(p.exe, "")
        p.cmdline
        try:
            p.get_num_threads()
        except psutil.AccessDenied:
            pass

        try:
            p.get_memory_info()
        except psutil.AccessDenied:
            pass

        # username property
        try:
            if POSIX:
                self.assertEqual(p.username, 'root')
            elif WINDOWS:
                self.assertEqual(p.username, 'NT AUTHORITY\\SYSTEM')
            else:
                p.username
        except psutil.AccessDenied:
            pass

        self.assertIn(0, psutil.get_pid_list())
        self.assertTrue(psutil.pid_exists(0))

    def test__all__(self):
        for name in dir(psutil):
            if name in ('callable', 'defaultdict', 'error', 'namedtuple',
                        'test', 'NUM_CPUS', 'BOOT_TIME', 'TOTAL_PHYMEM'):
                continue
            if not name.startswith('_'):
                try:
                    __import__(name)
                except ImportError:
                    if name not in psutil.__all__:
                        fun = getattr(psutil, name)
                        if fun is None:
                            continue
                        if (fun.__doc__ is not None and
                                'deprecated' not in fun.__doc__.lower()):
                            self.fail('%r not in psutil.__all__' % name)

    def test_Popen(self):
        # Popen class test
        # XXX this test causes a ResourceWarning on Python 3 because
        # psutil.__subproc instance doesn't get propertly freed.
        # Not sure what to do though.
        cmd = [PYTHON, "-c", "import time; time.sleep(2);"]
        proc = psutil.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            proc.name
            proc.stdin
            self.assertTrue(hasattr(proc, 'name'))
            self.assertTrue(hasattr(proc, 'stdin'))
            self.assertRaises(AttributeError, getattr, proc, 'foo')
        finally:
            proc.kill()
            proc.wait()


# ===================================================================
# --- Featch all processes test
# ===================================================================

class TestFetchAllProcesses(unittest.TestCase):
    """Test which iterates over all running processes and performs
    some sanity checks against Process API's returned values.
    """

    def setUp(self):
        if POSIX:
            import pwd
            pall = pwd.getpwall()
            self._uids = set([x.pw_uid for x in pall])
            self._usernames = set([x.pw_name for x in pall])

    def test_fetch_all(self):
        valid_procs = 0
        excluded_names = ['send_signal', 'suspend', 'resume', 'terminate',
                          'kill', 'wait', 'as_dict', 'get_cpu_percent', 'nice',
                          'parent', 'get_children', 'pid']
        attrs = []
        for name in dir(psutil.Process):
            if name.startswith("_"):
                continue
            if name.startswith("set_"):
                continue
            if name in excluded_names:
                continue
            attrs.append(name)

        default = object()
        failures = []
        for name in attrs:
            for p in psutil.process_iter():
                ret = default
                try:
                    try:
                        args = ()
                        attr = getattr(p, name, None)
                        if attr is not None and callable(attr):
                            if name == 'get_rlimit':
                                args = (psutil.RLIMIT_NOFILE,)
                            ret = attr(*args)
                        else:
                            ret = attr
                        valid_procs += 1
                    except NotImplementedError:
                        register_warning("%r was skipped because not "
                                         "implemented" % (self.__class__.__name__ +
                                                          '.test_' + name))
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        err = sys.exc_info()[1]
                        self.assertEqual(err.pid, p.pid)
                        if err.name:
                            # make sure exception's name attr is set
                            # with the actual process name
                            self.assertEqual(err.name, p.name)
                        self.assertTrue(str(err))
                        self.assertTrue(err.msg)
                    else:
                        if ret not in (0, 0.0, [], None, ''):
                            assert ret, ret
                        meth = getattr(self, name)
                        meth(ret)
                except Exception:
                    err = sys.exc_info()[1]
                    s = '\n' + '=' * 70 + '\n'
                    s += "FAIL: test_%s (proc=%s" % (name, p)
                    if ret != default:
                        s += ", ret=%s)" % repr(ret)
                    s += ')\n'
                    s += '-' * 70
                    s += "\n%s" % traceback.format_exc()
                    s = "\n".join((" " * 4) + i for i in s.splitlines())
                    failures.append(s)
                    break

        if failures:
            self.fail(''.join(failures))

        # we should always have a non-empty list, not including PID 0 etc.
        # special cases.
        self.assertTrue(valid_procs > 0)

    def cmdline(self, ret):
        pass

    def exe(self, ret):
        if not ret:
            self.assertEqual(ret, '')
        else:
            assert os.path.isabs(ret), ret
            # Note: os.stat() may return False even if the file is there
            # hence we skip the test, see:
            # http://stackoverflow.com/questions/3112546/os-path-exists-lies
            if POSIX:
                assert os.path.isfile(ret), ret
                if hasattr(os, 'access') and hasattr(os, "X_OK"):
                    # XXX may fail on OSX
                    self.assertTrue(os.access(ret, os.X_OK))

    def ppid(self, ret):
        self.assertTrue(ret >= 0)

    def name(self, ret):
        self.assertTrue(isinstance(ret, str))
        self.assertTrue(ret)

    def create_time(self, ret):
        self.assertTrue(ret > 0)
        # this can't be taken for granted on all platforms
        #self.assertGreaterEqual(ret, psutil.get_boot_time())
        # make sure returned value can be pretty printed
        # with strftime
        time.strftime("%Y %m %d %H:%M:%S", time.localtime(ret))

    def uids(self, ret):
        for uid in ret:
            self.assertTrue(uid >= 0)
            self.assertIn(uid, self._uids)

    def gids(self, ret):
        # note: testing all gids as above seems not to be reliable for
        # gid == 30 (nodoby); not sure why.
        for gid in ret:
            self.assertTrue(gid >= 0)
            #self.assertIn(uid, self.gids)

    def username(self, ret):
        self.assertTrue(ret)
        if os.name == 'posix':
            self.assertIn(ret, self._usernames)

    def status(self, ret):
        self.assertTrue(ret != "")
        self.assertTrue(ret != '?')
        self.assertIn(ret, VALID_PROC_STATUSES)

    def get_io_counters(self, ret):
        for field in ret:
            if field != -1:
                self.assertTrue(field >= 0)

    def get_ionice(self, ret):
        if LINUX:
            self.assertTrue(ret.ioclass >= 0)
            self.assertTrue(ret.value >= 0)
        else:
            self.assertTrue(ret >= 0)
            self.assertIn(ret, (0, 1, 2))

    def get_num_threads(self, ret):
        self.assertTrue(ret >= 1)

    def get_threads(self, ret):
        for t in ret:
            self.assertTrue(t.id >= 0)
            self.assertTrue(t.user_time >= 0)
            self.assertTrue(t.system_time >= 0)

    def get_cpu_times(self, ret):
        self.assertTrue(ret.user >= 0)
        self.assertTrue(ret.system >= 0)

    def get_memory_info(self, ret):
        self.assertTrue(ret.rss >= 0)
        self.assertTrue(ret.vms >= 0)

    def get_ext_memory_info(self, ret):
        for name in ret._fields:
            self.assertTrue(getattr(ret, name) >= 0)
        if POSIX and ret.vms != 0:
            # VMS is always supposed to be the highest
            for name in ret._fields:
                if name != 'vms':
                    value = getattr(ret, name)
                    assert ret.vms > value, ret
        elif WINDOWS:
            assert ret.peak_wset >= ret.wset, ret
            assert ret.peak_paged_pool >= ret.paged_pool, ret
            assert ret.peak_nonpaged_pool >= ret.nonpaged_pool, ret
            assert ret.peak_pagefile >= ret.pagefile, ret

    def get_open_files(self, ret):
        for f in ret:
            if WINDOWS:
                assert f.fd == -1, f
            else:
                self.assertIsInstance(f.fd, int)
            assert os.path.isabs(f.path), f
            assert os.path.isfile(f.path), f

    def get_num_fds(self, ret):
        self.assertTrue(ret >= 0)

    def get_connections(self, ret):
        for conn in ret:
            check_connection(conn)

    def getcwd(self, ret):
        if ret is not None:  # BSD may return None
            assert os.path.isabs(ret), ret
            try:
                st = os.stat(ret)
            except OSError:
                err = sys.exc_info()[1]
                # directory has been removed in mean time
                if err.errno != errno.ENOENT:
                    raise
            else:
                self.assertTrue(stat.S_ISDIR(st.st_mode))

    def get_memory_percent(self, ret):
        assert 0 <= ret <= 100, ret

    def is_running(self, ret):
        self.assertTrue(ret)

    def get_cpu_affinity(self, ret):
        assert ret != [], ret

    def terminal(self, ret):
        if ret is not None:
            assert os.path.isabs(ret), ret
            assert os.path.exists(ret), ret

    def get_memory_maps(self, ret):
        for nt in ret:
            for fname in nt._fields:
                value = getattr(nt, fname)
                if fname == 'path':
                    if not value.startswith('['):
                        assert os.path.isabs(nt.path), nt.path
                        # commented as on Linux we might get '/foo/bar (deleted)'
                        #assert os.path.exists(nt.path), nt.path
                elif fname in ('addr', 'perms'):
                    self.assertTrue(value)
                else:
                    self.assertIsInstance(value, (int, long))
                    assert value >= 0, value

    def get_num_handles(self, ret):
        if WINDOWS:
            self.assertGreaterEqual(ret, 0)
        else:
            self.assertGreaterEqual(ret, 0)

    def get_nice(self, ret):
        if POSIX:
            assert -20 <= ret <= 20, ret
        else:
            priorities = [getattr(psutil, x) for x in dir(psutil)
                          if x.endswith('_PRIORITY_CLASS')]
            self.assertIn(ret, priorities)

    def get_num_ctx_switches(self, ret):
        self.assertTrue(ret.voluntary >= 0)
        self.assertTrue(ret.involuntary >= 0)

    def get_rlimit(self, ret):
        self.assertEqual(len(ret), 2)
        self.assertGreaterEqual(ret[0], -1)
        self.assertGreaterEqual(ret[1], -1)


# ===================================================================
# --- Limited user tests
# ===================================================================

if hasattr(os, 'getuid') and os.getuid() == 0:
    class LimitedUserTestCase(TestProcess):
        """Repeat the previous tests by using a limited user.
        Executed only on UNIX and only if the user who run the test script
        is root.
        """
        # the uid/gid the test suite runs under
        PROCESS_UID = os.getuid()
        PROCESS_GID = os.getgid()

        def __init__(self, *args, **kwargs):
            TestProcess.__init__(self, *args, **kwargs)
            # re-define all existent test methods in order to
            # ignore AccessDenied exceptions
            for attr in [x for x in dir(self) if x.startswith('test')]:
                meth = getattr(self, attr)

                def test_(self):
                    try:
                        meth()
                    except psutil.AccessDenied:
                        pass
                setattr(self, attr, types.MethodType(test_, self))

        def setUp(self):
            safe_remove(TESTFN)
            os.setegid(1000)
            os.seteuid(1000)
            TestProcess.setUp(self)

        def tearDown(self):
            os.setegid(self.PROCESS_UID)
            os.seteuid(self.PROCESS_GID)
            TestProcess.tearDown(self)

        def test_nice(self):
            try:
                psutil.Process(os.getpid()).set_nice(-1)
            except psutil.AccessDenied:
                pass
            else:
                self.fail("exception not raised")

        def test_zombie_process(self):
            # causes problems if test test suite is run as root
            pass


# ===================================================================
# --- Example script tests
# ===================================================================

class TestExampleScripts(unittest.TestCase):
    """Tests for scripts in the examples directory."""

    def assert_stdout(self, exe, args=None):
        exe = os.path.join(EXAMPLES_DIR, exe)
        if args:
            exe = exe + ' ' + args
        try:
            out = sh(sys.executable + ' ' + exe).strip()
        except RuntimeError:
            err = sys.exc_info()[1]
            if 'AccessDenied' in str(err):
                return str(err)
            else:
                raise
        assert out, out
        return out

    def assert_syntax(self, exe, args=None):
        exe = os.path.join(EXAMPLES_DIR, exe)
        f = open(exe, 'r')
        try:
            src = f.read()
        finally:
            f.close()
        ast.parse(src)

    def test_check_presence(self):
        # make sure all example scripts have a test method defined
        meths = dir(self)
        for name in os.listdir(EXAMPLES_DIR):
            if name.endswith('.py'):
                if 'test_' + os.path.splitext(name)[0] not in meths:
                    # self.assert_stdout(name)
                    self.fail('no test defined for %r script'
                              % os.path.join(EXAMPLES_DIR, name))

    def test_disk_usage(self):
        self.assert_stdout('disk_usage.py')

    def test_free(self):
        self.assert_stdout('free.py')

    def test_meminfo(self):
        self.assert_stdout('meminfo.py')

    def test_process_detail(self):
        self.assert_stdout('process_detail.py')

    def test_who(self):
        self.assert_stdout('who.py')

    def test_netstat(self):
        self.assert_stdout('netstat.py')

    def test_pmap(self):
        self.assert_stdout('pmap.py', args=str(os.getpid()))

    @unittest.skipIf(ast is None,
                     'ast module not available on this python version')
    def test_killall(self):
        self.assert_syntax('killall.py')

    @unittest.skipIf(ast is None,
                     'ast module not available on this python version')
    def test_nettop(self):
        self.assert_syntax('nettop.py')

    @unittest.skipIf(ast is None,
                     'ast module not available on this python version')
    def test_top(self):
        self.assert_syntax('top.py')

    @unittest.skipIf(ast is None,
                     'ast module not available on this python version')
    def test_iotop(self):
        self.assert_syntax('iotop.py')


def cleanup():
    reap_children(search_all=True)
    DEVNULL.close()
    safe_remove(TESTFN)
    safe_rmdir(TESTFN_UNICODE)
    for path in _testfiles:
        safe_remove(path)

atexit.register(cleanup)
safe_remove(TESTFN)
safe_rmdir(TESTFN_UNICODE)


def test_main():
    tests = []
    test_suite = unittest.TestSuite()
    tests.append(TestSystemAPIs)
    tests.append(TestProcess)
    tests.append(TestFetchAllProcesses)

    if POSIX:
        from _posix import PosixSpecificTestCase
        tests.append(PosixSpecificTestCase)

    # import the specific platform test suite
    if LINUX:
        from _linux import LinuxSpecificTestCase as stc
    elif WINDOWS:
        from _windows import WindowsSpecificTestCase as stc
        from _windows import TestDualProcessImplementation
        tests.append(TestDualProcessImplementation)
    elif OSX:
        from _osx import OSXSpecificTestCase as stc
    elif BSD:
        from _bsd import BSDSpecificTestCase as stc
    elif SUNOS:
        from _sunos import SunOSSpecificTestCase as stc
    tests.append(stc)

    if hasattr(os, 'getuid'):
        if 'LimitedUserTestCase' in globals():
            tests.append(LimitedUserTestCase)
        else:
            register_warning("LimitedUserTestCase was skipped (super-user "
                             "privileges are required)")

    tests.append(TestExampleScripts)

    for test_class in tests:
        test_suite.addTest(unittest.makeSuite(test_class))
    result = unittest.TextTestRunner(verbosity=2).run(test_suite)
    return result.wasSuccessful()

if __name__ == '__main__':
    if not test_main():
        sys.exit(1)

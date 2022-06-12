import sys
import frozendict
from functools import wraps
import threading
import requests
import time
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry


def draw_progress_bar(percent, barLen=20):
    sys.stdout.write("\r")
    progress = ""
    for i in range(barLen):
        if i < int(barLen * percent):
            progress += "="
        else:
            progress += " "
    sys.stdout.write("[ %s ] %.2f%%" % (progress, percent * 100))
    sys.stdout.flush()


def freezeargs(func):
    """Transform mutable dictionary
    Into immutable
    Useful to be compatible with cache
    """

    @wraps(func)
    def wrapped(*args, **kwargs):
        args = tuple([frozendict(arg) if isinstance(arg, dict) else arg for arg in args])
        kwargs = {k: frozendict(v) if isinstance(v, dict) else v for k, v in kwargs.items()}
        return func(*args, **kwargs)

    return wrapped


class MultiThread(object):
    """
    Useful for i/o but not cpu
    """
    def __init__(self, function, argsVector, commonArgs=None, maxThreads=5, queue_results=False):
        self._function = function
        self._lock = threading.Lock()
        self._nextArgs = iter(argsVector).__next__
        self._commonArgs = commonArgs
        self._threadPool = [threading.Thread(target=self._doSome) for i in range(maxThreads)]
        if queue_results:
            self._queue = queue.Queue()
        else:
            self._queue = None

    def _doSome(self):
        while True:
            self._lock.acquire()
            try:
                try:
                    args = self._nextArgs()
                except StopIteration:
                    break
            finally:
                self._lock.release()
            all_args = args + self._commonArgs if self._commonArgs else args
            # result = self._function(args, *self._commonArgs)
            result = self._function(*all_args)
            if self._queue is not None:
                self._queue.put((args, result))

    def get(self, *a, **kw):
        if self._queue is not None:
            return self._queue.get(*a, **kw)
        else:
            raise ValueError('Not queueing results')

    def get_results(self):
        self.start()
        self.join()
        results = []
        while not self._queue.empty():
            out = self.get()
            results.append(out)
        return results

    def start(self):
        for thread in self._threadPool:
            time.sleep(0)  # necessary to give other threads a chance to run
            thread.start()

    def join(self, timeout=None):
        for thread in self._threadPool:
            thread.join(timeout)


def requests_retry_session(retries=5, backoff_factor=0, status_forcelist=(500, 502, 504), session=None,):
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session
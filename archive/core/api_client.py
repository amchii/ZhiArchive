from archive.core.archiver import Archiver
from archive.core.base import BaseWorker
from archive.core.monitor import Monitor


def get_api_client(name: str = None, people: str = None):
    if name == Archiver.name:
        return Archiver(people)
    elif name == Monitor.name:
        return Monitor(people)
    worker = BaseWorker(people)
    worker.name = name
    return worker

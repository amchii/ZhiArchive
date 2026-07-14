from archive.core.archiver import Archiver
from archive.core.base import BaseWorker
from archive.core.monitor import Monitor
from archive.services import get_current_services


def get_api_client(name: str = None, people: str = None):
    services = get_current_services()
    if services is not None and people is None:
        if name == Archiver.name:
            return services.archiver
        if name == Monitor.name:
            return services.monitor
        worker = BaseWorker(store=services.store)
        worker.name = name
        return worker
    if name == Archiver.name:
        return Archiver(people)
    elif name == Monitor.name:
        return Monitor(people)
    worker = BaseWorker(people)
    worker.name = name
    return worker

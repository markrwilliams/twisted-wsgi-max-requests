import attr
import os
import socket
import subprocess
import sys

from twisted.application import internet, service
from twisted.internet import defer, fdesc
from twisted.internet.endpoints import (
    serverFromString,
    TCP4ServerEndpoint, TCP6ServerEndpoint,
    UNIXServerEndpoint,
)
from twisted.internet.interfaces import IStreamServerEndpoint
from twisted.logger import Logger
from twisted.python.components import proxyForInterface
from twisted.python import usage, reflect
from twisted.python.threadpool import ThreadPool
from twisted.web.resource import IResource
from twisted.web.server import Site
from twisted.web.wsgi import WSGIResource
from zope.interface import implementer


@implementer(IStreamServerEndpoint)
@attr.s
class _NotifyListenEndpointWrapper(object):
    _port = None
    _endpoint = attr.ib()
    _notifications = attr.ib(default=attr.Factory(list))

    def notifyListen(self):
        if self._port:
            return defer.succeed(self._port)
        else:
            self._notifications.append(defer.Deferred())
            return self._notifications[-1]

    def _notify(self, port):
        self._port = port
        while self._notifications:
            self._notifications.pop(0).callback(port)

    def listen(self, factory):
        return self._endpoint.listen(factory).addCallback(self._notify)


@attr.s
class ListenersCollection(object):
    _variable = "_TWISTED_ADOPTABLE_LISTENERS"
    _unitSeparator = "\x1f"
    _recordSeparator = "\x1e"

    _descriptionsAndFilenos = attr.ib()

    def toEnvironment(self):
        value = self._recordSeparator.join(
            self._unitSeparator.join([description, str(fileno)])
            for description, fileno in self
        )
        return {self._variable: value}

    @classmethod
    def fromEnvironment(cls, environment):
        value = environment.get(cls._variable)
        if value:
            descriptionsAndFilenos = [
                (description, int(fileno))
                for record in value.split(cls._recordSeparator)
                for description, _, fileno in [
                        record.partition(cls._unitSeparator)
                ]
            ]
        else:
            descriptionsAndFilenos = []
        return cls(descriptionsAndFilenos)

    def __iter__(self):
        for description, fileno in self._descriptionsAndFilenos:
            fdesc._unsetCloseOnExec(fileno)
            yield description, fileno


class LivelyListenersService(service.MultiService):
    _released = False

    def __init__(self, reactor, descriptions, factory):
        service.MultiService.__init__(self)
        self._descriptions = descriptions
        self._factory = factory

        self._listeners = []

        def recordListener(listener):
            self._listeners.append(listener)
            return listener

        for description in self._descriptions:
            endpoint = _NotifyListenEndpointWrapper(serverFromString(
                reactor, description))
            endpoint.notifyListen().addCallback(recordListener)
            internet.StreamServerEndpointService(
                endpoint, factory
            ).setServiceParent(self)

    def stopAccepting(self):
        for listener in self._listeners:
            listener.stopReading()
        self._released = True

    def stopService(self):
        if not self._released:
            service.Service.stopService(self)

    def toCollection(self):
        pairs = [
            (description, port.fileno())
            for description, port in
            zip(self._descriptions, self._listeners)
        ]
        assert len(pairs) == len(self._descriptions), (
            pairs, self._descriptions
        )
        return ListenersCollection(pairs)


class AdoptedListenersService(service.Service):
    _released = False

    def __init__(self, reactor, collection, factory):
        self._reactor = reactor
        self._collection = collection
        self._factory = factory
        self._descriptions = []
        self._listeners = []

    def startService(self):
        for description, fileno in self._collection:
            endpoint = serverFromString(self._reactor, description)
            if isinstance(endpoint, TCP4ServerEndpoint):
                family = socket.AF_INET
            elif isinstance(endpoint, TCP6ServerEndpoint):
                family = socket.AF_INET6
            elif isinstance(endpoint, UNIXServerEndpoint):
                family = socket.AF_UNIX
            else:
                raise ValueError("Unknown endpoint {}".format(description))
            self._descriptions.append(description)
            self._listeners.append(
                self._reactor.adoptStreamPort(fileno, family, self._factory),
            )

    def stopAccepting(self):
        for listener in self._listeners:
            listener.stopReading()
        self._released = True

    def stopService(self):
        if not self._released:
            service.Service.stopService(self)

    def toCollection(self):
        pairs = [
            (description, port.fileno())
            for description, port in
            zip(self._descriptions, self._listeners)
        ]
        assert len(pairs) == len(self._descriptions), (
            pairs, self._descriptions
        )
        return ListenersCollection(pairs)


class MaximumRequestsResource(proxyForInterface(IResource)):
    _log = Logger()

    def __init__(self, reactor, maximumRequests, original):
        self._reactor = reactor
        self._maximumRequests = maximumRequests
        self._requests = 0
        self._inFlight = 0
        self._stopping = False
        self._notifications = []
        super(MaximumRequestsResource, self).__init__(original)

    def stopAccepting(self):
        pass

    def _decrementInFlight(self, result):
        self._inFlight -= 1
        if not self._inFlight:
            self._noPendingRequests()
        return result

    def _noPendingRequests(self):
        if self._stopping:
            self._log.info(
                "Last connection closed; stopping reactor.")
            self._reactor.stop()

    def notifyFinish(self):
        if self._stopping:
            return defer.succeed(None)
        else:
            self._notifications.append(defer.Deferred())
            return self._notifications[-1]

    def _finishing(self):
        while self._notifications:
            self._notifications.pop(0).callback(None)

    def render(self, request):
        self._inFlight += 1
        request.notifyFinish().addBoth(self._decrementInFlight)

        self._requests += 1
        if self._requests > (self._maximumRequests - 1):
            self._log.info(
                "Received {maximumRequests} request(s);"
                " will stop responding to WSGI requests on all ports",
                maximumRequests=self._maximumRequests)
            self.stopAccepting()
            self._stopping = True
            self._finishing()
            response = self.original.render(request)
            return response
        else:
            return self.original.render(request)


class ThreadPoolService(service.Service):

    def __init__(self, threadPool):
        self._threadPool = threadPool

    def startService(self):
        self._threadPool.start()

    def stopService(self):
        self._threadPool.stop()


class Options(usage.Options):
    optParameters = [["logfile", "l", None,
                      "Path to web CLF (Combined Log Format) log file."],
                     ["listen", None, "tcp:8080",
                      "A description of the port on which to listen."],
                     ["max-requests", None, 100,
                      "The maximum number of requests to serve before"
                      " terminating the process.", int]]

    def __init__(self):
        usage.Options.__init__(self)
        self['extraHeaders'] = []
        self['ports'] = []

    def parseArgs(self, *args):
        if len(args) != 1:
            raise usage.UsageError(
                "Must pass exactly one FQPN to a WSGI application")
        try:
            application = reflect.namedAny(args[0])
        except (AttributeError, ValueError):
            raise usage.UsageError("No such WSGI application: %r" % (args[0],))
        self['application'] = application


def makeService(config):
    from twisted.internet import reactor

    webService = service.MultiService()

    threadPool = ThreadPool()
    ThreadPoolService(threadPool).setServiceParent(webService)

    wsgiResource = WSGIResource(reactor, threadPool, config['application'])

    portsService = service.MultiService()
    portsService.setServiceParent(webService)
    root = MaximumRequestsResource(
        reactor, config['max-requests'], wsgiResource)

    if config['logfile']:
        site = Site(root, logPath=config['logfile'])
    else:
        site = Site(root)

    portsService.setServiceParent(webService)
    collection = ListenersCollection.fromEnvironment(os.environ)
    if not(list(collection)):
        svc = LivelyListenersService(reactor, [config['listen']], site)
        svc.setServiceParent(webService)
    else:
        svc = AdoptedListenersService(reactor, collection, site)
        svc.setServiceParent(webService)

    root.stopAccepting = svc.stopAccepting

    def respawn(_):
        environment = os.environ.copy()
        collection = svc.toCollection()
        environment.update(collection.toEnvironment())
        subprocess.Popen(
            [sys.executable] + sys.argv,
            env=environment,
            pass_fds=[fileno for _, fileno in collection],
        )

    root.notifyFinish().addCallback(respawn)

    return webService

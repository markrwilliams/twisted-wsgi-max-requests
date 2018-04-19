from twisted.application import service, strports
from twisted.logger import Logger
from twisted.python.components import proxyForInterface
from twisted.python import usage, reflect
from twisted.python.threadpool import ThreadPool
from twisted.web.resource import IResource
from twisted.web.server import Site
from twisted.web.wsgi import WSGIResource



class MaximumRequestsResource(proxyForInterface(IResource)):
    _log = Logger()

    def __init__(self, reactor, portsService, maximumRequests, original):
        self._reactor = reactor
        self._maximumRequests = maximumRequests
        self._portsService = portsService
        self._requests = 0
        super(MaximumRequestsResource, self).__init__(original)

    def _stop(self, _):
        self._log.info(
            "Last connection closed; stopping reactor.")
        self._reactor.stop()

    def render(self, request):
        self._requests += 1
        if self._requests > (self._maximumRequests - 1):
            self._log.info(
                "Received {maximumRequests} request(s);"
                " will stop responding to WSGI requests on all ports",
                maximumRequests=self._maximumRequests)
            self._portsService.stopService()
            request.notifyFinish().addBoth(self._stop)
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
                     ["max-requests", None, 100,
                      "The maximum number of requests to serve before"
                      " terminating the process.", int]]

    def __init__(self):
        usage.Options.__init__(self)
        self['extraHeaders'] = []
        self['ports'] = []

    def opt_listen(self, port):
        self['ports'].append(port)

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
        reactor, portsService, config['max-requests'], wsgiResource)

    if config['logfile']:
        site = Site(root, logPath=config['logfile'])
    else:
        site = Site(root)

    portsService.setServiceParent(webService)
    if not config['ports']:
        config['ports'] = ['tcp:8080']

    for port in config['ports']:
        strports.service(port, site).setServiceParent(portsService)

    return webService

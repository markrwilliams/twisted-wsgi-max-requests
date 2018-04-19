from twisted.application.service import ServiceMaker

TwistedWeb = ServiceMaker(
    "Twisted Web WSGI server.",
    "max_requests._implementation",
    "A Twisted WSGI server that only serves a limited number of requests.",
    "max-requests-wsgi")

## What?

Gracefully restart Twisted Web's WSGI server after a certain number of requests.

## How?
```
twist max-requests-wsgi \
    --listen tcp:8000 \
    --max-requests=10 \
    import.path.to.wsgi.application
```

After 10 requests, this will stop accepting on port 8000, pass the
socket to a new replacement process, and then terminate.



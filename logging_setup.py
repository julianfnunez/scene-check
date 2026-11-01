"""Console logging for the host and bundled MCP adapters."""
import logging


class PollingFilter(logging.Filter):
    def filter(self, record):
        # Uvicorn access records: client, method, path, HTTP version, status.
        if not isinstance(record.args, tuple) or len(record.args) != 5:
            return True
        _, method, path, _, status = record.args
        path = str(path).split('?', 1)[0]
        polling = path in ('/api/scenes', '/api/checks') or path.startswith('/api/checks/')
        return not (method == 'GET' and polling and isinstance(status, int) and 200 <= status < 400)


def configure_logging():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
                        datefmt='%H:%M:%S')
    logging.getLogger().setLevel(logging.INFO)
    # These libraries otherwise log every request, sometimes including vendor URLs.
    for name in ('mcp.server.lowlevel.server', 'httpx', 'httpcore'):
        logging.getLogger(name).setLevel(logging.WARNING)
    access = logging.getLogger('uvicorn.access')
    if not any(isinstance(f, PollingFilter) for f in access.filters):
        access.addFilter(PollingFilter())

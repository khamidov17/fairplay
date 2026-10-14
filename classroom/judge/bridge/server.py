import threading
from socketserver import TCPServer, ThreadingMixIn


class ThreadingTCPListener(ThreadingMixIn, TCPServer):
    allow_reuse_address = True
    # Connection threads (one per judge or site request) must not keep the process alive
    # after shutdown: otherwise SIGTERM leaves a bridge that still holds its ports.
    daemon_threads = True
    block_on_close = False


class Server:
    def __init__(self, addresses, handler):
        self.servers = []
        try:
            for address in addresses:
                self.servers.append(ThreadingTCPListener(address, handler))
        except BaseException:
            self.close()
            raise
        self._shutdown = threading.Event()

    def serve_forever(self):
        threads = [threading.Thread(target=server.serve_forever) for server in self.servers]
        for thread in threads:
            thread.daemon = True
            thread.start()
        try:
            self._shutdown.wait()
        except KeyboardInterrupt:
            self.shutdown()
        finally:
            for thread in threads:
                thread.join()

    def shutdown(self):
        for server in self.servers:
            server.shutdown()
        self.close()
        self._shutdown.set()

    def close(self):
        for server in self.servers:
            server.server_close()

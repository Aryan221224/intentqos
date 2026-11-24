"""Accept-and-close TCP listeners (e.g. 1.1.1.1:443, 8.8.8.8:443): what the 4G Bridge dials to see the internet."""
import socket, sys, threading
def serve(addr, port):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind((addr, port)); s.listen(64)
    while True:
        c, _ = s.accept(); c.close()
for a in sys.argv[1:]:
    h, p = a.rsplit(":", 1); threading.Thread(target=serve, args=(h, int(p)), daemon=True).start()
threading.Event().wait()

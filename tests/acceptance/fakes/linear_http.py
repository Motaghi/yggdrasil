"""Record/replay HTTP transport; M1 must supply the actual GraphQL pairs."""

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    pairs = iter(json.loads(Path(args.pairs).read_text()))

    class Replay(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
            request = {"path": self.path, "body": json.loads(body)}
            with Path(args.log).open("a") as log:
                log.write(json.dumps(request) + "\n")
            pair = next(pairs, None)
            if pair is None or pair["request"] != request:
                self.send_response(503)
                self.end_headers()
                return
            response = pair["response"]
            self.send_response(response["status"])
            for key, value in response.get("headers", {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(json.dumps(response["body"]).encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", args.port), Replay)
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

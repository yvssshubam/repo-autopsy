import http.server, mimetypes, socketserver

mimetypes.add_type("text/css", ".css")
mimetypes.add_type("application/javascript", ".js")

with socketserver.TCPServer(("", 5500), http.server.SimpleHTTPRequestHandler) as httpd:
    print("http://127.0.0.1:5500")
    httpd.serve_forever()
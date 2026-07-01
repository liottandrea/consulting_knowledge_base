# Corporate proxy CA certs

If your network uses a TLS-intercepting proxy (e.g. Zscaler), place the
corporate root CA certificate(s) here as PEM files ending in **.crt**:

    certs/zscaler-root.crt

They are installed into the image's trust store at build time so uv/pip and
runtime model downloads can validate HTTPS through the proxy. Leave this dir
empty (just .gitkeep) if you don't need it.

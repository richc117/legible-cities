"""The JSON-RPC protocol's schema, one file per protocol version.

Hand-written, and the contract: the desktop app generates its types from
``v1.json``, and ``tests/test_serve.py`` checks that what the server actually
sends validates against it. ``python -m schematic.serve --schema`` prints it.
"""

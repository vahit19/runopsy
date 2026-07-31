"""Allow ``python -m runopsy_cli``.

The console script is the normal way in, but a subprocess in a test cannot rely on a
``runopsy`` executable being on PATH — and the concurrency tests must launch the hook
exactly as a runtime does, as a fresh process per event.
"""

from runopsy_cli.main import app

app()

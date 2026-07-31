# runopsy

**Find where an AI agent run started going wrong — not just where it stopped.**

```bash
uv tool install runopsy        # or: pipx install runopsy
runopsy --help
```

This is the meta-distribution: it installs the CLI, the analysis engine, the collector,
replay, the runtime adapters and the local web view. Everything works offline with no
provider key.

```bash
runopsy record -s "make" -s "pytest"   # wrap any pipeline
runopsy diagnose latest                # where it started going wrong
runopsy ui                             # the timeline and failure map
```

Extras:

```bash
pip install "runopsy[inspect]"   # read Inspect AI eval logs
```

If you are writing code against Runopsy rather than using the CLI, depend on the piece
you need — `runopsy-core` is the framework-agnostic engine and pulls in nothing but
Pydantic.

Full documentation: https://github.com/vahit19/runopsy

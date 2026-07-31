# runopsy-ui

The 2D timeline and causal failure map, and an optional 3D view of the same run.

```bash
npm install
npm run build        # writes into ../runopsy-server/src/runopsy_server/static/
npm run dev          # proxies /v1 to a runopsy ui server on :8765
```

The build output is not committed. Releases produce it just before packaging so the
`runopsy-server` wheel carries a working view without a Node toolchain, while a source
checkout that skips the build falls back to the server-rendered index — a diagnosis
tool should not go dark because nobody ran a JavaScript build.

## What the drawing promises

Observed structure and inferred propagation never look alike. `precedes` edges are
solid; propagation is dashed, labelled *may reach N%*, and in 3D becomes a translucent
arc that fades with confidence. The distinction between a recorded dependency and an
inference is the product's central claim, so it survives into the rendering rather than
being flattened into arrows that all look the same.

Wording matches the terminal exactly — *suspected onset*, *unverified*. A finding that
reads as a suspicion in one surface and a conclusion in another has no calibration at
all, and this is the surface most likely to be screenshotted into a ticket.

## The contract

This package reads field names that live in Python. `TestTheContractTheWebViewReliesOn`
in `tests/test_server.py` pins them from that side, including that `edges` never carries
an `affects` kind — which is where a merge of the two edge kinds would first show up.

# Getting started

You need an account on the host (nginx HTTP Basic Auth). Open the app in a
browser. The home page is the globe and weather panel.

To see what that page looks like without a login, use the
[UI tour home](/muscatdb/home/). Weather fetches are off in the tour.

Install from a clone if you are developing:

```bash
uv sync
uv run muscat-db serve
```

Operators still use the in-app Guide at `/guide` for diagrams, schema, and CLI.

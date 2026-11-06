# API

`GET /health/live` and `GET /health/ready` provide process health without
returning configuration or credentials. `GET /api/v1/health` provides the
versioned service check.

`POST /api/v1/agent/chat` accepts `{ "message": "...", "context": {} }` and
returns a typed role, explanation, and bounded tool names. Tool execution is
still subject to backend validation and authorization.

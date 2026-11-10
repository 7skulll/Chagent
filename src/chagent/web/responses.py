"""The JSON reply shape every route returns, and the error models in its schema.

Separate from ``app`` so a router module can build a response without importing
the module that assembles the application.
"""

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from chagent.web.schemas import ErrorResponse


#: Every error status the local API can answer with, declared once so all
#: routers publish the same contract in /openapi.json.
ERROR_RESPONSES = {code: {"model": ErrorResponse} for code in (400, 403, 404, 409, 413, 415, 503)}


def reply(value, status=200, headers=None):
    return JSONResponse(jsonable_encoder(value), status_code=status,
                        headers={"Cache-Control": "no-store", **(headers or {})})

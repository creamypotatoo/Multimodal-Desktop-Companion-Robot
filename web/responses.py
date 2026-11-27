"""Consistent JSON envelopes for every API response.

Success: ``{"ok": true, "data": {...}}``
Failure: ``{"ok": false, "error": {"code": "...", "message": "...", "field": ...}}``
"""
from flask import jsonify


def ok(data, status=200):
    return jsonify(ok=True, data=data), status


def error(code, message, status, field=None):
    body = {'code': code, 'message': message}
    if field is not None:
        body['field'] = field
    return jsonify(ok=False, error=body), status

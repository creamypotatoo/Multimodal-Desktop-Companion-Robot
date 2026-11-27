"""REST API (``/api/v1``). Every route requires login; state changes need CSRF.

Handlers parse JSON and delegate to the configured robot and utility services.
"""
from flask import Blueprint, current_app, request, session

from controller import Action, Authority
from productivity import UtilityError
from web.auth import login_required
from web.responses import error, ok
from web.services import ConflictError, ServiceError

bp = Blueprint('api', __name__, url_prefix='/api/v1')


class BadRequest(ServiceError):
    status = 400
    code = 'bad_request'


class UnsupportedMedia(ServiceError):
    status = 415
    code = 'unsupported_media_type'


def robot():
    return current_app.extensions['eli_robot']


def operator():
    return Authority(session.get('control_session'), can_move=True, can_wake=True)


def body(*required, optional=()):
    """The JSON object body, with exactly the allowed keys present."""
    if not request.is_json:
        raise UnsupportedMedia('Content-Type must be application/json')
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise BadRequest('Request body must be a JSON object')
    missing = [name for name in required if name not in data]
    if missing:
        raise BadRequest(f'Missing field: {missing[0]}', field=missing[0])
    if required or optional:
        unknown = sorted(set(data) - set(required) - set(optional))
        if unknown:
            raise BadRequest(f'Unknown field: {unknown[0]}', field=unknown[0])
    return data


@bp.errorhandler(ServiceError)
def service_error(exc):
    return error(exc.code, exc.message, exc.status, exc.field)


@bp.before_request
@login_required
def _require_login():
    return None


@bp.get('/status')
def status():
    return ok(robot().status())


@bp.get('/mode')
def get_mode():
    return ok(robot().get_mode())


@bp.put('/mode')
def set_mode():
    return ok(robot().set_mode(body('mode')['mode'], authority=operator()))


@bp.get('/expression')
def get_expression():
    return ok(robot().get_expression())


@bp.put('/expression')
def set_expression():
    return ok(robot().set_expression(body('expression')['expression']))


@bp.get('/volume')
def get_volume():
    return ok(robot().get_volume())


@bp.put('/volume')
def set_volume():
    return ok(robot().set_volume(body('volume')['volume']))


@bp.get('/settings')
def get_settings():
    return ok(robot().get_settings())


@bp.patch('/settings')
def update_settings():
    return ok(robot().update_settings(body()))


@bp.get('/movement')
def movement():
    return ok(robot().movement())


@bp.post('/movement/move')
def move():
    data = body('direction', optional=('duration_ms',))
    return ok(robot().move(data['direction'], data.get('duration_ms'), authority=operator()))


@bp.put('/movement/speed')
def set_speed():
    return ok(robot().set_speed(body('speed')['speed'], authority=operator()))


@bp.post('/movement/stop')
def stop():
    return ok(robot().stop())


@bp.post('/movement/emergency-stop')
def emergency_stop():
    return ok(robot().command(Action.EMERGENCY_STOP))


@bp.post('/movement/reset-safety')
def reset_safety():
    return ok(robot().command(Action.RESET_SAFETY, authority=operator()))


@bp.put('/movement/servo')
def set_servo():
    data = body('servo', 'angle')
    return ok(robot().set_servo(data['servo'], data['angle'], authority=operator()))


@bp.get('/companion')
def companion():
    return ok(robot().companion())


@bp.post('/conversation')
def conversation():
    return ok(robot().conversation(body('action')['action']))


@bp.get('/perception')
def perception():
    return ok(robot().perception())


@bp.get('/diagnostics')
def diagnostics():
    return ok(robot().diagnostics())


def runtime():
    unified = current_app.extensions.get('eli_runtime')
    if unified is None:
        raise ConflictError('Voice and audio controls require the unified runtime')
    return unified


@bp.get('/audio')
def audio_status():
    unified = current_app.extensions.get('eli_runtime')
    return ok(dict(enabled=False) if unified is None else dict(enabled=True, **unified.audio_status()))


@bp.put('/audio/wake')
def wake_settings():
    return ok(runtime().configure_wake(body()))


@bp.put('/audio/input')
def audio_input():
    return ok(runtime().select_input(body('device')['device']))


@bp.put('/audio/sensitivity')
def audio_sensitivity():
    data = body('threshold', 'noise_ratio')
    return ok(runtime().configure_sensitivity(data['threshold'], data['noise_ratio']))


@bp.post('/audio/microphone-test')
def audio_microphone_test():
    return ok(runtime().test_microphone())


@bp.post('/audio/stop')
def audio_stop():
    return ok(runtime().stop_speaking())


@bp.post('/audio/test')
def audio_test():
    return ok(runtime().test_speaker())


@bp.put('/audio/mute')
def audio_mute():
    return ok(runtime().mute(body('muted')['muted']))


def utilities():
    return current_app.extensions['eli_utilities']


@bp.errorhandler(UtilityError)
def utility_error(exc):
    code = {400: 'validation_error', 404: 'not_found', 409: 'conflict'}[exc.status]
    return error(code, str(exc), exc.status, exc.field)


@bp.get('/utilities')
def utility_states():
    return ok(utilities().snapshot())


@bp.get('/utilities/events')
def utility_events():
    after = request.args.get('after', '0')
    if len(after) > 20 or not after.isascii() or not after.isdecimal():
        raise BadRequest('after must be a non-negative event ID', field='after')
    return ok(utilities().notifications(int(after)))


@bp.put('/utilities/<name>/settings')
def utility_settings(name):
    return ok(robot().command(Action.UTILITY_SETTINGS, name=name, settings=body()))


@bp.post('/utilities/<name>/actions')
def utility_action(name):
    data = body('action', optional=('duration',))
    if 'duration' in data and data['duration'] is None:
        raise BadRequest('duration must be an integer', field='duration')
    return ok(robot().command(Action.UTILITY, name=name, action=data['action'],
                              duration=data.get('duration')))

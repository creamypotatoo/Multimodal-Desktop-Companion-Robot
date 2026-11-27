"""Server-rendered dashboard pages. Data is loaded by the page's JS module."""
from flask import Blueprint, render_template

from web.auth import login_required

bp = Blueprint('pages', __name__)

# endpoint, URL, nav label, icon id, short description
PAGES = (
    ('home', '/', 'Home', 'home', 'Status and shortcuts'),
    ('companion', '/companion', 'Companion', 'chat', 'Conversation and AI'),
    ('movement', '/movement', 'Movement', 'move', 'Drive and servos'),
    ('productivity', '/productivity', 'Focus', 'timer', 'Timers and Pomodoro'),
    ('perception', '/perception', 'Perception', 'eye', 'Camera and recognition'),
    ('diagnostics', '/diagnostics', 'Diagnostics', 'pulse', 'System health'),
    ('settings', '/settings', 'Settings', 'sliders', 'Eyes, audio and mode'),
)
# Mobile bottom bar shows the most used pages; the rest are on Home.
MOBILE_NAV = ('home', 'companion', 'movement', 'productivity', 'settings')


def _make_view(name):
    @login_required
    def view():
        return render_template(f'{name}.html', page=name)
    view.__name__ = name
    return view


for _name, _url, *_ in PAGES:
    bp.add_url_rule(_url, _name, _make_view(_name))

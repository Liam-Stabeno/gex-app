"""
The dashboard page goes through Jinja (Flask's render_template), so its JavaScript must not
hold Jinja syntax: a '{{' in a template literal (`${{ a: 1 }[k]}`) made every page load a
500 on 2026-10-29.

Run from the project root:   python -m pytest tests/test_templates.py -q
"""
import os

import jinja2

TEMPLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'templates')


def test_dashboard_template_renders():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(TEMPLATES))
    html = env.get_template('dashboard.html').render()
    assert 'createGexPairDiv' in html and 'ROLLING PROFILE' in html

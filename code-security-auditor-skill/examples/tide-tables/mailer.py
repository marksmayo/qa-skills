"""Tide alert e-mails rendered from Jinja templates."""
from jinja2 import Environment, FileSystemLoader

# Planted: the e-mail environment turns auto-escaping off, and station notes are user-supplied.
mail_env = Environment(loader=FileSystemLoader("templates/mail"), autoescape=False)

# Safe counterpart: the page environment keeps it on.
page_env = Environment(loader=FileSystemLoader("templates"), autoescape=True)


def render_alert(station):
    return mail_env.get_template("alert.txt").render(station=station)

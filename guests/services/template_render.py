from datetime import date
from django.template import Template, Context

def calc_age(birthdate):
    if not birthdate:
        return ""
    today = date.today()
    years = today.year - birthdate.year
    if (today.month, today.day) < (birthdate.month, birthdate.day):
        years -= 1
    return years

def render_message_for_guest(message_text, guest):
    context = {
        "first_name": guest.first_name or "",
        "last_name": guest.last_name or "",
        "phone": guest.phone or "",
        "email": guest.email or "",
        "birthdate": guest.birthdate.strftime("%d.%m.%Y") if guest.birthdate else "",
        "age": calc_age(guest.birthdate),
    }

    template = Template(message_text)
    return template.render(Context(context))

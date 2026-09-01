from django import template

register = template.Library()

@register.filter(name='custom_abs')
def custom_abs(value):
    try:
        return abs(value)
    except (TypeError, ValueError):
        return value
